"""Agent Registry preview -> GA migration guards (no AWS, no CDK synth).

Agent Registry graduated out of AgentCore into its own AWS service. The danger
is that the migration regresses SILENTLY:

  * The deprecated ``bedrock-agentcore-control`` model STILL exposes
    CreateRegistryRecord (with the old ``descriptorType`` parameter), so a
    reverted call site does not raise UnknownOperation — it just talks to the
    preview shim.
  * The IAM prefix moved to ``agent-registry:``. Because the deploy-path
    auto-register is deliberately best-effort, a stale ``bedrock-agentcore:``
    grant surfaces only as a "skipped" log line, never a failed deploy.

So these are text/source assertions over the backend and the CDK stacks, in the
same spirit as test_iam_completeness.py.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND.parent
_STACKS_DIR = _REPO_ROOT / "infra" / "stacks"

# Match only quoted IAM action literals, so prose/comments that mention the old
# prefix (e.g. "was bedrock-agentcore:*Registry*") don't trip the assertions.
_LEGACY_REGISTRY_ACTION = re.compile(r'"bedrock-agentcore:[A-Za-z]*Registr[A-Za-z]*"')


def _stack_files() -> list[Path]:
    files = sorted(_STACKS_DIR.glob("*.py")) + sorted((_STACKS_DIR / "platform").glob("*.py"))
    assert files, f"no stack sources found under {_STACKS_DIR}"
    return files


def _stack_source() -> str:
    return "\n".join(p.read_text() for p in _stack_files())


def _backend_sources() -> list[Path]:
    return sorted((_BACKEND / "src").rglob("*.py"))


# -- IAM action prefix -------------------------------------------------------


def test_no_legacy_bedrock_agentcore_registry_actions():
    """Every Registry IAM action must use the GA `agent-registry:` prefix."""
    offenders = []
    for path in _stack_files():
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if _LEGACY_REGISTRY_ACTION.search(line):
                offenders.append(f"{path.relative_to(_REPO_ROOT)}:{i}: {line.strip()}")
    assert not offenders, "Registry IAM actions still on the bedrock-agentcore prefix:\n" + "\n".join(offenders)


def test_status_update_step_can_create_registry_records():
    """The auto-register runs in the status_update step Lambda, so ITS role — not
    the deployment Lambda's — is the principal on CreateRegistryRecord."""
    source = (_STACKS_DIR / "platform" / "step_lambdas.py").read_text()
    assert '"agent-registry:CreateRegistryRecord"' in source
    assert '"agent-registry:GetRegistry"' in source
    # teardown deletes the record it created
    assert '"agent-registry:DeleteRegistryRecord"' in source


def test_status_update_step_can_update_records_for_a_redeploy():
    """register() is an upsert: a redeploy collides on name+recordVersion and falls
    back to UpdateRegistryRecord, which needs a lookup first. Missing either action
    AccessDenies inside the best-effort wrapper, leaving the record pinned to the
    FIRST deployment's runtime ARN with nothing surfaced."""
    source = (_STACKS_DIR / "platform" / "step_lambdas.py").read_text()
    assert '"agent-registry:UpdateRegistryRecord"' in source
    assert '"agent-registry:ListRegistryRecords"' in source


def test_status_update_step_is_not_granted_operations_its_path_never_calls():
    """Least privilege for the auto-register role, pinned deliberately.

    The step role's synthesized policy allows exactly six registry operations, and
    an ``iam:SimulateCustomPolicy`` run against the real template confirmed it
    DENIES SearchDiscoverableRegistryRecords, SubmitRegistryRecordForApproval and
    UpdateRegistryRecordStatus. Those three denials are correct, not a gap: the
    auto-register path only reads the registry status, creates a record, and (on
    redeploy) looks it up and updates it. Approval transitions and discovery are
    API-Lambda concerns driven by a human.

    This test exists because the denials LOOK like the bug this migration started
    with — a missing grant swallowed by the best-effort handler — so the next
    person to run a simulation would be tempted to paper over them by widening the
    role. Widening it would hand the deploy pipeline the ability to approve its own
    records, which is the one thing the governance model must not allow.
    """
    source = (_STACKS_DIR / "platform" / "step_lambdas.py").read_text()
    role = source[source.index("StepStatusUpdateRole") :] if "StepStatusUpdateRole" in source else source
    for action in (
        "agent-registry:SubmitRegistryRecordForApproval",
        "agent-registry:UpdateRegistryRecordStatus",
        "agent-registry:SearchDiscoverableRegistryRecords",
    ):
        assert f'"{action}"' not in role, (
            f"{action} was granted to the auto-register step role. Its code path never "
            "calls it, and approval transitions must not be available to the deploy "
            "pipeline — a record that can approve itself is not governed."
        )


