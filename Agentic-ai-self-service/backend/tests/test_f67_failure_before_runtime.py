"""F-67: a deploy refused before the runtime step is not a retention.

Measured live 2026-09-23 (deployment 2226d1df, stack acfe2e-p0920). A second owner's deploy
was refused at the gateway pre-flight -- the name was another owner's -- before it created
anything. Failure cleanup still invented a name-only ``agent_runtime`` row for a runtime
that never existed, its ownership read was denied, and the refusal was recorded
``delete_retained`` with "protected=1". Without that row, the empty manifest still ended
``delete_retained``, "could not prove", because nothing said the gateway step created nothing.

The causes here are the live shape, copied from that execution's history.
"""

import json
from unittest.mock import MagicMock, patch

import pytest


def _cause(error_type: str, handler: str, line: int = 648) -> dict:
    return {
        "Error": error_type,
        "Cause": json.dumps(
            {
                "errorMessage": "Gateway deployment failed: Gateway name 'g' is in use by another deployment "
                "you do not own. Choose a different gateway name.",
                "errorType": error_type,
                "requestId": "r-1",
                "stackTrace": [
                    f'  File "/var/task/src/app/step_handlers/{handler}.py", line {line}, in handler\n'
                    f"    raise {error_type}(\n"
                ],
            }
        ),
    }


def _event(error_info: dict | None, **extra) -> dict:
    event = {
        "deployment_id": "d-1",
        "deployment_mode": "runtime",
        "friendly_runtime_name": "g8fb_1790140024",
        "target_region": "us-east-1",
        **extra,
    }
    if error_info is not None:
        event["error_info"] = error_info
    return event


def _store(resources: list | None = None) -> MagicMock:
    store = MagicMock()
    state = MagicMock()
    state.model_dump.return_value = {
        "deployment_id": "d-1",
        "status": "failed",
        "resource_manifest_version": 1,
        "resource_manifest_complete": False,
        "created_resources": resources or [],
    }
    store.get.return_value = state
    return store


def _run(store: MagicMock, event: dict) -> tuple[list[dict], tuple]:
    from app.step_handlers import status_update_step

    tried: list[dict] = []
    with (
        patch.object(status_update_step, "_cleanup_resource", side_effect=lambda res, *_a, **_k: tried.append(res)),
        patch("app.services.gateway_deployer.unrecorded_deployment_secret_rows", return_value=([], [])),
    ):
        status_update_step._auto_cleanup_on_failure(store, "d-1", event)
    return tried, store.update_delete_status.call_args.args


def _runtime_rows(event: dict) -> list[dict]:
    from app.step_handlers.status_update_step import _supplemental_failure_resources

    return [r for r in _supplemental_failure_resources({}, event, "us-east-1") if r["type"] == "agent_runtime"]


# The live case, end to end through the failure cleanup.


def test_the_live_refusal_at_the_gateway_preflight_ends_deleted():
    store = _store()
    tried, status = _run(store, _event(_cause("GatewayRefusedBeforeSideEffects", "gateway_step")))

    assert tried == []
    assert status[1] == "deleted", status
    assert "the gateway step refused the deployment before creating anything" in status[2]
    assert any(c.kwargs.get("resource_manifest_complete") is True for c in store.update_status.call_args_list)


def test_a_gateway_failure_that_may_have_created_something_still_cannot_prove_it():
    """The generic gateway failure type is what the step raises after any side effect."""
    _tried, status = _run(_store(), _event(_cause("StepFailedWithUnrecordedRows", "gateway_step")))
    assert status[1] == "delete_retained"
    assert "could not prove" in status[2]


def test_the_gateway_proof_never_suppresses_a_recorded_row():
    row = {"type": "guardrail", "id": "gr-1", "region": "us-east-1", "created_by_deployment": True}
    tried, _status = _run(_store([row]), _event(_cause("GatewayRefusedBeforeSideEffects", "gateway_step")))
    assert [r["id"] for r in tried] == ["gr-1"]


def test_the_gateway_proof_names_the_gateway_handler():
    """The same type raised from any other handler proves nothing about the gateway step."""
    _tried, status = _run(_store(), _event(_cause("GatewayRefusedBeforeSideEffects", "memory_step")))
    assert status[1] == "delete_retained"


# The guessed runtime row.


@pytest.mark.parametrize(
    "handler",
    [
        "validate_step",
        "guardrails_step",
        "mcp_server_step",
        "knowledge_base_step",
        "gateway_step",
        "memory_step",
        "policy_step",
        "codegen_step",
        "iam_step",
    ],
)
def test_a_failure_before_runtime_configure_guesses_no_runtime(handler):
    assert _runtime_rows(_event(_cause("StepFailedWithUnrecordedRows", handler))) == []


