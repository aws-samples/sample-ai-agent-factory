"""A successful teardown must not report an error it already decided to ignore.

Manifest teardown (Step 0a) deletes the ``agent_runtime`` row, and then the legacy
per-component fallback calls ``destroy_runtime`` a second time. That second call
sees the runtime mid-transition and returns
``success:false, message:"Runtime destroy error: ... Current status: DELETING"``.

Bug 159 stopped *counting* that as a failure, but the message was still appended,
so ``DELETE /api/runtime/{id}`` returned ``success:true`` with a body reading
"Runtime X deleted; Runtime destroy error: ConflictException ... DELETING".
Observed live on a clean delete of a real deployment. Customers delete often, so
this is the string they'd see every time and reasonably read as a broken teardown.

These tests pin both halves: the phantom message is suppressed when the manifest
owned the delete, and a genuine failure with NO manifest is still surfaced.
"""

import sys
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, "src")

_RUNTIME_ID = "agent_abc-QNPVKl93O8"
_REGION = "us-east-1"


@pytest.fixture(autouse=True)
def _pin_the_home_region(monkeypatch):
    """Make the teardown region deterministic regardless of test import order.

    ``_run_delete_cleanup`` starts from ``config.aws_region``, and ``AppConfig`` is a frozen
    dataclass built once at the first import of ``app.config``. ``tests/conftest.py`` sets
    ``AWS_REGION=us-east-1``, but it does so in an autouse *fixture* — so the value only reaches
    the config object when nothing imported the app during collection. Run this file alone and
    the region is ``us-east-1``; run it after any module that imports the app at module scope and
    it is whatever the developer's shell resolved (here the CLI default ``us-west-2``).

    The stubs below assert the region they are handed, so without this the file passed in
    isolation and failed in a full-suite run — which is how it was found. Pinned by replacing the
    module global rather than by setting the attribute, because the dataclass is frozen.
    """
    import app.deployment_handler as deployment_handler

    monkeypatch.setattr(deployment_handler, "config", replace(deployment_handler.config, aws_region=_REGION))


def _record(*, with_manifest: bool) -> dict:
    rec = {
        # This fixture isolates runtime-message assembly. An empty deployment id
        # deliberately skips the unrelated deterministic KB-tool cleanup branch.
        "deployment_id": "",
        "user_id": "sub-1",
        "runtime_id": _RUNTIME_ID,
        "deployment_mode": "runtime",
    }
    if with_manifest:
        rec["created_resources"] = [
            {"type": "agent_runtime", "id": _RUNTIME_ID, "region": "us-east-1"},
        ]
    return rec


@pytest.fixture
def dh(monkeypatch):
    """Isolate _run_delete_cleanup: real message assembly, stubbed AWS calls."""
    import app.deployment_handler as dh

    monkeypatch.setattr(dh, "_get_state_store", lambda: MagicMock())
    # The manifest arm reports the authoritative success line.
    monkeypatch.setattr(
        dh,
        "_delete_managed_resource",
        lambda res, region, **_kwargs: f"[manifest] runtime {res.get('id')}: Runtime {res.get('id')} deleted",
    )
    # The legacy fallback's second delete hits the mid-transition conflict.
    monkeypatch.setattr(
        dh,
        "destroy_runtime",
        lambda rid, region: {
            "success": False,
            "message": (
                "Runtime destroy error: An error occurred (ConflictException) when "
                "calling the DeleteAgentRuntime operation: The agent is currently "
                "being modified by another operation. Current status: DELETING."
            ),
        },
    )
    return dh


def _run(dh, monkeypatch, *, with_manifest: bool):
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: _record(with_manifest=with_manifest))
    return dh._run_delete_cleanup(_RUNTIME_ID, "sub-1")


def test_manifest_owned_delete_hides_the_phantom_conflict(dh, monkeypatch):
    resp = _run(dh, monkeypatch, with_manifest=True)
    msg = resp.message
    assert "deleted" in msg, msg
    assert "Runtime destroy error" not in msg, (
        f"a teardown that succeeded must not report the conflict Bug 159 already decided to ignore; got: {msg}"
    )
    assert "ConflictException" not in msg, msg
    assert resp.success is True, msg