def test_every_registry_call_the_adapter_makes_is_granted_somewhere():
    """Derive the IAM requirement from the code instead of restating it.

    Each ``self.control.x()`` / ``self.data.x()`` in the adapter is a real API call,
    and boto3 method names map 1:1 onto operation names, which map 1:1 onto
    `agent-registry:` action names. So the grants can be checked against the call
    sites mechanically, and adding a call with no grant anywhere fails here.

    Scope limit, stated because it is easy to over-read: this unions the grants
    across all stacks, so it does NOT prove the role that makes a given call holds
    the action. Per-role coverage needs a per-role assertion — see
    test_status_update_step_can_update_records_for_a_redeploy, which is what
    actually caught the missing UpdateRegistryRecord on the deploy path.
    """
    adapter = (_BACKEND / "src" / "app" / "services" / "aws_agent_registry.py").read_text()
    called = {
        "".join(part.title() for part in m.split("_"))
        for m in re.findall(r"self\.(?:control|data)\.([a-z_]+)\(", adapter)
    }
    assert called, "found no client calls — did the adapter move?"
    granted = set(re.findall(r'"agent-registry:([A-Za-z]+)"', _stack_source()))
    missing = sorted(called - granted)
    assert not missing, f"adapter calls these operations with no matching IAM grant: {missing}"


def test_gating_never_reads_the_data_plane():
    """Approval decisions must come from the control plane, never the search index.

    Live-verified GA behaviour: the data plane serves a record demoted from
    APPROVED back to DRAFT as *still* APPROVED, for minutes. Both planes spell the
    field ``status``, so the unsafe call looks exactly like the safe one at the
    call site.

    This is reachable on the ordinary path, not an exotic one — register() upserts
    on redeploy and updating content demotes the record — so routing gating
    through search() for speed would let a stale index clear an integration whose
    content has since changed. A happy-path test would not notice.

    Walks the AST of unapproved_integrations() and asserts it touches neither the
    data-plane client nor search().
    """
    adapter = (_BACKEND / "src" / "app" / "services" / "aws_agent_registry.py").read_text()
    fn = next(
        n
        for n in ast.walk(ast.parse(adapter))
        if isinstance(n, ast.FunctionDef) and n.name == "unapproved_integrations"
    )
    forbidden = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Attribute):
            continue
        # `self.data.x()` / `reg.data.x()` — the data-plane client
        if node.attr == "data" or (isinstance(node.value, ast.Attribute) and node.value.attr == "data"):
            forbidden.append(f"line {node.lineno}: reads `.data` (data plane)")
        if node.attr in ("search", "search_discoverable_registry_records"):
            forbidden.append(f"line {node.lineno}: calls .{node.attr}()")
    assert not forbidden, (
        "unapproved_integrations() must decide approval from the control plane "
        "(list_records_strict), not the stale discovery index:\n" + "\n".join(forbidden)
    )


def test_deployment_lambda_has_ga_registry_actions():
    source = (_STACKS_DIR / "platform" / "lambdas.py").read_text()
    for action in (
        "agent-registry:CreateRegistryRecord",
        "agent-registry:ListRegistryRecords",
        "agent-registry:SubmitRegistryRecordForApproval",
        "agent-registry:UpdateRegistryRecordStatus",
        "agent-registry:DeleteRegistryRecord",
    ):
        assert f'"{action}"' in source, f"missing {action}"


def test_search_action_uses_ga_operation_name():
    """GA renamed SearchRegistryRecords -> SearchDiscoverableRegistryRecords.

    Checked against quoted action literals only — comments explaining the rename
    legitimately mention the old name.
    """
    source = _stack_source()
    assert '"agent-registry:SearchDiscoverableRegistryRecords"' in source
    actions = set(re.findall(r'"[a-z0-9-]+:[A-Za-z]+"', source))
    assert not {a for a in actions if a.endswith(':SearchRegistryRecords"')}


# -- boto3 client names + payload shape --------------------------------------


