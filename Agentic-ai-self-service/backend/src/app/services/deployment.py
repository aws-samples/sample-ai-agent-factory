"""Workflow deployment engine for AWS AgentCore.

This module provides the WorkflowExecutor class that handles:
- Deployment orchestration using bedrock-agentcore-starter-toolkit CLI
- Configuration file generation (.bedrock_agentcore.yaml)
- Multi-region deployment support
- Deployment status tracking
- Rollback on failure

Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7
"""

import asyncio
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import boto3

from app.models import (
    AgentCoreComponentType,
    ComponentNode,
    DeploymentConfig,
    DeploymentResult,
    RollbackError,
    RollbackResult,
    RuntimeConfiguration,
    GatewayConfiguration,
    WorkflowDefinition,
)
from app.models.enums import StrandsModelProvider
from app.services import runtime_deployer
from app.services.observability import build_otel_env_vars, get_platform_observability_defaults
from app.services.runtime_deployer import (
    upload_code_to_s3,
    create_agent_runtime,
    create_runtime_iam_role,
    wait_for_runtime_ready,
    destroy_runtime,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Name Sanitization
# ============================================================================


def sanitize_aws_name(name: str) -> str:
    """Sanitize name to be AWS-compliant.

    AWS AgentCore Gateway names must match: ([0-9a-zA-Z][-]?){1,48}
    Only alphanumeric characters and hyphens are allowed.

    Thin wrapper over the shared ``naming.sanitize_agentcore_name`` (hyphen
    style). Kept as a named function because callers/tests import it.

    Args:
        name: Original name

    Returns:
        Sanitized name with underscores replaced by hyphens
    """
    from app.services.naming import sanitize_agentcore_name

    return sanitize_agentcore_name(name, style="hyphen")


def _record_resource_best_effort(deployment_id: str, region: str, resource: dict) -> None:
    """Bug 9 parity: mirror the SFN step handlers' manifest writes onto the
    DIRECT deploy path. Append one created sub-resource to ``created_resources``
    so the generic delete path can tear it down. Best-effort by contract — a
    DeploymentStateStore failure here must NEVER fail the deploy. Recorded TYPE
    strings match the _delete_managed_resource dispatcher.
    """
    try:
        from app.services.deployment_state_store import DeploymentStateStore

        store = DeploymentStateStore(
            table_name=os.environ.get(
                "DEPLOYMENT_TABLE_NAME",
                os.environ.get("DEPLOYMENTS_TABLE_NAME", "DeploymentState"),
            ),
            region=region,
        )
        resource.setdefault("region", region)
        store.record_resource(deployment_id, resource)
    except Exception as exc:  # noqa: BLE001
        # SECURITY (CodeQL py/clear-text-logging-sensitive-data): log only the
        # resource TYPE + exception CLASS — never the resource dict or a full
        # traceback that could carry a secret-bearing local in frame.
        logger.warning(
            "Direct-path record_resource failed for %s (non-fatal): type=%s err=%s",
            deployment_id,
            str(resource.get("type")),
            type(exc).__name__,
        )


# ============================================================================
# AWS Region Configuration
# ============================================================================

VALID_AWS_REGIONS = [
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "eu-central-1",
    "eu-north-1",
    "ap-southeast-1",
    "ap-southeast-2",
    "ap-northeast-1",
    "ap-northeast-2",
    "ap-south-1",
    "sa-east-1",
    "ca-central-1",
]


class DeploymentPhase(str, Enum):
    """Phases of deployment process."""

    INITIALIZING = "initializing"
    GENERATING_CODE = "generating_code"
    CONFIGURING = "configuring"
    LAUNCHING = "launching"
    DEPLOYING_GATEWAY = "deploying_gateway"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"


@dataclass
class DeploymentState:
    """Tracks the state of a deployment operation."""

    deployment_id: str
    workflow_id: str
    phase: DeploymentPhase = DeploymentPhase.INITIALIZING
    agent_name: Optional[str] = None
    runtime_id: Optional[str] = None
    gateway_name: Optional[str] = None
    endpoint_url: Optional[str] = None
    error_message: Optional[str] = None
    work_dir: Optional[str] = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None


# ============================================================================
# Agent Code Generator
# ============================================================================


def _get_model_code(runtime_config: RuntimeConfiguration, region: str):
    """Extract model import and init code from runtime config."""
    model_id = runtime_config.model.model_id
    provider = getattr(runtime_config, "model_provider", None)
    if provider is None:
        provider = StrandsModelProvider.BEDROCK
    provider_str = provider.value if hasattr(provider, "value") else str(provider)
    from app.services.code_generator import _get_model_init_code

    return _get_model_init_code(provider_str, model_id, region)


def generate_agent_code(runtime_config: RuntimeConfiguration) -> str:
    """Generate basic agent.py with no extra components."""
    return generate_unified_agent_code(runtime_config, connected_tools=[])


def generate_gateway_agent_code(
    runtime_config: RuntimeConfiguration,
    gateway_result: dict,
    region: str,
) -> str:
    """Generate gateway-connected agent.py."""
    return generate_unified_agent_code(
        runtime_config,
        connected_tools=["gateway"],
        gateway_result=gateway_result,
        region=region,
    )


def generate_unified_agent_code(
    runtime_config: RuntimeConfiguration,
    connected_tools: list[str],
    gateway_result: Optional[dict] = None,
    memory_id: Optional[str] = None,
    region: Optional[str] = None,
) -> str:
    """Generate agent.py that integrates ALL connected AgentCore components.

    Produces a single agent.py that combines whichever components are connected:
    - Gateway: MCPClient + streamablehttp_client + OAuth2 Cognito
    - Memory: AgentCoreMemorySessionManager for conversation persistence
    - Code Interpreter: @tool wrapper around code_session
    - Browser: AgentCoreBrowser tool
    - Observability: env vars (handled at deploy time, not in code)
    - Identity: auth header propagation via context
    - Policy: gateway-level feature (not in agent code)

    Uses official AWS patterns from amazon-bedrock-agentcore-samples.
    """
    region = region or os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))
    from app.services.code_generator import _escape_triple_quotes

    system_prompt = _escape_triple_quotes(runtime_config.system_prompt)
    model_import, model_init = _get_model_code(runtime_config, region)
    tq = '"""'

    has_gateway = "gateway" in connected_tools and gateway_result
    has_memory = "memory" in connected_tools
    has_code_interpreter = "code_interpreter" in connected_tools
    has_browser = "browser" in connected_tools

    # --- Build imports ---
    imports = [
        "import os",
        "import json",
        "",
        "from strands import Agent",
        model_import,
        "from bedrock_agentcore.runtime import BedrockAgentCoreApp",
    ]

    if has_gateway:
        imports += [
            "import urllib.request",
            "import urllib.parse",
            "import base64",
            "from strands.tools.mcp.mcp_client import MCPClient",
            "from mcp.client.streamable_http import streamablehttp_client",
        ]

    if has_memory:
        imports += [
            "from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig",
            "from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager",
        ]

    if has_code_interpreter or has_browser:
        imports.append("from strands import tool")

    if has_code_interpreter:
        imports.append("from bedrock_agentcore.tools.code_interpreter_client import code_session")

    if has_browser:
        imports.append("from bedrock_agentcore.tools.browser_client import browser_session")

    imports_str = "\n".join(imports)

    # --- Build config section ---
    config_lines = [
        f"SYSTEM_PROMPT = {tq}{system_prompt}{tq}",
        f'REGION = os.environ.get("AWS_REGION", "{region}")',
    ]

    if has_gateway:
        # Don't embed credentials in source code — rely on env vars set at deploy time
        config_lines += [
            'GATEWAY_URL = os.environ.get("GATEWAY_URL", "")',
            'COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")',
            'COGNITO_CLIENT_SECRET = os.environ.get("COGNITO_CLIENT_SECRET", "")',
            'COGNITO_TOKEN_ENDPOINT = os.environ.get("COGNITO_TOKEN_ENDPOINT", "")',
            'COGNITO_SCOPE = os.environ.get("COGNITO_SCOPE", "")',
        ]

    if has_memory:
        mid = memory_id or ""
        config_lines.append(f'MEMORY_ID = os.environ.get("MEMORY_ID", "{mid}")')

    config_str = "\n".join(config_lines)

    # --- Build helper functions ---
    helpers = []

    helpers.append(f"""
def load_model():
    {model_init}
    return model
""")

    if has_gateway:
        helpers.append("""
def _get_oauth_token():
    if not COGNITO_CLIENT_ID or not COGNITO_TOKEN_ENDPOINT:
        return ""
    try:
        form = {"grant_type": "client_credentials", "client_id": COGNITO_CLIENT_ID,
                "client_secret": COGNITO_CLIENT_SECRET}
        if COGNITO_SCOPE:
            form["scope"] = COGNITO_SCOPE
        data = urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(COGNITO_TOKEN_ENDPOINT, data=data,
                                      headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())["access_token"]
    except Exception as e:
        print(f"Warning: Failed to get gateway token: {e}")
        return ""


def _create_transport():
    token = _get_oauth_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return streamablehttp_client(url=GATEWAY_URL, headers=headers)


def get_full_tools_list(client):
    \"\"\"Retrieve tools from the MCP gateway, handling pagination.

    Bug 189: large SaaS connectors expose MANY large-schema operations (Asana ~30,
    GitHub ~1194). Loading ALL of them into the agent's system prompt blows the
    model context window (observed: 204,908 tokens > 200,000 max ->
    ContextWindowOverflowException, every invoke fails with a 500). Cap the number
    of tools bound to the agent so it stays well under the context limit. An agent
    cannot usefully wield hundreds of tools anyway; the cap keeps the most-recently
    listed page(s) and is generous enough for real workflows. Override via
    MAX_GATEWAY_TOOLS.

    Default 20: observed per-tool cost for large SaaS schemas is ~6.7K tokens
    (Asana's 30 tools = ~205K prompt tokens), so 20 tools (~137K) leaves headroom
    under the 200K model context for the system prompt + conversation.\"\"\"
    import os as _os
    max_tools = int(_os.environ.get("MAX_GATEWAY_TOOLS", "20"))
    more_tools = True
    tools = []
    pagination_token = None
    while more_tools:
        tmp_tools = client.list_tools_sync(pagination_token=pagination_token)
        tools.extend(tmp_tools)
        if len(tools) >= max_tools or tmp_tools.pagination_token is None:
            more_tools = False
        else:
            pagination_token = tmp_tools.pagination_token
    if len(tools) > max_tools:
        print(f"Gateway exposed {len(tools)} tools; capping to {max_tools} to fit the model context window (set MAX_GATEWAY_TOOLS to change).")
        tools = tools[:max_tools]
    return tools
""")

    if has_code_interpreter:
        helpers.append("""
@tool
def execute_python(code: str, description: str = "") -> str:
    \"\"\"Execute Python code in a secure sandbox. Use this for calculations, data analysis, or any Python task.\"\"\"
    if description:
        code = f"# {description}\\n{code}"
    with code_session(REGION) as client:
        response = client.invoke("executeCode", {
            "code": code,
            "language": "python",
            "clearContext": False,
        })
    for event in response.get("stream", [response]):
        result = event.get("result", event)
        return json.dumps(result) if isinstance(result, dict) else str(result)
    return "No output"
""")

    if has_browser:
        helpers.append("""
@tool
def browse_web(url: str, task: str = "") -> str:
    \"\"\"Browse a web page and extract information. Provide a URL and optionally a task describing what to look for.\"\"\"
    with browser_session(REGION) as client:
        response = client.invoke("navigateAndExtract", {
            "url": url,
            "task": task or f"Extract main content from {url}",
        })
    for event in response.get("stream", [response]):
        result = event.get("result", event)
        return json.dumps(result) if isinstance(result, dict) else str(result)
    return "No content extracted"
""")

    helpers_str = "\n".join(helpers)

    # --- Build lazy-init section (boto3 clients may not have valid creds at module load) ---
    local_tools = []
    if has_code_interpreter:
        local_tools.append("execute_python")
    if has_browser:
        local_tools.append("browse_web")
    local_tools_str = ", ".join(local_tools)

    lazy_init_lines = ["""
# Lazy init — boto3/MCP clients may not have valid creds at module load time
_model = None
_gateway_tools = None

def _get_model():
    global _model
    if _model is None:
        _model = load_model()
    return _model"""]

    if has_gateway:
        lazy_init_lines.append("""
def _get_gateway_tools():
    global _gateway_tools
    if _gateway_tools is None:
        _gateway_tools = []
        if GATEWAY_URL:
            mcp_client = MCPClient(_create_transport)
            mcp_client.start()
            _gateway_tools = get_full_tools_list(mcp_client)
    return _gateway_tools""")
        all_tools_expr = f"[{local_tools_str}{',' if local_tools_str else ''}] + list(_get_gateway_tools())" if local_tools_str else "list(_get_gateway_tools())"
    else:
        all_tools_expr = f"[{local_tools_str}]"

    lazy_init_str = "\n".join(lazy_init_lines)

    # --- Build entrypoint ---
    if has_memory:
        entrypoint = f"""
@app.entrypoint
def invoke(payload):
    prompt = payload.get("prompt", "Hello!")
    session_id = payload.get("session_id", "default")
    actor_id = payload.get("actor_id", "user")

    session_manager = None
    if MEMORY_ID:
        mem_config = AgentCoreMemoryConfig(
            memory_id=MEMORY_ID, session_id=session_id, actor_id=actor_id,
        )
        session_manager = AgentCoreMemorySessionManager(mem_config, region_name=REGION)

    agent = Agent(
        model=_get_model(), system_prompt=SYSTEM_PROMPT,
        tools={all_tools_expr},
        **({{"session_manager": session_manager}} if session_manager else {{}}),
    )
    result = agent(prompt)
    return {{"response": str(result), "session_id": session_id}}
"""
    else:
        entrypoint = f"""
@app.entrypoint
def invoke(payload):
    prompt = payload.get("prompt", "Hello!")
    agent = Agent(
        model=_get_model(), system_prompt=SYSTEM_PROMPT,
        tools={all_tools_expr},
    )
    result = agent(prompt)
    return {{"response": str(result)}}
"""

    # --- Assemble final code ---
    return f"""{tq}AgentCore Runtime Agent — unified component integration.{tq}
{imports_str}

app = BedrockAgentCoreApp()

{config_str}
{helpers_str}
{lazy_init_str}
{entrypoint}

if __name__ == "__main__":
    app.run()
"""


