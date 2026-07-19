"""Unit tests for the per-agent identity policy builder (Gap P3.3B).

Pure-function coverage — no AWS, no moto. Mirrors the property-based style of
tests/test_iam_properties.py.

Asserts:
  1. empty connected_tools -> only the 3 baseline statements (Bedrock/S3/Logs).
  2. each tool added individually injects exactly its statement and nothing else.
  3. gateway/memory/kb statements carry the supplied ARN in Resource (not "*")
     and EXCLUDE other tools' ARNs/actions.
  4. omitting an ARN -> "*" fallback for that one tool only.
  5. no wildcards beyond Bedrock-model "*" + CW-Logs "*" (+ documented per-tool
     fallbacks); S3 must be bucket-scoped when a bucket name is supplied.
  6. build_per_agent_role_name returns "AgentCoreRuntime-{name}", <=64 chars,
     valid IAM role-name charset.
  7. deterministic output for a given input.
  8. cross-tool ACL-drift guard: per_agent tool->statement map stays in sync
     with runtime_deployer.create_runtime_iam_role's per-tool branches.
"""

import json
import re
import sys

sys.path.insert(0, "src")

import pytest
from app.services.per_agent_identity import (
    BEDROCK_AGENTCORE_TRUST_POLICY,
    build_per_agent_role_name,
    build_scoped_runtime_policy,
    build_trust_policy,
)
from hypothesis import given, settings
from hypothesis import strategies as st

# ============================================================================
# Constants / strategies
# ============================================================================

BASELINE_SIDS = {"BedrockModelAccess", "S3CodeAccess", "CloudWatchLogs"}

# Tools that scope to an ARN when one is supplied.
ARN_SCOPED = {
    "gateway": ("gateway_arn", "GatewayAccess"),
    "memory": ("memory_arn", "MemoryAccess"),
    "knowledge_base": ("kb_arn", "KnowledgeBaseAccess"),
}

# All supported tools and the Sid each injects.
TOOL_TO_SID = {
    "gateway": "GatewayAccess",
    "memory": "MemoryAccess",
    "knowledge_base": "KnowledgeBaseAccess",
    "guardrails": "GuardrailsAccess",
    "browser": "BrowserAccess",
    "code_interpreter": "CodeInterpreterAccess",
    "evaluation": "EvaluationAccess",
    "observability": "EvaluationAccess",
    "policy": "PolicyAccess",
}

SUPPORTED_TOOLS = list(TOOL_TO_SID.keys())

tool_subset_st = st.lists(
    st.sampled_from(SUPPORTED_TOOLS),
    min_size=0,
    max_size=len(SUPPORTED_TOOLS),
    unique=True,
)

BUCKET = "my-artifacts-bucket"
GW_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/gw-abc123"
MEM_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:memory/mem-abc123"
KB_ARN = "arn:aws:bedrock:us-east-1:123456789012:knowledge-base/kb-abc123"
OTEL_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-otel/x"


def _sids(policy: dict) -> list[str]:
    return [s["Sid"] for s in policy["Statement"]]


def _stmt(policy: dict, sid: str) -> dict:
    for s in policy["Statement"]:
        if s["Sid"] == sid:
            return s
    raise AssertionError(f"Sid {sid} not in policy: {_sids(policy)}")


def _all_actions(policy: dict) -> set[str]:
    actions: set[str] = set()
    for s in policy["Statement"]:
        a = s["Action"]
        actions.update(a if isinstance(a, list) else [a])
    return actions


# ============================================================================
# 1. Baseline only
# ============================================================================


def test_empty_tools_yields_only_baseline():
    policy = build_scoped_runtime_policy([], artifacts_bucket=BUCKET)
    assert _sids(policy) == ["BedrockModelAccess", "S3CodeAccess", "CloudWatchLogs"]
    assert policy["Version"] == "2012-10-17"


def test_none_tools_yields_only_baseline():
    policy = build_scoped_runtime_policy(None, artifacts_bucket=BUCKET)
    assert set(_sids(policy)) == BASELINE_SIDS


def test_baseline_s3_scoped_to_bucket():
    policy = build_scoped_runtime_policy([], artifacts_bucket=BUCKET)
    s3 = _stmt(policy, "S3CodeAccess")
    assert s3["Resource"] == [
        f"arn:aws:s3:::{BUCKET}",
        f"arn:aws:s3:::{BUCKET}/*",
    ]


def test_baseline_s3_falls_back_to_wildcard_without_bucket():
    policy = build_scoped_runtime_policy([])
    assert _stmt(policy, "S3CodeAccess")["Resource"] == "*"