def test_registry_adapter_targets_the_agent_registry_services():
    source = (_BACKEND / "src" / "app" / "services" / "aws_agent_registry.py").read_text()
    assert 'CONTROL_SERVICE = "agent-registry-control"' in source
    assert 'DATA_SERVICE = "agent-registry"' in source


def test_no_registry_calls_against_the_bedrock_agentcore_clients():
    """No source file may build a bedrock-agentcore client for Registry work."""
    offenders = []
    for path in _backend_sources():
        text = path.read_text()
        if "registry_record" not in text and "Registry" not in text:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if 'boto3.client("bedrock-agentcore' not in line:
                continue
            # Runtime/Gateway/Memory legitimately stay on bedrock-agentcore; only
            # flag it inside the registry adapter itself.
            if path.name == "aws_agent_registry.py":
                offenders.append(f"{path.relative_to(_REPO_ROOT)}:{i}: {line.strip()}")
    assert not offenders, "registry adapter still builds a bedrock-agentcore client:\n" + "\n".join(offenders)


def test_no_preview_kwargs_or_operations_in_backend_calls():
    """Preview parameter names / operation names must not reach the wire.

    This walks the AST rather than grepping lines, so the adapter's own
    preview->GA documentation table (which necessarily *names* the old
    spellings) doesn't register as a call site. What it looks at:

      * ``ast.keyword`` — a kwarg literally passed to a boto3 call
      * ``ast.Attribute`` — a client method name being invoked
      * ``ast.Dict`` keys — descriptor payload members

    All three are positions where a preview spelling would actually be sent.
    """
    # Deliberately narrow: only spellings that are unambiguously Registry-preview.
    # `authorizerType`/`authorizerConfiguration` are NOT listed — those remain
    # valid Gateway (bedrock-agentcore) parameters.
    bad_kwargs = {"descriptorType"}
    bad_methods = {"search_registry_records"}
    bad_dict_keys = {"inlineContent", "agentCard", "descriptorType"}

    offenders = []
    for path in _backend_sources():
        rel = path.relative_to(_REPO_ROOT)
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - would fail elsewhere
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg in bad_kwargs:
                offenders.append(f"{rel}:{node.value.lineno}: kwarg {node.arg}=")
            elif isinstance(node, ast.Attribute) and node.attr in bad_methods:
                offenders.append(f"{rel}:{node.lineno}: call .{node.attr}()")
            elif isinstance(node, ast.Dict):
                for key in node.keys:
                    if isinstance(key, ast.Constant) and key.value in bad_dict_keys:
                        offenders.append(f"{rel}:{key.lineno}: dict key {key.value!r}")
    assert not offenders, "preview Agent Registry spellings still on the wire:\n" + "\n".join(offenders)


def test_legacy_record_types_are_aliases_only():
    """The preview enum values may only survive as INPUT aliases.

    `_LEGACY_RECORD_TYPES` exists so an older caller passing "a2a" still works —
    but every value it maps to must be a GA enum member, and no preview value may
    leak into RECORD_TYPES (what we send to AWS).
    """
    from app.services import aws_agent_registry as ar

    assert set(ar._LEGACY_RECORD_TYPES.values()) <= set(ar.RECORD_TYPES)
    assert not set(ar._LEGACY_RECORD_TYPES) & set(ar.RECORD_TYPES)


# -- dependency floor --------------------------------------------------------


def test_lambda_bundle_pins_boto3_with_agent_registry_models():
    """1.43.66 is the first boto3 with the agent-registry service models.

    The Lambda bundle installs this file into backend/lib/, which PYTHONPATH
    shadows ahead of the runtime's built-in boto3 — so this floor is what
    actually decides whether the GA clients exist at runtime.
    """
    text = (_BACKEND / "requirements-lambda.txt").read_text()
    match = re.search(r"^boto3>=(\d+)\.(\d+)\.(\d+)", text, re.M)
    assert match, "boto3 pin not found in requirements-lambda.txt"
    assert tuple(int(g) for g in match.groups()) >= (1, 43, 66)


def test_pyproject_pins_boto3_with_agent_registry_models():
    text = (_BACKEND / "pyproject.toml").read_text()
    match = re.search(r'"boto3>=(\d+)\.(\d+)\.(\d+)"', text)
    assert match, "boto3 pin not found in pyproject.toml"
    assert tuple(int(g) for g in match.groups()) >= (1, 43, 66)