def generate_mcp_server_code(
    server_name: str = "MCP Server Agent",
    tools: Optional[list[str]] = None,
    system_prompt: str = "",
) -> str:
    """Generate FastMCP server code for an MCP Server Runtime.

    Uses the ``mcp`` package (bundled via the lean mcp bundle) to expose tools
    via the streamable-HTTP MCP transport. The server binds port 8000 — the
    AgentCore Runtime MCP-protocol ingress contract (Bug 173); 8080 made the
    server unreachable and the gateway target timed out fetching tools.
    """
    tools = tools or ["get_order", "get_customer", "list_orders", "process_refund"]
    tq = '"""'

    # Normalize tools: can be list of strings or list of dicts with toolName/implementation
    tool_names: list[str] = []
    custom_tool_defs: list[dict] = []
    for t in tools:
        if isinstance(t, dict):
            # Bug 174: accept ALL common name keys (toolName / tool_name / name)
            # and ALL body keys (implementation / code). The mcpServerConfig.tools
            # shape is a free dict (no strict schema), and callers/tests commonly
            # send {"name","description","code"} — the old parser only read
            # toolName+implementation, so such tools produced an EMPTY server and
            # the gateway target failed "MCP server ... has no tools".
            name = t.get("toolName") or t.get("tool_name") or t.get("name") or ""
            tool_names.append(name)
            if t.get("implementation") or t.get("code"):
                custom_tool_defs.append(t)
        else:
            tool_names.append(t)

    # --- Tool implementations ---
    tool_impls: list[str] = []

    if "get_order" in tool_names:
        tool_impls.append('''
@mcp.tool()
def get_order(order_id: str) -> str:
    """Look up order details by order ID. Returns order items, status, dates, and total."""
    order = ORDERS.get(order_id)
    if not order:
        return json.dumps({"error": f"Order {order_id} not found. Try ORD-12345 or ORD-67890."})
    return json.dumps(order)
''')

    if "get_customer" in tool_names:
        tool_impls.append('''
@mcp.tool()
def get_customer(customer_id: str) -> str:
    """Look up customer info and order summary by customer ID."""
    customer = CUSTOMERS.get(customer_id)
    if not customer:
        return json.dumps({"error": f"Customer {customer_id} not found. Try CUST-001 or CUST-002."})
    return json.dumps(customer)
''')

    if "list_orders" in tool_names:
        tool_impls.append('''
@mcp.tool()
def list_orders(customer_id: str = "") -> str:
    """List orders, optionally filtered by customer ID."""
    if customer_id:
        orders = [o for o in ORDERS.values() if o["customer_id"] == customer_id]
    else:
        orders = list(ORDERS.values())
    return json.dumps(orders)
''')

    if "process_refund" in tool_names:
        tool_impls.append('''
@mcp.tool()
def process_refund(order_id: str, amount: float = 0, reason: str = "") -> str:
    """Process a refund for an order with amount validation."""
    order = ORDERS.get(order_id)
    if not order:
        return json.dumps({"error": f"Order {order_id} not found"})
    max_refund = order["total"]
    if amount <= 0:
        amount = max_refund
    if amount > max_refund:
        return json.dumps({"error": f"Refund amount {amount} exceeds order total {max_refund}"})
    return json.dumps({
        "refund_id": f"REF-{order_id[-5:]}",
        "order_id": order_id,
        "amount": amount,
        "reason": reason or "Customer request",
        "status": "approved",
    })
''')

    if "duckduckgo_search" in tool_names:
        tool_impls.append('''
@mcp.tool()
def duckduckgo_search(query: str, max_results: int = 5) -> str:
    """Search the web using DuckDuckGo. Returns top results."""
    import urllib.request, urllib.parse
    url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_redirect=1"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        results = []
        for topic in data.get("RelatedTopics", [])[:max_results]:
            if "Text" in topic:
                results.append({"text": topic["Text"], "url": topic.get("FirstURL", "")})
        return json.dumps(results if results else [{"text": data.get("AbstractText", "No results"), "url": data.get("AbstractURL", "")}])
    except Exception as e:
        return json.dumps({"error": str(e)})
''')

    if "wikipedia_search" in tool_names:
        tool_impls.append('''
@mcp.tool()
def wikipedia_search(query: str) -> str:
    """Search and retrieve Wikipedia article summaries."""
    import urllib.request, urllib.parse
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(query)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AgentCore/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return json.dumps({"title": data.get("title", ""), "summary": data.get("extract", "No summary available")})
    except Exception as e:
        return json.dumps({"error": str(e)})
''')

    # Generate custom tools from dict definitions (name/toolName + code/implementation)
    for ct in custom_tool_defs:
        ct_name_raw = ct.get("toolName") or ct.get("tool_name") or ct.get("name") or "custom_tool"
        # Sanitize tool name to valid Python identifier (prevent code injection)
        ct_name = re.sub(r"[^a-zA-Z0-9_]", "_", ct_name_raw)
        if not ct_name or not ct_name[0].isalpha():
            ct_name = "tool_" + ct_name
        ct_desc = ct.get("description", "A custom tool").replace('"""', r'\"\"\"')
        # Bug 174: the body can arrive as `implementation` (a function BODY) or as
        # `code` (often a COMPLETE `def name(...): ...`). If `code` already defines
        # the function, emit it verbatim under @mcp.tool() (just ensure the def name
        # matches ct_name); otherwise treat the text as the function body.
        ct_code = ct.get("code")
        if ct_code and "def " in ct_code:
            body = ct_code.strip()
            # Point the decorator at whatever function the code defines (its name
            # becomes the MCP tool name). Normalise the registered name to ct_name
            # only if the code's def name is unsafe; otherwise keep the author's.
            tool_impls.append(f"\n@mcp.tool()\n{body}\n")
            continue
        ct_impl = ct.get("implementation") or ct.get("code") or "return {'result': 'ok'}"
        # Build parameter signature from inputSchema
        ct_schema = ct.get("inputSchema", ct.get("input_schema", {}))
        ct_props = ct_schema.get("properties", {})
        ct_required = set(ct_schema.get("required", []))
        params = []
        for pname, pinfo in ct_props.items():
            ptype = {
                "string": "str",
                "integer": "int",
                "number": "float",
                "boolean": "bool",
            }.get(pinfo.get("type", "string"), "str")
            if pname in ct_required:
                params.append(f"{pname}: {ptype}")
            else:
                default = '""' if ptype == "str" else "0" if ptype in ("int", "float") else "False"
                params.append(f"{pname}: {ptype} = {default}")
        params_str = ", ".join(params)
        # Indent implementation lines
        impl_lines = ct_impl.strip().split("\n")
        impl_str = "\n    ".join(impl_lines)
        tool_impls.append(f'''
@mcp.tool()
def {ct_name}({params_str}) -> str:
    """{ct_desc}"""
    {impl_str}
''')

    tools_str = "\n".join(tool_impls)

    # Only include mock data if any customer support tools are used
    has_customer_tools = any(t in tool_names for t in ["get_order", "get_customer", "list_orders", "process_refund"])
    mock_data = ""
    if has_customer_tools:
        mock_data = """
ORDERS = {
    "ORD-12345": {
        "order_id": "ORD-12345", "customer_id": "CUST-001", "status": "delivered",
        "items": [{"name": "Widget A", "qty": 2, "price": 9.99}],
        "total": 19.98, "date": "2025-01-15",
    },
    "ORD-67890": {
        "order_id": "ORD-67890", "customer_id": "CUST-002", "status": "shipped",
        "items": [{"name": "Gadget B", "qty": 1, "price": 49.99}, {"name": "Widget C", "qty": 3, "price": 12.00}],
        "total": 85.99, "date": "2025-02-01",
    },
    "ORD-11111": {
        "order_id": "ORD-11111", "customer_id": "CUST-001", "status": "processing",
        "items": [{"name": "Premium Widget", "qty": 1, "price": 149.99}],
        "total": 149.99, "date": "2025-03-01",
    },
}

CUSTOMERS = {
    "CUST-001": {"customer_id": "CUST-001", "name": "Alice Smith", "email": "alice@example.com", "total_orders": 5},
    "CUST-002": {"customer_id": "CUST-002", "name": "Bob Jones", "email": "bob@example.com", "total_orders": 3},
}
"""

    return f"""{tq}MCP Server Agent -- exposes tools via MCP protocol on AgentCore Runtime.{tq}
import json
import os
from mcp.server.fastmcp import FastMCP

# AgentCore Runtime with serverProtocol=MCP proxies the container's ingress to
# port 8000 (the documented MCP-runtime contract — see the AWS
# agentcore-samples MCP-server-as-a-target workshop, which uses the FastMCP
# default 8000). Binding 8080 (Bug 173) left the MCP server unreachable behind
# the runtime's MCP ingress: the Gateway's tool-discovery probe connected but
# the MCP handshake never landed, so the target failed with "Runtime
# initialization time exceeded ... 30s" and the gateway served 0 tools. Default
# to 8000; honour PORT only if AgentCore ever overrides it.
PORT = int(os.environ.get("PORT", "8000"))
mcp = FastMCP(host="0.0.0.0", port=PORT, stateless_http=True)
{mock_data}
{tools_str}

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
"""


