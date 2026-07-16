"""P-PLAT-027: Cedar ENFORCE must FAIL CLOSED, never silently degrade.

Live matrix run 2026-07-02 proved the old behavior leaked a restricted tool's
value: ENFORCE validation failed transiently ("Insufficient permissions to call
gateway"), the step downgraded to LOG_ONLY, lazy promotion never fired, and the
forbidden tool answered. These tests pin the new contract:

  * default: attach in ENFORCE even when the permit policy can't validate yet —
    AgentCore engines are default-deny, so ALL tools are denied (fail-closed)
    until the promoter validates the pending policies;
  * LOG_ONLY on validation failure requires the explicit opt-in
    ``policyConfig.on_enforce_failure = "log_only"``;
  * both paths carry ``enforce_pending`` so the promoter can finish the job.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.step_handlers import policy_step


def _ctrl_with_persistent_create_failed():
    """Control client where every created policy ends CREATE_FAILED with the
    transient-looking gateway-permission reason (persists across retries)."""
    ctrl = MagicMock()
    ctrl.list_policy_engines.return_value = {"policyEngines": []}
    ctrl.create_policy_engine.return_value = {
        "policyEngineId": "eng-1", "policyEngineArn": "arn:aws:bedrock-agentcore:us-east-1:1:policy-engine/eng-1",
    }
    ctrl.get_policy_engine.return_value = {"status": "ACTIVE"}
    ctrl.create_policy.return_value = {"policyId": "p1"}
    ctrl.get_policy.return_value = {
        "status": "CREATE_FAILED",
        "statusReasons": ["Insufficient permissions to call gateway with ID gw-1"],
    }
    ctrl.list_policies.return_value = {"policies": []}
    ctrl.get_gateway.return_value = {
        "name": "gw", "roleArn": "arn:role", "protocolType": "MCP", "status": "READY",
    }
    return ctrl


def _event(**pc_extra):
    pc = {"enabled": True, "mode": "ENFORCE",
          "rules": [{"effect": "forbid", "action": "get_secret"}]}
    pc.update(pc_extra)
    return {
        "deployment_id": "d1",
        "policy_config": pc,
        "gateway_result": {
            "gateway_id": "gw-1",
            "gateway_arn": "arn:aws:bedrock-agentcore:us-east-1:1:gateway/gw-1",
            "qualified_tools": ["T___get_canary", "T___get_secret"],
            "expected_tool_count": 2,
        },
    }


def _run(event):
    ctrl = _ctrl_with_persistent_create_failed()
    with patch.object(policy_step, "_get_deployment_store", return_value=MagicMock()), \
         patch.object(policy_step, "step_clients") as sc, \
         patch("time.sleep"):
        sc.client.return_value = ctrl
        out = policy_step.handler(event, None)
    return out, ctrl


def test_enforce_validation_failure_stays_enforce_by_default():
    """Fail closed: gateway attached in ENFORCE (default-deny) — never LOG_ONLY."""
    out, ctrl = _run(_event())
    pr = out["policy_result"]
    assert pr["mode"] == "ENFORCE"
    assert pr["downgraded_to_log_only"] is False
    assert pr["enforce_validation_pending"] is True
    assert pr["enforce_pending"], "promoter payload must be recorded"
    _, kw = ctrl.update_gateway.call_args
    assert kw["policyEngineConfiguration"]["mode"] == "ENFORCE"


def test_explicit_log_only_opt_in_downgrades():
    """on_enforce_failure=log_only is the ONLY way to trade enforcement for availability."""
    out, ctrl = _run(_event(on_enforce_failure="log_only"))
    pr = out["policy_result"]
    assert pr["mode"] == "LOG_ONLY"
    assert pr["downgraded_to_log_only"] is True
    assert pr["enforce_pending"], "promoter payload still recorded for later promotion"
    _, kw = ctrl.update_gateway.call_args
    assert kw["policyEngineConfiguration"]["mode"] == "LOG_ONLY"


def test_enforce_happy_path_unchanged():
    """When the permit validates ACTIVE, ENFORCE attaches with no pending flag."""
    ctrl = _ctrl_with_persistent_create_failed()
    ctrl.get_policy.return_value = {"status": "ACTIVE"}
    ctrl.list_policies.return_value = {
        "policies": [{"name": "x", "status": "ACTIVE", "policyId": "p1"}]
    }
    with patch.object(policy_step, "_get_deployment_store", return_value=MagicMock()), \
         patch.object(policy_step, "step_clients") as sc, \
         patch("time.sleep"):
        sc.client.return_value = ctrl
        out = policy_step.handler(_event(), None)
    pr = out["policy_result"]
    assert pr["mode"] == "ENFORCE"
    assert pr["enforce_validation_pending"] is False
    assert pr["enforce_pending"] is None
