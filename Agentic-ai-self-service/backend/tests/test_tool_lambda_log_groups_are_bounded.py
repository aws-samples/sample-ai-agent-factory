"""A tool Lambda's log group is bounded by the platform's retention while the function lives.

Measured on the matrix account on 2026-10-02, after Stage 80 had deleted every deployment:
``AgentCore-5cf168dadd-DynamicTools`` and ``AgentCore-5cf168dadd-CustomerSupportTools`` were
gone (GetFunction: ResourceNotFoundException), and the ``/aws/lambda/`` group of each was still
there with no ``retentionInDays``. They held the tool calls of every gateway on the stack.
Nothing in the platform created, governed or deleted them: Lambda makes the group itself on the
first invocation. The CFN export never had this gap, because it declares a retention-bounded
group for every function it emits.

The platform now treats them the way it treats the runtime's DEFAULT group. The group is
created or adopted with RUNTIME_LOG_RETENTION_DAYS once the function is in the deploy's abort
inventory and before its gateway target exists, and teardown leaves it to expire. A failure
fails the deploy (fail-closed) without orphaning the function it just made.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from app.services import gateway_deployer
from app.services.runtime_deployer import RUNTIME_LOG_RETENTION_DAYS
from botocore.exceptions import ClientError

from tests.test_gateway_empty_tool_plane_retry import GW_ID, _custom_tool, _FakeCtrl, _install

ACCOUNT_FN = "arn:aws:lambda:us-east-1:111122223333:function:"
FUNCTION = "AgentCore-0123456789-DynamicTools"
GROUP = f"/aws/lambda/{FUNCTION}"
WEB_AND_CANONICAL = 8  # the strands-gateway-agent DynamicTools schema count
LEGACY = 4  # the customer-support-assistant CustomerSupportTools schema count


def _error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{code} raised by the test"}}, operation)


class _FailingLogs:
    def __init__(self, *, create: Exception | None = None, retention: Exception | None = None) -> None:
        self.create, self.retention = create, retention
        self.calls: list[str] = []

    def create_log_group(self, **_kwargs):
        self.calls.append("create_log_group")
        if self.create is not None:
            raise self.create

    def put_retention_policy(self, **_kwargs):
        self.calls.append("put_retention_policy")
        if self.retention is not None:
            raise self.retention


# ---------------------------------------------------------------------------
# The governance call
# ---------------------------------------------------------------------------


def test_a_new_group_is_created_with_the_platform_retention(tool_log_groups):
    assert gateway_deployer.govern_tool_function_log_group(FUNCTION, "eu-central-1") == GROUP

    assert tool_log_groups.groups == {GROUP: {"retentionInDays": RUNTIME_LOG_RETENTION_DAYS}}
    assert tool_log_groups.regions == ["eu-central-1"], "the group must be governed in the function's region"
    assert RUNTIME_LOG_RETENTION_DAYS == 30


def test_the_group_lambda_already_made_is_adopted_and_bounded(tool_log_groups):
    tool_log_groups.groups[GROUP] = {}  # what Lambda creates on the first invocation: no retention

    gateway_deployer.govern_tool_function_log_group(FUNCTION, "us-east-1")

    assert tool_log_groups.groups[GROUP] == {"retentionInDays": RUNTIME_LOG_RETENTION_DAYS}


@pytest.mark.parametrize("code", ["AccessDeniedException", "LimitExceededException", "ThrottlingException"])
def test_any_other_create_failure_raises_before_retention(monkeypatch, code):
    logs = _FailingLogs(create=_error(code, "CreateLogGroup"))
    monkeypatch.setattr(gateway_deployer, "_create_logs_client", lambda _region: logs)

    with pytest.raises(ClientError):
        gateway_deployer.govern_tool_function_log_group(FUNCTION, "us-east-1")
    assert logs.calls == ["create_log_group"]


def test_a_retention_failure_raises(monkeypatch):
    logs = _FailingLogs(retention=_error("AccessDeniedException", "PutRetentionPolicy"))
    monkeypatch.setattr(gateway_deployer, "_create_logs_client", lambda _region: logs)

    with pytest.raises(ClientError):
        gateway_deployer.govern_tool_function_log_group(FUNCTION, "us-east-1")
    assert logs.calls == ["create_log_group", "put_retention_policy"]


# ---------------------------------------------------------------------------
# Where the deploy calls it
# ---------------------------------------------------------------------------


def _capture_targets(monkeypatch, tool_log_groups) -> list[tuple[str, dict]]:
    """Each target created, with the governed groups as they stood at that moment."""
    seen: list[tuple[str, dict]] = []

    def _create(_ctrl, _gateway_id, target_name, _params, **_kwargs):
        seen.append((target_name, {name: dict(group) for name, group in tool_log_groups.groups.items()}))
        return {"targetId": f"target-{len(seen)}", "status": "READY"}

    monkeypatch.setattr(gateway_deployer, "_create_gateway_target_with_retry", _create)
    return seen


@pytest.mark.parametrize(
    ("template_id", "helper", "function", "target", "count"),
    [
        (
            "strands-gateway-agent",
            "create_dynamic_gateway_lambda",
            "AgentCore-0123456789-DynamicTools",
            "DynamicTools",
            WEB_AND_CANONICAL,
        ),
        (
            "customer-support-assistant",
            "create_customer_support_lambda",
            "AgentCore-0123456789-CustomerSupportTools",
            "CustomerSupportTools",
            LEGACY,
        ),
    ],
)
def test_a_shared_tool_function_is_governed_before_its_target_exists(
    monkeypatch, tool_log_groups, template_id, helper, function, target, count
):
    _install(monkeypatch, ctrl=_FakeCtrl(), served=count, expected=count)
    monkeypatch.setattr(gateway_deployer, helper, lambda *_a, **_k: ACCOUNT_FN + function)
    seen = _capture_targets(monkeypatch, tool_log_groups)

    out = gateway_deployer.deploy_gateway(
        {"name": "log-governance"}, "us-east-1", template_id=template_id, deployment_id="d-1"
    )

    assert out["success"] is True, out.get("error")
    group = f"/aws/lambda/{function}"
    assert tool_log_groups.groups[group] == {"retentionInDays": RUNTIME_LOG_RETENTION_DAYS}
    assert [name for name, _groups in seen] == [target]
    assert seen[0][1].get(group) == {"retentionInDays": RUNTIME_LOG_RETENTION_DAYS}, (
        "the gateway could invoke the function before its log group was bounded"
    )
    assert tool_log_groups.regions == ["us-east-1"]


def _custom_tool_deploy(monkeypatch, *, served: int = 1):
    cleanup_calls = _install(monkeypatch, ctrl=_FakeCtrl(), served=served, expected=1)
    monkeypatch.setattr(gateway_deployer, "_ensure_lambda_role", lambda *a, **k: "arn:aws:iam::1:role/custom-role")
    monkeypatch.setattr(gateway_deployer, "_create_or_update_lambda", lambda *a, **k: ACCOUNT_FN + "custom")
    fn, _role, _safe, _binding = gateway_deployer._custom_tool_resource_names("lookup", "owner-a", GW_ID, "us-east-1")
    return cleanup_calls, fn


def test_a_custom_tool_function_is_governed_before_its_target_exists(monkeypatch, tool_log_groups):
    _cleanup, fn = _custom_tool_deploy(monkeypatch)
    seen = _capture_targets(monkeypatch, tool_log_groups)

    out = gateway_deployer.deploy_gateway(
        {"name": "agent-gateway"}, "us-east-1", custom_tools=[_custom_tool()], owner_sub="owner-a", deployment_id="d-1"
    )

    assert out["success"] is True, out.get("error")
    custom = [groups for name, groups in seen if name.startswith("CT-")]
    assert len(custom) == 1
    assert custom[0].get(f"/aws/lambda/{fn}") == {"retentionInDays": RUNTIME_LOG_RETENTION_DAYS}


def test_a_failed_governance_leaves_the_new_custom_function_in_the_abort_inventory(monkeypatch, tool_log_groups):
    cleanup_calls, fn = _custom_tool_deploy(monkeypatch)
    seen = _capture_targets(monkeypatch, tool_log_groups)
    logs = _FailingLogs(retention=_error("AccessDeniedException", "PutRetentionPolicy"))
    monkeypatch.setattr(gateway_deployer, "_create_logs_client", lambda _region: logs)

    out = gateway_deployer.deploy_gateway(
        {"name": "agent-gateway"}, "us-east-1", custom_tools=[_custom_tool()], owner_sub="owner-a", deployment_id="d-1"
    )

    assert out["success"] is False
    assert out["custom_tool_lambdas"] == [fn]
    assert cleanup_calls and cleanup_calls[0]["custom_tool_lambdas"] == [fn]
    assert not [name for name, _groups in seen if name.startswith("CT-")], (
        "a target was created for an ungoverned function"
    )


KB_FUNCTION = "AgentCore-0123456789-KBTool-abc123"
KB_RESULT = {"kb_id": "kb-1", "foundation_model_arn": "arn:aws:bedrock:us-east-1::foundation-model/m"}


def test_the_kb_tool_function_is_governed_before_its_target_exists(monkeypatch, tool_log_groups):
    _install(monkeypatch, ctrl=_FakeCtrl(), served=1, expected=1)
    monkeypatch.setattr(gateway_deployer, "create_knowledge_base_lambda", lambda *a, **k: ACCOUNT_FN + KB_FUNCTION)
    seen = _capture_targets(monkeypatch, tool_log_groups)

    out = gateway_deployer.deploy_gateway(
        {"name": "kb-gateway"}, "us-east-1", knowledge_base_result=KB_RESULT, deployment_id="d-kb-1"
    )

    assert out["success"] is True, out.get("error")
    kb = [groups for name, groups in seen if name.startswith("KBTool-")]
    assert len(kb) == 1
    assert kb[0].get(f"/aws/lambda/{KB_FUNCTION}") == {"retentionInDays": RUNTIME_LOG_RETENTION_DAYS}


def test_a_kb_function_created_before_a_failure_reaches_the_manifest(monkeypatch, tool_log_groups):
    """The abort inventory had no KB field, so a KB function made before a later failure in
    the same deploy was in no manifest row. Composed with the real recorder, as the
    shared-pool test in test_gateway_empty_tool_plane_retry does."""
    from app.step_handlers import gateway_step

    _install(monkeypatch, ctrl=_FakeCtrl(), served=1, expected=1)
    monkeypatch.setattr(gateway_deployer, "create_knowledge_base_lambda", lambda *a, **k: ACCOUNT_FN + KB_FUNCTION)
    monkeypatch.setattr(
        gateway_deployer,
        "_create_gateway_target_with_retry",
        MagicMock(side_effect=RuntimeError("CreateGatewayTarget failed")),
    )

    out = gateway_deployer.deploy_gateway(
        {"name": "kb-gateway"}, "us-east-1", knowledge_base_result=KB_RESULT, deployment_id="d-kb-1"
    )

    assert out["success"] is False
    assert out["kb_lambda_name"] == KB_FUNCTION
    rows: list[dict] = []
    store = MagicMock()
    store.record_resource.side_effect = lambda _dep, row: rows.append(row)
    gateway_step._record_gateway_resources(store, "d-kb-1", "us-east-1", out)
    assert {"type": "lambda", "name": KB_FUNCTION} in [{k: r.get(k) for k in ("type", "name")} for r in rows]
