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


# --------------------------------------------------------------------------- #
# Creating a gateway-scoped Cedar policy requires calling the gateway.         #
# --------------------------------------------------------------------------- #

_INVOKE_GATEWAY = "bedrock-agentcore:InvokeGateway"


def _policy_statements_granting(action: str) -> list[tuple[str, str, str]]:
    """Every ``iam.PolicyStatement(...)`` in the platform sources that grants ``action``.

    Returns (file name, enclosing function, source text of ``resources=``) per
    statement. AST rather than substring matching, because the whole point of these
    assertions is WHICH statement in WHICH role the action landed in — a text
    search cannot tell an ARN-scoped statement from the kitchen-sink
    ``resources=["*"]`` one next to it, nor one role's grant from another's.
    """
    found: list[tuple[str, str, str]] = []
    for path in _platform_source_files():
        source = path.read_text()
        for func in ast.walk(ast.parse(source)):
            if not isinstance(func, ast.FunctionDef):
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                if name != "PolicyStatement":
                    continue
                kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
                actions = kwargs.get("actions")
                if not isinstance(actions, ast.List):
                    continue
                if not any(isinstance(e, ast.Constant) and e.value == action for e in actions.elts):
                    continue
                resources = kwargs.get("resources")
                found.append((path.name, func.name, ast.get_source_segment(source, resources) if resources else ""))
    return found


# The two principals that CREATE policies, and the function that builds each
# one's role. Deliberately not a whole-file search: `build_shared_runtime_role`
# also grants InvokeGateway, on `*`, because that role is the AGENT calling its
# own gateway's tools at request time and is shared across every agent in the
# stack — a different principal with a different justification, and not what
# these assertions are about.
_POLICY_CREATING_PRINCIPALS = [
    ("step_lambdas.py", "_create_step_role"),
    ("lambdas.py", "build_deployment_lambda"),
]


def test_the_policy_path_can_call_the_gateway_it_scopes_policies_to():
    """AgentCore resolves the gateway named in a Cedar statement AS THE CALLER.

    So a principal that creates a gateway-scoped policy needs
    ``bedrock-agentcore:InvokeGateway`` on the gateway ARN — not only the
    Create/Manage verbs. Without it ``create_policy`` ends CREATE_FAILED with
    "Insufficient permissions to call gateway with ID <id>" in BOTH validation
    modes. Proven live on the customer-export path, which had the identical gap:
    same statement, gateway READY for many minutes, that action as the only
    variable.

    Two principals need it, and missing it on either is silent: the ``policy``
    step role, and the deployment Lambda role, which runs both the direct-deploy
    policy path and the lazy promoter that is supposed to RECOVER a permit the
    step left CREATE_FAILED. A promoter that cannot call the gateway can never
    finish that recovery, so the fail-closed engine stays deny-all forever.
    """
    granted = {(f, fn) for f, fn, _res in _policy_statements_granting(_INVOKE_GATEWAY)}
    missing = [p for p in _POLICY_CREATING_PRINCIPALS if p not in granted]
    assert not missing, (
        f"no statement grants {_INVOKE_GATEWAY} in {missing} — a policy-creating "
        "principal that cannot call the gateway cannot create a gateway-scoped "
        "Cedar policy at all, and the promoter can never recover one"
    )


@pytest.mark.parametrize("source_file,func_name", _POLICY_CREATING_PRINCIPALS)
def test_the_invoke_gateway_grant_is_scoped_to_gateway_arns(source_file, func_name):
    """It is a DATA-plane verb, so `*` here would let a deploy-time Lambda call
    every tool of every gateway in the account.

    ARCC cnt_AGx9pUNpmdOVZB (specific actions on specific resources) and
    cnt_BBrFTwAEgWxA30 (start at zero, add the minimum): unlike the Create*/List*
    verbs in the shared statement, InvokeGateway HAS a resource form and the ARN
    prefix is knowable at synth time, so there is no reason for a wildcard
    resource. Per-deploy gateway ids are not knowable, so the id stays wildcarded.
    """
    scoped = [res for f, fn, res in _policy_statements_granting(_INVOKE_GATEWAY) if (f, fn) == (source_file, func_name)]
    assert scoped, f"{_INVOKE_GATEWAY} is not granted in {source_file}::{func_name}"
    for res in scoped:
        assert ":gateway/" in res, (
            f"{_INVOKE_GATEWAY} in {source_file}::{func_name} is granted on {res!r}, which "
            "is not a gateway ARN — a data-plane invoke verb must not ride along on the "
            'control-plane resources=["*"] statement'
        )


