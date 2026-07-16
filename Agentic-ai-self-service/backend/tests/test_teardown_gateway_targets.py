"""Bug 187 — manifest teardown must delete a gateway's TARGETS before the
gateway, and must NOT mis-classify the 'has targets associated' ValidationException
as 'already gone' (which silently orphaned the gateway on every gateway-bearing
deployment).
"""
import sys
import types
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, "src")


@pytest.fixture
def patched_boto(monkeypatch):
    """Patch boto3.client used inside deployment_handler._delete_managed_resource."""
    import app.deployment_handler as dh

    ctrl = MagicMock()
    # Gateway has one target; delete_gateway fails until the target is gone.
    ctrl.list_gateway_targets.return_value = {"items": [{"targetId": "tgt-1"}]}
    state = {"targets": 1}

    def _del_gateway(**kw):
        if state["targets"] > 0:
            raise Exception(
                "An error occurred (ValidationException): Gateway has targets "
                "associated with it. Delete all targets before deleting the gateway."
            )
        return {}

    def _del_target(**kw):
        state["targets"] -= 1
        return {}

    ctrl.delete_gateway.side_effect = _del_gateway
    ctrl.delete_gateway_target.side_effect = _del_target

    monkeypatch.setattr(dh, "time", types.SimpleNamespace(sleep=lambda *_: None))
    monkeypatch.setattr("app.services.step_clients.client", lambda *a, **k: ctrl)
    return dh, ctrl


def test_gateway_teardown_deletes_targets_first(patched_boto):
    dh, ctrl = patched_boto
    msg = dh._delete_managed_resource(
        {"type": "gateway", "id": "gw-1", "region": "us-east-1"}, "us-east-1"
    )
    # target deleted, THEN gateway deleted
    ctrl.delete_gateway_target.assert_called_once_with(gatewayIdentifier="gw-1", targetId="tgt-1")
    assert ctrl.delete_gateway.called
    assert "deleted" in msg and "already gone" not in msg


def test_gateway_target_conflict_is_not_treated_as_gone(monkeypatch):
    """If targets can NEVER be removed, the teardown must RAISE (surfaced as a
    cleanup failure), not silently report 'already gone'."""
    import app.deployment_handler as dh

    ctrl = MagicMock()
    ctrl.list_gateway_targets.return_value = {"items": []}  # nothing to delete
    ctrl.delete_gateway.side_effect = Exception(
        "An error occurred (ValidationException): Gateway has targets associated with it."
    )
    monkeypatch.setattr(dh, "time", types.SimpleNamespace(sleep=lambda *_: None))
    monkeypatch.setattr("app.services.step_clients.client", lambda *a, **k: ctrl)

    with pytest.raises(Exception) as exc:
        dh._delete_managed_resource({"type": "gateway", "id": "gw-2", "region": "us-east-1"}, "us-east-1")
    assert "target" in str(exc.value).lower()


def test_genuine_not_found_gateway_is_gone(monkeypatch):
    import app.deployment_handler as dh

    ctrl = MagicMock()
    ctrl.list_gateway_targets.return_value = {"items": []}
    ctrl.delete_gateway.side_effect = Exception("ResourceNotFoundException: gateway gone")
    monkeypatch.setattr(dh, "time", types.SimpleNamespace(sleep=lambda *_: None))
    monkeypatch.setattr("app.services.step_clients.client", lambda *a, **k: ctrl)
    # A genuine not-found is idempotent success: the retry loop breaks on _gone
    # and the function returns normally (no raise). The key is it does NOT raise
    # and does NOT leave the gateway un-handled.
    msg = dh._delete_managed_resource({"type": "gateway", "id": "gw-3", "region": "us-east-1"}, "us-east-1")
    assert "gw-3" in msg