# ============================================================================
# 2. Each tool individually injects exactly its statement and nothing else
# ============================================================================


def test_each_tool_injects_exactly_one_statement():
    for tool, sid in TOOL_TO_SID.items():
        policy = build_scoped_runtime_policy(
            [tool],
            gateway_arn=GW_ARN,
            memory_arn=MEM_ARN,
            kb_arn=KB_ARN,
            artifacts_bucket=BUCKET,
        )
        extra = [s for s in _sids(policy) if s not in BASELINE_SIDS]
        assert extra == [sid], f"tool {tool}: expected [{sid}], got {extra}"


def test_unknown_tool_grants_nothing():
    policy = build_scoped_runtime_policy(["totally_made_up"], artifacts_bucket=BUCKET)
    assert set(_sids(policy)) == BASELINE_SIDS


def test_observability_aliases_evaluation():
    obs = build_scoped_runtime_policy(["observability"])
    ev = build_scoped_runtime_policy(["evaluation"])
    assert _stmt(obs, "EvaluationAccess")["Action"] == _stmt(ev, "EvaluationAccess")["Action"]


# ============================================================================
# 3. ARN scoping + cross-tool isolation (the core value of Gap 3B)
# ============================================================================


def test_arn_scoped_tools_carry_supplied_arn():
    for tool, (arn_kw, sid) in ARN_SCOPED.items():
        arn = {"gateway_arn": GW_ARN, "memory_arn": MEM_ARN, "kb_arn": KB_ARN}[arn_kw]
        policy = build_scoped_runtime_policy([tool], **{arn_kw: arn})
        assert _stmt(policy, sid)["Resource"] == [arn]


def test_gateway_only_excludes_memory_and_kb():
    policy = build_scoped_runtime_policy(["gateway"], gateway_arn=GW_ARN, memory_arn=MEM_ARN, kb_arn=KB_ARN)
    sids = _sids(policy)
    assert "GatewayAccess" in sids
    assert "MemoryAccess" not in sids
    assert "KnowledgeBaseAccess" not in sids
    # No memory/kb ARN or actions anywhere in the doc.
    actions = _all_actions(policy)
    assert not any("Memory" in a for a in actions)
    assert "bedrock:Retrieve" not in actions
    body = str(policy)
    assert MEM_ARN not in body
    assert KB_ARN not in body
    assert GW_ARN in body


def test_memory_only_excludes_gateway_and_kb():
    policy = build_scoped_runtime_policy(["memory"], gateway_arn=GW_ARN, memory_arn=MEM_ARN, kb_arn=KB_ARN)
    sids = _sids(policy)
    assert "MemoryAccess" in sids
    assert "GatewayAccess" not in sids
    assert "KnowledgeBaseAccess" not in sids
    actions = _all_actions(policy)
    assert "bedrock-agentcore:InvokeGateway" not in actions
    assert "bedrock:Retrieve" not in actions
    body = str(policy)
    assert GW_ARN not in body
    assert KB_ARN not in body
    assert MEM_ARN in body


def test_kb_only_excludes_gateway_and_memory():
    policy = build_scoped_runtime_policy(["knowledge_base"], gateway_arn=GW_ARN, memory_arn=MEM_ARN, kb_arn=KB_ARN)
    sids = _sids(policy)
    assert "KnowledgeBaseAccess" in sids
    assert "GatewayAccess" not in sids
    assert "MemoryAccess" not in sids
    body = str(policy)
    assert GW_ARN not in body
    assert MEM_ARN not in body
    assert KB_ARN in body


# ============================================================================
# 4. ARN omission -> "*" fallback for that ONE tool only
# ============================================================================


def test_arn_omission_falls_back_to_wildcard_for_that_tool_only():
    for tool, (arn_kw, sid) in ARN_SCOPED.items():
        # Supply the OTHER two ARNs but omit this one.
        kwargs = {"gateway_arn": GW_ARN, "memory_arn": MEM_ARN, "kb_arn": KB_ARN}
        kwargs[arn_kw] = None
        policy = build_scoped_runtime_policy([tool], **kwargs)
        assert _stmt(policy, sid)["Resource"] == "*", f"{tool} should fall back to '*'"


def test_all_arns_omitted_each_tool_wildcards():
    policy = build_scoped_runtime_policy(["gateway", "memory", "knowledge_base"])
    assert _stmt(policy, "GatewayAccess")["Resource"] == "*"
    assert _stmt(policy, "MemoryAccess")["Resource"] == "*"
    assert _stmt(policy, "KnowledgeBaseAccess")["Resource"] == "*"


