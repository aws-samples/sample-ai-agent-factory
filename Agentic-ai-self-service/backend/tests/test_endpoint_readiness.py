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

from unittest.mock import MagicMock, call, patch

from app.services.runtime_deployer import (
    _resolve_runtime_identifier,
    wait_for_default_endpoint_ready,
)


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


def test_endpoint_ready_on_page_two_is_not_reported_absent():
    ctrl = MagicMock()
    ctrl.list_agent_runtime_endpoints.side_effect = [
        {
            "runtimeEndpoints": [{"name": "other", "status": "READY"}],
            "nextToken": "page-2",
        },
        _ep("READY"),
    ]

    with patch("app.services.runtime_deployer.time.sleep"):
        result = wait_for_default_endpoint_ready(ctrl, "r-1", timeout=30)

    assert result["success"] is True
    assert ctrl.list_agent_runtime_endpoints.call_args_list == [
        call(agentRuntimeId="r-1", maxResults=100),
        call(
            agentRuntimeId="r-1",
            maxResults=100,
            nextToken="page-2",
        ),
    ]


def test_runtime_name_resolution_finds_page_two():
    ctrl = MagicMock()
    ctrl.list_agent_runtimes.side_effect = [
        {
            "agentRuntimes": [
                {
                    "agentRuntimeName": "other",
                    "agentRuntimeId": "other-AbCdEf1234",
                }
            ],
            "nextToken": "page-2",
        },
        {
            "agentRuntimes": [
                {
                    "agentRuntimeName": "wanted",
                    "agentRuntimeId": "wanted-ZyXwVu9876",
                }
            ]
        },
    ]

    assert _resolve_runtime_identifier(ctrl, "wanted") == "wanted-ZyXwVu9876"
    assert ctrl.list_agent_runtimes.call_args_list == [
        call(maxResults=100),
        call(maxResults=100, nextToken="page-2"),
    ]


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


def _ep_v(status: str, live: str, target: str | None = None) -> dict:
    ep = _ep(status)["runtimeEndpoints"][0]
    ep["liveVersion"] = live
    if target is not None:
        ep["targetVersion"] = target
    return {"runtimeEndpoints": [ep]}


def test_after_an_adopt_update_ready_on_the_old_version_is_not_ready():
    """Redeploy audit 2026-09-28, row 1: after UpdateAgentRuntime the DEFAULT endpoint is READY on
    the previous version until the new one goes live; the pre-warm would warm the old container
    and the gateway's 30 s discovery probe would then meet the new one cold."""
    ctrl = MagicMock()
    ctrl.list_agent_runtime_endpoints.side_effect = [
        _ep_v("READY", "2"),  # still the old version
        _ep_v("UPDATING", "2", "3"),
        _ep_v("READY", "2", "3"),  # new version pending -- not ready yet
        _ep_v("READY", "3"),
    ]
    with patch("app.services.runtime_deployer.time.sleep"):
        result = wait_for_default_endpoint_ready(ctrl, "r-1", timeout=120, expected_version="3")
    assert result["success"] is True
    assert result["live_version"] == "3"
    assert ctrl.list_agent_runtime_endpoints.call_count == 4


def test_without_an_expected_version_ready_is_still_ready():
    ctrl = MagicMock()
    ctrl.list_agent_runtime_endpoints.return_value = _ep_v("READY", "2")
    with patch("app.services.runtime_deployer.time.sleep"):
        result = wait_for_default_endpoint_ready(ctrl, "r-1", timeout=30)
    assert result["success"] is True
    assert ctrl.list_agent_runtime_endpoints.call_count == 1


def test_an_endpoint_that_never_reaches_the_expected_version_fails_naming_both_versions():
    ctrl = MagicMock()
    ctrl.list_agent_runtime_endpoints.return_value = _ep_v("READY", "2")
    with (
        patch("app.services.runtime_deployer.time.sleep"),
        patch(
            "app.services.runtime_deployer.time.monotonic", side_effect=[0.0, 0.0, 1.0, 2.0, 200.0, 200.0, 200.0, 200.0]
        ),
    ):
        result = wait_for_default_endpoint_ready(ctrl, "r-1", timeout=10, expected_version="3")
    assert result["success"] is False
    assert "version 3" in result["error"] and "last live version 2" in result["error"]
