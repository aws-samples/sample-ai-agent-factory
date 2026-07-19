"""IAM completeness / fan-out shift-left tests.

These tests parse infra/stacks/platform_stack.py as TEXT (no CDK synth, no AWS)
and assert that each per-step IAM action set in the ``agentcore_steps`` dict
includes the documented "fan-out" verbs that CreateHarness / CreateGateway /
CreateMemory transparently invoke under the hood.

Background — three live AccessDenied bugs this guards against:
  * Bug 151: CreateHarness internally calls CreateAgentRuntime, so the harness
    step role needs the AgentRuntime lifecycle verbs.
  * Bug 152: CreateHarness auto-provisions a default Memory, so the harness step
    role needs CreateMemory.
  * Bug 153: the first CreateOauth2CredentialProvider in a region implicitly
    provisions the default token-vault, so the caller needs CreateTokenVault.

Catching these as a unit assertion is far cheaper than a failed live deploy.

A second test guards the gateway_deployer embedded tool lambda: on tool failure
it should surface a STRUCTURED error shape (``tool_unavailable``) so the agent
can react instead of swallowing a raw stack trace.
"""

import ast
import re
from pathlib import Path

import pytest

# backend/tests/ -> backend/ -> repo root -> infra/stacks/. The platform stack
# was split from a single platform_stack.py into the platform/ package
# (commit "refactor(infra): split 4,430-line platform_stack.py"), so the IAM
# text assertions scan platform_stack.py PLUS every module in platform/.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_STACKS_DIR = _REPO_ROOT / "infra" / "stacks"
_PLATFORM_STACK = _STACKS_DIR / "platform_stack.py"


def _platform_source_files() -> list[Path]:
    files = [_PLATFORM_STACK] if _PLATFORM_STACK.is_file() else []
    files += sorted((_STACKS_DIR / "platform").glob("*.py"))
    assert files, f"no platform stack sources found under {_STACKS_DIR}"
    return files


def _platform_source() -> str:
    return "\n".join(p.read_text() for p in _platform_source_files())


def _agentcore_steps_source() -> str:
    """Return the source text of the ``agentcore_steps = {...}`` dict literal.

    Uses AST to locate the assignment precisely (resilient to surrounding code
    moving), then slices the literal out of the raw source so substring checks
    work regardless of formatting/comments inside each block.
    """
    for path in _platform_source_files():
        source = path.read_text()
        tree = ast.parse(source)
        lines = source.splitlines()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if "agentcore_steps" in targets:
                    # ast end_lineno is inclusive and 1-based.
                    return "\n".join(lines[node.lineno - 1 : node.end_lineno])
    raise AssertionError("could not locate `agentcore_steps = {...}` dict in the platform stack sources")


def _block_source(steps_source: str, key: str) -> str:
    """Slice a single ``"<key>": [ ... ]`` block out of the steps dict source.

    Block ends at the first line that closes the list (``],`` / ``]``) at the
    same or lower indentation than the opening key, which is robust to the
    multi-line, comment-heavy action lists in this file.
    """
    lines = steps_source.splitlines()
    start = None
    key_indent = 0
    for i, line in enumerate(lines):
        m = re.match(rf'^(\s*)"{re.escape(key)}"\s*:\s*\[', line)
        if m:
            start = i
            key_indent = len(m.group(1))
            break
    assert start is not None, f'block "{key}": [...] not found in agentcore_steps'

    for j in range(start + 1, len(lines)):
        stripped = lines[j].lstrip()
        indent = len(lines[j]) - len(stripped)
        if stripped.startswith("]") and indent <= key_indent:
            return "\n".join(lines[start : j + 1])
    # Fall back to the rest of the dict if no closer found (shouldn't happen).
    return "\n".join(lines[start:])