# ============================================================================
# 5. No stray wildcards beyond the documented baseline + per-tool fallbacks
# ============================================================================


@given(tools=tool_subset_st)
@settings(max_examples=100)
def test_no_unexpected_wildcard_resources_when_all_arns_supplied(tools):
    """With bucket + all ARNs supplied, only Bedrock-model and CW-Logs may use
    "*". Guardrails/browser/code_interpreter/evaluation/policy use "*" by
    AWS-shape necessity (no ARN available); those are the documented exceptions.
    """
    policy = build_scoped_runtime_policy(
        tools,
        gateway_arn=GW_ARN,
        memory_arn=MEM_ARN,
        kb_arn=KB_ARN,
        artifacts_bucket=BUCKET,
    )
    allowed_wildcard_sids = {
        "BedrockModelAccess",
        "CloudWatchLogs",
        "GuardrailsAccess",
        "BrowserAccess",
        "CodeInterpreterAccess",
        "EvaluationAccess",
        "PolicyAccess",
    }
    for stmt in policy["Statement"]:
        if stmt["Resource"] == "*":
            assert stmt["Sid"] in allowed_wildcard_sids, f"Unexpected wildcard Resource on {stmt['Sid']}"
        # S3 must always be bucket-scoped here (bucket supplied).
        if stmt["Sid"] == "S3CodeAccess":
            assert stmt["Resource"] != "*"


@given(tools=tool_subset_st)
@settings(max_examples=100)
def test_all_statements_are_allow(tools):
    policy = build_scoped_runtime_policy(tools, artifacts_bucket=BUCKET)
    for stmt in policy["Statement"]:
        assert stmt["Effect"] == "Allow"


# ============================================================================
# OTEL secret
# ============================================================================


def test_otel_secret_statement_only_when_arn_supplied():
    without = build_scoped_runtime_policy([])
    assert "OtelAuthHeaderSecret" not in _sids(without)

    with_otel = build_scoped_runtime_policy([], otel_secret_arn=OTEL_ARN)
    stmt = _stmt(with_otel, "OtelAuthHeaderSecret")
    assert stmt["Action"] == ["secretsmanager:GetSecretValue"]
    assert stmt["Resource"] == [OTEL_ARN]


# ============================================================================
# 6. Role-name helper
# ============================================================================


def test_role_name_format():
    assert build_per_agent_role_name("my_agent_v1") == "AgentCoreRuntime-my_agent_v1"


@given(
    name=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
        min_size=0,
        max_size=80,
    )
)
@settings(max_examples=200)
def test_role_name_within_iam_limits_and_charset(name):
    role_name = build_per_agent_role_name(name)
    assert role_name.startswith("AgentCoreRuntime-")
    assert len(role_name) <= 64
    # Valid IAM role-name charset.
    assert re.fullmatch(r"[A-Za-z0-9+=,.@_-]+", role_name)


def test_role_name_empty_input_has_default():
    role_name = build_per_agent_role_name("")
    assert role_name == "AgentCoreRuntime-agent_default"


def test_role_name_sanitizes_illegal_chars():
    role_name = build_per_agent_role_name("bad name/with*chars")
    assert re.fullmatch(r"[A-Za-z0-9+=,.@_-]+", role_name)


# ============================================================================
# 7. Determinism
# ============================================================================


def test_deterministic_output():
    args = dict(
        connected_tools=["gateway", "memory", "knowledge_base", "guardrails"],
        gateway_arn=GW_ARN,
        memory_arn=MEM_ARN,
        kb_arn=KB_ARN,
        otel_secret_arn=OTEL_ARN,
        artifacts_bucket=BUCKET,
    )
    a = build_scoped_runtime_policy(**args)
    b = build_scoped_runtime_policy(**args)
    assert a == b


def test_duplicate_tools_deduplicated():
    policy = build_scoped_runtime_policy(["gateway", "gateway", "gateway"], gateway_arn=GW_ARN)
    assert _sids(policy).count("GatewayAccess") == 1


def test_tool_order_preserved():
    policy = build_scoped_runtime_policy(["memory", "gateway"])
    non_baseline = [s for s in _sids(policy) if s not in BASELINE_SIDS]
    assert non_baseline == ["MemoryAccess", "GatewayAccess"]


# ============================================================================
# 8. Cross-tool ACL-drift guard (sync with runtime_deployer)
# ============================================================================


