"""F-56 teardown, as measured live on acfe2e-p0920 (2026-09-22).

A product DELETE of a Memory agent took ~175 s for AgentCore to finish deleting the
Memory. The confirmed-delete helper's default budget is 60 x 2 s, so the async teardown
recorded ``delete_retained`` although nothing was refused -- every real Memory delete
ended "retained". The async invoke has a 600 s Lambda, so it gets a longer, absolute
deadline anchored at the start of the invoke (the teardown steps after Memory keep a
reserve); the inline and auto-cleanup paths keep the default.

The repeat DELETE then converged on the Memory and its role, but failed on the code
object the first pass had already removed: ``GetObjectTagging`` answered ``NoSuchKey``,
which the manifest dispatcher's own ``_gone`` does not recognise. So any retry of a
partly finished teardown ended ``delete_failed`` forever.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import app.deployment_handler as dh
import pytest
from app.services.resource_ownership import ResourceDeletionRefused, owner_tags
from botocore.exceptions import ClientError

from tests.fake_versioned_s3 import FakeVersionedS3


def _client_error(code: str, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


class _Session:
    def __init__(self, **clients):
        self._clients = clients

    def client(self, service, **_kwargs):
        return self._clients[service]


class _LambdaContext:
    def __init__(self, remaining_ms: int):
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining_ms


S3_ROW = {"type": "s3_object", "id": "s3://artifact-bucket/deployments/x/code.zip", "region": "us-east-1"}


# --- the retry: an object already deleted by the first pass --------------------------


def _tagged(deployment_id="dep-1"):
    return {**owner_tags("us-east-1"), "DeploymentId": deployment_id}


def test_an_already_deleted_s3_object_is_absent_not_a_failure():
    s3 = FakeVersionedS3("Enabled")  # the first pass removed every version

    line = dh._delete_managed_resource(S3_ROW, "us-east-1", deployment_id="dep-1", target_session=_Session(s3=s3))

    assert "already absent" in line
    assert not [kw for op, kw in s3.calls if op == "DeleteObject"]


def test_an_unreadable_s3_object_is_still_retained():
    """Only a conclusive miss is absence: AccessDenied must keep refusing, and delete nothing."""
    s3 = FakeVersionedS3("Enabled")
    s3.put("artifact-bucket", "deployments/x/code.zip", _tagged())
    s3.deny.add("GetObjectTagging")

    with pytest.raises(ResourceDeletionRefused):
        dh._delete_managed_resource(S3_ROW, "us-east-1", deployment_id="dep-1", target_session=_Session(s3=s3))
    assert not [kw for op, kw in s3.calls if op == "DeleteObject"]


def test_an_owned_s3_object_is_still_deleted():
    s3 = FakeVersionedS3("Enabled")
    version = s3.put("artifact-bucket", "deployments/x/code.zip", _tagged())

    line = dh._delete_managed_resource(S3_ROW, "us-east-1", deployment_id="dep-1", target_session=_Session(s3=s3))

    assert line.endswith("deleted (1 version(s))")
    assert [kw for op, kw in s3.calls if op == "DeleteObject"] == [
        {"Bucket": "artifact-bucket", "Key": "deployments/x/code.zip", "VersionId": version}
    ]


# --- the budget: async teardown waits long enough, inline does not change --------------


def _memory_delete_kwargs(monkeypatch, context) -> dict:
    """Drive the real async entry point down to the real Memory dispatcher; capture its call."""
    captured: dict = {}
    monkeypatch.setattr(dh, "delete_memory_confirmed", lambda *a, **k: captured.update(k))
    monkeypatch.setattr(dh, "_lookup_deployment_record", lambda rid: {"deployment_id": "dep-1"})
    monkeypatch.setattr(dh, "_set_delete_status", MagicMock())

    def _cleanup(*_a, **_k):
        dh._delete_managed_resource(
            {"type": "memory", "id": "mem-1", "region": "us-east-1"},
            "us-east-1",
            target_session=_Session(**{"bedrock-agentcore-control": MagicMock()}),
            owner_sub="owner-1",
        )
        return dh.DeleteResponse(success=True, message="ok")

    monkeypatch.setattr(dh, "_run_delete_cleanup", _cleanup)
    started = time.monotonic()
    dh._handle_async_delete({"_async_delete": True, "runtime_id": "rt-1", "caller_sub": "owner-1"}, context)
    captured["_started"] = started
    return captured


def test_async_memory_confirmation_outlasts_a_measured_175s_delete(monkeypatch):
    kwargs = _memory_delete_kwargs(monkeypatch, _LambdaContext(remaining_ms=600_000))

    budget = kwargs["deadline_monotonic"] - kwargs["_started"]
    assert 300 <= budget <= 361
    assert kwargs["confirmation_attempts"] * 2.0 >= budget  # attempts must not be the tighter bound


def test_async_memory_confirmation_leaves_the_later_steps_a_reserve(monkeypatch):
    """With little Lambda time left the deadline shrinks; it never runs to the timeout."""
    kwargs = _memory_delete_kwargs(monkeypatch, _LambdaContext(remaining_ms=300_000))

    assert kwargs["deadline_monotonic"] - kwargs["_started"] <= 300 - 180 + 1


def test_the_budget_does_not_leak_past_the_async_invoke(monkeypatch):
    _memory_delete_kwargs(monkeypatch, _LambdaContext(remaining_ms=600_000))
    captured: dict = {}
    monkeypatch.setattr(dh, "delete_memory_confirmed", lambda *a, **k: captured.update(k))

    dh._delete_managed_resource(
        {"type": "memory", "id": "mem-1", "region": "us-east-1"},
        "us-east-1",
        target_session=_Session(**{"bedrock-agentcore-control": MagicMock()}),
        owner_sub="owner-1",
    )

    assert "deadline_monotonic" not in captured  # inline (29 s API cap) keeps the default


def test_handler_hands_the_lambda_context_to_the_async_delete(monkeypatch):
    seen = {}
    monkeypatch.setattr(dh, "_handle_async_delete", lambda event, context=None: seen.update(ctx=context) or {})
    ctx = _LambdaContext(600_000)

    dh.handler({"_async_delete": True, "runtime_id": "rt-1", "caller_sub": "s"}, ctx)

    assert seen["ctx"] is ctx


# --- the retry: a Memory the first pass left DELETING, now gone -----------------------
#
# Live, the first pass ended ``delete_retained`` with the Memory still DELETING and its
# role deliberately kept (deleting the role first strands the Memory). The repeat DELETE
# must then read the Memory as absent and go on to delete that kept role; otherwise the
# role guard turns one slow Memory into a permanently retained role.

OWNER = "b458d4f8-60e1-70fa-98bd-fb664f6e307c"
MEMORY_ROLE = "AgentCoreMemory-f56mem"


def _memory_manifest_record() -> dict:
    return {
        "deployment_id": "d70e79e1-0000-4000-8000-000000000000",
        "runtime_id": "rt-mem",
        "user_id": OWNER,
        "target_region": "us-east-1",
        "resource_manifest_complete": True,
        "delete_status": "delete_retained",
        "memory_result": {"memory_id": "mem-1", "memory_role_name": MEMORY_ROLE},
        "created_resources": [
            {"type": "agent_runtime", "id": "rt-mem", "region": "us-east-1"},
            {"type": "memory", "id": "mem-1", "region": "us-east-1"},
            {"type": "iam_role", "id": MEMORY_ROLE, "name": MEMORY_ROLE, "region": "us-east-1"},
        ],
    }


class _Gone:
    """Every other client: whatever the first pass removed now reads as absent."""

    def __init__(self, service):
        self._code = "NoSuchEntity" if service == "iam" else "ResourceNotFoundException"

    def __getattr__(self, op):
        def _call(**_kwargs):
            raise _client_error(self._code, op)

        return _call


class _AnySession:
    def __init__(self, control):
        self._control = control

    def client(self, service, **_kwargs):
        return self._control if service == "bedrock-agentcore-control" else _Gone(service)


def _wire_manifest_retry(monkeypatch, control):
    """The runtime row is already gone (the first pass deleted it); Memory and role are real."""
    from app.services import step_clients

    store = MagicMock()
    store._table = object()
    monkeypatch.setattr(dh, "_get_state_store", lambda: store)
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda _t, rid: _memory_manifest_record() if rid == "rt-mem" else None)
    monkeypatch.setattr(step_clients, "session_for_event", lambda _e: _AnySession(control))
    monkeypatch.setattr(dh, "destroy_runtime", lambda *a, **k: {"success": True, "message": "runtime already absent"})
    monkeypatch.setattr(dh, "manifest_delete_refusal", lambda *a, **k: None)
    monkeypatch.setattr(dh.boto3, "client", lambda service, **k: _Gone(service))
    deleted_roles: list[str] = []
    monkeypatch.setattr(dh, "delete_owned_iam_role", lambda _iam, name, _region: deleted_roles.append(name))
    return deleted_roles


def test_a_retry_after_the_memory_finished_deleting_removes_the_kept_role(monkeypatch):
    control = MagicMock()
    # The real delete_memory_confirmed and ownership read, against a Memory that is gone.
    control.get_memory.side_effect = _client_error("ResourceNotFoundException", "GetMemory")
    control.get_agent_runtime.side_effect = _client_error("ResourceNotFoundException", "GetAgentRuntime")
    deleted_roles = _wire_manifest_retry(monkeypatch, control)

    result = dh._run_delete_cleanup("rt-mem", OWNER)

    assert MEMORY_ROLE in deleted_roles
    assert result.success is True, result.message
    assert not result.retained
    control.delete_memory.assert_not_called()


def test_a_memory_still_deleting_on_the_retry_keeps_its_role(monkeypatch):
    """The other half: while the Memory is unconfirmed, the role is never touched."""
    control = MagicMock()
    control.get_agent_runtime.side_effect = _client_error("ResourceNotFoundException", "GetAgentRuntime")
    deleted_roles = _wire_manifest_retry(monkeypatch, control)
    monkeypatch.setattr(
        dh,
        "delete_memory_confirmed",
        MagicMock(side_effect=ResourceDeletionRefused("memory mem-1 is still DELETING")),
    )

    result = dh._run_delete_cleanup("rt-mem", OWNER)

    assert MEMORY_ROLE not in deleted_roles
    assert result.success is False
    assert result.retained is True