def test_without_a_manifest_a_real_failure_is_still_reported(dh, monkeypatch):
    """No manifest means the fallback IS the delete — its error must surface."""
    resp = _run(dh, monkeypatch, with_manifest=False)
    assert "Runtime destroy error" in resp.message, resp.message
    assert resp.success is False, resp.message


def test_the_other_race_does_not_duplicate_the_deleted_line(dh, monkeypatch):
    """The second call can WIN instead of conflicting (seen live in eu-central-1).

    Then it returns success with its own "Runtime X deleted", which used to be
    concatenated onto the manifest's identical line as "... deleted; ... deleted".
    """
    monkeypatch.setattr(
        dh,
        "destroy_runtime",
        lambda rid, region: {"success": True, "message": f"Runtime {rid} deleted"},
    )
    msg = _run(dh, monkeypatch, with_manifest=True).message
    assert msg.count("deleted") == 1, f"the deleted line must appear once; got: {msg}"


def test_a_manifest_without_a_runtime_row_still_reports_the_fallback(dh, monkeypatch):
    """A deploy can fail before the runtime is recorded, leaving a manifest with
    other resources but no ``agent_runtime``. There the fallback is the only thing
    deleting the runtime, so suppressing it would hide a genuine failure."""
    rec = _record(with_manifest=True)
    rec["created_resources"] = [{"type": "secret", "id": "agentcore-connector/x", "region": "us-east-1"}]
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: rec)
    resp = dh._run_delete_cleanup(_RUNTIME_ID, "sub-1")
    assert "Runtime destroy error" in resp.message, resp.message


@pytest.mark.parametrize(
    ("deployment_mode", "resource_type"),
    [
        ("runtime", "agent_runtime"),
        ("harness", "harness"),
    ],
)
def test_a_retained_primary_never_falls_through_to_the_legacy_destroy(
    dh,
    monkeypatch,
    deployment_mode,
    resource_type,
):
    """A protected manifest row is not the same thing as a successful delete."""
    record = {
        "deployment_id": "",
        "user_id": "sub-1",
        "runtime_id": _RUNTIME_ID,
        "deployment_mode": deployment_mode,
        "created_resources": [
            {
                "type": resource_type,
                "id": _RUNTIME_ID,
                "region": "us-east-1",
                "created_by_deployment": False,
            }
        ],
    }
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: record)
    store = MagicMock()
    store.has_other_live_resource_reference.return_value = True
    monkeypatch.setattr(dh, "_get_state_store", lambda: store)
    manifest_delete = MagicMock()
    runtime_destroy = MagicMock()
    harness_destroy = MagicMock()
    monkeypatch.setattr(dh, "_delete_managed_resource", manifest_delete)
    monkeypatch.setattr(dh, "destroy_runtime", runtime_destroy)
    monkeypatch.setattr(dh, "destroy_harness", harness_destroy)

    response = dh._run_delete_cleanup(_RUNTIME_ID, "sub-1")

    manifest_delete.assert_not_called()
    runtime_destroy.assert_not_called()
    harness_destroy.assert_not_called()
    assert response.success is False
    assert response.retained is True
    assert resource_type in response.message
    assert "Resources retained by deletion-authority policy" in response.message


def test_a_retained_knowledge_base_never_falls_through_to_legacy_kb_cleanup(
    dh,
    monkeypatch,
):
    """A fallback result field cannot overrule an authoritative manifest refusal."""
    kb_id = "KB-SHARED"
    record = {
        "deployment_id": "",
        "user_id": "sub-1",
        "runtime_id": _RUNTIME_ID,
        "deployment_mode": "runtime",
        "resource_manifest_complete": True,
        "knowledge_base_result": {
            "created_by_flow": True,
            "kb_id": kb_id,
            "data_source_id": "DS-SHARED",
            "kb_role_arn": "arn:aws:iam::111111111111:role/kb-shared",
        },
        "created_resources": [
            {
                "type": "knowledge_base",
                "id": kb_id,
                "region": "us-east-1",
                "created_by_deployment": False,
            },
            {
                "type": "agent_runtime",
                "id": _RUNTIME_ID,
                "region": "us-east-1",
                "created_by_deployment": False,
            },
        ],
    }
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: record)
    store = MagicMock()
    store.has_other_live_resource_reference.return_value = True
    monkeypatch.setattr(dh, "_get_state_store", lambda: store)
    target_session = MagicMock()
    monkeypatch.setattr(
        "app.services.step_clients.session_for_event",
        lambda event: target_session,
    )
    manifest_delete = MagicMock()
    runtime_destroy = MagicMock()
    monkeypatch.setattr(dh, "_delete_managed_resource", manifest_delete)
    monkeypatch.setattr(dh, "destroy_runtime", runtime_destroy)

    response = dh._run_delete_cleanup(_RUNTIME_ID, "sub-1")

    manifest_delete.assert_not_called()
    runtime_destroy.assert_not_called()
    target_session.client.assert_not_called()
    assert response.success is False
    assert response.retained is True
    assert "knowledge_base" in response.message