@pytest.mark.parametrize("handler", ["runtime_configure_step", "runtime_launch_step", "evaluation_step", "auth_step"])
def test_a_failure_at_or_after_runtime_configure_keeps_the_name_only_row(handler):
    (row,) = _runtime_rows(_event(_cause("RuntimeError", handler)))
    assert row["name"] == "g8fb_1790140024" and row["id"] is None


@pytest.mark.parametrize(
    "error_info",
    [
        None,
        {"Error": "States.Timeout", "Cause": "Task timed out after 900.00 seconds"},
        {"Error": "Sandbox.Timedout", "Cause": json.dumps({"errorMessage": "timed out"})},
        {"Error": "X", "Cause": json.dumps({"errorType": "X", "stackTrace": []})},
        {"Error": "X", "Cause": "{not json"},
        "a string",
    ],
    ids=["absent", "states-timeout", "sandbox-timeout", "empty-trace", "bad-json", "not-a-dict"],
)
def test_a_cause_that_names_no_handler_keeps_the_conservative_row(error_info):
    assert len(_runtime_rows(_event(error_info))) == 1


def test_a_known_runtime_id_is_always_kept():
    (row,) = _runtime_rows(_event(_cause("RuntimeError", "gateway_step"), runtime_id="rt-1"))
    assert row["id"] == "rt-1"


def test_an_iam_step_failure_still_names_the_role_it_may_have_created():
    """Only the runtime guess is gated; the role row is not."""
    from app.step_handlers.status_update_step import _supplemental_failure_resources

    event = _event(
        _cause("RuntimeError", "iam_step"),
        role_arn="arn:aws:iam::111111111111:role/AgentCoreRuntime-g8fb",
        role_created_by_deployment=True,
    )
    rows = _supplemental_failure_resources({}, event, "us-east-1")
    assert [(r["type"], r.get("name")) for r in rows] == [("iam_role", "AgentCoreRuntime-g8fb")]


# The emitter: the class name is the contract, and it is raised only before side effects.


def test_the_step_raises_the_proof_only_when_deploy_gateway_says_nothing_happened(monkeypatch):
    from app.services import failure_inventory
    from app.step_handlers import gateway_step

    assert failure_inventory.GatewayRefusedBeforeSideEffects.__name__ == "GatewayRefusedBeforeSideEffects"
    store = MagicMock()
    monkeypatch.setattr(gateway_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(gateway_step.step_clients, "session_for_event", lambda _e: MagicMock())
    monkeypatch.setattr(gateway_step.step_clients, "artifacts_bucket_for_event", lambda *_a, **_k: "b")

    for refused, expected in (
        (True, failure_inventory.GatewayRefusedBeforeSideEffects),
        (False, failure_inventory.StepFailedWithUnrecordedRows),
    ):
        monkeypatch.setattr(
            gateway_step,
            "deploy_gateway",
            lambda _r=refused, **_k: {"success": False, "error": "refused", "refused_before_side_effects": _r},
        )
        with pytest.raises(failure_inventory.StepFailedWithUnrecordedRows) as info:
            gateway_step.handler({"deployment_id": "d-1", "gateway_config": {"name": "g"}}, None)
        assert type(info.value) is expected


def _deploy_gateway_until(monkeypatch, *, preflight_refuses: bool, claim_refuses: bool) -> dict:
    from app.services import gateway_deployer

    ctrl = MagicMock()
    ctrl.get_gateway.return_value = {"authorizerConfiguration": {}}
    monkeypatch.setattr(gateway_deployer, "_create_agentcore_control_client", lambda _r: ctrl)
    monkeypatch.setattr(gateway_deployer, "_create_cognito_client", lambda _r: MagicMock())
    monkeypatch.setattr(
        gateway_deployer,
        "_list_all_gateways",
        lambda _c: [{"name": "g", "gatewayId": "gw-1"}] if preflight_refuses else [],
    )
    monkeypatch.setattr(
        gateway_deployer, "cleanup_gateway_resources", MagicMock(side_effect=AssertionError("nothing to clean"))
    )

    def _claim(_name):
        if claim_refuses:
            raise RuntimeError("claim refused")

    return gateway_deployer.deploy_gateway(
        {"name": "g"},
        "us-east-1",
        owner_sub="owner-b",
        gateway_consumers=lambda _g, _p: [{"owner_sub": "owner-a", "client_ids": []}],
        claim_gateway_name=_claim,
    )


def test_deploy_gateway_marks_a_preflight_refusal_as_before_side_effects(monkeypatch):
    result = _deploy_gateway_until(monkeypatch, preflight_refuses=True, claim_refuses=False)
    assert result["success"] is False and "you do not own" in result["error"]
    assert result["refused_before_side_effects"] is True


def test_deploy_gateway_does_not_mark_a_failure_from_the_claim_onward(monkeypatch):
    """A claim write whose response was lost may have landed, so the flag is set before it."""
    result = _deploy_gateway_until(monkeypatch, preflight_refuses=False, claim_refuses=True)
    assert result["success"] is False
    assert result["refused_before_side_effects"] is False
