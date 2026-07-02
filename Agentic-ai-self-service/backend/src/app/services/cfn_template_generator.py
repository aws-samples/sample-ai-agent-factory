"""CloudFormation Template Generator for AgentCore Flows.

Converts a DeployRequest (workflow definition from the frontend) into a
complete CloudFormation template bundle using native AWS::BedrockAgentCore::*
resource types. Only one Custom Resource is needed: Custom::AgentCodePackage
for merging agent code with dependency bundles.

The generated bundle includes:
- template.yaml — CloudFormation template
- agent-code/agent.py — Pre-generated agent code (env-var driven)
- cfn-provider/ — Custom Resource Lambda for code packaging
- tool-lambdas/ — Gateway tool Lambda functions (if applicable)
- deploy.sh — One-command deployment script
- teardown.sh — Stack deletion script
- README.md — Documentation
"""

import copy
import io
import json
import logging
import os
import re
import zipfile
from dataclasses import dataclass, field
from typing import Optional

import yaml

from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services.code_generator import generate_agent_code
from app.services.observability import build_otel_env_vars

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gateway tool schemas (from gateway_deployer.py)
# ---------------------------------------------------------------------------

GATEWAY_TOOL_SCHEMAS: dict[str, dict] = {
    "duckduckgo_search": {
        "name": "duckduckgo_search",
        "description": "Search the web using DuckDuckGo.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    "wikipedia_search": {
        "name": "wikipedia_search",
        "description": "Search Wikipedia and return an article summary.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    "weather_api": {
        "name": "get_weather",
        "description": "Get current weather for a location.",
        "inputSchema": {
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
    },
    "web_page_fetcher": {
        "name": "fetch_webpage",
        "description": "Fetch and extract text content from a webpage URL.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    "get_order": {
        "name": "get_order",
        "description": "Look up order details by order ID.",
        "inputSchema": {
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "The order ID (e.g. ORD-12345)"}},
            "required": ["order_id"],
        },
    },
    "get_customer": {
        "name": "get_customer",
        "description": "Look up customer information by customer ID.",
        "inputSchema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string", "description": "The customer ID (e.g. CUST-001)"}},
            "required": ["customer_id"],
        },
    },
    "list_orders": {
        "name": "list_orders",
        "description": "List orders for a customer by customer ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "The customer ID"},
                "limit": {"type": "integer", "description": "Max orders to return (default 10)"},
            },
            "required": ["customer_id"],
        },
    },
    "process_refund": {
        "name": "process_refund",
        "description": "Process a refund for an order.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "The order ID to refund"},
                "amount": {"type": "number", "description": "Refund amount in dollars"},
                "reason": {"type": "string", "description": "Reason for the refund"},
            },
            "required": ["order_id", "amount", "reason"],
        },
    },
}

# Customer support tool IDs use a separate Lambda
_CUSTOMER_SUPPORT_TOOL_IDS = {"get_order", "get_customer", "list_orders", "process_refund"}
_DYNAMIC_TOOL_IDS = {"duckduckgo_search", "wikipedia_search", "weather_api", "web_page_fetcher"}

# Strands-based agents need the full bundle; boto3-only agents need the lighter one
STRANDS_BUNDLE_KEY = "agentcore-deps/strands-mcp.zip"
BASE_BUNDLE_KEY = "agentcore-deps/base.zip"


def _needs_strands_bundle(agent_code: str) -> bool:
    """Check if generated code imports strands (needs the larger bundle)."""
    return "from strands " in agent_code or "import strands" in agent_code


def _to_pascal_case_schema(schema: dict) -> dict:
    """Convert JSON Schema keys to PascalCase for CFN SchemaDefinition.

    CFN's GatewayTarget ToolSchema.InlinePayload requires PascalCase:
    type→Type, properties→Properties, required→Required, description→Description, items→Items
    """
    key_map = {
        "type": "Type",
        "properties": "Properties",
        "required": "Required",
        "description": "Description",
        "items": "Items",
        "enum": "Enum",
        "default": "Default",
    }
    result = {}
    for k, v in schema.items():
        new_key = key_map.get(k, k)
        if isinstance(v, dict):
            result[new_key] = _to_pascal_case_schema(v)
        elif isinstance(v, list) and all(isinstance(item, dict) for item in v):
            result[new_key] = [_to_pascal_case_schema(item) for item in v]
        else:
            result[new_key] = v
    return result


def _sanitize_name(name: str) -> str:
    """Sanitize for AgentCore resource names: [a-zA-Z][a-zA-Z0-9_]{0,47}."""
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name).lower()[:48]
    if sanitized and not sanitized[0].isalpha():
        sanitized = "agent_" + sanitized
    return sanitized or "agent_default"


def _sanitize_gateway_name(name: str) -> str:
    """Sanitize for Gateway names: ^([0-9a-zA-Z][-]?){1,100}$."""
    sanitized = re.sub(r"[^a-zA-Z0-9-]", "-", name).lower()[:100]
    if sanitized and not sanitized[0].isalnum():
        sanitized = "gw-" + sanitized
    return sanitized or "gw-default"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class CfnBundle:
    """Complete CloudFormation bundle ready for download."""

    template_yaml: str
    agent_code: str
    cfn_provider_code: bytes  # zip bytes of cfn-provider/
    tool_lambda_code: Optional[bytes] = None  # zip bytes of tool-lambdas/
    custom_tool_code: Optional[bytes] = None  # zip bytes of custom-tools/
    mcp_server_code: Optional[str] = None
    deploy_sh: str = ""
    teardown_sh: str = ""
    readme: str = ""
    deployment_name: str = "agent"

    def to_zip(self) -> bytes:
        """Package everything into a downloadable zip."""
        buf = io.BytesIO()
        prefix = f"{self.deployment_name}-cfn"
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{prefix}/template.yaml", self.template_yaml)
            zf.writestr(f"{prefix}/agent-code/agent.py", self.agent_code)
            zf.writestr(f"{prefix}/cfn-provider.zip", self.cfn_provider_code)
            if self.tool_lambda_code:
                zf.writestr(f"{prefix}/tool-lambdas.zip", self.tool_lambda_code)
            if self.custom_tool_code:
                zf.writestr(f"{prefix}/custom-tools.zip", self.custom_tool_code)
            if self.mcp_server_code:
                zf.writestr(f"{prefix}/agent-code/mcp_server.py", self.mcp_server_code)
            zf.writestr(f"{prefix}/deploy.sh", self.deploy_sh)
            zf.writestr(f"{prefix}/teardown.sh", self.teardown_sh)
            zf.writestr(f"{prefix}/README.md", self.readme)
        buf.seek(0)
        return buf.read()


# ---------------------------------------------------------------------------
# Template builder
# ---------------------------------------------------------------------------