def test_per_tool_map_in_sync_with_shared_role():
    """Every per-tool branch in runtime_deployer.create_runtime_iam_role must
    have a corresponding tool in the per-agent builder, so a per_agent runtime
    never silently loses a capability the shared role would grant.
    """
    import inspect

    from app.services import runtime_deployer
    from app.services.per_agent_identity import _TOOL_BUILDERS

    src = inspect.getsource(runtime_deployer.create_runtime_iam_role)
    # Tools the shared role branches on, e.g. `if tool == "gateway":`.
    shared_tools = set(re.findall(r'tool == ["\']([a-z_]+)["\']', src))
    # Tuple-membership branches, e.g. `elif tool in ("evaluation", "observability"):`.
    for group in re.findall(r"tool in \(([^)]*)\)", src):
        shared_tools.update(re.findall(r'["\']([a-z_]+)["\']', group))
    missing = shared_tools - set(_TOOL_BUILDERS.keys())
    assert not missing, (
        f"Shared role grants tools {missing} that per_agent builder lacks — ACL drift. Add them to _TOOL_BUILDERS."
    )


# ============================================================================
# Trust policy
# ============================================================================


def test_trust_policy_principal():
    tp = build_trust_policy()
    stmt = tp["Statement"][0]
    assert stmt["Principal"]["Service"] == "bedrock-agentcore.amazonaws.com"
    assert stmt["Action"] == "sts:AssumeRole"
    assert stmt["Effect"] == "Allow"


def test_trust_policy_returns_fresh_copy():
    a = build_trust_policy()
    a["Statement"][0]["Effect"] = "Deny"
    b = build_trust_policy()
    assert b["Statement"][0]["Effect"] == "Allow"
    # Module constant also untouched.
    assert BEDROCK_AGENTCORE_TRUST_POLICY["Statement"][0]["Effect"] == "Allow"


# ============================================================================
# moto round-trip — prove the policy + trust doc attach to a real IAM role and
# that the role name matches what runtime_deployer.destroy_runtime cleans up.
# This validates the iam_step per_agent branch end-to-end at the IAM layer
# (the branch itself lives in the shared iam_step.py applied by the main loop).
# ============================================================================

moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402


@mock_aws
def test_policy_round_trips_through_real_iam():
    import boto3

    iam = boto3.client("iam", region_name="us-east-1")
    runtime_name = "demo_triage_v3"
    role_name = build_per_agent_role_name(runtime_name)

    iam.create_role(
        RoleName=role_name,
        AssumeRolePolicyDocument=json.dumps(build_trust_policy()),
    )
    policy = build_scoped_runtime_policy(
        ["gateway", "memory"],
        gateway_arn=GW_ARN,
        memory_arn=MEM_ARN,
        artifacts_bucket=BUCKET,
    )
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="AgentCoreRuntimePolicy",
        PolicyDocument=json.dumps(policy),
    )

    fetched = iam.get_role_policy(RoleName=role_name, PolicyName="AgentCoreRuntimePolicy")
    assert fetched["PolicyDocument"] == policy

    # Role name must match destroy_runtime's `AgentCoreRuntime-{name}` candidate
    # so per-agent roles are cleaned up (and NOT skipped by the Bug-62 guard,
    # which only skips names == shared OR ending in '-shared').
    assert role_name == f"AgentCoreRuntime-{runtime_name}"
    assert not role_name.endswith("-shared")


def test_role_name_matches_destroy_runtime_cleanup_candidate():
    """The per-agent role name MUST equal the candidate runtime_deployer.
    destroy_runtime derives from the canonical runtime_id, or per-agent roles
    leak on delete (Bug 25/124 cascade).

    destroy_runtime computes:
        name_for_role = re.sub(r"-[A-Za-z0-9]{10}$", "", runtime_id)
        candidate     = f"AgentCoreRuntime-{name_for_role}"
    Canonical runtime_id == {agentcore_runtime_name}-{10hash}.
    """
    runtime_name = "demo_triage_v3"
    canonical_runtime_id = f"{runtime_name}-AbCdEfGh01"  # name + 10-char hash

    # Mirror destroy_runtime's derivation exactly.
    name_for_role = re.sub(r"-[A-Za-z0-9]{10}$", "", canonical_runtime_id)
    destroy_candidate = f"AgentCoreRuntime-{name_for_role}"

    assert build_per_agent_role_name(runtime_name) == destroy_candidate


def test_per_agent_role_not_skipped_by_bug62_guard():
    """Per-agent role names must NOT collide with the shared-role guard: they
    are not '-shared'-suffixed and won't equal the stack shared role name."""
    for name in ("a", "demo_triage_v3", "agent_default"):
        role = build_per_agent_role_name(name)
        assert not role.endswith("-shared")
