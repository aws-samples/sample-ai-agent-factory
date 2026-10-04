"""F-56: a Memory delete acknowledgement is not proof of deletion.

``delete_memory_confirmed`` already owns the correct bounded protocol:
re-check stack and caller ownership, issue the idempotent delete, and poll until
``GetMemory`` proves absence.  These tests pin that protocol to every production
teardown surface.  A raw ``delete_memory`` followed by a success message is the
defect, even when the service returned HTTP 200.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app import deployment_handler as dh
from app.services import step_clients
from app.services.resource_ownership import ResourceDeletionRefused
from app.step_handlers import status_update_step as sus
from botocore.exceptions import ClientError

OWNER = "b458d4f8-60e1-70fa-98bd-fb664f6e307c"
REGION = "us-east-1"
MEMORY = "memory-AbCdEf1234"
FAILED_DEPLOYMENT = "5bb2084b-d586-46d6-a5f3-494cd24cfc89"


class _Session:
    def __init__(self, control=None, iam=None) -> None:
        self.control = control or MagicMock()
        self.iam = iam or MagicMock()

    def client(self, service, **_kwargs):
        if service == "bedrock-agentcore-control":
            return self.control
        if service == "iam":
            return self.iam
        return MagicMock()


def test_manifest_teardown_uses_confirmed_memory_deletion(monkeypatch):
    control = MagicMock()
    session = _Session(control=control)
    calls: list[dict] = []

    def _confirmed(client, memory_id, **kwargs):
        calls.append(
            {
                "client": client,
                "memory_id": memory_id,
                **kwargs,
            }
        )

    monkeypatch.setattr(dh, "delete_memory_confirmed", _confirmed, raising=False)

    message = dh._delete_managed_resource(
        {
            "type": "memory",
            "id": MEMORY,
            "region": REGION,
        },
        REGION,
        deployment_id="dep-1",
        target_session=session,
        owner_sub=OWNER,
    )

    assert calls == [
        {
            "client": control,
            "memory_id": MEMORY,
            "region": REGION,
            "owner_sub": OWNER,
        }
    ]
    control.delete_memory.assert_not_called()
    assert "confirmed" in message.lower()


def test_failure_auto_cleanup_uses_confirmed_memory_deletion(monkeypatch):
    control = MagicMock()
    calls: list[dict] = []

    def _confirmed(client, memory_id, **kwargs):
        calls.append(
            {
                "client": client,
                "memory_id": memory_id,
                **kwargs,
            }
        )

    monkeypatch.setattr(sus, "delete_memory_confirmed", _confirmed, raising=False)
    monkeypatch.setattr(
        step_clients,
        "client",
        lambda _event, service, **_kwargs: control if service == "bedrock-agentcore-control" else MagicMock(),
    )

    sus._cleanup_resource(
        {
            "type": "memory",
            "id": MEMORY,
            "region": REGION,
        },
        REGION,
        {
            "deployment_id": "dep-1",
            "owner_sub": OWNER,
        },
    )

    assert calls == [
        {
            "client": control,
            "memory_id": MEMORY,
            "region": REGION,
            "owner_sub": OWNER,
        }
    ]
    control.delete_memory.assert_not_called()


def _legacy_record() -> dict:
    return {
        "deployment_id": "",
        "runtime_id": "runtime-with-memory",
        "user_id": OWNER,
        "target_region": REGION,
        "resource_manifest_complete": False,
        "memory_result": {
            "memory_id": MEMORY,
            "memory_name": "memory_name",
            "memory_role_name": "AgentCoreMemory-memory_name",
        },
    }


def _wire_legacy_cleanup(monkeypatch, confirmed):
    control = MagicMock()
    iam = MagicMock()
    session = _Session(control=control, iam=iam)
    store = MagicMock()
    store._table = object()

    monkeypatch.setattr(dh, "_get_state_store", lambda: store)
    monkeypatch.setattr(
        dh,
        "_scan_for_runtime",
        lambda _table, runtime_id: _legacy_record() if runtime_id == "runtime-with-memory" else None,
    )
    monkeypatch.setattr(
        step_clients,
        "session_for_event",
        lambda _event: session,
    )
    monkeypatch.setattr(dh, "manifest_delete_refusal", lambda *args, **kwargs: None)
    monkeypatch.setattr(dh, "assert_agentcore_resource_owned", lambda *args, **kwargs: {})
    monkeypatch.setattr(dh, "delete_memory_confirmed", confirmed, raising=False)
    monkeypatch.setattr(
        dh,
        "destroy_runtime",
        lambda *args, **kwargs: {
            "success": True,
            "message": "runtime deleted",
        },
    )
    return control


def test_legacy_teardown_confirms_memory_before_deleting_its_role(monkeypatch):
    order: list[str] = []
    control = _wire_legacy_cleanup(
        monkeypatch,
        lambda *args, **kwargs: order.append("memory-confirmed"),
    )
    monkeypatch.setattr(
        dh,
        "delete_owned_iam_role",
        lambda *args, **kwargs: order.append("role-deleted"),
    )

    result = dh._run_delete_cleanup("runtime-with-memory", OWNER)

    assert result.success is True
    assert order == ["memory-confirmed", "role-deleted"]
    control.delete_memory.assert_not_called()


def test_unconfirmed_legacy_memory_keeps_its_execution_role(monkeypatch):
    def _not_confirmed(*_args, **_kwargs):
        raise ResourceDeletionRefused("memory is still DELETING; deletion is not confirmed")

    control = _wire_legacy_cleanup(monkeypatch, _not_confirmed)
    delete_role = MagicMock()
    monkeypatch.setattr(dh, "delete_owned_iam_role", delete_role)

    result = dh._run_delete_cleanup("runtime-with-memory", OWNER)

    assert result.success is False
    assert result.retained is True
    assert "memory" in result.message.lower()
    delete_role.assert_not_called()
    control.delete_memory.assert_not_called()


def test_failed_deployment_cleanup_never_treats_its_uuid_as_a_runtime_id(
    monkeypatch,
):
    """The delete endpoint accepts a deployment id when runtime creation failed.

    The identifier is only a record lookup surrogate.  Passing it on to
    ``destroy_runtime`` would ask AWS to destroy a caller-supplied name that was
    never persisted as this deployment's runtime.
    """
    record = {
        "deployment_id": FAILED_DEPLOYMENT,
        "runtime_id": None,
        "user_id": OWNER,
        "target_region": REGION,
        "status": "failed",
        "resource_manifest_complete": False,
        "created_resources": [],
    }

    class _State:
        def model_dump(self, **_kwargs):
            return dict(record)

    store = MagicMock()
    store._table = object()
    store.get.return_value = _State()
    monkeypatch.setattr(dh, "_get_state_store", lambda: store)
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda *_args, **_kwargs: None)

    # The teardown also sweeps the KB tool Lambda and role by their deployment-id-derived
    # names, because a failed deploy can create them without recording them. This deploy
    # created nothing, so answer the way AWS does for an absent resource. A bare MagicMock
    # would instead "find" an untagged function and correctly retain it, which measures
    # the fixture rather than the UUID contract under test.
    def _missing(code: str):
        return ClientError({"Error": {"Code": code, "Message": "not found"}}, "Get")

    lam = MagicMock()
    lam.get_function.side_effect = _missing("ResourceNotFoundException")
    lam.delete_function.side_effect = _missing("ResourceNotFoundException")
    iam = MagicMock()
    iam.get_role.side_effect = _missing("NoSuchEntity")

    class _EmptyAccount(_Session):
        def client(self, service, **kwargs):
            if service == "lambda":
                return lam
            return super().client(service, **kwargs)

    monkeypatch.setattr(
        step_clients,
        "session_for_event",
        lambda _event: _EmptyAccount(iam=iam),
    )
    monkeypatch.setattr(dh, "manifest_delete_refusal", lambda *args, **kwargs: None)
    destroy = MagicMock(
        return_value={
            "success": True,
            "message": "runtime deleted",
        }
    )
    monkeypatch.setattr(dh, "destroy_runtime", destroy)

    result = dh._run_delete_cleanup(FAILED_DEPLOYMENT, OWNER)

    destroy.assert_not_called()
    assert result.success is True
