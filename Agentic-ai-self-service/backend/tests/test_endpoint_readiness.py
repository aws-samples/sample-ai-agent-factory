"""Bug 166 regression: launch must gate on the DEFAULT endpoint, not just the
runtime status.

The AgentCore runtime can report READY while its DEFAULT endpoint is still
provisioning; invoking in that window raises ResourceNotFoundException ("No
endpoint or agent found with qualifier 'DEFAULT'"). wait_for_default_endpoint_ready
polls the endpoint so a deploy never reports success while the agent is
uninvokable.

Pure unit tests — the control client is a MagicMock; no AWS, no sleep cost
(time.sleep is patched).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services.runtime_deployer import wait_for_default_endpoint_ready


def _ep(status: str) -> dict:
    return {
        "runtimeEndpoints": [
            {
                "name": "DEFAULT",
                "status": status,
                "agentRuntimeEndpointArn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-1/runtime-endpoint/DEFAULT",
            }
        ]
    }


def test_endpoint_ready_returns_arn():
    ctrl = MagicMock()
    ctrl.list_agent_runtime_endpoints.return_value = _ep("READY")
    with patch("app.services.runtime_deployer.time.sleep"):
        result = wait_for_default_endpoint_ready(ctrl, "r-1", timeout=30)
    assert result["success"] is True
    assert result["endpoint_arn"].endswith("runtime-endpoint/DEFAULT")


def test_endpoint_polls_until_ready():
    """CREATING first, then READY — must keep polling, not bail."""
    ctrl = MagicMock()
    ctrl.list_agent_runtime_endpoints.side_effect = [
        {"runtimeEndpoints": []},  # not listed yet
        _ep("CREATING"),  # provisioning
        _ep("READY"),  # ready
    ]
    with patch("app.services.runtime_deployer.time.sleep"):
        result = wait_for_default_endpoint_ready(ctrl, "r-1", timeout=60)
    assert result["success"] is True
    assert ctrl.list_agent_runtime_endpoints.call_count == 3


def test_endpoint_failed_status_fails_fast():
    ctrl = MagicMock()
    ctrl.list_agent_runtime_endpoints.return_value = _ep("CREATE_FAILED")
    with patch("app.services.runtime_deployer.time.sleep"):
        result = wait_for_default_endpoint_ready(ctrl, "r-1", timeout=30)
    assert result["success"] is False
    assert "FAILED" in result["status"]


def test_endpoint_timeout_reports_last_status():
    ctrl = MagicMock()
    ctrl.list_agent_runtime_endpoints.return_value = _ep("CREATING")
    # timeout=0 → loop body never runs; should still return a structured failure.
    with patch("app.services.runtime_deployer.time.sleep"):
        result = wait_for_default_endpoint_ready(ctrl, "r-1", timeout=0)
    assert result["success"] is False
    assert "did not become READY" in result["error"]