# Actions that LOOK like AgentCore verbs and are not. IAM accepts a nonexistent
# action silently and authorizes nothing, so each of these is a grant that reads
# as capability the role does not have — the failure mode is a reviewer (or the
# next engineer) believing a call is permitted when it can never be.
#
# ADDING TO THIS LIST TAKES THREE INDEPENDENT CHECKS, NOT ONE. In decreasing
# authority, the oracles available and what each is worth:
#   1. A live AccessDenied naming the action proves it exists and is enforced.
#      Conclusive against everything below.
#   2. AWS's machine-readable Service Reference feed — the index at
#      https://servicereference.us-east-1.amazonaws.com/ then
#      /v1/<service>/<service>.json. Authoritative but it LAGS.
#   3. `aws accessanalyzer validate-policy --policy-type IDENTITY_POLICY` ->
#      INVALID_ACTION "The action ... does not exist". Reads the SAME dataset as
#      (2), so it inherits the same lag and is not an independent opinion.
#   4. botocore's service model: NOT an oracle. AuthorizeAction, InvokeGateway,
#      ManageAdminPolicy, ManageResourceScopedPolicy and CreateTokenVault are all
#      real IAM actions with no SDK operation behind them, so the model is silent
#      on the real and the fake alike.
#
# THE COUNTEREXAMPLE THAT SETS THE BAR: `bedrock-agentcore:CreateTokenVault` is
# absent from BOTH the Service Reference feed and Access Analyzer, and a live
# fresh-account deploy still fails "not authorized to perform:
# bedrock-agentcore:CreateTokenVault on resource: .../token-vault/default". So
# INVALID_ACTION is NECESSARY BUT NOT SUFFICIENT. A verb may only be retired when
# it is absent from the reference AND nothing in the repo calls it AND no
# service-side implicit authorization needs it. Pruning on absence alone is how the
# export path lost that grant once — see the note in
# test_cfn_export_contract.py::TestEmittedActionsAreRealIamActions, and the
# inert-but-kept list in _ABSENT_BUT_KEPT below.
#
# Scope: this test reads the platform stack sources only. The EXPORT path's
# counterpart is
# TestEmittedActionsAreRealIamActions::test_the_specific_inert_grants_are_gone,
# which asserts over the actions the generator actually emits (stronger than a
# text search, since it cannot be fooled by YAML quoting) and carries the same
# names. Add a retired verb to BOTH lists, or the two paths drift.
_NONEXISTENT_ACTIONS = [
    # Retired 2026-09-19 from the policy step role and the deployment Lambda role.
    # `ManageResourceScopedPolicy` is real and stays; these two are not. The
    # control-plane model has Get/Put/DeleteResourcePolicy — resource-BASED
    # policies, an unrelated feature.
    "bedrock-agentcore:GetResourceScopedPolicy",
    "bedrock-agentcore:ListResourceScopedPolicies",
    # Retired earlier from build_shared_runtime_role; kept here so they cannot
    # come back on a copy-paste.
    "bedrock-agentcore:GetLastKTurns",
    "bedrock-agentcore:RetrieveMemories",
]