# Required fan-out actions per step. Each entry is asserted as a substring
# within that step's block in agentcore_steps. Get/Delete variants are checked
# alongside the Create verbs the contract calls out.
_REQUIRED = {
    "harness": [
        "bedrock-agentcore:CreateHarness",
        "bedrock-agentcore:GetHarness",
        "bedrock-agentcore:DeleteHarness",
        # Bug 151 — Harness is built on a Runtime.
        "bedrock-agentcore:CreateAgentRuntime",
        "bedrock-agentcore:GetAgentRuntime",
        "bedrock-agentcore:DeleteAgentRuntime",
        # Bug 153 — first OAuth2 cred provider provisions the token vault.
        "bedrock-agentcore:CreateTokenVault",
        "bedrock-agentcore:GetTokenVault",
        # Bug 150 — connected gateway needs an outbound OAuth2 cred provider.
        "bedrock-agentcore:CreateOauth2CredentialProvider",
        # Bug 152 — CreateHarness auto-provisions a default Memory.
        "bedrock-agentcore:CreateMemory",
        "bedrock-agentcore:GetMemory",
        "bedrock-agentcore:DeleteMemory",
    ],
    "gateway": [
        "bedrock-agentcore:CreateApiKeyCredentialProvider",
        "bedrock-agentcore:CreateOauth2CredentialProvider",
        "bedrock-agentcore:SynchronizeGatewayTargets",
    ],
    "memory": [
        "bedrock-agentcore:CreateMemory",
    ],
}


@pytest.mark.parametrize(
    "step,action",
    [(step, action) for step, actions in _REQUIRED.items() for action in actions],
)
def test_agentcore_step_grants_fanout_action(step, action):
    """Each documented fan-out action must appear in its step's IAM block."""
    steps_source = _agentcore_steps_source()
    block = _block_source(steps_source, step)
    assert action in block, (
        f'IAM completeness gap: step "{step}" is missing the required action '
        f'"{action}". CreateHarness/CreateGateway/CreateMemory fan out to this '
        f"verb under the hood; omitting it causes a live AccessDenied deploy "
        f'failure (Bug 151/152/153 class). Add it to agentcore_steps["{step}"] '
        f"in infra/stacks/platform_stack.py."
    )


def test_harness_block_holds_full_runtime_and_memory_lifecycle():
    """Sanity: the harness block carries BOTH runtime and memory lifecycles.

    Belt-and-suspenders over the parametrized check — a single assertion that
    the harness step is self-sufficient for the resources CreateHarness
    transparently creates (Bug 151 runtime + Bug 152 memory + Bug 153 vault).
    """
    block = _block_source(_agentcore_steps_source(), "harness")
    runtime_verbs = ["CreateAgentRuntime", "GetAgentRuntime", "DeleteAgentRuntime"]
    memory_verbs = ["CreateMemory", "GetMemory", "DeleteMemory"]
    missing = [v for v in runtime_verbs + memory_verbs + ["CreateTokenVault"] if f"bedrock-agentcore:{v}" not in block]
    assert not missing, (
        f"harness step IAM block is missing transparently-required verbs: {missing}. See Bug 151/152/153."
    )


# --------------------------------------------------------------------------- #
# gateway_deployer embedded tool lambda: structured failure shape             #
# --------------------------------------------------------------------------- #


def _dynamic_tools_code() -> str:
    from app.services.gateway_deployer import DYNAMIC_TOOLS_LAMBDA_CODE

    return DYNAMIC_TOOLS_LAMBDA_CODE


def test_dynamic_tools_lambda_has_retry_logic():
    """The embedded tool lambda must retry transient HTTP failures.

    This is the always-true floor of the contract: outbound tool calls
    (search/wikipedia/weather/fetch) go through a helper with bounded retries
    so a single network blip doesn't surface as a hard tool failure.
    """
    code = _dynamic_tools_code()
    assert "retries" in code and "time.sleep" in code, (
        "DYNAMIC_TOOLS_LAMBDA_CODE lost its HTTP retry logic (_http_get retries + backoff)."
    )