class CfnTemplateGenerator:
    """Generates CloudFormation templates from AgentCore workflow definitions."""

    def generate(self, request: DeployRequest) -> CfnBundle:
        """Generate a complete CFN bundle from a deploy request."""
        config = request.config
        deployment_name = _sanitize_gateway_name(config.name)
        template_id = request.template_id
        connected_tools = request.connected_tools or []
        gateway_config = request.gateway_config
        gateway_tools = request.gateway_tools or []
        custom_tools = request.custom_tools or []
        memory_config = request.memory_config
        policy_config = request.policy_config
        mcp_server_config = request.mcp_server_config
        evaluation_config = request.evaluation_config
        knowledge_base_config = request.knowledge_base_config

        # Determine what components are needed
        has_gateway = (
            "gateway" in connected_tools
            or gateway_config is not None
            or bool(gateway_tools)
            or template_id
            in (
                "strands-gateway-agent",
                "customer-support-assistant",
                "customer-support-blueprint",
                "mcp-server-gateway-target",
            )
        )
        has_memory = (
            "memory" in connected_tools
            or memory_config is not None
            or template_id in ("customer-support-assistant", "customer-support-blueprint")
        )
        has_policy = policy_config is not None
        has_mcp_server = (
            mcp_server_config is not None or template_id == "mcp-server-gateway-target"
        )
        has_evaluation = (
            evaluation_config is not None
            and evaluation_config.get("enabled", False)
        )
        has_guardrails = "guardrails" in connected_tools

        # Build template
        template = self._init_template(deployment_name, template_id)

        # Always present
        self._add_cfn_provider(template, has_mcp_server=has_mcp_server)
        self._add_code_package(template, config)

        # Runtime IAM role (always needed). Forward the OTEL auth secret ARN
        # if the user configured an OTLP backend that needs Bearer/Basic auth.
        _obs = config.observability.model_dump() if getattr(config, "observability", None) else {}
        _otel_secret_arn = _obs.get("auth_header_secret_arn") or _obs.get("authHeaderSecretArn")
        self._add_runtime_role(
            template,
            connected_tools,
            has_gateway,
            has_memory,
            has_evaluation,
            otel_secret_arn=_otel_secret_arn,
        )

        # Conditional components
        if has_gateway:
            self._add_cognito(template, deployment_name)
            self._add_gateway_role(template)
            self._add_gateway(template, deployment_name)

            # Add predefined tool targets
            tool_ids = self._resolve_gateway_tools(template_id, gateway_tools, custom_tools)
            if tool_ids or custom_tools:
                self._add_tool_lambda_role(template)
            if tool_ids:
                self._add_tool_lambdas_and_targets(template, deployment_name, tool_ids)

            # Add custom AI-generated tool targets
            if custom_tools:
                self._add_custom_tool_targets(template, deployment_name, custom_tools)

            # Add Knowledge Base tool Lambda + gateway target
            if knowledge_base_config:
                self._add_kb_tool_lambda_and_target(template, deployment_name, knowledge_base_config)

        if has_memory:
            self._add_memory_role(template)
            self._add_memory(template, deployment_name, memory_config)

        if has_policy and has_gateway:
            self._add_policy_engine(template, deployment_name)
            self._add_policies(template, policy_config)

        if has_mcp_server and has_gateway:
            self._add_mcp_server_cognito(template, deployment_name)
            self._add_mcp_server_runtime(template, deployment_name, config)
            self._add_mcp_server_gateway_target(template, deployment_name)

        if has_evaluation:
            self._add_evaluation_role(template)
            self._add_evaluation(template, deployment_name, evaluation_config)

        if has_guardrails:
            self._add_guardrail(template, deployment_name)

        # Runtime + endpoint (always last — depends on everything above)
        self._add_runtime(template, config, deployment_name, has_gateway, has_memory, has_mcp_server, has_guardrails)
        self._add_runtime_endpoint(template, deployment_name)
        self._add_outputs(template, has_gateway, has_memory, has_mcp_server)

        # Generate portable agent code
        _obs_enabled = bool(
            getattr(config, "observability", None)
            or "observability" in (connected_tools or [])
            or getattr(config, "enable_otel", False)
        )
        agent_code = generate_agent_code(
            config=config,
            tools=connected_tools,
            gateway_config=None,
            template_id=template_id,
            gateway_tools=gateway_tools,
            custom_tools=[ct.model_dump() if hasattr(ct, "model_dump") else ct for ct in custom_tools],
            portable=True,
            observability_enabled=_obs_enabled,
        )

        # Generate MCP server code if needed (FastMCP, not HTTP runtime)
        mcp_server_code = None
        if has_mcp_server:
            from app.services.deployment import generate_mcp_server_code
            mcp_server_code = generate_mcp_server_code(
                server_name=f"{config.name}-mcp-server",
                tools=mcp_server_config.get("tools") if mcp_server_config else None,
                system_prompt=config.system_prompt,
            )

        # Package CFN provider Lambda code
        cfn_provider_zip = self._package_cfn_provider()

        # Package tool Lambda code if needed
        tool_lambda_zip = None
        if has_gateway and self._resolve_gateway_tools(template_id, gateway_tools, custom_tools):
            tool_lambda_zip = self._package_tool_lambdas(template_id, gateway_tools)

        # Package custom tool Lambda code if needed
        custom_tool_zip = None
        if custom_tools:
            custom_tool_zip = self._package_custom_tools(custom_tools)

        # Set correct dependency bundle default based on generated code
        bundle_key = STRANDS_BUNDLE_KEY if _needs_strands_bundle(agent_code) else BASE_BUNDLE_KEY
        template["Parameters"]["DependencyBundleKey"]["Default"] = bundle_key

        # Generate deployment scripts
        deploy_sh = self._generate_deploy_script(deployment_name, config, has_mcp_server, bundle_key)
        teardown_sh = self._generate_teardown_script()
        readme = self._generate_readme(deployment_name, template_id, config, has_gateway, has_memory, has_policy, has_mcp_server)

        # Serialize template
        template_yaml = yaml.dump(template, default_flow_style=False, sort_keys=False, allow_unicode=True)

        return CfnBundle(
            template_yaml=template_yaml,
            agent_code=agent_code,
            cfn_provider_code=cfn_provider_zip,
            tool_lambda_code=tool_lambda_zip,
            custom_tool_code=custom_tool_zip,
            mcp_server_code=mcp_server_code,
            deploy_sh=deploy_sh,
            teardown_sh=teardown_sh,
            readme=readme,
            deployment_name=deployment_name,
        )

    # ------------------------------------------------------------------
    # Template skeleton
    # ------------------------------------------------------------------

    def _init_template(self, deployment_name: str, template_id: Optional[str]) -> dict:
        return {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Description": f"AgentCore {template_id or 'custom'} stack — {deployment_name} | Generated by AgentCore Flows",
            "Parameters": {
                "DeploymentName": {
                    "Type": "String",
                    "Default": re.sub(r"[^a-zA-Z0-9]", "", deployment_name)[:40] or "agentcore",
                    "AllowedPattern": "^[a-zA-Z][a-zA-Z0-9]{0,39}$",
                    "ConstraintDescription": "Alphanumeric only, no hyphens/underscores, 1-40 chars, must start with letter",
                    "Description": "Base name for all resources (alphanumeric only — used in resource names with strict naming rules)",
                },
                "ModelId": {
                    "Type": "String",
                    "Default": "us.anthropic.claude-sonnet-5",
                    "Description": "Bedrock model ID (cross-region inference profile)",
                },
                "ArtifactsBucket": {
                    "Type": "String",
                    "Description": "S3 bucket for code artifacts and dependency bundles",
                },
                "AgentCodeKey": {
                    "Type": "String",
                    "Default": "cfn-assets/agent-code.zip",
                    "Description": "S3 key for the agent code zip",
                },
                "CfnProviderCodeKey": {
                    "Type": "String",
                    "Default": "cfn-assets/cfn-provider.zip",
                    "Description": "S3 key for the CFN Custom Resource Lambda zip",
                },
                "DependencyBundleKey": {
                    "Type": "String",
                    "Default": STRANDS_BUNDLE_KEY,
                    "Description": "S3 key for the pre-built dependency bundle",
                },
            },
            "Resources": {},
            "Outputs": {},
        }

    # ------------------------------------------------------------------
    # Custom Resource: Code Package
    # ------------------------------------------------------------------

    def _add_cfn_provider(self, template: dict, has_mcp_server: bool = False) -> None:
        """Add the CFN provider Lambda + role for Custom::AgentCodePackage."""
        policies = [
            {
                "PolicyName": "CodePackagingPolicy",
                "PolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                            "Resource": {"Fn::Sub": "arn:aws:s3:::${ArtifactsBucket}/*"},
                        }
                    ],
                },
            },
            {
                # Custom::AgentCorePolicy handler reads/creates policies on
                # the policy engine. Always granted because templates may
                # include Cedar policies. See tasks/lessons.md Bug 72.
                "PolicyName": "AgentCorePolicyManagement",
                "PolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "bedrock-agentcore:CreatePolicy",
                                "bedrock-agentcore:DeletePolicy",
                                "bedrock-agentcore:ListPolicies",
                                "bedrock-agentcore:GetPolicy",
                                "bedrock-agentcore:GetPolicyEngine",
                                "bedrock-agentcore:ListPolicyEngines",
                                # AgentCore CreatePolicy implicitly requires
                                # ManageAdminPolicy permission (undocumented).
                                # See tasks/lessons.md Bug 93.
                                "bedrock-agentcore:ManageAdminPolicy",
                                "bedrock-agentcore:UpdatePolicy",
                            ],
                            "Resource": "*",
                        }
                    ],
                },
            },
        ]

        # MCP server targets need OAuth2 credential provider management
        if has_mcp_server:
            policies.append({
                "PolicyName": "CredentialProviderPolicy",
                "PolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "bedrock-agentcore:*",  # OAuth2 credential provider needs multiple actions incl. CreateTokenVault
                                "secretsmanager:CreateSecret",
                                "secretsmanager:DeleteSecret",
                                "secretsmanager:GetSecretValue",
                                "secretsmanager:PutSecretValue",
                            ],
                            "Resource": "*",
                        }
                    ],
                },
            })

        template["Resources"]["CfnProviderRole"] = {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "lambda.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                },
                "ManagedPolicyArns": [
                    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                ],
                "Policies": policies,
            },
        }

        template["Resources"]["CfnProviderLambda"] = {
            "Type": "AWS::Lambda::Function",
            "Properties": {
                "Runtime": "python3.13",
                "Handler": "handler.handler",
                "Role": {"Fn::GetAtt": ["CfnProviderRole", "Arn"]},
                "Code": {
                    "S3Bucket": {"Ref": "ArtifactsBucket"},
                    "S3Key": {"Ref": "CfnProviderCodeKey"},
                },
                "Timeout": 300,
                "MemorySize": 1024,
            },
        }

    def _add_code_package(self, template: dict, config: RuntimeConfig) -> None:
        """Add the Custom::AgentCodePackage resource."""
        template["Resources"]["AgentCodePackage"] = {
            "Type": "Custom::AgentCodePackage",
            "Properties": {
                "ServiceToken": {"Fn::GetAtt": ["CfnProviderLambda", "Arn"]},
                "ArtifactsBucket": {"Ref": "ArtifactsBucket"},
                "AgentCodeKey": {"Ref": "AgentCodeKey"},
                "DependencyBundleKey": {"Ref": "DependencyBundleKey"},
                "OutputKey": {"Fn::Sub": "deployments/${AWS::StackName}/code.zip"},
            },
        }

    # ------------------------------------------------------------------
    # IAM: Runtime Execution Role
    # ------------------------------------------------------------------

    def _add_runtime_role(
        self,
        template: dict,
        connected_tools: list,
        has_gateway: bool,
        has_memory: bool,
        has_evaluation: bool,
        otel_secret_arn: Optional[str] = None,
    ) -> None:
        statements = [
            {
                "Sid": "BedrockModelAccess",
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                "Resource": "*",
            },
            {
                "Sid": "S3CodeAccess",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:ListBucket"],
                "Resource": [
                    {"Fn::Sub": "arn:aws:s3:::${ArtifactsBucket}"},
                    {"Fn::Sub": "arn:aws:s3:::${ArtifactsBucket}/*"},
                ],
            },
            {
                "Sid": "CloudWatchLogs",
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": "*",
            },
        ]

        if has_gateway or "gateway" in connected_tools:
            statements.append(
                {
                    "Sid": "GatewayAccess",
                    "Effect": "Allow",
                    "Action": [
                        "bedrock-agentcore:InvokeGateway",
                        "bedrock-agentcore:ListGateways",
                        "bedrock-agentcore:GetGateway",
                    ],
                    "Resource": "*",
                }
            )

        if has_memory or "memory" in connected_tools:
            statements.append(
                {
                    "Sid": "MemoryAccess",
                    "Effect": "Allow",
                    "Action": [
                        "bedrock-agentcore:*Memory*",
                        "bedrock-agentcore:CreateEvent",
                        "bedrock-agentcore:GetLastKTurns",
                        "bedrock-agentcore:RetrieveMemories",
                        "bedrock-agentcore:ListSessions",
                        "bedrock-agentcore:ListActors",
                        "bedrock-agentcore:ListEvents",
                        "bedrock-agentcore-control:GetMemory",
                        "bedrock-agentcore-control:ListMemories",
                    ],
                    "Resource": "*",
                }
            )

        if "browser" in connected_tools:
            statements.append(
                {
                    "Sid": "BrowserAccess",
                    "Effect": "Allow",
                    "Action": ["bedrock-agentcore:*Browser*"],
                    "Resource": "*",
                }
            )

        if "code_interpreter" in connected_tools:
            statements.append(
                {
                    "Sid": "CodeInterpreterAccess",
                    "Effect": "Allow",
                    "Action": ["bedrock-agentcore:*CodeInterpreter*"],
                    "Resource": "*",
                }
            )

        if "guardrails" in connected_tools:
            statements.append(
                {
                    "Sid": "GuardrailsAccess",
                    "Effect": "Allow",
                    "Action": ["bedrock:ApplyGuardrail", "bedrock:GetGuardrail"],
                    "Resource": "*",
                }
            )

        if has_evaluation:
            statements.append(
                {
                    "Sid": "EvaluationAccess",
                    "Effect": "Allow",
                    "Action": [
                        "bedrock-agentcore:Evaluate",
                        "bedrock-agentcore-control:CreateOnlineEvaluationConfig",
                        "bedrock-agentcore-control:GetOnlineEvaluationConfig",
                        "bedrock-agentcore-control:ListOnlineEvaluationConfigs",
                        "bedrock-agentcore-control:ListEvaluators",
                        "bedrock-agentcore-control:GetEvaluator",
                        "logs:StartQuery",
                        "logs:GetQueryResults",
                    ],
                    "Resource": "*",
                }
            )

        if otel_secret_arn:
            statements.append(
                {
                    "Sid": "OtelAuthHeaderSecret",
                    "Effect": "Allow",
                    "Action": ["secretsmanager:GetSecretValue"],
                    "Resource": [otel_secret_arn],
                }
            )

        template["Resources"]["RuntimeExecutionRole"] = {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "RoleName": {"Fn::Sub": "AgentCoreRuntime-${AWS::StackName}"},
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                },
                "Policies": [
                    {
                        "PolicyName": "RuntimePolicy",
                        "PolicyDocument": {"Version": "2012-10-17", "Statement": statements},
                    }
                ],
            },
        }

    # ------------------------------------------------------------------
    # Cognito OAuth (for Gateway auth)
    # ------------------------------------------------------------------

    def _add_cognito(self, template: dict, deployment_name: str) -> None:
        template["Resources"]["CognitoUserPool"] = {
            "Type": "AWS::Cognito::UserPool",
            "Properties": {
                "UserPoolName": {"Fn::Sub": "AgentCore-${DeploymentName}"},
                "AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True},
            },
        }

        template["Resources"]["CognitoResourceServer"] = {
            "Type": "AWS::Cognito::UserPoolResourceServer",
            "Properties": {
                "UserPoolId": {"Ref": "CognitoUserPool"},
                "Identifier": {"Fn::Sub": "agentcore-${DeploymentName}"},
                "Name": {"Fn::Sub": "agentcore-${DeploymentName}"},
                "Scopes": [{"ScopeName": "invoke", "ScopeDescription": "Invoke gateway"}],
            },
        }

        template["Resources"]["CognitoUserPoolDomain"] = {
            "Type": "AWS::Cognito::UserPoolDomain",
            "Properties": {
                "UserPoolId": {"Ref": "CognitoUserPool"},
                "Domain": {"Fn::Sub": "ac-${DeploymentName}-${AWS::AccountId}"},
            },
        }

        template["Resources"]["CognitoUserPoolClient"] = {
            "Type": "AWS::Cognito::UserPoolClient",
            "DependsOn": "CognitoResourceServer",
            "Properties": {
                "UserPoolId": {"Ref": "CognitoUserPool"},
                "ClientName": {"Fn::Sub": "${DeploymentName}-client"},
                "GenerateSecret": True,
                "AllowedOAuthFlows": ["client_credentials"],
                "AllowedOAuthFlowsUserPoolClient": True,
                "AllowedOAuthScopes": [{"Fn::Sub": "agentcore-${DeploymentName}/invoke"}],
            },
        }

    # ------------------------------------------------------------------
    # Gateway IAM Role
    # ------------------------------------------------------------------

    def _add_gateway_role(self, template: dict) -> None:
        template["Resources"]["GatewayRole"] = {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "RoleName": {"Fn::Sub": "AgentCoreGateway-${AWS::StackName}"},
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                },
                "Policies": [
                    {
                        "PolicyName": "GatewayPolicy",
                        "PolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Sid": "AgentCoreGatewayOps",
                                    "Effect": "Allow",
                                    "Action": [
                                        "bedrock-agentcore:InvokeGateway",
                                        "bedrock-agentcore:GetGateway",
                                        "bedrock-agentcore:ListGateways",
                                        "bedrock-agentcore:GetGatewayTarget",
                                        "bedrock-agentcore:ListGatewayTargets",
                                        "bedrock-agentcore:InvokeAgent",
                                        "bedrock-agentcore:GetPolicyEngine",
                                        "bedrock-agentcore:GetPolicy",
                                        "bedrock-agentcore:AuthorizeAction",
                                        "bedrock-agentcore:PartiallyAuthorizeActions",
                                        # CFN-provider's GenesisPolicyEngineCheck binds the
                                        # PolicyEngine to the Gateway target; that bind call
                                        # validates the Gateway role can call this action on
                                        # the policy engine. See tasks/lessons.md Bug 67.
                                        "bedrock-agentcore:CheckAuthorizePermissions",
                                    ],
                                    "Resource": "*",
                                },
                                {
                                    "Sid": "BedrockModelAccess",
                                    "Effect": "Allow",
                                    "Action": [
                                        "bedrock:InvokeModel",
                                        "bedrock:InvokeModelWithResponseStream",
                                    ],
                                    "Resource": "*",
                                },
                                {
                                    "Sid": "LambdaInvoke",
                                    "Effect": "Allow",
                                    "Action": ["lambda:InvokeFunction"],
                                    "Resource": {"Fn::Sub": "arn:aws:lambda:${AWS::Region}:${AWS::AccountId}:function:agentcore-${DeploymentName}-*"},
                                },
                                {
                                    "Sid": "CredentialProviderAccess",
                                    "Effect": "Allow",
                                    "Action": [
                                        "agent-credential-provider:GetCredentials",
                                        "agent-credential-provider:ListCredentialProviders",
                                    ],
                                    "Resource": "*",
                                },
                                {
                                    "Sid": "SecretsManagerRead",
                                    "Effect": "Allow",
                                    "Action": ["secretsmanager:GetSecretValue"],
                                    "Resource": "*",
                                },
                            ],
                        },
                    }
                ],
            },
        }

    # ------------------------------------------------------------------
    # Gateway (native CFN)
    # ------------------------------------------------------------------

    def _add_gateway(self, template: dict, deployment_name: str) -> None:
        template["Resources"]["AgentCoreGateway"] = {
            "Type": "AWS::BedrockAgentCore::Gateway",
            "DependsOn": ["GatewayRole", "CognitoUserPoolClient"],
            "Properties": {
                "Name": {"Fn::Sub": "${DeploymentName}-gateway"},
                "AuthorizerType": "CUSTOM_JWT",
                "ProtocolType": "MCP",
                "RoleArn": {"Fn::GetAtt": ["GatewayRole", "Arn"]},
                "AuthorizerConfiguration": {
                    "CustomJWTAuthorizer": {
                        "DiscoveryUrl": {
                            "Fn::Sub": "https://cognito-idp.${AWS::Region}.amazonaws.com/${CognitoUserPool}/.well-known/openid-configuration"
                        },
                        "AllowedClients": [{"Ref": "CognitoUserPoolClient"}],
                    }
                },
                "Description": {"Fn::Sub": "Gateway for ${DeploymentName}"},
                "Tags": {"ManagedBy": "CloudFormation", "Stack": {"Ref": "AWS::StackName"}},
            },
        }

    # ------------------------------------------------------------------
    # Tool Lambda + Gateway Targets
    # ------------------------------------------------------------------

    def _add_tool_lambda_role(self, template: dict) -> None:
        template["Resources"]["ToolLambdaRole"] = {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "RoleName": {"Fn::Sub": "AgentCoreLambda-${AWS::StackName}"},
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "lambda.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                },
                "ManagedPolicyArns": [
                    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                ],
            },
        }

        # Add ToolLambdaCodeKey parameter
        if "ToolLambdaCodeKey" not in template["Parameters"]:
            template["Parameters"]["ToolLambdaCodeKey"] = {
                "Type": "String",
                "Default": "cfn-assets/tool-lambdas.zip",
                "Description": "S3 key for the tool Lambda code zip",
            }

    def _resolve_gateway_tools(
        self,
        template_id: Optional[str],
        gateway_tools: list,
        custom_tools: list,
    ) -> list[str]:
        """Determine which gateway tool IDs are needed."""
        # Template-specific tools
        if template_id == "customer-support-blueprint":
            return ["get_order", "get_customer", "list_orders", "process_refund"]
        if template_id in ("strands-gateway-agent", "customer-support-assistant"):
            return list(_DYNAMIC_TOOL_IDS)

        # User-specified tools — items may be dicts with toolId or plain strings
        tool_ids = [
            (t["toolId"] if isinstance(t, dict) else t)
            for t in gateway_tools
        ]
        # Custom tools don't add to gateway_tools — they become separate targets
        return tool_ids

    def _add_tool_lambdas_and_targets(
        self, template: dict, deployment_name: str, tool_ids: list[str]
    ) -> None:
        """Add Lambda functions and Gateway targets for the specified tools."""
        # Group tools by Lambda function
        customer_tools = [t for t in tool_ids if t in _CUSTOMER_SUPPORT_TOOL_IDS]
        dynamic_tools = [t for t in tool_ids if t in _DYNAMIC_TOOL_IDS]

        if dynamic_tools:
            self._add_tool_lambda(template, "DynamicToolsLambda", "dynamic_tools.handler", dynamic_tools)
            self._add_gateway_target(template, "DynamicToolsTarget", "DynamicTools", "DynamicToolsLambda", dynamic_tools)

        if customer_tools:
            self._add_tool_lambda(template, "CustomerSupportToolsLambda", "customer_support_tools.handler", customer_tools)
            self._add_gateway_target(template, "CustomerSupportTarget", "CustomerSupportTools", "CustomerSupportToolsLambda", customer_tools)

    def _add_tool_lambda(
        self, template: dict, logical_id: str, handler: str, tool_ids: list[str]
    ) -> None:
        template["Resources"][logical_id] = {
            "Type": "AWS::Lambda::Function",
            "Properties": {
                "FunctionName": {"Fn::Sub": f"agentcore-${{DeploymentName}}-{logical_id.lower().replace('lambda', '')}"},
                "Runtime": "python3.13",
                "Handler": handler,
                "Role": {"Fn::GetAtt": ["ToolLambdaRole", "Arn"]},
                "Code": {
                    "S3Bucket": {"Ref": "ArtifactsBucket"},
                    "S3Key": {"Ref": "ToolLambdaCodeKey"},
                },
                "Timeout": 30,
                "MemorySize": 256,
            },
        }

        # Permission for Gateway to invoke Lambda
        template["Resources"][f"{logical_id}Permission"] = {
            "Type": "AWS::Lambda::Permission",
            "Properties": {
                "FunctionName": {"Fn::GetAtt": [logical_id, "Arn"]},
                "Action": "lambda:InvokeFunction",
                "Principal": "bedrock-agentcore.amazonaws.com",
            },
        }

    def _add_gateway_target(
        self,
        template: dict,
        logical_id: str,
        target_name: str,
        lambda_logical_id: str,
        tool_ids: list[str],
    ) -> None:
        """Add a Gateway Target pointing to a Lambda with inline tool schemas."""
        schemas = []
        for tid in tool_ids:
            schema = GATEWAY_TOOL_SCHEMAS.get(tid)
            if schema:
                # Convert to CFN InlinePayload format with PascalCase keys
                schemas.append({
                    "Name": schema["name"],
                    "Description": schema.get("description", ""),
                    "InputSchema": _to_pascal_case_schema(schema.get("inputSchema", {})),
                })

        template["Resources"][logical_id] = {
            "Type": "AWS::BedrockAgentCore::GatewayTarget",
            "DependsOn": ["AgentCoreGateway", lambda_logical_id, f"{lambda_logical_id}Permission"],
            "Properties": {
                "GatewayIdentifier": {"Fn::GetAtt": ["AgentCoreGateway", "GatewayIdentifier"]},
                "Name": target_name,
                "TargetConfiguration": {
                    "Mcp": {
                        "Lambda": {
                            "LambdaArn": {"Fn::GetAtt": [lambda_logical_id, "Arn"]},
                            "ToolSchema": {"InlinePayload": schemas},
                        }
                    }
                },
                "CredentialProviderConfigurations": [
                    {"CredentialProviderType": "GATEWAY_IAM_ROLE"}
                ],
                "Description": f"Gateway target: {target_name}",
            },
        }

    # ------------------------------------------------------------------
    # Custom AI-Generated Tool Targets
    # ------------------------------------------------------------------

    def _add_custom_tool_targets(self, template: dict, deployment_name: str, custom_tools: list) -> None:
        """Add Lambda functions and Gateway Targets for custom AI-generated tools."""
        # Add parameter for custom tool code zip
        if "CustomToolCodeKey" not in template["Parameters"]:
            template["Parameters"]["CustomToolCodeKey"] = {
                "Type": "String",
                "Default": "cfn-assets/custom-tools.zip",
                "Description": "S3 key for the custom tool Lambda code zip",
            }

        for i, tool in enumerate(custom_tools):
            # Normalize tool definition (handle both dict and Pydantic model)
            if hasattr(tool, "model_dump"):
                tool = tool.model_dump(by_alias=True)
            tool_name = tool.get("toolName", tool.get("tool_name", f"custom_tool_{i}"))
            safe_name = re.sub(r"[^a-zA-Z0-9]", "", tool_name)[:32]
            logical_id = f"CustomTool{safe_name}Lambda"
            target_id = f"CustomTool{safe_name}Target"

            # Lambda function
            template["Resources"][logical_id] = {
                "Type": "AWS::Lambda::Function",
                "Properties": {
                    "FunctionName": {"Fn::Sub": f"agentcore-${{DeploymentName}}-ct-{safe_name.lower()}"},
                    "Runtime": "python3.13",
                    "Handler": f"ct_{safe_name.lower()}.handler",
                    "Role": {"Fn::GetAtt": ["ToolLambdaRole", "Arn"]},
                    "Code": {
                        "S3Bucket": {"Ref": "ArtifactsBucket"},
                        "S3Key": {"Ref": "CustomToolCodeKey"},
                    },
                    "Timeout": 30,
                    "MemorySize": 256,
                },
            }

            # Lambda invoke permission for Gateway
            template["Resources"][f"{logical_id}Permission"] = {
                "Type": "AWS::Lambda::Permission",
                "Properties": {
                    "FunctionName": {"Fn::GetAtt": [logical_id, "Arn"]},
                    "Action": "lambda:InvokeFunction",
                    "Principal": "bedrock-agentcore.amazonaws.com",
                },
            }

            # Gateway target with tool schema
            input_schema = tool.get("inputSchema", tool.get("input_schema", {}))
            template["Resources"][target_id] = {
                "Type": "AWS::BedrockAgentCore::GatewayTarget",
                "DependsOn": ["AgentCoreGateway", logical_id],
                "Properties": {
                    "GatewayIdentifier": {"Fn::GetAtt": ["AgentCoreGateway", "GatewayIdentifier"]},
                    "Name": f"CT-{safe_name}",
                    "TargetConfiguration": {
                        "Mcp": {
                            "Lambda": {
                                "LambdaArn": {"Fn::GetAtt": [logical_id, "Arn"]},
                                "ToolSchema": {
                                    "InlinePayload": [
                                        {
                                            "Name": tool_name,
                                            "Description": tool.get("description", ""),
                                            "InputSchema": _to_pascal_case_schema(input_schema),
                                        }
                                    ]
                                },
                            }
                        }
                    },
                    "CredentialProviderConfigurations": [
                        {"CredentialProviderType": "GATEWAY_IAM_ROLE"}
                    ],
                    "Description": f"Custom tool: {tool.get('displayName', tool.get('display_name', tool_name))}",
                },
            }

    def _package_custom_tools(self, custom_tools: list) -> bytes:
        """Package custom tool Lambda code into a zip."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for tool in custom_tools:
                if hasattr(tool, "model_dump"):
                    tool = tool.model_dump(by_alias=True)
                tool_name = tool.get("toolName", tool.get("tool_name", "custom_tool"))
                safe_name = re.sub(r"[^a-zA-Z0-9]", "", tool_name)[:32].lower()
                lambda_code = tool.get("lambdaCode", tool.get("lambda_code", ""))

                # Wrap user code in MCP-compatible Lambda handler
                handler_code = f'''"""Custom tool: {tool_name} — Generated by AgentCore Flows."""
import json

{lambda_code}

def handler(event, context):
    """MCP tool handler for Gateway."""
    tool_name = context.client_context.custom.get("bedrockAgentCoreToolName", "") if hasattr(context, "client_context") and context.client_context else event.get("name", "")
    args = event.get("arguments", event.get("input", {{}}))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {{}}
    # Remove non-argument keys that may leak from the MCP event envelope
    for _k in ("name", "id", "method", "jsonrpc"):
        args.pop(_k, None)
    try:
        result = {safe_name}(**args) if "{safe_name}" in tool_name else {{"error": f"Unknown tool: {{tool_name}}"}}
        if isinstance(result, str):
            return {{"content": [{{"type": "text", "text": result}}]}}
        return {{"content": [{{"type": "text", "text": json.dumps(result)}}]}}
    except Exception as e:
        return {{"content": [{{"type": "text", "text": json.dumps({{"error": str(e)}})}}]}}
'''
                zf.writestr(f"ct_{safe_name}.py", handler_code)
        buf.seek(0)
        return buf.read()

    # ------------------------------------------------------------------
    # Knowledge Base Tool Lambda + Gateway Target
    # ------------------------------------------------------------------

    def _add_kb_tool_lambda_and_target(self, template: dict, deployment_name: str, kb_config: dict) -> None:
        """Add a Lambda function and Gateway Target for the Knowledge Base tool."""
        kb_mode = kb_config.get("kbMode", "existing")
        foundation_model_id = kb_config.get("foundationModelId", "us.anthropic.claude-sonnet-5")

        # For "existing" mode, KB ID is a parameter; for "create_new", it's created by the KB resources
        if kb_mode == "existing":
            kb_id_value = kb_config.get("knowledgeBaseId", "")
            template["Parameters"]["KnowledgeBaseId"] = {
                "Type": "String",
                "Default": kb_id_value,
                "Description": "ID of the existing Bedrock Knowledge Base",
            }
            kb_id_ref = {"Ref": "KnowledgeBaseId"}
        else:
            # Create New mode: add KB resources
            self._add_kb_creation_resources(template, deployment_name, kb_config)
            kb_id_ref = {"Fn::GetAtt": ["BedrockKnowledgeBase", "KnowledgeBaseId"]}

        model_arn = {"Fn::Sub": f"arn:aws:bedrock:${{AWS::Region}}::foundation-model/{foundation_model_id}"}

        # IAM Role for KB Lambda (needs Bedrock KB permissions)
        template["Resources"]["KBToolLambdaRole"] = {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "RoleName": {"Fn::Sub": "AgentCoreKBTool-${AWS::StackName}"},
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "lambda.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                },
                "ManagedPolicyArns": [
                    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                ],
                "Policies": [
                    {
                        "PolicyName": "BedrockKBAccess",
                        "PolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Effect": "Allow",
                                    "Action": [
                                        "bedrock:Retrieve",
                                        "bedrock:RetrieveAndGenerate",
                                        "bedrock:InvokeModel",
                                    ],
                                    "Resource": "*",
                                }
                            ],
                        },
                    }
                ],
            },
        }

        # KB Lambda code (inline)
        kb_lambda_code = (
            "import json, os, boto3\n"
            "bedrock_runtime = boto3.client('bedrock-agent-runtime', region_name=os.environ.get('AWS_REGION', 'us-east-1'))\n"
            "def lambda_handler(event, context):\n"
            "    query = event.get('query', '')\n"
            "    kb_id = os.environ['KNOWLEDGE_BASE_ID']\n"
            "    model_arn = os.environ['FOUNDATION_MODEL_ARN']\n"
            "    try:\n"
            "        resp = bedrock_runtime.retrieve_and_generate(\n"
            "            input={'text': query},\n"
            "            retrieveAndGenerateConfiguration={\n"
            "                'type': 'KNOWLEDGE_BASE',\n"
            "                'knowledgeBaseConfiguration': {\n"
            "                    'knowledgeBaseId': kb_id,\n"
            "                    'modelArn': model_arn,\n"
            "                }\n"
            "            }\n"
            "        )\n"
            "        answer = resp.get('output', {}).get('text', 'No answer found.')\n"
            "        citations = []\n"
            "        for c in resp.get('citations', [])[:5]:\n"
            "            for ref in c.get('retrievedReferences', [])[:2]:\n"
            "                loc = ref.get('location', {})\n"
            "                citations.append({\n"
            "                    'text': ref.get('content', {}).get('text', '')[:200],\n"
            "                    'source': loc.get('s3Location', {}).get('uri', '') or loc.get('webLocation', {}).get('url', ''),\n"
            "                })\n"
            "        return {'statusCode': 200, 'body': json.dumps({'answer': answer, 'citations': citations})}\n"
            "    except Exception as e:\n"
            "        return {'statusCode': 200, 'body': json.dumps({'error': str(e)})}\n"
        )

        # Lambda function
        template["Resources"]["KBToolLambda"] = {
            "Type": "AWS::Lambda::Function",
            "Properties": {
                "FunctionName": {"Fn::Sub": "agentcore-${DeploymentName}-kb-tool"},
                "Runtime": "python3.13",
                "Handler": "index.lambda_handler",
                "Role": {"Fn::GetAtt": ["KBToolLambdaRole", "Arn"]},
                "Code": {"ZipFile": kb_lambda_code},
                "Timeout": 30,
                "MemorySize": 256,
                "Environment": {
                    "Variables": {
                        "KNOWLEDGE_BASE_ID": kb_id_ref,
                        "FOUNDATION_MODEL_ARN": model_arn,
                    }
                },
            },
        }

        # Lambda invoke permission for Gateway
        template["Resources"]["KBToolLambdaPermission"] = {
            "Type": "AWS::Lambda::Permission",
            "Properties": {
                "FunctionName": {"Fn::GetAtt": ["KBToolLambda", "Arn"]},
                "Action": "lambda:InvokeFunction",
                "Principal": "bedrock-agentcore.amazonaws.com",
            },
        }

        # Gateway target with KB tool schema
        template["Resources"]["KBToolTarget"] = {
            "Type": "AWS::BedrockAgentCore::GatewayTarget",
            "DependsOn": ["AgentCoreGateway", "KBToolLambda", "KBToolLambdaPermission"],
            "Properties": {
                "GatewayIdentifier": {"Fn::GetAtt": ["AgentCoreGateway", "GatewayIdentifier"]},
                "Name": "KBTool",
                "TargetConfiguration": {
                    "Mcp": {
                        "Lambda": {
                            "LambdaArn": {"Fn::GetAtt": ["KBToolLambda", "Arn"]},
                            "ToolSchema": {
                                "InlinePayload": [
                                    {
                                        "Name": "knowledge_base_query",
                                        "Description": "Search the knowledge base to answer questions using Retrieval Augmented Generation (RAG).",
                                        "InputSchema": {
                                            "Type": "object",
                                            "Properties": {
                                                "query": {"Type": "string", "Description": "The question to answer from the knowledge base"},
                                            },
                                            "Required": ["query"],
                                        },
                                    }
                                ]
                            },
                        }
                    }
                },
                "CredentialProviderConfigurations": [
                    {"CredentialProviderType": "GATEWAY_IAM_ROLE"}
                ],
                "Description": "Knowledge Base RAG query tool",
            },
        }

    def _add_kb_creation_resources(self, template: dict, deployment_name: str, kb_config: dict) -> None:
        """Add CFN resources to create a new Bedrock Knowledge Base (create_new mode)."""
        embedding_model_id = kb_config.get("embeddingModelId", "amazon.titan-embed-text-v2:0")
        kb_name = kb_config.get("kbName", "agentcore-kb")
        kb_description = kb_config.get("kbDescription", "Knowledge Base created by AgentCore Flows")
        data_source_type = kb_config.get("dataSourceType", "s3")
        vector_store_type = kb_config.get("vectorStoreType", "s3_vectors")
        chunking_strategy = kb_config.get("chunkingStrategy", "FIXED_SIZE")

        # Build IAM policy statements based on config
        iam_statements: list[dict] = [
            {
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel"],
                "Resource": {"Fn::Sub": f"arn:aws:bedrock:${{AWS::Region}}::foundation-model/{embedding_model_id}"},
            },
        ]

        # S3 data source permissions
        if data_source_type == "s3":
            s3_uri = kb_config.get("s3BucketUri", "arn:aws:s3:::*")
            bucket_arn = s3_uri.replace("s3://", "arn:aws:s3:::").split("/")[0]
            iam_statements.append({
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:ListBucket"],
                "Resource": [bucket_arn, f"{bucket_arn}/*"],
            })

        # Credential-based data sources need Secrets Manager
        secret_arns = []
        if data_source_type == "confluence":
            secret_arns.append(kb_config.get("confluenceCredentialsSecretArn", ""))
        elif data_source_type == "salesforce":
            secret_arns.append(kb_config.get("salesforceCredentialsSecretArn", ""))
        elif data_source_type == "sharepoint":
            secret_arns.append(kb_config.get("sharePointCredentialsSecretArn", ""))

        # Vector store permissions
        if vector_store_type == "opensearch_serverless":
            iam_statements.append({
                "Effect": "Allow",
                "Action": ["aoss:APIAccessAll"],
                "Resource": kb_config.get("opensearchCollectionArn", "*"),
            })
        elif vector_store_type == "rds":
            iam_statements.append({
                "Effect": "Allow",
                "Action": ["rds-data:ExecuteStatement", "rds-data:BatchExecuteStatement"],
                "Resource": kb_config.get("rdsResourceArn", "*"),
            })
            rds_secret = kb_config.get("rdsCredentialsSecretArn", "")
            if rds_secret:
                secret_arns.append(rds_secret)

        # Transformation Lambda permissions
        transform_lambda = kb_config.get("transformationLambdaArn", "")
        if transform_lambda:
            iam_statements.append({
                "Effect": "Allow",
                "Action": ["lambda:InvokeFunction"],
                "Resource": transform_lambda,
            })

        # S3 for transformation intermediate storage
        transform_s3 = kb_config.get("transformationS3Uri", "")
        if transform_s3 and transform_s3.startswith("s3://"):
            t_bucket = transform_s3[5:].split("/")[0]
            t_bucket_arn = f"arn:aws:s3:::{t_bucket}"
            iam_statements.append({
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
                "Resource": [t_bucket_arn, f"{t_bucket_arn}/*"],
            })

        valid_secrets = [s for s in secret_arns if s]
        if valid_secrets:
            iam_statements.append({
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": valid_secrets if len(valid_secrets) > 1 else valid_secrets[0],
            })

        # IAM Role for Knowledge Base
        template["Resources"]["KnowledgeBaseRole"] = {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "RoleName": {"Fn::Sub": "AgentCoreKBRole-${AWS::StackName}"},
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "bedrock.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                },
                "Policies": [
                    {
                        "PolicyName": "KBPolicy",
                        "PolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": iam_statements,
                        },
                    }
                ],
            },
        }

        # Storage configuration
        storage_config = self._build_cfn_storage_config(kb_config)

        # Knowledge Base
        template["Resources"]["BedrockKnowledgeBase"] = {
            "Type": "AWS::Bedrock::KnowledgeBase",
            "Properties": {
                "Name": {"Fn::Sub": f"{kb_name}-${{DeploymentName}}"},
                "Description": kb_description,
                "RoleArn": {"Fn::GetAtt": ["KnowledgeBaseRole", "Arn"]},
                "KnowledgeBaseConfiguration": {
                    "Type": "VECTOR",
                    "VectorKnowledgeBaseConfiguration": {
                        "EmbeddingModelArn": {"Fn::Sub": f"arn:aws:bedrock:${{AWS::Region}}::foundation-model/{embedding_model_id}"},
                    },
                },
                "StorageConfiguration": storage_config,
            },
        }

        # Data Source
        chunking_config = self._build_chunking_config(chunking_strategy, kb_config)
        ds_config = self._build_cfn_data_source_config(kb_config)

        # Build VectorIngestionConfiguration (chunking + parsing + transformation)
        ingestion_config: dict = {"ChunkingConfiguration": chunking_config}

        # Parsing strategy
        parsing_strategy = kb_config.get("parsingStrategy", "default")
        if parsing_strategy == "bedrock_data_automation":
            ingestion_config["ParsingConfiguration"] = {
                "ParsingStrategy": "BEDROCK_DATA_AUTOMATION",
                "BedrockDataAutomationConfiguration": {"ParsingModality": "MULTIMODAL"},
            }
        elif parsing_strategy == "bedrock_foundation_model":
            parsing_model_id = kb_config.get("parsingModelId", "us.anthropic.claude-sonnet-5")
            fm_cfg: dict = {
                "ModelArn": {"Fn::Sub": f"arn:aws:bedrock:${{AWS::Region}}::foundation-model/{parsing_model_id}"},
                "ParsingModality": "MULTIMODAL",
            }
            parsing_prompt = kb_config.get("parsingPrompt", "")
            if parsing_prompt:
                fm_cfg["ParsingPrompt"] = {"ParsingPromptText": parsing_prompt}
            ingestion_config["ParsingConfiguration"] = {
                "ParsingStrategy": "BEDROCK_FOUNDATION_MODEL",
                "BedrockFoundationModelConfiguration": fm_cfg,
            }

        # Custom transformation Lambda
        transform_lambda = kb_config.get("transformationLambdaArn", "")
        transform_s3 = kb_config.get("transformationS3Uri", "")
        if transform_lambda and transform_s3:
            ingestion_config["CustomTransformationConfiguration"] = {
                "IntermediateStorage": {
                    "S3Location": {"URI": transform_s3},
                },
                "Transformations": [
                    {
                        "TransformationFunction": {
                            "TransformationLambdaConfiguration": {"LambdaArn": transform_lambda},
                        },
                        "StepToApply": "POST_CHUNKING",
                    }
                ],
            }

        ds_properties: dict = {
            "KnowledgeBaseId": {"Fn::GetAtt": ["BedrockKnowledgeBase", "KnowledgeBaseId"]},
            "Name": {"Fn::Sub": f"{kb_name}-ds-${{DeploymentName}}"},
            "DataSourceConfiguration": ds_config,
            "VectorIngestionConfiguration": ingestion_config,
        }

        # Data deletion policy
        deletion_policy = kb_config.get("dataDeletionPolicy", "DELETE")
        if deletion_policy != "DELETE":
            ds_properties["DataDeletionPolicy"] = deletion_policy

        # KMS key for transient data encryption
        kms_key = kb_config.get("kmsKeyArn", "")
        if kms_key:
            ds_properties["ServerSideEncryptionConfiguration"] = {"KmsKeyArn": kms_key}

        template["Resources"]["KBDataSource"] = {
            "Type": "AWS::Bedrock::DataSource",
            "DependsOn": ["BedrockKnowledgeBase"],
            "Properties": ds_properties,
        }

    @staticmethod
    def _build_cfn_storage_config(kb_config: dict) -> dict:
        """Build CFN StorageConfiguration based on vector store type."""
        vector_store_type = kb_config.get("vectorStoreType", "s3_vectors")

        if vector_store_type == "opensearch_serverless":
            return {
                "Type": "OPENSEARCH_SERVERLESS",
                "OpensearchServerlessConfiguration": {
                    "CollectionArn": kb_config.get("opensearchCollectionArn", ""),
                    "VectorIndexName": kb_config.get("opensearchVectorIndexName", "bedrock-knowledge-base-default-index"),
                    "FieldMapping": {
                        "VectorField": kb_config.get("opensearchVectorField", "bedrock-knowledge-base-default-vector"),
                        "TextField": kb_config.get("opensearchTextField", "AMAZON_BEDROCK_TEXT_CHUNK"),
                        "MetadataField": kb_config.get("opensearchMetadataField", "AMAZON_BEDROCK_METADATA"),
                    },
                },
            }

        if vector_store_type == "rds":
            return {
                "Type": "RDS",
                "RdsConfiguration": {
                    "ResourceArn": kb_config.get("rdsResourceArn", ""),
                    "CredentialsSecretArn": kb_config.get("rdsCredentialsSecretArn", ""),
                    "DatabaseName": kb_config.get("rdsDatabaseName", ""),
                    "TableName": kb_config.get("rdsTableName", ""),
                    "FieldMapping": {
                        "PrimaryKeyField": kb_config.get("rdsPrimaryKeyField", "id"),
                        "VectorField": kb_config.get("rdsVectorField", "embedding"),
                        "TextField": kb_config.get("rdsTextField", "chunks"),
                        "MetadataField": kb_config.get("rdsMetadataField", "metadata"),
                    },
                },
            }

        # Default: S3_VECTORS (fully managed)
        return {"Type": "S3_VECTORS"}

    @staticmethod
    def _build_cfn_data_source_config(kb_config: dict) -> dict:
        """Build CFN DataSourceConfiguration for all supported source types."""
        data_source_type = kb_config.get("dataSourceType", "s3")

        if data_source_type == "s3":
            s3_uri = kb_config.get("s3BucketUri", "s3://my-bucket/")
            bucket_arn = s3_uri.replace("s3://", "arn:aws:s3:::").split("/")[0]
            s3_cfg: dict = {"BucketArn": bucket_arn}
            prefix = "/".join(s3_uri.replace("s3://", "").split("/")[1:])
            if prefix:
                s3_cfg["InclusionPrefixes"] = [prefix]
            return {"Type": "S3", "S3Configuration": s3_cfg}

        if data_source_type == "web_crawler":
            web_url = kb_config.get("webCrawlerUrl", "https://docs.example.com")
            scope = kb_config.get("webCrawlerScope", "HOST_ONLY")
            return {
                "Type": "WEB",
                "WebConfiguration": {
                    "SourceConfiguration": {
                        "UrlConfiguration": {"SeedUrls": [{"Url": web_url}]},
                    },
                    "CrawlerConfiguration": {
                        "CrawlerLimits": {"RateLimit": 10},
                        "Scope": scope,
                    },
                },
            }

        if data_source_type == "confluence":
            return {
                "Type": "CONFLUENCE",
                "ConfluenceConfiguration": {
                    "SourceConfiguration": {
                        "HostUrl": kb_config.get("confluenceHostUrl", ""),
                        "HostType": "SAAS",
                        "AuthType": "OAUTH2_CLIENT_CREDENTIALS",
                        "CredentialsSecretArn": kb_config.get("confluenceCredentialsSecretArn", ""),
                    },
                    "CrawlerConfiguration": {
                        "FilterConfiguration": {
                            "Type": "PATTERN",
                            "PatternObjectFilter": {
                                "Filters": [{"ObjectType": "Page", "InclusionFilters": [".*"]}],
                            },
                        },
                    },
                },
            }

        if data_source_type == "salesforce":
            return {
                "Type": "SALESFORCE",
                "SalesforceConfiguration": {
                    "SourceConfiguration": {
                        "HostUrl": kb_config.get("salesforceHostUrl", ""),
                        "AuthType": "OAUTH2_CLIENT_CREDENTIALS",
                        "CredentialsSecretArn": kb_config.get("salesforceCredentialsSecretArn", ""),
                    },
                    "CrawlerConfiguration": {
                        "FilterConfiguration": {
                            "Type": "PATTERN",
                            "PatternObjectFilter": {
                                "Filters": [{"ObjectType": "Knowledge", "InclusionFilters": [".*"]}],
                            },
                        },
                    },
                },
            }

        if data_source_type == "sharepoint":
            site_urls_str = kb_config.get("sharePointSiteUrls", "")
            site_urls = [u.strip() for u in site_urls_str.split(",") if u.strip()]
            return {
                "Type": "SHAREPOINT",
                "SharePointConfiguration": {
                    "SourceConfiguration": {
                        "Domain": kb_config.get("sharePointDomain", ""),
                        "SiteUrls": site_urls,
                        "TenantId": kb_config.get("sharePointTenantId", ""),
                        "HostType": "ONLINE",
                        "AuthType": "OAUTH2_CLIENT_CREDENTIALS",
                        "CredentialsSecretArn": kb_config.get("sharePointCredentialsSecretArn", ""),
                    },
                    "CrawlerConfiguration": {
                        "FilterConfiguration": {
                            "Type": "PATTERN",
                            "PatternObjectFilter": {
                                "Filters": [{"ObjectType": "Page", "InclusionFilters": [".*"]}],
                            },
                        },
                    },
                },
            }

        return {"Type": data_source_type.upper()}

    @staticmethod
    def _build_chunking_config(strategy: str, kb_config: dict) -> dict:
        """Build CFN chunking configuration for a KB data source."""
        if strategy == "NONE":
            return {"ChunkingStrategy": "NONE"}
        if strategy == "SEMANTIC":
            return {"ChunkingStrategy": "SEMANTIC", "SemanticChunkingConfiguration": {"MaxTokens": 300, "BufferSize": 0, "BreakpointPercentileThreshold": 95}}
        if strategy == "HIERARCHICAL":
            return {
                "ChunkingStrategy": "HIERARCHICAL",
                "HierarchicalChunkingConfiguration": {
                    "LevelConfigurations": [
                        {"MaxTokens": 1500},
                        {"MaxTokens": 300},
                    ],
                    "OverlapTokens": 60,
                },
            }
        # Default: FIXED_SIZE
        max_tokens = kb_config.get("maxTokens", 300)
        overlap_pct = kb_config.get("overlapPercentage", 20)
        return {
            "ChunkingStrategy": "FIXED_SIZE",
            "FixedSizeChunkingConfiguration": {
                "MaxTokens": max_tokens,
                "OverlapPercentage": overlap_pct,
            },
        }

    # ------------------------------------------------------------------
    # Memory (native CFN)
    # ------------------------------------------------------------------

    def _add_memory_role(self, template: dict) -> None:
        template["Resources"]["MemoryExecutionRole"] = {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "RoleName": {"Fn::Sub": "AgentCoreMemory-${AWS::StackName}"},
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                },
                "Policies": [
                    {
                        "PolicyName": "MemoryPolicy",
                        "PolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Sid": "BedrockModelAccess",
                                    "Effect": "Allow",
                                    "Action": [
                                        "bedrock:InvokeModel",
                                        "bedrock:InvokeModelWithResponseStream",
                                    ],
                                    "Resource": "*",
                                },
                                {
                                    "Sid": "MemoryDataPlane",
                                    "Effect": "Allow",
                                    "Action": [
                                        "bedrock-agentcore:CreateEvent",
                                        "bedrock-agentcore:GetLastKTurns",
                                        "bedrock-agentcore:RetrieveMemories",
                                        "bedrock-agentcore:ListSessions",
                                        "bedrock-agentcore:ListActors",
                                        "bedrock-agentcore:ListEvents",
                                    ],
                                    "Resource": "*",
                                },
                                {
                                    "Sid": "MemoryControlPlane",
                                    "Effect": "Allow",
                                    "Action": [
                                        "bedrock-agentcore-control:GetMemory",
                                        "bedrock-agentcore-control:ListMemories",
                                    ],
                                    "Resource": "*",
                                },
                            ],
                        },
                    }
                ],
            },
        }

    def _add_memory(self, template: dict, deployment_name: str, memory_config: Optional[dict]) -> None:
        mc = memory_config or {}
        expiry = mc.get("eventExpiryDuration", 30)

        # Build strategies
        strategies = []
        raw_strategies = mc.get("strategies", [])
        if raw_strategies:
            strategy_key_map = {
                "semantic": "SemanticMemoryStrategy",
                "summary": "SummaryMemoryStrategy",
                "episodic": "EpisodicMemoryStrategy",
                "user_preferences": "UserPreferenceMemoryStrategy",
                "custom": "CustomMemoryStrategy",
            }
            for s in raw_strategies:
                stype = s.get("type", "semantic").lower()
                cfn_key = strategy_key_map.get(stype)
                if cfn_key:
                    strategies.append({cfn_key: {"Name": f"{stype}_strategy"}})
        else:
            # Default strategies — Name field is required by CFN
            strategies = [
                {"SemanticMemoryStrategy": {"Name": "semantic_strategy"}},
                {"SummaryMemoryStrategy": {"Name": "summary_strategy"}},
            ]

        template["Resources"]["AgentCoreMemory"] = {
            "Type": "AWS::BedrockAgentCore::Memory",
            "DependsOn": ["MemoryExecutionRole"],
            "Properties": {
                "Name": {"Fn::Sub": "${DeploymentName}_memory"},
                "EventExpiryDuration": expiry,
                "MemoryExecutionRoleArn": {"Fn::GetAtt": ["MemoryExecutionRole", "Arn"]},
                "MemoryStrategies": strategies,
                "Description": {"Fn::Sub": "Memory for ${DeploymentName}"},
                "Tags": {"ManagedBy": "CloudFormation"},
            },
        }

    # ------------------------------------------------------------------
    # Policy Engine + Policies (native CFN)
    # ------------------------------------------------------------------

    def _add_policy_engine(self, template: dict, deployment_name: str) -> None:
        template["Resources"]["PolicyEngine"] = {
            "Type": "AWS::BedrockAgentCore::PolicyEngine",
            "Properties": {
                "Name": {"Fn::Sub": "${DeploymentName}_policy_engine"},
                "Description": {"Fn::Sub": "Policy engine for ${DeploymentName}"},
                "Tags": [{"Key": "ManagedBy", "Value": "CloudFormation"}],
            },
        }

        # Attach policy engine to gateway — Gateway must depend on PolicyEngine
        if "AgentCoreGateway" in template["Resources"]:
            gw = template["Resources"]["AgentCoreGateway"]
            gw["Properties"]["PolicyEngineConfiguration"] = {
                "Arn": {"Fn::GetAtt": ["PolicyEngine", "PolicyEngineArn"]},
                "Mode": "ENFORCE",
            }
            # Add PolicyEngine to Gateway's DependsOn
            deps = gw.get("DependsOn", [])
            if isinstance(deps, str):
                deps = [deps]
            if "PolicyEngine" not in deps:
                deps.append("PolicyEngine")
            gw["DependsOn"] = deps

    def _add_policies(self, template: dict, policy_config: Optional[dict]) -> None:
        pc = policy_config or {}
        policies = pc.get("policies", pc.get("statements", []))

        if not policies:
            # Default permit-all policy — Cedar requires `when` clause and AgentCore::Gateway resource type
            policies = [
                {
                    "name": "default_permit",
                    "description": "Default permit policy",
                    "statement": 'permit(principal, action, resource is AgentCore::Gateway)\nwhen { true };',
                }
            ]

        for i, pol in enumerate(policies):
            name = re.sub(r"[^A-Za-z0-9_]", "_", pol.get("name", f"policy_{i}"))
            logical_id = f"Policy{i}" if i > 0 else "DefaultPolicy"

            # Build Cedar statement from raw string or conditions format
            statement = pol.get("statement", "")
            if not statement and pol.get("conditions"):
                # Convert conditions to Cedar when-clause
                effect = pol.get("effect", "permit")
                when_parts = []
                for cond in pol["conditions"]:
                    field = cond.get("field", "")
                    op = cond.get("operator", "==")
                    val = cond.get("value", "")
                    when_parts.append(f'{field} {op} "{val}"' if isinstance(val, str) else f'{field} {op} {val}')
                when_clause = " && ".join(when_parts) if when_parts else "true"
                statement = f'{effect}(principal, action, resource is AgentCore::Gateway) when {{ {when_clause} }};'
            if not statement:
                statement = 'permit(principal, action, resource is AgentCore::Gateway) when { context.authenticated == true };'

            # Use Custom::AgentCorePolicy (our cfn-provider Lambda) instead
            # of native AWS::BedrockAgentCore::Policy so we can wait for the
            # PolicyEngine to be truly ACTIVE before binding the policy. The
            # native CFN type's stabilization timeout fires too quickly in
            # fresh accounts, leaving the stack in ROLLBACK_COMPLETE. See
            # tasks/lessons.md Bug 72.
            template["Resources"][logical_id] = {
                "Type": "Custom::AgentCorePolicy",
                "DependsOn": ["PolicyEngine", "AgentCoreGateway", "CfnProviderLambda"],
                "Properties": {
                    "ServiceToken": {"Fn::GetAtt": ["CfnProviderLambda", "Arn"]},
                    "PolicyEngineId": {"Fn::GetAtt": ["PolicyEngine", "PolicyEngineId"]},
                    "Name": name,
                    "Statement": statement,
                    "Description": pol.get("description", ""),
                },
            }

    # ------------------------------------------------------------------
    # MCP Server Runtime (for mcp-server-gateway-target pattern)
    # ------------------------------------------------------------------

    def _add_mcp_server_cognito(self, template: dict, deployment_name: str) -> None:
        """Add a separate Cognito pool for MCP Server Runtime auth."""
        template["Resources"]["McpCognitoUserPool"] = {
            "Type": "AWS::Cognito::UserPool",
            "Properties": {
                "UserPoolName": {"Fn::Sub": "AgentCore-MCP-${DeploymentName}"},
                "AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True},
            },
        }

        template["Resources"]["McpCognitoResourceServer"] = {
            "Type": "AWS::Cognito::UserPoolResourceServer",
            "Properties": {
                "UserPoolId": {"Ref": "McpCognitoUserPool"},
                "Identifier": {"Fn::Sub": "agentcore-mcp-${DeploymentName}"},
                "Name": {"Fn::Sub": "agentcore-mcp-${DeploymentName}"},
                "Scopes": [{"ScopeName": "invoke", "ScopeDescription": "Invoke MCP server"}],
            },
        }

        template["Resources"]["McpCognitoDomain"] = {
            "Type": "AWS::Cognito::UserPoolDomain",
            "Properties": {
                "UserPoolId": {"Ref": "McpCognitoUserPool"},
                "Domain": {"Fn::Sub": "ac-mcp-${DeploymentName}-${AWS::AccountId}"},
            },
        }

        template["Resources"]["McpCognitoClient"] = {
            "Type": "AWS::Cognito::UserPoolClient",
            "DependsOn": "McpCognitoResourceServer",
            "Properties": {
                "UserPoolId": {"Ref": "McpCognitoUserPool"},
                "ClientName": {"Fn::Sub": "mcp-${DeploymentName}-client"},
                "GenerateSecret": True,
                "AllowedOAuthFlows": ["client_credentials"],
                "AllowedOAuthFlowsUserPoolClient": True,
                "AllowedOAuthScopes": [{"Fn::Sub": "agentcore-mcp-${DeploymentName}/invoke"}],
            },
        }

        # MCP Server code key parameter
        if "McpServerCodeKey" not in template["Parameters"]:
            template["Parameters"]["McpServerCodeKey"] = {
                "Type": "String",
                "Default": "cfn-assets/mcp-server-code.zip",
                "Description": "S3 key for the MCP server code zip",
            }

    def _add_mcp_server_runtime(self, template: dict, deployment_name: str, config: RuntimeConfig) -> None:
        """Add MCP Server Runtime + its IAM role."""
        # IAM role for MCP server
        template["Resources"]["McpServerRole"] = {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "RoleName": {"Fn::Sub": "AgentCoreMCP-${AWS::StackName}"},
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                },
                "Policies": [
                    {
                        "PolicyName": "McpServerPolicy",
                        "PolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Sid": "BedrockAccess",
                                    "Effect": "Allow",
                                    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                                    "Resource": "*",
                                },
                                {
                                    "Sid": "S3CodeAccess",
                                    "Effect": "Allow",
                                    "Action": ["s3:GetObject", "s3:ListBucket"],
                                    "Resource": [
                                        {"Fn::Sub": "arn:aws:s3:::${ArtifactsBucket}"},
                                        {"Fn::Sub": "arn:aws:s3:::${ArtifactsBucket}/*"},
                                    ],
                                },
                                {
                                    "Sid": "CloudWatchLogs",
                                    "Effect": "Allow",
                                    "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                                    "Resource": "*",
                                },
                            ],
                        },
                    }
                ],
            },
        }

        # Code package for MCP server
        template["Resources"]["McpServerCodePackage"] = {
            "Type": "Custom::AgentCodePackage",
            "Properties": {
                "ServiceToken": {"Fn::GetAtt": ["CfnProviderLambda", "Arn"]},
                "ArtifactsBucket": {"Ref": "ArtifactsBucket"},
                "AgentCodeKey": {"Ref": "McpServerCodeKey"},
                "DependencyBundleKey": {"Ref": "DependencyBundleKey"},
                "OutputKey": {"Fn::Sub": "deployments/${AWS::StackName}/mcp-server-code.zip"},
            },
        }

        # MCP Server Runtime
        template["Resources"]["McpServerRuntime"] = {
            "Type": "AWS::BedrockAgentCore::Runtime",
            "DependsOn": ["McpServerRole", "McpServerCodePackage", "McpCognitoClient"],
            "Properties": {
                "AgentRuntimeName": {"Fn::Sub": "${DeploymentName}_mcp_server"},
                "AgentRuntimeArtifact": {
                    "CodeConfiguration": {
                        "Code": {
                            "S3": {
                                "Bucket": {"Ref": "ArtifactsBucket"},
                                "Prefix": {"Fn::GetAtt": ["McpServerCodePackage", "CodeZipPrefix"]},
                            }
                        },
                        "EntryPoint": ["mcp_server.py"],
                        "Runtime": "PYTHON_3_13",
                    }
                },
                "RoleArn": {"Fn::GetAtt": ["McpServerRole", "Arn"]},
                "NetworkConfiguration": {"NetworkMode": "PUBLIC"},
                "ProtocolConfiguration": "MCP",
                "EnvironmentVariables": {
                    "AWS_REGION": {"Ref": "AWS::Region"},
                    "MODEL_ID": {"Ref": "ModelId"},
                },
                "AuthorizerConfiguration": {
                    "CustomJWTAuthorizer": {
                        "DiscoveryUrl": {
                            "Fn::Sub": "https://cognito-idp.${AWS::Region}.amazonaws.com/${McpCognitoUserPool}/.well-known/openid-configuration"
                        },
                        "AllowedClients": [{"Ref": "McpCognitoClient"}],
                    }
                },
                "Description": {"Fn::Sub": "MCP Server Runtime for ${DeploymentName}"},
                "Tags": {"ManagedBy": "CloudFormation"},
            },
        }

        # MCP Server Endpoint (wait for ready)
        template["Resources"]["McpServerEndpoint"] = {
            "Type": "AWS::BedrockAgentCore::RuntimeEndpoint",
            "Properties": {
                "AgentRuntimeId": {"Fn::GetAtt": ["McpServerRuntime", "AgentRuntimeId"]},
                "Name": {"Fn::Sub": "${DeploymentName}_mcp_endpoint"},
                "Description": "MCP Server endpoint",
            },
        }

    def _add_mcp_server_gateway_target(self, template: dict, deployment_name: str) -> None:
        """Add the MCP Server as a Gateway Target with OAuth credentials.

        Uses Custom::OAuth2CredentialProvider to create an OAuth2 credential
        provider via bedrock-agentcore-control API (no native CFN type exists).
        The credential provider authenticates the Gateway to the MCP Server
        Runtime using Cognito client_credentials flow.
        """
        # Custom resource: OAuth2 credential provider for MCP server auth
        # Also computes the URL-encoded MCP endpoint URL (CFN has no url-encode fn)
        template["Resources"]["McpOAuth2CredentialProvider"] = {
            "Type": "Custom::OAuth2CredentialProvider",
            "DependsOn": ["McpCognitoClient", "McpCognitoDomain", "McpServerEndpoint"],
            "Properties": {
                "ServiceToken": {"Fn::GetAtt": ["CfnProviderLambda", "Arn"]},
                "ProviderName": {"Fn::Sub": "mcp-cred-${DeploymentName}"},
                "DiscoveryUrl": {
                    "Fn::Sub": "https://cognito-idp.${AWS::Region}.amazonaws.com/${McpCognitoUserPool}/.well-known/openid-configuration"
                },
                "ClientId": {"Ref": "McpCognitoClient"},
                "ClientSecret": {"Fn::GetAtt": ["McpCognitoClient", "ClientSecret"]},
                "RuntimeArn": {"Fn::GetAtt": ["McpServerRuntime", "AgentRuntimeArn"]},
            },
        }

        template["Resources"]["McpServerGatewayTarget"] = {
            "Type": "AWS::BedrockAgentCore::GatewayTarget",
            "DependsOn": ["AgentCoreGateway", "McpOAuth2CredentialProvider"],
            "Properties": {
                "GatewayIdentifier": {"Fn::GetAtt": ["AgentCoreGateway", "GatewayIdentifier"]},
                "Name": "MCPServerRuntime",
                "TargetConfiguration": {
                    "Mcp": {
                        "McpServer": {
                            "Endpoint": {"Fn::GetAtt": ["McpOAuth2CredentialProvider", "McpEndpointUrl"]},
                        }
                    }
                },
                "CredentialProviderConfigurations": [
                    {
                        "CredentialProviderType": "OAUTH",
                        "CredentialProvider": {
                            "OauthCredentialProvider": {
                                "ProviderArn": {"Fn::GetAtt": ["McpOAuth2CredentialProvider", "CredentialProviderArn"]},
                                "Scopes": [{"Fn::Sub": "agentcore-mcp-${DeploymentName}/invoke"}],
                            }
                        },
                    }
                ],
                "Description": "MCP Server Runtime target",
            },
        }

    # ------------------------------------------------------------------
    # Evaluation (native CFN)
    # ------------------------------------------------------------------

    def _add_evaluation_role(self, template: dict) -> None:
        template["Resources"]["EvaluationRole"] = {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "RoleName": {"Fn::Sub": "AgentCoreEval-${AWS::StackName}"},
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                },
                "Policies": [
                    {
                        "PolicyName": "EvaluationPolicy",
                        "PolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Effect": "Allow",
                                    "Action": [
                                        "bedrock:InvokeModel",
                                        "bedrock:InvokeModelWithResponseStream",
                                        "bedrock-agentcore:*",
                                        "bedrock-agentcore-control:*",
                                        "logs:CreateLogGroup",
                                        "logs:CreateLogStream",
                                        "logs:PutLogEvents",
                                        "logs:DescribeLogGroups",
                                        "logs:DescribeLogStreams",
                                        "logs:FilterLogEvents",
                                        "logs:StartQuery",
                                        "logs:GetQueryResults",
                                        "logs:GetLogEvents",
                                    ],
                                    "Resource": "*",
                                }
                            ],
                        },
                    }
                ],
            },
        }

    def _add_evaluation(self, template: dict, deployment_name: str, evaluation_config: Optional[dict]) -> None:
        ec = evaluation_config or {}
        evaluators = ec.get("evaluators", [
            "Builtin.GoalSuccessRate",
            "Builtin.Correctness",
            "Builtin.ToolSelectionAccuracy",
        ])
        sampling_rate = ec.get("samplingPercentage", 100)

        template["Resources"]["OnlineEvaluation"] = {
            "Type": "AWS::BedrockAgentCore::OnlineEvaluationConfig",
            "DependsOn": ["EvaluationRole", "AgentCoreRuntime"],
            "Properties": {
                "OnlineEvaluationConfigName": {"Fn::Sub": "${DeploymentName}_evaluation"},
                "Rule": {"SamplingConfig": {"SamplingPercentage": sampling_rate}},
                "DataSourceConfig": {
                    "CloudWatchLogs": {
                        "LogGroupNames": [
                            {"Fn::Sub": "/aws/bedrock-agentcore/runtimes/${AgentCoreRuntime.AgentRuntimeId}"}
                        ],
                        "ServiceNames": [{"Fn::GetAtt": ["AgentCoreRuntime", "AgentRuntimeId"]}],
                    }
                },
                "Evaluators": [{"EvaluatorId": ev} for ev in evaluators],
                "EvaluationExecutionRoleArn": {"Fn::GetAtt": ["EvaluationRole", "Arn"]},
            },
        }

    # ------------------------------------------------------------------
    # Guardrails (standard Bedrock CFN)
    # ------------------------------------------------------------------

    def _add_guardrail(self, template: dict, deployment_name: str) -> None:
        """Add AWS::Bedrock::Guardrail + GuardrailVersion resources."""
        template["Resources"]["BedrockGuardrail"] = {
            "Type": "AWS::Bedrock::Guardrail",
            "Properties": {
                "Name": {"Fn::Sub": "${DeploymentName}-guardrail"},
                "Description": {"Fn::Sub": "Guardrail for ${DeploymentName}"},
                "BlockedInputMessaging": "Your request was blocked by a safety guardrail.",
                "BlockedOutputsMessaging": "The response was blocked by a safety guardrail.",
                "ContentPolicyConfig": {
                    "FiltersConfig": [
                        {"Type": "HATE", "InputStrength": "HIGH", "OutputStrength": "HIGH"},
                        {"Type": "INSULTS", "InputStrength": "HIGH", "OutputStrength": "HIGH"},
                        {"Type": "SEXUAL", "InputStrength": "HIGH", "OutputStrength": "HIGH"},
                        {"Type": "VIOLENCE", "InputStrength": "HIGH", "OutputStrength": "HIGH"},
                        {"Type": "MISCONDUCT", "InputStrength": "HIGH", "OutputStrength": "HIGH"},
                        {"Type": "PROMPT_ATTACK", "InputStrength": "HIGH", "OutputStrength": "NONE"},
                    ],
                },
                "Tags": [
                    {"Key": "ManagedBy", "Value": "CloudFormation"},
                    {"Key": "Stack", "Value": {"Ref": "AWS::StackName"}},
                ],
            },
        }
        template["Resources"]["BedrockGuardrailVersion"] = {
            "Type": "AWS::Bedrock::GuardrailVersion",
            "DependsOn": ["BedrockGuardrail"],
            "Properties": {
                "GuardrailIdentifier": {"Fn::GetAtt": ["BedrockGuardrail", "GuardrailId"]},
                "Description": "Initial version created by AgentCore Flows",
            },
        }

    # ------------------------------------------------------------------
    # Runtime (native CFN)
    # ------------------------------------------------------------------

    def _add_runtime(
        self,
        template: dict,
        config: RuntimeConfig,
        deployment_name: str,
        has_gateway: bool,
        has_memory: bool,
        has_mcp_server: bool,
        has_guardrails: bool = False,
    ) -> None:
        depends = ["RuntimeExecutionRole", "AgentCodePackage"]
        env_vars: dict = {
            "AWS_REGION": {"Ref": "AWS::Region"},
            "MODEL_ID": {"Ref": "ModelId"},
        }

        if has_gateway:
            depends.extend(["AgentCoreGateway"])
            env_vars["GATEWAY_URL"] = {"Fn::GetAtt": ["AgentCoreGateway", "GatewayUrl"]}
            env_vars["COGNITO_CLIENT_ID"] = {"Ref": "CognitoUserPoolClient"}
            env_vars["COGNITO_CLIENT_SECRET"] = {"Fn::GetAtt": ["CognitoUserPoolClient", "ClientSecret"]}
            env_vars["COGNITO_TOKEN_ENDPOINT"] = {
                "Fn::Sub": "https://ac-${DeploymentName}-${AWS::AccountId}.auth.${AWS::Region}.amazoncognito.com/oauth2/token"
            }
            env_vars["COGNITO_SCOPE"] = {"Fn::Sub": "agentcore-${DeploymentName}/invoke"}

        if has_memory:
            depends.append("AgentCoreMemory")
            env_vars["MEMORY_ID"] = {"Fn::GetAtt": ["AgentCoreMemory", "MemoryId"]}

        if has_mcp_server:
            depends.append("McpServerGatewayTarget")

        if has_guardrails:
            depends.append("BedrockGuardrailVersion")
            env_vars["GUARDRAIL_ID"] = {"Fn::GetAtt": ["BedrockGuardrail", "GuardrailId"]}
            env_vars["GUARDRAIL_VERSION"] = {"Fn::GetAtt": ["BedrockGuardrailVersion", "Version"]}

        # Inject OTLP observability env vars via single source of truth.
        # CFN exports favor a stable runtime name as deployment ID stand-in.
        # NOTE: platform_defaults intentionally NOT passed here — CFN exports
        # are designed to deploy in other AWS accounts that won't have this
        # platform's SSM params. Per-canvas observability config is the only
        # source for exports.
        otel_env = build_otel_env_vars(
            config.observability.model_dump() if getattr(config, "observability", None) else None,
            runtime_name=deployment_name,
            deployment_id=deployment_name,
            enable_otel_legacy=bool(getattr(config, "enable_otel", False)),
        )
        env_vars.update(otel_env)

        template["Resources"]["AgentCoreRuntime"] = {
            "Type": "AWS::BedrockAgentCore::Runtime",
            "DependsOn": depends,
            "Properties": {
                "AgentRuntimeName": {"Fn::Sub": "${DeploymentName}_runtime"},
                "AgentRuntimeArtifact": {
                    "CodeConfiguration": {
                        "Code": {
                            "S3": {
                                "Bucket": {"Ref": "ArtifactsBucket"},
                                "Prefix": {"Fn::GetAtt": ["AgentCodePackage", "CodeZipPrefix"]},
                            }
                        },
                        "EntryPoint": [config.entrypoint or "agent.py"],
                        "Runtime": config.python_runtime or "PYTHON_3_13",
                    }
                },
                "RoleArn": {"Fn::GetAtt": ["RuntimeExecutionRole", "Arn"]},
                "NetworkConfiguration": {"NetworkMode": "PUBLIC"},
                "ProtocolConfiguration": config.protocol or "HTTP",
                "EnvironmentVariables": env_vars,
                "Description": {"Fn::Sub": "Runtime for ${DeploymentName}"},
                "Tags": {"ManagedBy": "CloudFormation", "Stack": {"Ref": "AWS::StackName"}},
            },
        }

    def _add_runtime_endpoint(self, template: dict, deployment_name: str) -> None:
        template["Resources"]["RuntimeEndpoint"] = {
            "Type": "AWS::BedrockAgentCore::RuntimeEndpoint",
            "DependsOn": ["AgentCoreRuntime"],
            "Properties": {
                "AgentRuntimeId": {"Fn::GetAtt": ["AgentCoreRuntime", "AgentRuntimeId"]},
                "Name": {"Fn::Sub": "${DeploymentName}_endpoint"},
                "Description": "Default endpoint",
            },
        }

    # ------------------------------------------------------------------
    # Outputs
    # ------------------------------------------------------------------

    def _add_outputs(self, template: dict, has_gateway: bool, has_memory: bool, has_mcp_server: bool) -> None:
        template["Outputs"]["RuntimeId"] = {
            "Description": "AgentCore Runtime ID",
            "Value": {"Fn::GetAtt": ["AgentCoreRuntime", "AgentRuntimeId"]},
            "Export": {"Name": {"Fn::Sub": "${AWS::StackName}-RuntimeId"}},
        }
        template["Outputs"]["RuntimeArn"] = {
            "Description": "AgentCore Runtime ARN",
            "Value": {"Fn::GetAtt": ["AgentCoreRuntime", "AgentRuntimeArn"]},
        }
        template["Outputs"]["EndpointArn"] = {
            "Description": "Runtime Endpoint ARN",
            "Value": {"Fn::GetAtt": ["RuntimeEndpoint", "AgentRuntimeEndpointArn"]},
        }

        if has_gateway:
            template["Outputs"]["GatewayUrl"] = {
                "Description": "Gateway URL",
                "Value": {"Fn::GetAtt": ["AgentCoreGateway", "GatewayUrl"]},
                "Export": {"Name": {"Fn::Sub": "${AWS::StackName}-GatewayUrl"}},
            }
            template["Outputs"]["GatewayId"] = {
                "Description": "Gateway ID",
                "Value": {"Fn::GetAtt": ["AgentCoreGateway", "GatewayIdentifier"]},
            }

        if has_memory:
            template["Outputs"]["MemoryId"] = {
                "Description": "Memory ID",
                "Value": {"Fn::GetAtt": ["AgentCoreMemory", "MemoryId"]},
                "Export": {"Name": {"Fn::Sub": "${AWS::StackName}-MemoryId"}},
            }

        if has_mcp_server:
            template["Outputs"]["McpServerRuntimeId"] = {
                "Description": "MCP Server Runtime ID",
                "Value": {"Fn::GetAtt": ["McpServerRuntime", "AgentRuntimeId"]},
            }

    # ------------------------------------------------------------------
    # Asset packaging
    # ------------------------------------------------------------------

    def _package_cfn_provider(self) -> bytes:
        """Package cfn_provider/ as a zip for Lambda deployment.

        Bundles a recent boto3 + botocore from the Lambda's lib directory
        so Custom::AgentCorePolicy can use APIs (list_policy_engines,
        create_policy) that aren't yet in the runtime's bundled SDK.
        See tasks/lessons.md Bug 92.
        """
        buf = io.BytesIO()
        provider_dir = os.path.join(os.path.dirname(__file__), "cfn_provider")
        # backend/lib already has boto3 + botocore from install-lambda-deps.sh
        lib_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "lib"))
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for filename in ("handler.py", "cfn_response.py", "__init__.py"):
                filepath = os.path.join(provider_dir, filename)
                if os.path.exists(filepath):
                    with open(filepath) as f:
                        zf.writestr(filename, f.read())
            # Bundle boto3 + botocore + dependencies if available locally.
            if os.path.isdir(lib_dir):
                for pkg in ("boto3", "botocore", "dateutil", "jmespath", "s3transfer", "urllib3"):
                    pkg_dir = os.path.join(lib_dir, pkg)
                    if not os.path.isdir(pkg_dir):
                        continue
                    for root, _dirs, files in os.walk(pkg_dir):
                        for fname in files:
                            if fname.endswith((".pyc",)) or "__pycache__" in root:
                                continue
                            full = os.path.join(root, fname)
                            arcname = os.path.relpath(full, lib_dir)
                            zf.write(full, arcname)
        buf.seek(0)
        return buf.read()

    def _package_tool_lambdas(self, template_id: Optional[str], gateway_tools: list) -> bytes:
        """Package tool Lambda code as a zip.

        Only includes the Lambda files that are actually needed based on the
        connected tools.  Reads source from the project's tool-lambdas
        directory; falls back to generating minimal stubs if unavailable.
        """
        # Determine which Lambda files are needed
        # gateway_tools items may be dicts with toolId or plain strings
        _tool_ids = [
            (t["toolId"] if isinstance(t, dict) else t) for t in gateway_tools
        ]
        needs_dynamic = any(t in _DYNAMIC_TOOL_IDS for t in _tool_ids)
        needs_customer = any(t in _CUSTOMER_SUPPORT_TOOL_IDS for t in _tool_ids)
        # Template overrides
        if template_id in ("strands-gateway-agent", "customer-support-assistant"):
            needs_dynamic = True
        if template_id == "customer-support-blueprint":
            needs_customer = True

        needed_files = []
        if needs_dynamic:
            needed_files.append("dynamic_tools.py")
        if needs_customer:
            needed_files.append("customer_support_tools.py")
        # Fallback: include both if nothing matched (safety net)
        if not needed_files:
            needed_files = ["dynamic_tools.py", "customer_support_tools.py"]

        buf = io.BytesIO()
        tool_lambdas_dir = os.path.join(os.path.dirname(__file__), "..", "..", "tool_lambdas")

        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for filename in needed_files:
                filepath = os.path.join(tool_lambdas_dir, filename)
                if os.path.exists(filepath):
                    with open(filepath) as f:
                        zf.writestr(filename, f.read())
                else:
                    zf.writestr(filename, self._generate_tool_lambda_stub(filename))

        buf.seek(0)
        return buf.read()

    def _generate_tool_lambda_stub(self, filename: str) -> str:
        """Generate a minimal tool Lambda stub."""
        if "customer_support" in filename:
            return '''"""Customer Support Tools Lambda for AgentCore Gateway.

Provides: get_order, get_customer, list_orders, process_refund
"""
import json

MOCK_ORDERS = {
    "ORD-12345": {"order_id": "ORD-12345", "customer_id": "CUST-001", "status": "delivered", "total": 150.00,
                  "items": [{"name": "Widget A", "qty": 2, "price": 75.00}]},
}
MOCK_CUSTOMERS = {
    "CUST-001": {"customer_id": "CUST-001", "name": "Alice Example", "email": "alice@example.com", "orders": ["ORD-12345"]},
}


def handler(event, context):
    """MCP tool handler for Gateway."""
    tool_name = context.client_context.custom.get("bedrockAgentCoreToolName", "") if hasattr(context, "client_context") and context.client_context else event.get("name", "")
    args = event

    if "get_order" in tool_name:
        order = MOCK_ORDERS.get(args.get("order_id", ""), {"error": "Order not found"})
        return {"content": [{"type": "text", "text": json.dumps(order)}]}
    elif "get_customer" in tool_name:
        customer = MOCK_CUSTOMERS.get(args.get("customer_id", ""), {"error": "Customer not found"})
        return {"content": [{"type": "text", "text": json.dumps(customer)}]}
    elif "list_orders" in tool_name:
        cid = args.get("customer_id", "")
        orders = [o for o in MOCK_ORDERS.values() if o["customer_id"] == cid]
        return {"content": [{"type": "text", "text": json.dumps(orders)}]}
    elif "process_refund" in tool_name:
        return {"content": [{"type": "text", "text": json.dumps({"status": "refund_processed", "order_id": args.get("order_id"), "amount": args.get("amount")})}]}
    return {"content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}]}
'''
        else:
            return '''"""Dynamic Tools Lambda for AgentCore Gateway.

Provides: duckduckgo_search, wikipedia_search, get_weather, fetch_webpage
"""
import json
import urllib.request
import urllib.parse

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"


def _http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode()


def handler(event, context):
    """MCP tool handler for Gateway."""
    tool_name = context.client_context.custom.get("bedrockAgentCoreToolName", "") if hasattr(context, "client_context") and context.client_context else event.get("name", "")
    args = event

    if "duckduckgo_search" in tool_name:
        query = urllib.parse.quote_plus(args.get("query", ""))
        data = _http_get(f"https://api.duckduckgo.com/?q={query}&format=json&no_html=1")
        return {"content": [{"type": "text", "text": data[:4000]}]}
    elif "wikipedia_search" in tool_name:
        query = urllib.parse.quote_plus(args.get("query", ""))
        data = _http_get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{query}")
        return {"content": [{"type": "text", "text": data[:4000]}]}
    elif "get_weather" in tool_name:
        loc = urllib.parse.quote_plus(args.get("location", ""))
        geo = _http_get(f"https://geocoding-api.open-meteo.com/v1/search?name={loc}&count=1")
        geo_data = json.loads(geo)
        if not geo_data.get("results"):
            return {"content": [{"type": "text", "text": "Location not found"}]}
        lat = geo_data["results"][0]["latitude"]
        lon = geo_data["results"][0]["longitude"]
        weather = _http_get(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true")
        return {"content": [{"type": "text", "text": weather}]}
    elif "fetch_webpage" in tool_name:
        url = args.get("url", "")
        data = _http_get(url)
        return {"content": [{"type": "text", "text": data[:8000]}]}
    return {"content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}]}
'''

    # ------------------------------------------------------------------
    # deploy.sh / teardown.sh / README.md generation
    # ------------------------------------------------------------------

    def _generate_deploy_script(self, deployment_name: str, config: RuntimeConfig, has_mcp_server: bool, bundle_key: str = STRANDS_BUNDLE_KEY) -> str:
        mcp_upload = ""
        if has_mcp_server:
            mcp_upload = """
# Upload MCP server code
echo "Packaging MCP server code..."
cd agent-code && zip -r ../mcp-server-code.zip mcp_server.py && cd ..
aws s3 cp mcp-server-code.zip "s3://${BUCKET}/cfn-assets/${STACK_NAME}/mcp-server-code.zip" --region "$REGION"
"""

        return f'''#!/usr/bin/env bash
# deploy.sh — Deploy AgentCore {deployment_name} CloudFormation stack
# Generated by AgentCore Flows
set -euo pipefail

STACK_NAME="${{1:-{deployment_name}}}"
REGION="${{2:-us-east-1}}"
BUCKET="${{3:-}}"

if [[ -z "$BUCKET" ]]; then
    echo "Usage: ./deploy.sh [stack-name] [region] <artifacts-bucket>"
    echo "  artifacts-bucket is REQUIRED — S3 bucket for code artifacts"
    exit 1
fi

# Derive a clean deployment name (alphanumeric, max 20 chars) for resource naming
DEPLOY_NAME=$(echo "$STACK_NAME" | tr -cd 'a-zA-Z0-9' | cut -c1-20)
DEPLOY_NAME=${{DEPLOY_NAME:-{deployment_name}}}

echo "=== AgentCore Stack Deployment ==="
echo "Stack:  $STACK_NAME"
echo "Deploy: $DEPLOY_NAME"
echo "Region: $REGION"
echo "Bucket: $BUCKET"
echo ""

# 1. Check AWS credentials
echo "Checking AWS credentials..."
aws sts get-caller-identity --output table || {{ echo "ERROR: AWS credentials not configured"; exit 1; }}

# 2. Ensure S3 bucket exists
if ! aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" 2>/dev/null; then
    echo "Creating S3 bucket: $BUCKET"
    aws s3 mb "s3://$BUCKET" --region "$REGION"
fi

# 3. Check/upload dependency bundle
BUNDLE_KEY="{bundle_key}"
if ! aws s3api head-object --bucket "$BUCKET" --key "$BUNDLE_KEY" --region "$REGION" >/dev/null 2>&1; then
    echo ""
    echo "WARNING: Dependency bundle not found at s3://$BUCKET/$BUNDLE_KEY"
    echo "You need to upload the pre-built dependency bundle."
    echo "See README.md for instructions."
    echo ""
    exit 1
fi

# 4. Package and upload assets (stack-specific keys to avoid collisions)
echo "Packaging CFN provider Lambda..."
aws s3 cp cfn-provider.zip "s3://$BUCKET/cfn-assets/${{STACK_NAME}}/cfn-provider.zip" --region "$REGION"

echo "Packaging agent code..."
cd agent-code && zip -r ../agent-code.zip . -x '*/__pycache__/*' '*.pyc' && cd ..
aws s3 cp agent-code.zip "s3://$BUCKET/cfn-assets/${{STACK_NAME}}/agent-code.zip" --region "$REGION"
rm -f agent-code.zip

if [[ -f tool-lambdas.zip ]]; then
    echo "Uploading tool Lambda code..."
    aws s3 cp tool-lambdas.zip "s3://$BUCKET/cfn-assets/${{STACK_NAME}}/tool-lambdas.zip" --region "$REGION"
fi

if [[ -f custom-tools.zip ]]; then
    echo "Uploading custom tool Lambda code..."
    aws s3 cp custom-tools.zip "s3://$BUCKET/cfn-assets/${{STACK_NAME}}/custom-tools.zip" --region "$REGION"
fi
{mcp_upload}
# 5. Build parameter overrides
PARAM_OVERRIDES=(
    "DeploymentName=$DEPLOY_NAME"
    "ArtifactsBucket=$BUCKET"
    "AgentCodeKey=cfn-assets/${{STACK_NAME}}/agent-code.zip"
    "CfnProviderCodeKey=cfn-assets/${{STACK_NAME}}/cfn-provider.zip"
    "DependencyBundleKey=$BUNDLE_KEY"
)

if [[ -f tool-lambdas.zip ]]; then
    PARAM_OVERRIDES+=("ToolLambdaCodeKey=cfn-assets/${{STACK_NAME}}/tool-lambdas.zip")
fi

if [[ -f custom-tools.zip ]]; then
    PARAM_OVERRIDES+=("CustomToolCodeKey=cfn-assets/${{STACK_NAME}}/custom-tools.zip")
fi

if [[ -f mcp-server-code.zip ]]; then
    PARAM_OVERRIDES+=("McpServerCodeKey=cfn-assets/${{STACK_NAME}}/mcp-server-code.zip")
fi

# Deploy CloudFormation stack
echo ""
echo "Deploying CloudFormation stack..."
aws cloudformation deploy \\
    --stack-name "$STACK_NAME" \\
    --template-file template.yaml \\
    --capabilities CAPABILITY_NAMED_IAM \\
    --parameter-overrides "${{PARAM_OVERRIDES[@]}}" \\
    --region "$REGION" \\
    --no-fail-on-empty-changeset

# 6. Show outputs
echo ""
echo "=== Stack Outputs ==="
aws cloudformation describe-stacks \\
    --stack-name "$STACK_NAME" \\
    --region "$REGION" \\
    --query "Stacks[0].Outputs" \\
    --output table

echo ""
echo "Deployment complete!"
'''

    def _generate_teardown_script(self) -> str:
        return '''#!/usr/bin/env bash
# teardown.sh — Delete AgentCore CloudFormation stack
set -euo pipefail

STACK_NAME="${1:-}"
REGION="${2:-us-east-1}"

if [[ -z "$STACK_NAME" ]]; then
    echo "Usage: ./teardown.sh <stack-name> [region]"
    exit 1
fi

echo "Deleting stack: $STACK_NAME"
aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
echo "Waiting for deletion..."
aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION"
echo "Stack deleted."
'''

    def _generate_readme(
        self,
        deployment_name: str,
        template_id: Optional[str],
        config: RuntimeConfig,
        has_gateway: bool,
        has_memory: bool,
        has_policy: bool,
        has_mcp_server: bool,
    ) -> str:
        components = ["Runtime", "RuntimeEndpoint"]
        if has_gateway:
            components.extend(["Cognito OAuth", "Gateway", "GatewayTargets"])
        if has_memory:
            components.append("Memory")
        if has_policy:
            components.extend(["PolicyEngine", "Policies"])
        if has_mcp_server:
            components.extend(["MCP Server Runtime", "MCP Server Cognito"])

        return f'''# AgentCore Stack: {deployment_name}

Template: {template_id or "custom"}
Components: {", ".join(components)}

## Prerequisites

1. AWS CLI v2 configured with credentials
2. An S3 bucket for artifacts
3. Pre-built dependency bundle uploaded to S3

### Dependency Bundle

The AgentCore Runtime requires pre-bundled Python dependencies in code.zip.
Upload the appropriate bundle to your S3 bucket:

- **strands-mcp.zip** (43MB) — for agents using Strands framework + MCP
- **base.zip** (18MB) — for lightweight boto3-only agents

Upload: `aws s3 cp strands-mcp.zip s3://YOUR-BUCKET/{STRANDS_BUNDLE_KEY}`

## Quick Start

```bash
chmod +x deploy.sh teardown.sh
./deploy.sh my-agent us-east-1 my-artifacts-bucket
```

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| DeploymentName | {deployment_name} | Base name for all resources |
| ModelId | {config.model.get("id", "us.anthropic.claude-sonnet-5")} | Bedrock model ID |
| ArtifactsBucket | (required) | S3 bucket for code and bundles |
| DependencyBundleKey | {STRANDS_BUNDLE_KEY} | S3 key for dependency bundle |

## Customization

### Agent Code
Edit `agent-code/agent.py` to customize agent behavior. The code reads all
configuration from environment variables — no hardcoded credentials.

### Tool Lambdas
{"Unzip `tool-lambdas.zip`, edit the Lambda handlers, rezip, and redeploy." if has_gateway else "N/A — no gateway tools."}

## Teardown

```bash
./teardown.sh my-agent us-east-1
```

## Architecture

This stack uses native `AWS::BedrockAgentCore::*` CloudFormation resource types.
No Custom Resource Lambdas are needed for AgentCore resource lifecycle — only one
Custom Resource handles code packaging (merging agent code + dependency bundle).

Generated by AgentCore Flows.
'''
