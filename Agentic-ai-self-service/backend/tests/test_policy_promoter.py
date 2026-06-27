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


def _state(mode="LOG_ONLY", pending=True):
    pr = {"mode": mode, "engine_arn": "arn:eng", "downgraded_to_log_only": mode != "ENFORCE"}
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


def test_best_effort_never_raises():
    ctrl = MagicMock()
    ctrl.list_policies.side_effect = Exception("boom")
    ctrl.create_policy.side_effect = Exception("boom")
    ctrl.get_gateway.side_effect = Exception("boom")
    with patch.object(pp, "_ctrl", return_value=ctrl):
        out = pp.try_promote_to_enforce(_state(), "us-east-1")
    assert out["promoted"] is False  # degraded, no exception
