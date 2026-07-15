"""Bug 178: lazy promotion of a Cedar engine from LOG_ONLY to ENFORCE.

The gateway's policy-authorization plane converges ~minutes after deploy, so the
policy step attaches LOG_ONLY + records an ``enforce_pending`` payload. The
test/invoke path calls try_promote_to_enforce() minutes later to flip to ENFORCE
once the intended policies are ACTIVE. These tests use a MagicMock control client
(no AWS) to pin the idempotency + safety rules.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services import policy_promoter as pp


def _state(mode="LOG_ONLY", pending=True, validation_pending=False):
    pr = {"mode": mode, "engine_arn": "arn:eng", "downgraded_to_log_only": mode != "ENFORCE"}
    if validation_pending:
        pr["enforce_validation_pending"] = True
    if pending:
        pr["enforce_pending"] = {
            "engine_id": "eng-1", "gateway_id": "gw-1",
            "gateway_arn": "arn:aws:bedrock-agentcore:us-east-1:1:gateway/gw-1",
            "policies": [{"name": "allow", "statement": "permit(...);", "description": ""}],
        }
    return {"deployment_id": "d1", "policy_result": pr}


def test_noop_when_nothing_pending():
    assert pp.try_promote_to_enforce({"policy_result": {"mode": "LOG_ONLY"}}, "us-east-1") is None


def test_noop_when_already_enforce():
    assert pp.try_promote_to_enforce(_state(mode="ENFORCE"), "us-east-1") is None


def test_failclosed_enforce_unbricks_when_policies_active():
    """P-PLAT-027: fail-closed attach — engine already ENFORCE, policies pending.

    The promoter must still run (create the pending policies) and report
    promoted=True WITHOUT calling update_gateway (mode is already ENFORCE).
    """
    ctrl = MagicMock()
    ctrl.list_policies.return_value = {"policies": [{"name": "allow", "status": "ACTIVE", "policyId": "p1"}]}
    with patch.object(pp, "_ctrl", return_value=ctrl):
        out = pp.try_promote_to_enforce(
            _state(mode="ENFORCE", validation_pending=True), "us-east-1")
    assert out["promoted"] is True and out["mode"] == "ENFORCE"
    ctrl.update_gateway.assert_not_called()


def test_failclosed_enforce_stays_denied_until_policies_active():
    """P-PLAT-027: policies still failing — stays ENFORCE (denied), no flip."""
    ctrl = MagicMock()
    ctrl.list_policies.return_value = {"policies": []}
    ctrl.create_policy.return_value = {"policyId": "p1"}
    ctrl.get_policy.return_value = {"status": "CREATE_FAILED"}
    with patch.object(pp, "_ctrl", return_value=ctrl):
        out = pp.try_promote_to_enforce(
            _state(mode="ENFORCE", validation_pending=True), "us-east-1")
    assert out["promoted"] is False and out["mode"] == "ENFORCE"
    ctrl.update_gateway.assert_not_called()


def test_promotes_when_policies_active():
    ctrl = MagicMock()
    # policy already ACTIVE on the engine
    ctrl.list_policies.return_value = {"policies": [{"name": "allow", "status": "ACTIVE", "policyId": "p1"}]}
    ctrl.get_gateway.return_value = {"name": "gw", "roleArn": "r", "protocolType": "MCP",
                                     "policyEngineConfiguration": {"arn": "arn:eng"}}
    with patch.object(pp, "_ctrl", return_value=ctrl):
        out = pp.try_promote_to_enforce(_state(), "us-east-1")
    assert out["promoted"] is True and out["mode"] == "ENFORCE"
    # flipped via update_gateway with ENFORCE
    _, kw = ctrl.update_gateway.call_args
    assert kw["policyEngineConfiguration"]["mode"] == "ENFORCE"


def test_stays_log_only_when_no_active_policy():
    ctrl = MagicMock()
    # nothing active, and recreate also fails to go active
    ctrl.list_policies.return_value = {"policies": []}
    ctrl.create_policy.return_value = {"policyId": "p1"}
    ctrl.get_policy.return_value = {"status": "CREATE_FAILED"}
    with patch.object(pp, "_ctrl", return_value=ctrl):
        out = pp.try_promote_to_enforce(_state(), "us-east-1")
    assert out["promoted"] is False and out["mode"] == "LOG_ONLY"
    ctrl.update_gateway.assert_not_called()  # never flip to a deny-all ENFORCE


def test_recovers_create_failed_in_place_without_delete():
    """Race-free convergence: a CREATE_FAILED policy already occupies the name.

    The promoter must UPDATE it in place (stable policyId, no name-free window)
    rather than delete+recreate — which used to let concurrent status-poll runs
    clobber each other forever. Asserts update_policy is used, delete_policy is
    NOT, and the policy converges to ACTIVE.
    """
    ctrl = MagicMock()
    # a CREATE_FAILED policy already exists on the engine (from the deploy attach)
    ctrl.list_policies.return_value = {
        "policies": [{"name": "allow", "status": "CREATE_FAILED", "policyId": "p1"}]}
    # gateway has since converged: the in-place update validates ACTIVE
    ctrl.get_policy.return_value = {"status": "ACTIVE"}
    ctrl.get_gateway.return_value = {"name": "gw", "roleArn": "r", "protocolType": "MCP",
                                     "policyEngineConfiguration": {"arn": "arn:eng"}}
    with patch.object(pp, "_ctrl", return_value=ctrl):
        out = pp.try_promote_to_enforce(_state(), "us-east-1")
    assert out["promoted"] is True and out["mode"] == "ENFORCE"
    # updated in place — no delete, no create (race-free)
    ctrl.update_policy.assert_called_once()
    ctrl.delete_policy.assert_not_called()
    ctrl.create_policy.assert_not_called()
    _, ukw = ctrl.update_policy.call_args
    assert ukw["policyId"] == "p1"
    assert ukw["validationMode"] == "IGNORE_ALL_FINDINGS"
    # update_policy's description is a STRUCTURE {"optionalValue": str}, not a
    # bare string (create_policy's is a string) — a str raises ParamValidationError.
    assert isinstance(ukw["description"], dict) and "optionalValue" in ukw["description"]


def test_skips_in_flight_policy_owned_by_concurrent_run():
    """A CREATING policy belongs to another concurrent promoter run — leave it."""
    ctrl = MagicMock()
    ctrl.list_policies.return_value = {
        "policies": [{"name": "allow", "status": "CREATING", "policyId": "p1"}]}
    with patch.object(pp, "_ctrl", return_value=ctrl):
        out = pp.try_promote_to_enforce(_state(), "us-east-1")
    assert out["promoted"] is False
    ctrl.update_policy.assert_not_called()
    ctrl.delete_policy.assert_not_called()
    ctrl.create_policy.assert_not_called()


def test_best_effort_never_raises():
    ctrl = MagicMock()
    ctrl.list_policies.side_effect = Exception("boom")
    ctrl.create_policy.side_effect = Exception("boom")
    ctrl.get_gateway.side_effect = Exception("boom")
    with patch.object(pp, "_ctrl", return_value=ctrl):
        out = pp.try_promote_to_enforce(_state(), "us-east-1")
    assert out["promoted"] is False  # degraded, no exception
