# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AtlassianConnectorStack — Jira+Confluence as a governed target on the gateway via
the per-user OAuth (3LO) path, now **fully declarative in CDK** (no post-deploy script).

Creates, in one ``cdk deploy``:
  * The Jira+Confluence MCP server on an AgentCore Runtime (container via Finch).
  * The native Atlassian **OAuth2 credential provider** using the AgentCore Identity ↔
    Secrets Manager integration (``client_secret_config``): AgentCore reads the client
    secret DIRECTLY from Secrets Manager, so the value never enters the CloudFormation
    template and secret rotation is picked up automatically.
  * The gateway **mcpServer target** (schema-upfront + OAUTH/3LO) via a CFN-tracked
    **custom resource** — needed only because the target endpoint is the Runtime's
    invocation URL, which requires URL-encoding the ARN (no CFN urlencode intrinsic).
  * The **12 Cedar policies** (read=all, write=role=atlassian-writer) as ``CfnPolicy``.

Because the target + policies are CFN-managed, ``cdk destroy`` removes them cleanly —
no orphans (the old ``setup_connector.py`` left the target + policies untracked).

Prerequisites (one-time, external — a client secret is not an AWS-native artifact):
  * Create the client-secret in Secrets Manager BEFORE deploying, as JSON with a
    ``clientSecret`` key (the native integration reads that key):
      aws secretsmanager create-secret --name enterprise-mcp-connector/atlassian-client-secret \\
        --secret-string '{"clientSecret":"<your-atlassian-oauth-client-secret>"}'
  * After deploy, register the provider's ``CallbackUrl`` output in your Atlassian
    3LO app's redirect URIs (needed before the first per-user consent).