def test_policy_engine_is_retained_and_the_detach_failure_is_reported(
    monkeypatch,
):
    """Never hide a gateway-authorizer/update refusal and delete its engine.

    The pre-hardening branch used ``except Exception: pass`` around gateway
    authorizer handling. A denied/invalid UpdateGateway then disappeared from
    the response and teardown continued into DeletePolicyEngine while the
    gateway could still reference it.
    """
    import app.deployment_handler as deployment_handler
    from app.services import step_clients

    runtime_id = "runtime-policy-detach"
    record = {
        "deployment_id": "",
        "user_id": "sub-1",
        "runtime_id": runtime_id,
        "deployment_mode": "runtime",
        "policy_result": {"engine_id": "engine-1"},
        "gateway_result": {"gateway_id": "gateway-1"},
        "created_resources": [],
    }
    store = MagicMock()
    store._table = object()
    control = MagicMock()
    detail = {
        "gatewayId": "gateway-1",
        "name": "orders",
        "roleArn": "arn:aws:iam::111111111111:role/gateway",
        "authorizerType": "CUSTOM_JWT",
        "protocolType": "MCP",
        "authorizerConfiguration": {
            "customJWTAuthorizer": {
                "discoveryUrl": "https://issuer.example/.well-known/openid-configuration",
                "allowedClients": ["client-1"],
            }
        },
    }
    # The lock reads the gateway itself (F-66e), and teardown names this id-only
    # legacy row from the same read (F-66f): one detail serves both.
    control.get_gateway.return_value = {**detail, "status": "READY"}

    def _owned(_client, resource_type, resource_id, region):
        assert region == "us-east-1"
        if resource_type == "policy_engine":
            assert resource_id == "engine-1"
            return {"policyEngineId": resource_id}
        assert resource_type == "gateway"
        assert resource_id == "gateway-1"
        return dict(detail)

    control.update_gateway.side_effect = PermissionError("authorizer configuration update denied")
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "111111111111"}
    session = MagicMock()
    session.client.side_effect = lambda service, **_k: sts if service == "sts" else control
    delete_engine = MagicMock()
    monkeypatch.setattr(
        deployment_handler,
        "_get_state_store",
        lambda: store,
    )
    monkeypatch.setattr(
        deployment_handler,
        "_scan_for_runtime",
        lambda table, requested: record,
    )
    monkeypatch.setattr(step_clients, "session_for_event", lambda event: session)
    monkeypatch.setattr(
        deployment_handler,
        "assert_agentcore_resource_owned",
        _owned,
    )
    monkeypatch.setattr(
        deployment_handler,
        "delete_policy_engine_confirmed",
        delete_engine,
    )
    monkeypatch.setattr(
        deployment_handler,
        "cleanup_gateway_resources",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        deployment_handler,
        "destroy_runtime",
        lambda *args, **kwargs: {
            "success": True,
            "message": f"Runtime {runtime_id} deleted",
        },
    )

    result = deployment_handler._run_delete_cleanup(runtime_id, "sub-1")

    control.update_gateway.assert_called_once()
    update = control.update_gateway.call_args.kwargs
    assert update["authorizerConfiguration"] == {
        "customJWTAuthorizer": {
            "discoveryUrl": "https://issuer.example/.well-known/openid-configuration",
            "allowedClients": ["client-1"],
        }
    }
    assert "policyEngineConfiguration" not in update
    delete_engine.assert_not_called()
    assert result.success is False
    assert result.retained is True
    assert "Policy engine engine-1 left in place (protected)" in result.message
    assert "could not be safely detached" in result.message