@pytest.mark.parametrize("action", _NONEXISTENT_ACTIONS)
def test_no_role_grants_an_action_that_does_not_exist(action):
    """A grant that authorizes nothing is worse than no grant: it misleads."""
    for path in _platform_source_files():
        source = path.read_text()
        # Quoted, so the explanatory comments that name these actions in prose
        # (which is how the next reader learns why they are gone) do not trip it.
        assert f'"{action}"' not in source, (
            f"{path.name} grants {action}, which is not a real IAM action — IAM "
            "accepts it and authorizes nothing. Remove it; if a call really needs "
            "authorizing, find the action that exists (Access Analyzer "
            "validate-policy names the invalid ones)."
        )


# --------------------------------------------------------------------------- #
# The other side of the same coin: actions Access Analyzer calls nonexistent     #
# that we KEEP, and the real grant each one must never be mistaken for.         #
# --------------------------------------------------------------------------- #
#
# A sweep of all 273 actions the platform stack synthesizes through
# `aws accessanalyzer validate-policy` returned exactly nine INVALID_ACTION
# findings. None of them is removed, for two different reasons:
#
#   * CreateTokenVault is PROVEN NEEDED LIVE (AccessDenied on a fresh account),
#     so Access Analyzer is simply wrong about it — see _NONEXISTENT_ACTIONS above.
#   * The other eight are inert but HARMLESS, and each sits next to the real
#     capability-bearing verb for the same operation. Deleting the inert one is
#     cosmetic; deleting the REAL one because it looked like the duplicate is a
#     live AccessDenied. That is what this test guards.
#
# So the value asserted below is the COUNTERPART, not the inert action. If someone
# tidies this area up, the inert names may go; the counterparts may not.
_ABSENT_BUT_KEPT = {
    # inert action: the real action(s) that actually authorize the capability
    "agent-registry:BatchGetDiscoverableRegistryRecord": [
        "agent-registry:ListDiscoverableRegistryRecords",
        "agent-registry:SearchDiscoverableRegistryRecords",
    ],
    # Index metadata on an OpenSearch Serverless collection is a DATA-plane read,
    # authorized by aoss:APIAccessAll, not by a control-plane Describe verb.
    "aoss:DescribeIndex": ["aoss:APIAccessAll"],
    "bedrock-agentcore:ListTokenVaults": ["bedrock-agentcore:GetTokenVault"],
    # The browser tool is driven by session verbs, not a single Invoke.
    "bedrock-agentcore:InvokeBrowser": [
        "bedrock-agentcore:StartBrowserSession",
        "bedrock-agentcore:ConnectBrowserAutomationStream",
    ],
    # The Converse / ConverseStream APIs are authorized by InvokeModel /
    # InvokeModelWithResponseStream. There is no bedrock:Converse IAM action, so
    # dropping InvokeModel "because Converse covers it" removes model access.
    "bedrock:Converse": ["bedrock:InvokeModel"],
    "bedrock:ConverseStream": ["bedrock:InvokeModelWithResponseStream"],
    "s3vectors:DescribeIndex": ["s3vectors:GetIndex"],
    "s3vectors:DescribeVectorBucket": ["s3vectors:GetVectorBucket"],
}


@pytest.mark.parametrize(
    "inert,counterparts",
    [(k, v) for k, v in _ABSENT_BUT_KEPT.items()],
    ids=list(_ABSENT_BUT_KEPT),
)
def test_the_real_counterpart_of_an_inert_grant_is_still_granted(inert, counterparts):
    """An inert grant must never be the thing holding a capability up.

    Each key is an action IAM Access Analyzer reports as nonexistent, which the
    stack still grants. The assertion is on the VALUE: the verb that really
    authorizes that operation is present. If a cleanup pass removed the real one
    and left the inert lookalike, the role would read as capable and authorize
    nothing — the failure mode _NONEXISTENT_ACTIONS exists to describe, arrived at
    from the opposite direction.
    """
    source = _platform_source()
    missing = [c for c in counterparts if f'"{c}"' not in source]
    assert not missing, (
        f"{inert} is granted but authorizes nothing (Access Analyzer: does not "
        f"exist), and its real counterpart(s) {missing} are no longer granted "
        "anywhere in the platform stack sources. The capability is gone while the "
        "policy still looks like it is there."
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