def generate_requirements(runtime_config: RuntimeConfiguration) -> str:
    """Generate requirements.txt from RuntimeConfiguration.

    Returns empty string — the AgentCore Runtime does NOT install from
    requirements.txt. All dependencies are pre-bundled into code.zip
    via S3 dependency bundles (base.zip or strands-mcp.zip).

    Args:
        runtime_config: Runtime configuration

    Returns:
        Empty string (deps are pre-bundled)
    """
    return ""


# ============================================================================
# Workflow Executor
# ============================================================================


class WorkflowExecutor:
    """Handles workflow deployment to AWS AgentCore.

    Uses the bedrock-agentcore-starter-toolkit CLI for deployment.
    """

    def __init__(self, region: str = "us-west-2"):
        """Initialize the executor with target region."""
        if region not in VALID_AWS_REGIONS:
            raise ValueError(f"Invalid AWS region: {region}")

        self.region = region
        self._deployments: dict[str, DeploymentState] = {}

    def set_region(self, region: str) -> None:
        """Change the target deployment region."""
        if region not in VALID_AWS_REGIONS:
            raise ValueError(f"Invalid AWS region: {region}")
        self.region = region

    async def deploy(
        self,
        workflow: WorkflowDefinition,
        config: DeploymentConfig,
        template_id: Optional[str] = None,
        connected_tools: Optional[list] = None,
        gateway_tools: Optional[list] = None,
        custom_tools: Optional[list] = None,
        identity_config: Optional[dict] = None,
        mcp_server_config: Optional[dict] = None,
        memory_config: Optional[dict] = None,
        evaluation_config: Optional[dict] = None,
        policy_config: Optional[dict] = None,
        guardrails_config: Optional[dict] = None,
        knowledge_base_config: Optional[dict] = None,
        observability_config: Optional[dict] = None,
        connectors: Optional[list] = None,
        owner_sub: str = "",
        deployment_mode: str = "runtime",
    ) -> DeploymentResult:
        """Deploy workflow to AWS AgentCore.

        Deployment order:
        1. Deploy Gateway (if present) — creates Cognito, Lambda targets, MCP gateway
        2. Generate agent code — includes gateway MCP client if gateway was deployed
        3. Upload code + bundled deps to S3
        4. Create IAM role and Runtime — with gateway env vars injected
        5. Wait for Runtime to become ready

        Args:
            workflow: The workflow definition to deploy
            config: Deployment configuration
            template_id: Optional template identifier for template-specific code generation
            connected_tools: Optional list of connected component types (e.g. ['gateway', 'memory'])
            gateway_tools: Optional list of tool IDs connected to the gateway (e.g. ['weather_api'])
            custom_tools: Optional list of custom tool definitions with lambdaCode
            identity_config: Optional identity provider configuration
            mcp_server_config: Optional MCP server runtime configuration
            memory_config: Optional memory configuration from frontend
            evaluation_config: Optional evaluation configuration from frontend
            policy_config: Optional policy engine configuration from frontend
            knowledge_base_config: Optional knowledge base configuration from frontend

        Returns:
            DeploymentResult with status and endpoint URL
        """
        deployment_id = str(uuid.uuid4())
        connected_tools = connected_tools or []
        gateway_tools = gateway_tools or []
        custom_tools = custom_tools or []
        connectors = connectors or []
        state = DeploymentState(
            deployment_id=deployment_id,
            workflow_id=workflow.id,
        )
        self._deployments[deployment_id] = state

        self.set_region(config.aws_region)

        try:
            # Find Runtime component
            runtime_node = self._find_component(workflow, AgentCoreComponentType.RUNTIME)
            if not runtime_node:
                raise ValueError("Workflow must have a Runtime component")

            runtime_config = runtime_node.data
            if not isinstance(runtime_config, RuntimeConfiguration):
                raise ValueError("Invalid Runtime configuration")

            state.agent_name = runtime_config.name

            # ------------------------------------------------------------------
            # Phase 0: Deploy MCP Server Runtime (if present)
            # When a second Runtime is connected to the Gateway as a target,
            # deploy it first so we can use its endpoint as a gateway target.
            # ------------------------------------------------------------------
            mcp_server_runtime_arn: Optional[str] = None
            mcp_server_runtime_id: Optional[str] = None

            if mcp_server_config:
                logger.info(
                    "Deploying MCP Server Runtime: %s",
                    mcp_server_config.get("name", "mcp-server"),
                )
                mcp_name = mcp_server_config.get("name", "mcp-server")
                mcp_tools = mcp_server_config.get("tools", [])
                mcp_system_prompt = mcp_server_config.get("systemPrompt", "")

                # Generate MCP server code
                mcp_code = generate_mcp_server_code(
                    server_name=mcp_name,
                    tools=mcp_tools if mcp_tools else None,
                    system_prompt=mcp_system_prompt,
                )
                logger.info("Generated MCP server code (%d bytes)", len(mcp_code))

                # Upload MCP server code to S3 with stable prefix keyed on
                # runtime name (Bug 61) — AgentCore IAM cache is keyed on
                # (role, S3 prefix) so per-deploy prefixes hit a 17-20 min race.
                bucket = os.environ.get("ARTIFACTS_BUCKET_NAME", "")
                mcp_s3_key = f"deployments/by-name/{runtime_deployer.sanitize_runtime_name(mcp_name)}/mcp-server-code.zip"
                if bucket:
                    s3_client = boto3.client("s3", region_name=self.region)
                    # Use strands-mcp.zip bundle (includes mcp package with FastMCP)
                    deps_bundle = None
                    try:
                        resp = s3_client.get_object(Bucket=bucket, Key="agentcore-deps/strands-mcp.zip")
                        deps_bundle = resp["Body"].read()
                    except Exception as e:
                        logger.warning("Failed to download deps bundle: %s", e)

                    upload_code_to_s3(
                        s3_client,
                        bucket,
                        mcp_s3_key,
                        mcp_code,
                        "",
                        "agent.py",
                        deps_bundle=deps_bundle,
                    )
                    logger.info("Uploaded MCP server code to s3://%s/%s", bucket, mcp_s3_key)

                # Create IAM role for MCP server runtime
                sts = boto3.client("sts")
                account_id = sts.get_caller_identity()["Account"]
                iam_client = boto3.client("iam")
                _mcp_role_name = f"{mcp_name}-mcp-role"
                mcp_role_arn = create_runtime_iam_role(
                    iam_client,
                    _mcp_role_name,
                    account_id,
                    self.region,
                    [],  # MCP server doesn't need extra tool permissions
                )
                _record_resource_best_effort(
                    deployment_id, self.region,
                    {"type": "iam_role", "name": _mcp_role_name},
                )

                # Create MCP server runtime (protocol=MCP)
                agentcore_ctrl = boto3.client("bedrock-agentcore-control", region_name=self.region)
                mcp_runtime_result = create_agent_runtime(
                    agentcore_ctrl=agentcore_ctrl,
                    runtime_name=runtime_deployer.sanitize_runtime_name(mcp_name),
                    role_arn=mcp_role_arn,
                    s3_bucket=bucket,
                    s3_key=mcp_s3_key,
                    entrypoint="agent.py",
                    python_runtime="PYTHON_3_13",
                    protocol="MCP",
                    env_vars={"AWS_REGION": self.region},
                )
                mcp_server_runtime_id = mcp_runtime_result["runtime_id"]
                logger.info("Created MCP server runtime: %s", mcp_server_runtime_id)
                _record_resource_best_effort(
                    deployment_id, self.region,
                    {"type": "agent_runtime", "id": mcp_server_runtime_id},
                )

                # Wait for MCP server runtime to be ready
                mcp_launch = wait_for_runtime_ready(agentcore_ctrl, mcp_server_runtime_id)
                if not mcp_launch.get("success"):
                    raise RuntimeError(
                        f"MCP Server Runtime failed to become ready: {mcp_launch.get('error', 'unknown')}"
                    )

                # Use runtime ARN (gateway deployer converts to HTTPS URL)
                mcp_server_runtime_arn = mcp_runtime_result.get("arn", "")
                logger.info("MCP Server Runtime ready: arn=%s", mcp_server_runtime_arn)

            # ------------------------------------------------------------------
            # Phase 1: Deploy Gateway FIRST (if present)
            # We need the gateway URL and Cognito credentials before generating
            # agent code so the Runtime can connect to it.
            # ------------------------------------------------------------------
            gateway_result: Optional[dict] = None
            gateway_node = self._find_component(workflow, AgentCoreComponentType.GATEWAY)
            has_gateway = gateway_node is not None or "gateway" in connected_tools

            if has_gateway:
                state.phase = DeploymentPhase.DEPLOYING_GATEWAY

                # Determine gateway name from node config or runtime name
                gw_name = runtime_config.name
                if gateway_node and isinstance(gateway_node.data, GatewayConfiguration):
                    gw_name = gateway_node.data.name
                    state.gateway_name = gw_name

                gw_config_dict = {"name": gw_name}

                from app.services.gateway_deployer import (
                    deploy_gateway as boto3_deploy_gateway,
                )

                gateway_result = await asyncio.to_thread(
                    boto3_deploy_gateway,
                    gateway_config=gw_config_dict,
                    region=self.region,
                    template_id=template_id,
                    gateway_tools=gateway_tools,
                    identity_config=identity_config,
                    custom_tools=custom_tools,
                    connectors=connectors,
                    owner_sub=owner_sub,
                    mcp_server_runtime_arn=mcp_server_runtime_arn,
                )

                if not gateway_result.get("success"):
                    raise RuntimeError(f"Gateway deployment failed: {gateway_result.get('error', 'unknown')}")

                logger.info(
                    "Gateway deployed: url=%s, id=%s",
                    gateway_result.get("gateway_url"),
                    gateway_result.get("gateway_id"),
                )

                # Bug 9 parity: mirror gateway_step's manifest writes here so the
                # generic delete path can tear down the gateway + Cognito pool +
                # tool lambdas/roles + connector secrets/providers on the direct
                # path too. Types match _delete_managed_resource exactly.
                _gw = gateway_result
                if _gw.get("gateway_id"):
                    _record_resource_best_effort(deployment_id, self.region, {"type": "gateway", "id": _gw["gateway_id"]})
                if _gw.get("gateway_name"):
                    _record_resource_best_effort(deployment_id, self.region, {"type": "iam_role", "name": f"AgentCoreGateway-{_gw['gateway_name']}"})
                _gw_pool = (_gw.get("client_info") or {}).get("user_pool_id")
                if _gw_pool:
                    _record_resource_best_effort(deployment_id, self.region, {"type": "cognito_user_pool", "id": _gw_pool})
                for _fn in [_gw.get("lambda_function_name"), _gw.get("kb_lambda_name"), *( _gw.get("custom_tool_lambdas") or [])]:
                    if _fn:
                        _record_resource_best_effort(deployment_id, self.region, {"type": "lambda", "name": _fn})
                for _rn in _gw.get("custom_tool_roles") or []:
                    if _rn:
                        _record_resource_best_effort(deployment_id, self.region, {"type": "iam_role", "name": _rn})
                for _sa in _gw.get("connector_secret_arns") or []:
                    if _sa:
                        _record_resource_best_effort(deployment_id, self.region, {"type": "secret", "id": _sa})
                for _entry in _gw.get("connector_credential_providers") or []:
                    if not _entry:
                        continue
                    _kind, _, _pname = str(_entry).partition(":")
                    if not _pname:
                        _kind, _pname = "OAUTH", str(_entry)
                    _ptype = "api_key_credential_provider" if _kind.upper() == "API_KEY" else "oauth2_credential_provider"
                    _record_resource_best_effort(deployment_id, self.region, {"type": _ptype, "name": _pname})

            # ------------------------------------------------------------------
            # Phase 1.5: Deploy Policy Engine (if policy node is connected)
            # ------------------------------------------------------------------
            if "policy" in connected_tools and gateway_result:
                try:
                    gateway_id = gateway_result.get("gateway_id", "")
                    gateway_arn = gateway_result.get("gateway_arn", "")
                    if gateway_id:
                        agentcore_ctrl_policy = boto3.client(
                            "bedrock-agentcore-control",
                            region_name=self.region,
                        )
                        _pc = policy_config or {}
                        raw_engine_name = _pc.get("name", f"PolicyEngine_{gateway_id[:16]}")
                        # Engine names: [A-Za-z][A-Za-z0-9_]* — no hyphens
                        engine_name = re.sub(r"[^A-Za-z0-9_]", "_", raw_engine_name)

                        # Check if engine already exists
                        engine_id = None
                        engine_arn = None
                        try:
                            existing = agentcore_ctrl_policy.list_policy_engines(maxResults=100)
                            for pe in existing.get("policyEngines", existing.get("items", [])):
                                if pe.get("name") == engine_name:
                                    engine_id = pe.get("policyEngineId")
                                    engine_arn = pe.get("policyEngineArn")
                                    break
                        except Exception as exc:
                            logger.warning("Could not list policy engines: %s", exc)

                        if not engine_id:
                            pe_resp = agentcore_ctrl_policy.create_policy_engine(
                                name=engine_name,
                                description=f"Policy engine for gateway {gateway_id}",
                            )
                            engine_id = pe_resp.get("policyEngineId", "")
                            engine_arn = pe_resp.get("policyEngineArn", "")

                            # Wait for engine to be ready
                            import time as _time_pe

                            for _ in range(12):
                                pe_status = agentcore_ctrl_policy.get_policy_engine(policyEngineId=engine_id)
                                if pe_status.get("status") in ("ACTIVE", "READY"):
                                    break
                                _time_pe.sleep(5)

                        # Create policies from config or default permit-all
                        _pc_policies = _pc.get("policies", [])
                        if _pc_policies:
                            for pol in _pc_policies:
                                pol_name = pol.get("name", "policy")
                                pol_statement = pol.get("statement", "")
                                if not pol_statement:
                                    continue
                                try:
                                    agentcore_ctrl_policy.create_policy(
                                        policyEngineId=engine_id,
                                        name=pol_name,
                                        description=pol.get("description", ""),
                                        definition={"cedar": {"statement": pol_statement}},
                                    )
                                except Exception as pol_err:
                                    if "already exists" not in str(pol_err).lower():
                                        logger.warning("Could not create policy %s: %s", pol_name, pol_err)
                        else:
                            # Default permit-all (Cedar requires a when clause)
                            default_statement = (
                                f'permit(principal, action, resource == AgentCore::Gateway::"{gateway_arn}")\nwhen {{ true }};'
                                if gateway_arn
                                else 'permit(principal, action, resource is AgentCore::Gateway)\nwhen { true };'
                            )
                            try:
                                agentcore_ctrl_policy.create_policy(
                                    policyEngineId=engine_id,
                                    name="default_permit_all",
                                    description="Default permit-all policy for gateway tools",
                                    definition={"cedar": {"statement": default_statement}},
                                )
                            except Exception as pol_err:
                                if "already exists" not in str(pol_err).lower():
                                    logger.warning("Could not create policy: %s", pol_err)

                        # Attach policy engine to gateway
                        gw_detail = agentcore_ctrl_policy.get_gateway(gatewayIdentifier=gateway_id)
                        update_params = {
                            "gatewayIdentifier": gateway_id,
                            "name": gw_detail.get("name", ""),
                            "roleArn": gw_detail.get("roleArn", ""),
                            "protocolType": gw_detail.get("protocolType", "MCP"),
                            "policyEngineConfiguration": {
                                "arn": engine_arn,
                                # Bug 134 (proper fix): ENFORCE works now that the
                                # baseline permit + schema-correct principal/action
                                # let the principal discover+call tools. ENFORCE is
                                # the default; LOG_ONLY available for audit dry-runs.
                                "mode": _pc.get("mode", "ENFORCE"),
                            },
                        }
                        for opt_field in (
                            "description",
                            "authorizerType",
                            "authorizerConfiguration",
                            "protocolConfiguration",
                        ):
                            if gw_detail.get(opt_field):
                                update_params[opt_field] = gw_detail[opt_field]
                        agentcore_ctrl_policy.update_gateway(**update_params)
                        logger.info(
                            "Attached policy engine %s to gateway %s",
                            engine_id,
                            gateway_id,
                        )

                        # Wait for gateway to be ready again
                        import time as _time_gw

                        for _ in range(24):
                            gw = agentcore_ctrl_policy.get_gateway(gatewayIdentifier=gateway_id)
                            if gw.get("status") == "READY":
                                break
                            _time_gw.sleep(5)
                except Exception as policy_err:
                    logger.warning("Policy deployment failed (non-fatal): %s", policy_err)

            # ------------------------------------------------------------------
            # Phase 1.6: Deploy Guardrails (if guardrails node is connected)
            # ------------------------------------------------------------------
            guardrails_result: dict = {}
            if "guardrails" in connected_tools and guardrails_config:
                try:
                    bedrock_guardrails = boto3.client("bedrock", region_name=self.region)
                    gc_mode = guardrails_config.get("mode", "existing")

                    if gc_mode == "existing":
                        gid = guardrails_config.get("guardrailId", guardrails_config.get("guardrail_id", ""))
                        gver = guardrails_config.get("guardrailVersion", guardrails_config.get("guardrail_version", "DRAFT"))
                        if gid:
                            bedrock_guardrails.get_guardrail(guardrailIdentifier=gid, guardrailVersion=gver)
                            guardrails_result = {"guardrail_id": gid, "guardrail_version": gver, "created_by_flow": False}
                            logger.warning("Validated existing guardrail: %s (v%s)", gid, gver)
                    elif gc_mode == "create_new":
                        from app.step_handlers.guardrails_step import _build_content_filter_config, _build_pii_config, _build_topic_config, _build_word_config
                        import re as _re_gr
                        gr_name = _re_gr.sub(r"[^a-zA-Z0-9_-]", "-", f"agentcore-{runtime_config.name}-guardrail")[:64]
                        create_params: dict = {"name": gr_name, "blockedInputMessaging": "Request blocked by guardrail.", "blockedOutputsMessaging": "Response blocked by guardrail."}
                        cf = _build_content_filter_config(guardrails_config.get("contentFilters") or guardrails_config.get("content_filters") or {})
                        if cf:
                            create_params["contentPolicyConfig"] = cf
                        pii = _build_pii_config(guardrails_config.get("piiFilters") or guardrails_config.get("pii_filters") or [])
                        if pii:
                            create_params["sensitiveInformationPolicyConfig"] = pii
                        topics = _build_topic_config(guardrails_config.get("deniedTopics") or guardrails_config.get("denied_topics") or [])
                        if topics:
                            create_params["topicPolicyConfig"] = topics
                        words = _build_word_config(guardrails_config.get("wordFilters") or guardrails_config.get("word_filters") or [])
                        if words:
                            create_params["wordPolicyConfig"] = words
                        cr_resp = bedrock_guardrails.create_guardrail(**create_params)
                        gid = cr_resp.get("guardrailId", "")
                        # Wait for READY
                        import time as _time_gr
                        for _ in range(24):
                            gs = bedrock_guardrails.get_guardrail(guardrailIdentifier=gid)
                            if gs.get("status") == "READY":
                                break
                            _time_gr.sleep(5)
                        ver_resp = bedrock_guardrails.create_guardrail_version(guardrailIdentifier=gid)
                        gver = ver_resp.get("version", "1")
                        guardrails_result = {"guardrail_id": gid, "guardrail_version": gver, "created_by_flow": True}
                        logger.warning("Created guardrail: %s (v%s)", gid, gver)
                except Exception as gr_err:
                    logger.warning("Guardrails deployment failed (non-fatal): %s", gr_err)

            # ------------------------------------------------------------------
            # Phase 2: Generate agent code (gateway-aware if gateway was deployed)
            # ------------------------------------------------------------------
            state.phase = DeploymentPhase.GENERATING_CODE

            work_dir = tempfile.mkdtemp(prefix=f"agentcore-{workflow.id}-")
            state.work_dir = work_dir

            # Create memory resource if memory is connected
            memory_id: Optional[str] = None
            if "memory" in connected_tools:
                try:
                    import json as _json

                    memory_client = boto3.client(
                        "bedrock-agentcore-control",
                        region_name=self.region,
                    )
                    iam_client = boto3.client("iam")
                    # Memory names must match [a-zA-Z][a-zA-Z0-9_]{0,47}
                    import re as _re

                    raw_mem_name = f"{runtime_config.name}_memory"
                    mem_name = _re.sub(r"[^a-zA-Z0-9_]", "_", raw_mem_name)
                    mem_name = _re.sub(r"_+", "_", mem_name).strip("_")[:48]
                    if not mem_name or not mem_name[0].isalpha():
                        mem_name = "M" + mem_name

                    # Check if memory already exists
                    # NOTE: list_memories items do NOT have a 'name' field —
                    # only arn, id, status, createdAt, updatedAt.
                    # The name is embedded as id prefix: "{name}-{suffix}"
                    try:
                        mems = memory_client.list_memories(maxResults=100)
                        mems_keys = [k for k in mems.keys() if k != "ResponseMetadata"]
                        for k in mems_keys:
                            v = mems[k]
                            if not isinstance(v, list):
                                continue
                            for m in v:
                                if not isinstance(m, dict):
                                    continue
                                m_id = m.get("id") or m.get("memoryId") or ""
                                if m.get("name") == mem_name or m_id.startswith(f"{mem_name}-") or m_id == mem_name:
                                    memory_id = m_id
                                    if not memory_id:
                                        arn = m.get("arn", m.get("memoryArn", ""))
                                        if ":memory/" in arn:
                                            memory_id = arn.split(":memory/")[-1]
                                    logger.info(
                                        "Found existing memory '%s': %s",
                                        mem_name,
                                        memory_id,
                                    )
                                    break
                            if memory_id:
                                break
                    except Exception as exc:
                        logger.warning("Could not list memories: %s", exc)

                    if not memory_id:
                        # Create IAM role for memory
                        memory_role_name = f"AgentCoreMemory-{mem_name}"
                        trust_policy = {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Effect": "Allow",
                                    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                                    "Action": "sts:AssumeRole",
                                }
                            ],
                        }
                        try:
                            role_resp = iam_client.create_role(
                                RoleName=memory_role_name,
                                AssumeRolePolicyDocument=_json.dumps(trust_policy),
                                Description=f"Memory execution role for {mem_name}",
                            )
                            memory_role_arn = role_resp["Role"]["Arn"]
                            iam_client.put_role_policy(
                                RoleName=memory_role_name,
                                PolicyName="MemoryExecutionPolicy",
                                PolicyDocument=_json.dumps(
                                    {
                                        "Version": "2012-10-17",
                                        "Statement": [
                                            {
                                                "Effect": "Allow",
                                                "Action": [
                                                    "bedrock:InvokeModel",
                                                    "bedrock:InvokeModelWithResponseStream",
                                                    "bedrock-agentcore:*",
                                                    "bedrock-agentcore-control:*",
                                                ],
                                                "Resource": "*",
                                            }
                                        ],
                                    }
                                ),
                            )
                            import time as _time

                            _time.sleep(10)  # IAM propagation
                            _record_resource_best_effort(
                                deployment_id, self.region,
                                {"type": "iam_role", "name": memory_role_name},
                            )
                        except iam_client.exceptions.EntityAlreadyExistsException:
                            memory_role_arn = iam_client.get_role(RoleName=memory_role_name)["Role"]["Arn"]

                        create_params = {
                            "clientToken": str(uuid.uuid4()),
                            "name": mem_name,
                            "description": f"Memory for {runtime_config.name}",
                            "memoryExecutionRoleArn": memory_role_arn,
                            "memoryStrategies": [],
                            "eventExpiryDuration": (memory_config or {}).get("eventExpiryDuration", 90),
                        }

                        # Add strategies from frontend memory_config
                        # AWS API expects keys: semanticMemoryStrategy, summaryMemoryStrategy, etc.
                        _STRATEGY_KEY_MAP = {
                            "semantic": "semanticMemoryStrategy",
                            "summary": "summaryMemoryStrategy",
                            "episodic": "episodicMemoryStrategy",
                            "user_preferences": "userPreferenceMemoryStrategy",
                            "custom": "customMemoryStrategy",
                        }
                        mem_cfg = memory_config
                        if not mem_cfg:
                            mem_cfg = config.extra.get("memory_config") if hasattr(config, "extra") else None
                        if not mem_cfg:
                            mem_node = self._find_component(workflow, AgentCoreComponentType.MEMORY)
                            if mem_node and hasattr(mem_node.data, "__dict__"):
                                mem_cfg = getattr(mem_node.data, "_raw_config", None)
                        if isinstance(mem_cfg, dict):
                            strategies = mem_cfg.get("strategies", [])
                            if strategies:
                                mem_strategies = []
                                for strat in strategies:
                                    strat_type = strat.get("type", "semantic").lower()
                                    api_key = _STRATEGY_KEY_MAP.get(strat_type)
                                    if not api_key:
                                        logger.warning(
                                            "Unknown strategy type '%s', skipping",
                                            strat_type,
                                        )
                                        continue
                                    # Strategy names: [a-zA-Z][a-zA-Z0-9_]{0,47} — no hyphens
                                    raw_sname = strat.get("name", f"{mem_name}_{strat_type}")
                                    safe_sname = _re.sub(r"[^a-zA-Z0-9_]", "_", raw_sname)
                                    safe_sname = _re.sub(r"_+", "_", safe_sname).strip("_")[:48]
                                    if not safe_sname or not safe_sname[0].isalpha():
                                        safe_sname = "S" + safe_sname
                                    mem_strategies.append(
                                        {
                                            api_key: {
                                                "name": safe_sname,
                                                "description": strat.get(
                                                    "description",
                                                    f"{strat_type} strategy",
                                                ),
                                                "namespaces": strat.get(
                                                    "namespaces",
                                                    [f"agent/{{actorId}}/{strat_type}/"],
                                                ),
                                            }
                                        }
                                    )
                                create_params["memoryStrategies"] = mem_strategies

                        mem_resp = memory_client.create_memory(**create_params)
                        resp_keys = [k for k in mem_resp.keys() if k != "ResponseMetadata"]
                        logger.info("create_memory response keys: %s", resp_keys)
                        # Try multiple key patterns
                        for _k in ("memoryId", "id", "memory_id"):
                            memory_id = mem_resp.get(_k, "")
                            if memory_id and len(memory_id) >= 12:
                                break
                        if not memory_id:
                            nested = mem_resp.get("memory", {})
                            if isinstance(nested, dict):
                                memory_id = nested.get("memoryId", nested.get("id", ""))
                        if not memory_id:
                            # Extract from ARN
                            for _k in ("arn", "memoryArn"):
                                arn = mem_resp.get(_k, "")
                                if ":memory/" in arn:
                                    memory_id = arn.split(":memory/")[-1]
                                    break
                        logger.info("Created memory resource: %s", memory_id)
                        if memory_id:
                            _record_resource_best_effort(
                                deployment_id, self.region,
                                {"type": "memory", "id": memory_id},
                            )

                        if memory_id:
                            # Wait for memory to become ACTIVE
                            for _ in range(24):  # up to 120s
                                try:
                                    mem_status = memory_client.get_memory(memoryId=memory_id)
                                    status = mem_status.get("status", "")
                                    if status in ("ACTIVE", "READY"):
                                        logger.info("Memory %s is %s", memory_id, status)
                                        break
                                    if "FAILED" in status:
                                        logger.warning("Memory %s entered %s", memory_id, status)
                                        break
                                except Exception as exc:
                                    logger.warning("Could not poll memory status: %s", exc)
                                import time as _time

                                _time.sleep(5)
                        else:
                            logger.warning("create_memory succeeded but could not extract ID from response")

                except Exception as mem_err:
                    if "already exists" in str(mem_err).lower() or "conflict" in str(mem_err).lower():
                        try:
                            import time as _time

                            _time.sleep(3)  # eventual consistency
                            mems = memory_client.list_memories(maxResults=100)
                            mems_keys = [k for k in mems.keys() if k != "ResponseMetadata"]
                            for k in mems_keys:
                                v = mems[k]
                                if not isinstance(v, list):
                                    continue
                                for m in v:
                                    if not isinstance(m, dict):
                                        continue
                                    m_id = m.get("id") or m.get("memoryId") or ""
                                    if m.get("name") == mem_name or m_id.startswith(f"{mem_name}-") or m_id == mem_name:
                                        memory_id = m_id
                                        if not memory_id:
                                            arn = m.get("arn", m.get("memoryArn", ""))
                                            if ":memory/" in arn:
                                                memory_id = arn.split(":memory/")[-1]
                                        break
                                if memory_id:
                                    break
                        except Exception as exc2:
                            logger.warning("Could not list memories after conflict: %s", exc2)
                    if not memory_id:
                        logger.warning("Failed to create memory: %s", mem_err)

            # Knowledge Base deployment via direct path is not supported —
            # KB creation requires the full SFN step handler (knowledge_base_step.py).
            # Log a warning so users know to use the SFN deploy path.
            if knowledge_base_config and "knowledge_base" in connected_tools:
                logger.warning(
                    "Knowledge Base deployment is only supported via the SFN deploy path. "
                    "Use the deployed platform (not local dev) to deploy Knowledge Base configurations."
                )

            # ------------------------------------------------------------------
            # Phase B branch — AgentCore Harness (managed authoring path).
            # When deployment_mode == "harness" the shared steps above (gateway,
            # memory) have already run, so the harness can wire a connected
            # gateway + memory. We SKIP codegen / S3 upload / runtime IAM /
            # runtime configure+launch entirely and instead create+wait a
            # managed Harness, mirroring how this direct path calls
            # runtime_deployer for the default runtime mode. Bug 9: this is the
            # direct-path twin of step_handlers/harness_step.py.
            # ------------------------------------------------------------------
            if deployment_mode == "harness":
                from app.services import harness_deployer

                harness_name = harness_deployer.sanitize_harness_name(runtime_config.name)
                gateway_arn = None
                if gateway_result:
                    gateway_arn = gateway_result.get("gateway_arn") or gateway_result.get("arn")

                memory_arn = None
                if memory_id:
                    try:
                        sts = boto3.client("sts")
                        account_id = sts.get_caller_identity()["Account"]
                        memory_arn = (
                            f"arn:aws:bedrock-agentcore:{self.region}:{account_id}:memory/{memory_id}"
                        )
                    except Exception:
                        logger.warning("Could not resolve memory ARN for harness")

                iam_client = boto3.client("iam")
                harness_role_arn = harness_deployer.get_shared_or_new_harness_role(
                    iam_client, harness_name
                )

                agentcore_ctrl = boto3.client("bedrock-agentcore-control", region_name=self.region)
                create_result = harness_deployer.create_harness(
                    agentcore_ctrl,
                    harness_name,
                    harness_role_arn,
                    model_id=runtime_config.model.model_id or None,
                    system_prompt=runtime_config.system_prompt or None,
                    gateway_arn=gateway_arn,
                    memory_arn=memory_arn,
                )
                harness_id = create_result.get("harness_id", "")
                if not harness_id:
                    raise RuntimeError("create_harness returned no harness_id")
                early_harness_arn = create_result.get("arn", "")

                # Bug 9 parity: mirror harness_step's manifest writes.
                _record_resource_best_effort(
                    deployment_id, self.region,
                    {"type": "harness", "id": harness_id},
                )
                if not os.environ.get("SHARED_HARNESS_ROLE_ARN", ""):
                    _record_resource_best_effort(
                        deployment_id, self.region,
                        {"type": "iam_role", "name": f"AgentCoreHarness-{harness_name}"},
                    )

                # ORPHAN GUARD (Bug 153): the AWS harness resource now exists.
                # wait_for_harness_ready can run for up to 600s; persist the
                # destroyable handle (harness_id/arn, mirrored into
                # runtime_id/runtime_arn for the GSI lookup) onto the record NOW
                # with status IN_PROGRESS, so a failure/timeout during the wait
                # still leaves the delete path something to clean up.
                try:
                    from app.services.deployment_state_store import DeploymentStateStore as _DSS

                    _early_store = _DSS(
                        table_name=os.environ.get(
                            "DEPLOYMENT_TABLE_NAME",
                            os.environ.get("DEPLOYMENTS_TABLE_NAME", "DeploymentState"),
                        ),
                        region=self.region,
                    )
                    if _early_store.get(deployment_id) is not None:
                        from app.models.deployment_models import DeploymentStatusEnum as _DSE

                        _early_store.update_status(
                            deployment_id,
                            _DSE.IN_PROGRESS,
                            runtime_id=harness_id,
                            runtime_arn=early_harness_arn or None,
                            harness_id=harness_id,
                            harness_arn=early_harness_arn or None,
                            deployment_mode="harness",
                        )
                except Exception:
                    logger.warning(
                        "Could not pre-persist harness handle for %s (orphan guard)",
                        deployment_id,
                        exc_info=True,
                    )

                ready = harness_deployer.wait_for_harness_ready(agentcore_ctrl, harness_id)
                if not ready.get("success"):
                    raise RuntimeError(
                        f"Harness failed to become ready: {ready.get('error', 'unknown error')}"
                    )
                harness_arn = ready.get("arn") or create_result.get("arn", "")

                state.phase = DeploymentPhase.COMPLETED
                state.runtime_id = harness_id
                state.endpoint_url = harness_arn
                state.completed_at = datetime.now(timezone.utc)

                created_resources = [f"harness:{harness_name}"]
                if gateway_result:
                    created_resources.append(f"gateway:{gateway_result.get('gateway_url', '')}")

                # Persist harness id/arn + deployment_mode to the record so the
                # delete/test paths route to harness_deployer.
                try:
                    from app.services.deployment_state_store import DeploymentStateStore as _DSS

                    _store = _DSS(
                        table_name=os.environ.get(
                            "DEPLOYMENT_TABLE_NAME",
                            os.environ.get("DEPLOYMENTS_TABLE_NAME", "DeploymentState"),
                        ),
                        region=self.region,
                    )
                    if _store.get(deployment_id) is not None:
                        from app.models.deployment_models import DeploymentStatusEnum as _DSE

                        _store.update_status(
                            deployment_id,
                            _DSE.SUCCEEDED,
                            completed_at=state.completed_at,
                            runtime_endpoint=harness_arn,
                            # Mirror the SFN path: persist the harness handle into
                            # the runtime_id/runtime_arn fields so the DELETE /
                            # test-runtime GSI lookup finds the record and the
                            # deployment_mode branch routes to harness_deployer.
                            runtime_id=harness_id,
                            runtime_arn=harness_arn,
                            harness_id=harness_id,
                            harness_arn=harness_arn,
                            deployment_mode="harness",
                            gateway_result=gateway_result or None,
                        )
                except Exception:
                    logger.warning(
                        "Could not persist harness record for %s", deployment_id, exc_info=True
                    )

                return DeploymentResult(
                    deployment_id=deployment_id,
                    status="success",
                    endpoint_url=harness_arn,
                    created_resources=created_resources,
                    runtime_id=harness_id,
                )

            if template_id:
                from app.services.code_generator import (
                    generate_agent_code as cg_generate_agent_code,
                )
                from app.models.deployment_models import (
                    RuntimeConfig as CgRuntimeConfig,
                )

                cg_config = CgRuntimeConfig(
                    name=runtime_config.name,
                    framework=runtime_config.framework.value,
                    model={
                        "modelId": runtime_config.model.model_id,
                        "provider": runtime_config.model_provider.value
                        if hasattr(runtime_config.model_provider, "value")
                        else "bedrock",
                    },
                    system_prompt=runtime_config.system_prompt,
                    model_provider=runtime_config.model_provider.value
                    if hasattr(runtime_config.model_provider, "value")
                    else "bedrock",
                )
                # OTEL: enabled when ANY of:
                #   - platform-level OTEL defaults are configured (Reading A)
                #   - per-canvas Observability node is wired
                #   - observability_config supplied directly
                #   - legacy enable_otel flag set
                _obs_enabled = bool(
                    get_platform_observability_defaults()
                    or observability_config
                    or "observability" in (connected_tools or [])
                    or getattr(runtime_config, "enable_otel", False)
                )
                agent_code = cg_generate_agent_code(
                    config=cg_config,
                    template_id=template_id,
                    gateway_config=gateway_result,
                    tools=connected_tools,
                    gateway_tools=gateway_tools,
                    custom_tools=custom_tools,
                    observability_enabled=_obs_enabled,
                )
            else:
                # Use unified generator with ALL connected components
                agent_code = generate_unified_agent_code(
                    runtime_config,
                    connected_tools=connected_tools,
                    gateway_result=gateway_result,
                    memory_id=memory_id,
                    region=self.region,
                )
                if (
                    get_platform_observability_defaults()
                    or observability_config
                    or "observability" in (connected_tools or [])
                    or getattr(runtime_config, "enable_otel", False)
                ):
                    from app.services.code_generator import _inject_otel
                    agent_code = _inject_otel(agent_code)

            # Write agent code to temp dir
            agent_path = Path(work_dir) / "agent.py"
            agent_path.write_text(agent_code)
            logger.info("Generated agent code at %s", agent_path)

            # ------------------------------------------------------------------
            # Phase 3: Upload code + bundled deps to S3
            # ------------------------------------------------------------------
            # Stable prefix keyed on runtime name (Bug 61) — AgentCore IAM
            # cache is keyed on (role, S3 prefix). Reusing the same prefix
            # across redeploys means the cache populates once per agent.
            bucket = os.environ.get("ARTIFACTS_BUCKET_NAME", "")
            s3_key = f"deployments/by-name/{runtime_deployer.sanitize_runtime_name(runtime_config.name)}/code.zip"

            from app.services.code_generator import generate_requirements as cg_gen_reqs
            from app.models.deployment_models import RuntimeConfig as ReqsConfig

            reqs_config = ReqsConfig.model_validate(
                {
                    "name": runtime_config.name,
                    "framework": runtime_config.framework,
                    "model": {"modelId": runtime_config.model.model_id},
                }
            )
            requirements_txt = cg_gen_reqs(
                config=reqs_config,
                tools=connected_tools,
                template_id=template_id,
            )

            if bucket:
                s3_client = boto3.client("s3", region_name=self.region)

                needs_strands = "from strands " in agent_code or "import strands" in agent_code
                bundle_key = "agentcore-deps/strands-mcp.zip" if needs_strands else "agentcore-deps/base.zip"
                deps_bundle = None
                try:
                    resp = s3_client.get_object(Bucket=bucket, Key=bundle_key)
                    deps_bundle = resp["Body"].read()
                    logger.info(
                        "Downloaded deps bundle: %s (%d bytes)",
                        bundle_key,
                        len(deps_bundle),
                    )
                except Exception as e:
                    logger.warning("Failed to download deps bundle %s: %s", bundle_key, e)

                upload_code_to_s3(
                    s3_client,
                    bucket,
                    s3_key,
                    agent_code,
                    requirements_txt,
                    "agent.py",
                    deps_bundle=deps_bundle,
                )
                logger.info("Uploaded code zip to s3://%s/%s", bucket, s3_key)
            else:
                logger.warning("No ARTIFACTS_BUCKET_NAME set, skipping S3 upload")

            # ------------------------------------------------------------------
            # Phase 4: Create IAM role and Runtime
            # ------------------------------------------------------------------
            state.phase = DeploymentPhase.CONFIGURING

            sts = boto3.client("sts")
            account_id = sts.get_caller_identity()["Account"]

            iam_client = boto3.client("iam")
            # Platform-level OTEL secret (when configured) takes precedence
            # over per-canvas, matching build_otel_env_vars's lock semantics.
            _platform = get_platform_observability_defaults()
            if _platform and _platform.get("auth_header_secret_arn"):
                _otel_secret = _platform["auth_header_secret_arn"]
            else:
                _otel_secret = (observability_config or {}).get("auth_header_secret_arn") \
                    or (observability_config or {}).get("authHeaderSecretArn")
            _runtime_role_name = f"{runtime_config.name}-role"
            role_arn = create_runtime_iam_role(
                iam_client,
                _runtime_role_name,
                account_id,
                self.region,
                connected_tools,
                otel_secret_arn=_otel_secret,
            )
            _record_resource_best_effort(
                deployment_id, self.region,
                {"type": "iam_role", "name": _runtime_role_name},
            )

            agentcore_ctrl = boto3.client("bedrock-agentcore-control", region_name=self.region)

            # Build env vars — inject gateway credentials if gateway was deployed
            env_vars: dict[str, str] = {
                "AWS_REGION": self.region,
                "MODEL_ID": runtime_config.model.model_id,
                "SYSTEM_PROMPT": runtime_config.system_prompt,
            }

            if gateway_result:
                client_info = gateway_result.get("client_info", {})
                env_vars["GATEWAY_URL"] = gateway_result.get("gateway_url", "")
                env_vars["COGNITO_CLIENT_ID"] = client_info.get("client_id", "")
                env_vars["COGNITO_CLIENT_SECRET"] = client_info.get("client_secret", "")
                env_vars["COGNITO_TOKEN_ENDPOINT"] = client_info.get("token_endpoint", "")
                env_vars["COGNITO_SCOPE"] = client_info.get("scope", "")

            if memory_id:
                env_vars["MEMORY_ID"] = memory_id

            if "code_interpreter" in connected_tools:
                env_vars["AGENTCORE_CODE_INTERPRETER"] = "enabled"

            if "browser" in connected_tools:
                env_vars["AGENTCORE_BROWSER"] = "enabled"

            if guardrails_result.get("guardrail_id"):
                env_vars["GUARDRAIL_ID"] = guardrails_result["guardrail_id"]
                env_vars["GUARDRAIL_VERSION"] = guardrails_result.get("guardrail_version", "DRAFT")

            otel_env = build_otel_env_vars(
                observability_config,
                runtime_name=runtime_config.name,
                deployment_id=deployment_id,
                enable_otel_legacy=bool(getattr(runtime_config, "enable_otel", False)),
                platform_defaults=get_platform_observability_defaults(),
            )
            env_vars.update(otel_env)

            runtime_result = create_agent_runtime(
                agentcore_ctrl,
                runtime_deployer.sanitize_runtime_name(runtime_config.name),
                role_arn,
                bucket,
                s3_key,
                "agent.py",
                runtime_config.python_runtime.value
                if hasattr(runtime_config.python_runtime, "value")
                else "PYTHON_3_13",
                runtime_config.protocol.value if hasattr(runtime_config.protocol, "value") else "HTTP",
                env_vars,
            )
            state.runtime_id = runtime_result["runtime_id"]
            _record_resource_best_effort(
                deployment_id, self.region,
                {"type": "agent_runtime", "id": state.runtime_id},
            )

            # ------------------------------------------------------------------
            # Phase 5: Wait for runtime to become ready
            # ------------------------------------------------------------------
            state.phase = DeploymentPhase.LAUNCHING

            launch_result = wait_for_runtime_ready(agentcore_ctrl, state.runtime_id)
            if not launch_result.get("success"):
                raise RuntimeError(launch_result.get("error", "Runtime failed to become ready"))

            try:
                endpoints_resp = agentcore_ctrl.list_agent_runtime_endpoints(agentRuntimeId=state.runtime_id)
                endpoints = endpoints_resp.get("agentRuntimeEndpoints", endpoints_resp.get("endpoints", []))
                if endpoints:
                    endpoint_url = endpoints[0].get("url", endpoints[0].get("endpoint", ""))
                else:
                    endpoint_url = f"https://{runtime_config.name}.agentcore.{self.region}.amazonaws.com"
            except Exception:
                endpoint_url = f"https://{runtime_config.name}.agentcore.{self.region}.amazonaws.com"

            # Complete
            state.phase = DeploymentPhase.COMPLETED
            state.endpoint_url = endpoint_url
            state.completed_at = datetime.now(timezone.utc)

            created_resources = [f"agent:{runtime_config.name}"]
            if gateway_result:
                gw_url = gateway_result.get("gateway_url", "")
                created_resources.append(f"gateway:{gw_url}")

            return DeploymentResult(
                deployment_id=deployment_id,
                status="success",
                endpoint_url=endpoint_url,
                created_resources=created_resources,
                runtime_id=state.runtime_id,
            )

        except Exception as e:
            logger.error(f"Deployment failed: {e}")
            state.error_message = str(e)
            state.phase = DeploymentPhase.FAILED

            return DeploymentResult(
                deployment_id=deployment_id,
                status="failed",
                error_message=str(e),
                created_resources=[],
            )

    def _find_component(
        self, workflow: WorkflowDefinition, component_type: AgentCoreComponentType
    ) -> Optional[ComponentNode]:
        """Find a component of the specified type in the workflow."""
        for node in workflow.nodes:
            if node.type == component_type:
                return node
        return None

    async def rollback(self, deployment_id: str) -> RollbackResult:
        """Rollback a deployment by destroying the agent."""
        state = self._deployments.get(deployment_id)
        if state is None:
            return RollbackResult(
                success=False,
                errors=[
                    RollbackError(
                        resource_arn="unknown",
                        error_message=f"Deployment {deployment_id} not found",
                    )
                ],
            )

        state.phase = DeploymentPhase.ROLLING_BACK
        errors: list[RollbackError] = []

        # Destroy agent runtime via boto3
        if state.runtime_id:
            try:
                result = destroy_runtime(state.runtime_id, self.region)
                if not result.get("success"):
                    errors.append(
                        RollbackError(
                            resource_arn=f"runtime:{state.runtime_id}",
                            error_message=result.get("message", "Unknown error"),
                        )
                    )
            except Exception as e:
                errors.append(
                    RollbackError(
                        resource_arn=f"runtime:{state.runtime_id}",
                        error_message=str(e),
                    )
                )

        # Destroy gateway using boto3 (no CLI delete command exists)
        if state.gateway_name:
            try:
                import boto3

                control_client = boto3.client("bedrock-agentcore-control", region_name=self.region)
                # List gateways to find the one matching by name
                gateways = control_client.list_gateways()
                for gw in gateways.get(
                    "items",
                    gateways.get("gateways", gateways.get("gatewaySummaries", [])),
                ):
                    if gw.get("name") == state.gateway_name:
                        # Delete all targets first
                        targets = control_client.list_gateway_targets(gatewayIdentifier=gw["gatewayId"])
                        for target in targets.get(
                            "items",
                            targets.get("targets", targets.get("gatewayTargetSummaries", [])),
                        ):
                            control_client.delete_gateway_target(
                                gatewayIdentifier=gw["gatewayId"],
                                targetId=target["targetId"],
                            )
                        # Delete the gateway
                        control_client.delete_gateway(gatewayIdentifier=gw["gatewayId"])
                        logger.info(f"Deleted gateway: {state.gateway_name}")
                        break

            except Exception as e:
                errors.append(
                    RollbackError(
                        resource_arn=f"gateway:{state.gateway_name}",
                        error_message=str(e),
                    )
                )

        return RollbackResult(
            success=len(errors) == 0,
            errors=errors,
        )

    def get_deployment_status(self, deployment_id: str) -> Optional[DeploymentState]:
        """Get the current status of a deployment."""
        return self._deployments.get(deployment_id)
