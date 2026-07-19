"""IAM permission management for AgentCore tool execution roles.

Extracted from routers/deployment.py. Provides scoped IAM policy
attachment for connected tools (browser, code_interpreter, memory,
gateway) and execution role name extraction from agentcore config.

Requirements: 5.5, 6.4
"""

import json
import logging
import os

import boto3
import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-tool IAM policy statements
# ---------------------------------------------------------------------------

# Maps tool name → list of IAM policy statements required by that tool.
# Each tool gets only the minimum permissions it needs.
TOOL_POLICY_STATEMENTS: dict[str, list[dict]] = {
    "browser": [
        {
            "Sid": "BedrockAgentCoreBrowserAccess",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:CreateBrowser",
                "bedrock-agentcore:ListBrowsers",
                "bedrock-agentcore:GetBrowser",
                "bedrock-agentcore:DeleteBrowser",
                "bedrock-agentcore:StartBrowserSession",
                "bedrock-agentcore:ListBrowserSessions",
                "bedrock-agentcore:GetBrowserSession",
                "bedrock-agentcore:StopBrowserSession",
                "bedrock-agentcore:UpdateBrowserStream",
                "bedrock-agentcore:ConnectBrowserAutomationStream",
                "bedrock-agentcore:ConnectBrowserLiveViewStream",
            ],
            "Resource": "*",
        }
    ],
    "code_interpreter": [
        {
            "Sid": "BedrockAgentCoreCodeInterpreterAccess",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:CreateCodeInterpreter",
                "bedrock-agentcore:StartCodeInterpreterSession",
                "bedrock-agentcore:InvokeCodeInterpreter",
                "bedrock-agentcore:StopCodeInterpreterSession",
                "bedrock-agentcore:DeleteCodeInterpreter",
                "bedrock-agentcore:ListCodeInterpreters",
                "bedrock-agentcore:GetCodeInterpreter",
                "bedrock-agentcore:GetCodeInterpreterSession",
                "bedrock-agentcore:ListCodeInterpreterSessions",
            ],
            "Resource": "*",
        }
    ],
    "memory": [
        {
            "Sid": "BedrockAgentCoreMemoryAccess",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:CreateMemory",
                "bedrock-agentcore:GetMemory",
                "bedrock-agentcore:DeleteMemory",
                "bedrock-agentcore:ListMemories",
                "bedrock-agentcore:SearchMemory",
                "bedrock-agentcore:IngestMemory",
            ],
            "Resource": "*",
        }
    ],
    "gateway": [
        {
            "Sid": "BedrockAgentCoreGatewayAccess",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:InvokeGateway",
                "bedrock-agentcore:ListGateways",
                "bedrock-agentcore:GetGateway",
            ],
            "Resource": "*",
        }
    ],
}

# Bedrock model access is needed when browser or code_interpreter is connected
_BEDROCK_MODEL_STATEMENT = {
    "Sid": "BedrockModelAccess",
    "Effect": "Allow",
    "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
    ],
    "Resource": "*",
}


# ---------------------------------------------------------------------------
# Boto3 wrapper helpers
# ---------------------------------------------------------------------------


def _create_iam_client():
    """Create and return a boto3 IAM client."""
    return boto3.client("iam")


def _put_role_inline_policy(iam_client, role_name: str, policy_name: str, policy_document: dict) -> None:
    """Attach an inline policy to an IAM role.

    Args:
        iam_client: boto3 IAM client.
        role_name: Name of the IAM role.
        policy_name: Name for the inline policy.
        policy_document: Policy document dict.
    """
    iam_client.put_role_policy(
        RoleName=role_name,
        PolicyName=policy_name,
        PolicyDocument=json.dumps(policy_document),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_tool_policy_statements(tools: list[str]) -> list[dict]:
    """Build the list of IAM policy statements for a set of tools.

    This is a pure function (no AWS calls) that can be tested independently.

    Args:
        tools: List of tool identifiers (e.g. ``["browser", "code_interpreter"]``).

    Returns:
        List of IAM policy statement dicts covering exactly the requested tools.
    """
    statements: list[dict] = []
    needs_bedrock_model = False

    for tool in tools:
        tool_stmts = TOOL_POLICY_STATEMENTS.get(tool)
        if tool_stmts:
            statements.extend(tool_stmts)
        if tool in ("browser", "code_interpreter"):
            needs_bedrock_model = True

    if needs_bedrock_model:
        statements.append(_BEDROCK_MODEL_STATEMENT)

    return statements


def add_tool_permissions(
    role_name: str,
    tools: list[str],
    region: str,
    account_id: str,
) -> dict:
    """Add minimum required IAM permissions for connected tools to an execution role.

    Args:
        role_name: IAM role name to attach the policy to.
        tools: List of tool identifiers.
        region: AWS region (for logging context).
        account_id: AWS account ID (for logging context).

    Returns:
        Dict with ``success`` bool and ``message`` or ``error`` string.

    Requirements: 5.5, 6.4
    """
    statements = build_tool_policy_statements(tools)

    if not statements:
        return {"success": True, "message": "No tool permissions needed"}

    policy_document = {
        "Version": "2012-10-17",
        "Statement": statements,
    }

    try:
        iam_client = _create_iam_client()
        _put_role_inline_policy(iam_client, role_name, "AgentCoreToolsAccess", policy_document)
        logger.info("Added tool permissions to role %s for tools %s", role_name, tools)
        return {"success": True, "message": f"Added tool permissions to {role_name}"}
    except Exception as e:
        logger.error("Failed to add tool permissions to %s: %s", role_name, e)
        return {"success": False, "error": str(e)}


def get_execution_role_name(deploy_dir: str) -> str | None:
    """Extract execution role name from agentcore YAML config.

    Reads ``.bedrock_agentcore.yaml`` in the deploy directory and
    extracts the role name from the default agent's ``execution_role`` ARN.

    Args:
        deploy_dir: Path to the deployment directory.

    Returns:
        Role name string, or ``None`` if not found.
    """
    config_path = os.path.join(deploy_dir, ".bedrock_agentcore.yaml")
    if not os.path.exists(config_path):
        return None

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)
    except Exception:
        return None

    default_agent = config.get("default_agent", "")
    agents = config.get("agents", {})
    if default_agent and default_agent in agents:
        agent_config = agents[default_agent]
        role_arn = agent_config.get("aws", {}).get("execution_role", "")
        if role_arn:
            return role_arn.split("/")[-1]

    return None