Deploy order: the core gateway stack first (this reads its gateway id/ARN + policy
engine id from SSM).
"""
import json
import os
import re
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    CustomResource,
    Duration,
    Stack,
    aws_bedrockagentcore as agentcore,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_ssm as ssm,
    custom_resources as cr,
)
from constructs import Construct

from connectors._bundling import bundled_asset

_HERE = Path(__file__).resolve().parent
_MCP_SERVER_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "connectors",
                               "atlassian", "mcp_server")
_CEDAR_FILE = _HERE.parents[1] / "policies" / "atlassian-read-all-write-admin.cedar"

PROVIDER_NAME = "mcp-connector-atlassian"
CLIENT_SECRET_NAME = "enterprise-mcp-connector/atlassian-client-secret"  # nosec B105 - Secrets Manager secret NAME, not a credential; the value is created out-of-band and read by AgentCore
RETURN_URL = "http://localhost:8080/callback"  # dev shortcut (authorize.py); SPA overrides via _meta
SCOPES = [
    "read:jira-work", "write:jira-work", "read:jira-user",
    "read:confluence-content.all", "write:confluence-content",
    "read:confluence-space.summary", "search:confluence", "offline_access",
]


# Static tool schema (schema-upfront). Mirrors connectors/atlassian/mcp_server/server.py.
def _s(d=""):
    return {"type": "string", **({"description": d} if d else {})}


def _i(d=""):
    return {"type": "integer", **({"description": d} if d else {})}


def _t(name, desc, props, required):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": props, "required": required}}


MCP_TOOL_SCHEMA = [
    _t("getJiraIssue", "Get a Jira issue by key.", {"issueKey": _s("e.g. PROJ-123")}, ["issueKey"]),
    _t("searchJiraIssuesUsingJql", "Search Jira issues with a JQL query.",
       {"jql": _s(), "maxResults": _i()}, ["jql"]),
    _t("getVisibleJiraProjects", "List Jira projects the user can access.", {"maxResults": _i()}, []),
    _t("createJiraIssue", "Create a Jira issue in a project.",
       {"projectKey": _s(), "summary": _s(), "issueType": _s(), "description": _s()},
       ["projectKey", "summary"]),
    _t("editJiraIssue", "Update a Jira issue's summary and/or description.",
       {"issueKey": _s(), "summary": _s(), "description": _s()}, ["issueKey"]),
    _t("addCommentToJiraIssue", "Add a comment to a Jira issue.",
       {"issueKey": _s(), "comment": _s()}, ["issueKey", "comment"]),
    _t("transitionJiraIssue", "Move a Jira issue to a new workflow state.",
       {"issueKey": _s(), "transitionId": _s()}, ["issueKey", "transitionId"]),
    _t("getConfluencePage", "Get a Confluence page by id.", {"pageId": _s()}, ["pageId"]),
    _t("searchConfluenceUsingCql", "Search Confluence content with CQL.",
       {"cql": _s(), "limit": _i()}, ["cql"]),
    _t("getConfluenceSpaces", "List Confluence spaces.", {"limit": _i()}, []),
    _t("createConfluencePage", "Create a Confluence page (storage-format body).",
       {"spaceId": _s(), "title": _s(), "body": _s()}, ["spaceId", "title", "body"]),
    _t("updateConfluencePage", "Update a Confluence page (version = next number).",
       {"pageId": _s(), "title": _s(), "body": _s(), "version": _i()},
       ["pageId", "title", "body", "version"]),
]


class AtlassianConnectorStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *,
                 client_id: str, cloud_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Core gateway identifiers (deploy the gateway stack first).
        gateway_id = ssm.StringParameter.value_for_string_parameter(
            self, "/enterprise-mcp-gateway/gateway/id")
        gateway_arn = ssm.StringParameter.value_for_string_parameter(
            self, "/enterprise-mcp-gateway/gateway/arn")
        policy_engine_id = ssm.StringParameter.value_for_string_parameter(
            self, "/enterprise-mcp-gateway/policy-engine/id")

        # Non-secret connector config → SSM (discovery pattern).
        ssm.StringParameter(self, "ClientIdParam",
                            parameter_name="/enterprise-mcp-connector/atlassian/client-id",
                            string_value=client_id)
        ssm.StringParameter(self, "CloudIdParam",
                            parameter_name="/enterprise-mcp-connector/atlassian/cloud-id",
                            string_value=cloud_id)

        # Jira+Confluence MCP server on an AgentCore Runtime.
        runtime = agentcore.Runtime(
            self, "AtlassianMcpRuntime",
            runtime_name="atlassian_mcp_server",
            agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_asset(
                _MCP_SERVER_DIR, platform=ecr_assets.Platform.LINUX_ARM64),
            protocol_configuration=agentcore.ProtocolType.MCP,
            # PUBLIC NETWORK IS REQUIRED, NOT A DEFAULT: this container's whole job is to
            # call https://api.atlassian.com (see mcp_server/server.py), so it needs
            # internet egress. Atlassian is third-party SaaS, so there is no PrivateLink
            # target; a VPC-private runtime would need a NAT gateway to reach the same
            # public endpoint — added cost and complexity for no reduction in exposure.
            #
            # Inbound is not open: the runtime accepts only requests bearing a valid
            # Atlassian OIDC token (JWT authorizer below), restricted to the listed scopes,
            # and only the Authorization header is forwarded into the container. In this
            # sample it is reached solely through the AgentCore Gateway, which applies the
            # Cognito JWT check, the interceptors and Cedar ENFORCE first.
            #
            # For a VPC-only deployment (an internal MCP server with no third-party egress),
            # swap this for a VPC configuration and front it with PrivateLink.
            network_configuration=agentcore.RuntimeNetworkConfiguration.using_public_network(),
            # JWT authorizer VALIDATES the per-user Atlassian token; the header allowlist
            # FORWARDS it into the container so server.py can call Jira/Confluence.
            authorizer_configuration=agentcore.RuntimeAuthorizerConfiguration.using_jwt(
                discovery_url="https://auth.atlassian.com/.well-known/openid-configuration",
                allowed_scopes=["read:jira-work", "write:jira-work", "read:jira-user",
                                "offline_access"]),
            request_header_configuration=agentcore.RequestHeaderConfiguration(
                allowlisted_headers=["Authorization"]),
            environment_variables={"ATLASSIAN_CLOUD_ID": cloud_id},
        )

        # Native Atlassian OAuth2 credential provider — AgentCore Identity reads the
        # client secret DIRECTLY from Secrets Manager (client_secret_config), so the
        # value never enters the template and rotation is picked up automatically.
        # Requires aws-cdk-lib >= 2.261 (client_secret_config prop). The secret must be
        # JSON with a "clientSecret" key (SecretReference requires SecretId + JsonKey).
        provider = agentcore.CfnOAuth2CredentialProvider(
            self, "AtlassianProvider",
            name=PROVIDER_NAME,
            credential_provider_vendor="AtlassianOauth2",
            oauth2_provider_config_input=agentcore.CfnOAuth2CredentialProvider.Oauth2ProviderConfigInputProperty(
                atlassian_oauth2_provider_config=agentcore.CfnOAuth2CredentialProvider.AtlassianOauth2ProviderConfigInputProperty(
                    client_id=client_id,
                    # EXTERNAL = read the secret from OUR Secrets Manager (vs MANAGED = AgentCore's own vault).
                    client_secret_source="EXTERNAL",  # nosec B106 - API enum value (EXTERNAL|MANAGED), not a secret
                    client_secret_config=agentcore.CfnOAuth2CredentialProvider.SecretReferenceProperty(
                        secret_id=CLIENT_SECRET_NAME,
                        json_key="clientSecret",
                    ),
                ),
            ),
        )

        # With EXTERNAL secret source, the gateway EXEC ROLE reads the client secret from
        # OUR Secrets Manager when fetching the outbound OAuth token at runtime — grant it.
        # (Attached to the imported gateway role from the connector stack, so it's removed
        # on `cdk destroy` of this stack; wildcard suffix matches the secret's random ARN tail.)
        gateway_exec_arn = ssm.StringParameter.value_for_string_parameter(
            self, "/enterprise-mcp-gateway/roles/gateway-exec-arn")
        iam.Role.from_role_arn(
            self, "GatewayExecRole", gateway_exec_arn, mutable=True,
        ).add_to_principal_policy(iam.PolicyStatement(
            sid="ReadAtlassianClientSecret",
            effect=iam.Effect.ALLOW,
            actions=["secretsmanager:GetSecretValue"],
            resources=[f"arn:{self.partition}:secretsmanager:{self.region}:{self.account}"
                       f":secret:{CLIENT_SECRET_NAME}-*"],
        ))

        # Gateway mcpServer target via a CFN-tracked custom resource (builds the runtime
        # invocation URL, which CFN can't url-encode). Schema-upfront + OAUTH/3LO.
        register_fn = self._target_fn(gateway_arn)
        target_provider = cr.Provider(self, "AtlassianTargetProvider", on_event_handler=register_fn)
        target = CustomResource(
            self, "AtlassianTarget",
            service_token=target_provider.service_token,
            properties={
                "GatewayId": gateway_id,
                "RuntimeArn": runtime.agent_runtime_arn,
                "ProviderArn": provider.attr_credential_provider_arn,
                "Region": self.region,
                "Scopes": SCOPES,
                "DefaultReturnUrl": RETURN_URL,
                "ToolSchema": json.dumps({"tools": MCP_TOOL_SCHEMA}),
                "TargetName": "Atlassian",
            },
        )

        # 12 Cedar policies (read=all, write=role-gated). One statement per CfnPolicy;
        # each depends on the target so the tool schema exists before it's referenced.
        for name, statement in self._cedar_statements(gateway_arn):
            pol = agentcore.CfnPolicy(
                self, f"Policy{name}",
                policy_engine_id=policy_engine_id,
                name=name,
                definition={"cedar": {"statement": statement}},
                validation_mode="IGNORE_ALL_FINDINGS",  # unconditional read permits are intentional
            )
            pol.node.add_dependency(target)

        CfnOutput(self, "RuntimeArn", value=runtime.agent_runtime_arn)
        CfnOutput(self, "ProviderName", value=PROVIDER_NAME)
        CfnOutput(self, "CallbackUrl", value=provider.attr_callback_url,
                  description="Register this in your Atlassian 3LO app's redirect URIs")

    # ------------------------------------------------------------------
    def _target_fn(self, gateway_arn: str):
        fn = lambda_.Function(
            self, "AtlassianTargetFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.on_event",
            code=bundled_asset(_HERE / "register_atlassian_target"),
            timeout=Duration.minutes(3),
            memory_size=256,
            description="Custom resource: create/update/delete the Atlassian gateway target (CDK)",
        )
        fn.add_to_role_policy(iam.PolicyStatement(
            sid="ManageAtlassianGatewayTarget",
            effect=iam.Effect.ALLOW,
            actions=["bedrock-agentcore:CreateGatewayTarget",
                     "bedrock-agentcore:UpdateGatewayTarget",
                     "bedrock-agentcore:DeleteGatewayTarget",
                     "bedrock-agentcore:GetGatewayTarget",
                     "bedrock-agentcore:ListGatewayTargets"],
            # Scoped to THIS gateway and its target sub-resources only (the `/*` suffix
            # matches the target ARNs regardless of their exact id form).
            resources=[gateway_arn, f"{gateway_arn}/*"],
        ))
        return fn

    def _cedar_statements(self, gateway_arn: str):
        cedar = _CEDAR_FILE.read_text().replace("__GATEWAY_ARN__", gateway_arn)
        no_comments = re.sub(r"//[^\n]*", "", cedar)
        out = []
        for chunk in no_comments.split(";"):
            if "permit" not in chunk and "forbid" not in chunk:
                continue
            stmt = chunk.strip() + ";"
            m = re.search(r"Atlassian___(\w+)", stmt)
            name = f"atlassian_{m.group(1)}" if m else f"atlassian_policy_{len(out)}"
            out.append((name, stmt))
        return out
