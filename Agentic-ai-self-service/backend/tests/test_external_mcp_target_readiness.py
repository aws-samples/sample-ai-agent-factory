"""The readiness gate on an external ``mcpServer`` Gateway target.

``create_gateway_target`` returns while AgentCore is still performing its own
``initialize`` handshake against the remote MCP endpoint; the target then settles
on READY or FAILED asynchronously. Nothing used to read that outcome, so a wrong
endpoint, key, or credential prefix produced a **green deploy with a toolless
agent** — the deployment record said ``succeeded`` while the target sat at FAILED
with *"returned HTTP 400 to the initialize handshake"*, observed for real on a
live gateway. That is the same silent-empty-tool-plane failure
``_wait_for_gateway_to_serve_tools`` guards on the gateway itself; this path had
no equivalent.
"""

import pytest
from app.services.gateway_deployer import _wait_for_mcp_target_ready


class _Ctrl:
    """Minimal control-plane double that walks a scripted status sequence."""

    def __init__(self, statuses, reasons=None, read_errors=0):
        self.statuses = list(statuses)
        self.reasons = reasons or []
        self.read_errors = read_errors
        self.deleted = []

    def get_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803 — boto3 casing
        if self.read_errors > 0:
            self.read_errors -= 1
            raise RuntimeError("transient read race")
        # The last scripted status is sticky, so a script ending in CREATING models
        # a target that never settles rather than one that silently turns READY.
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"status": status, "statusReasons": self.reasons}

    def delete_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        self.deleted.append(targetId)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("app.services.gateway_deployer.time.sleep", lambda *_: None)


class TestItPasses:
    def test_a_target_that_reaches_ready_returns_quietly(self):
        ctrl = _Ctrl(["CREATING", "CREATING", "READY"])
        _wait_for_mcp_target_ready(ctrl, "gw-1", "t-1", "mcp-litellm")
        assert ctrl.deleted == []

    def test_a_transient_read_error_is_retried_not_fatal(self):
        """Control-plane reads race with target creation; a failed poll must not
        be mistaken for a failed target."""
        ctrl = _Ctrl(["READY"], read_errors=2)
        _wait_for_mcp_target_ready(ctrl, "gw-1", "t-1", "mcp-litellm")

    def test_an_unrecognized_terminal_status_is_accepted(self):
        """Only CREATING/UPDATING/SYNCHRONIZING mean "keep waiting"; a status this
        code does not know about must not hang the deploy forever."""
        ctrl = _Ctrl(["SOMETHING_NEW"])
        _wait_for_mcp_target_ready(ctrl, "gw-1", "t-1", "mcp-litellm")


class TestItFailsLoud:
    def test_a_failed_target_raises_with_the_reason_verbatim(self):
        """AgentCore's statusReasons name the remote status code — that string is
        the entire diagnostic, so it is surfaced rather than summarized."""
        reason = "MCP server 'https://proxy/mcp/' returned HTTP 400 to the initialize handshake."
        ctrl = _Ctrl(["CREATING", "FAILED"], reasons=[reason])
        with pytest.raises(RuntimeError) as e:
            _wait_for_mcp_target_ready(ctrl, "gw-1", "t-1", "mcp-litellm")
        assert reason in str(e.value)
        assert "mcp-litellm" in str(e.value)

    def test_a_failed_target_is_deleted_so_a_retry_is_not_name_blocked(self):
        ctrl = _Ctrl(["FAILED"], reasons=["nope"])
        with pytest.raises(RuntimeError):
            _wait_for_mcp_target_ready(ctrl, "gw-1", "t-1", "mcp-litellm")
        assert ctrl.deleted == ["t-1"]

    def test_a_failure_still_raises_when_no_reason_is_reported(self):
        ctrl = _Ctrl(["FAILED"], reasons=[])
        with pytest.raises(RuntimeError, match="no reason reported"):
            _wait_for_mcp_target_ready(ctrl, "gw-1", "t-1", "mcp-litellm")

    def test_a_target_stuck_in_creating_times_out_rather_than_passing(self):
        ctrl = _Ctrl(["CREATING"])
        with pytest.raises(RuntimeError, match="did not become ready"):
            _wait_for_mcp_target_ready(ctrl, "gw-1", "t-1", "mcp-litellm", timeout=0.1)