def test_dynamic_tools_lambda_returns_structured_unavailable_error():
    """On tool failure the lambda should emit a STRUCTURED error shape.

    The backend agent is standardizing this on the substring ``tool_unavailable``
    so the agent runtime can distinguish a recoverable tool outage from a bad
    request. Until that lands, this test documents the gap as an xfail rather
    than blocking the suite (per the improvements contract).
    """
    code = _dynamic_tools_code()
    if "tool_unavailable" not in code:
        pytest.xfail(
            "GAP: DYNAMIC_TOOLS_LAMBDA_CODE does not yet emit a structured "
            "'tool_unavailable' error shape on failure — it returns a bare "
            "{'error': str(e)} from lambda_handler. Coordinate with the backend "
            "agent to standardize the failure payload; this test flips to "
            "passing once 'tool_unavailable' is present."
        )
    assert "tool_unavailable" in code


# ---------------------------------------------------------------------------
# Manifest teardown completeness: the deployment Lambda role (which runs the
# _delete_managed_resource dispatcher) must be able to DELETE every resource
# type the dispatcher handles. Bug 165: the manifest added a `guardrail` case
# but the deployment role lacked bedrock:DeleteGuardrail -> orphan on delete.
# ---------------------------------------------------------------------------

# Each manifest dispatcher type -> the IAM delete action the delete handler calls.
_MANIFEST_DELETE_ACTIONS = [
    "bedrock-agentcore:DeleteAgentRuntime",  # agent_runtime
    "bedrock-agentcore:DeleteHarness",  # harness
    "bedrock-agentcore:DeleteMemory",  # memory
    "bedrock-agentcore:DeleteGateway",  # gateway
    "bedrock-agentcore:DeleteOauth2CredentialProvider",  # oauth2_credential_provider
    "bedrock-agentcore:DeleteApiKeyCredentialProvider",  # api_key_credential_provider
    "bedrock-agentcore:DeletePolicyEngine",  # policy_engine
    "bedrock:DeleteGuardrail",  # guardrail (Bug 165)
    "secretsmanager:DeleteSecret",  # secret
    "s3vectors:DeleteVectorBucket",  # s3_vectors_bucket (Bug 167)
    "s3vectors:DeleteIndex",  # s3_vectors_bucket indexes (Bug 167)
    "bedrock:DeleteKnowledgeBase",  # knowledge_base manifest type (Bug 167)
    "bedrock:DeleteDataSource",  # knowledge_base data sources (Bug 167)
]


@pytest.mark.parametrize("action", _MANIFEST_DELETE_ACTIONS)
def test_manifest_delete_action_is_granted_somewhere(action):
    """Every delete verb the manifest dispatcher invokes must be granted in IAM.

    The deployment Lambda runs _delete_managed_resource; if it lacks one of these
    the corresponding resource orphans with AccessDenied on teardown. We assert the
    action string is present in platform_stack.py (granted to the deployment/
    delete role). iam_role / lambda / cognito_user_pool deletes are IAM/lambda/
    cognito service actions covered by the role's existing broad grants and the
    runtime-role delete path, so they are not re-asserted here.
    """
    source = _platform_source()
    assert action in source, (
        f"Manifest teardown calls {action} but it is not granted anywhere in "
        f"the platform stack sources — the resource will orphan with AccessDenied on delete."
    )


def test_deployment_role_can_delete_mcp_server_lambda():
    """Bug 175: the MCP-server path's intercept lambda is named 'MCPServerRuntime'
    (no AgentCore prefix). The deployment role's lambda:DeleteFunction resource
    scope MUST cover MCPServer* or deleting an MCP-server flow orphans the
    function with AccessDenied."""
    source = _platform_source()
    assert "function:MCPServer" in source, (
        "deployment role lambda:DeleteFunction scope does not cover the "
        "MCPServerRuntime lambda — MCP-server flow deletes will orphan it."
    )
