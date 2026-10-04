"""F-44: the MCP step has one truthful readiness deadline.

The MCP step performs three sequential readiness phases:

1. wait for the AgentCore runtime,
2. wait for its DEFAULT endpoint,
3. mint a Cognito token and invoke the runtime to pre-warm it.

Those phases used independent 300s, 180s, and up-to-620s budgets inside a
600s Lambda.  The Lambda could therefore be killed while still working, and
both endpoint failure and a failed pre-warm were treated as success.  These
tests pin the behavioral contract rather than any particular allocation of
seconds: every phase shares one absolute monotonic deadline, and a deployment
cannot report a usable MCP tool plane unless all three phases succeed.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from unittest.mock import MagicMock

import pytest
from app.services import runtime_deployer
from app.step_handlers import mcp_server_step

from tests.test_mcp_runtime_prewarm_auth import _handler_event, _wire_handler


class _LambdaContext:
    def __init__(self, remaining_ms: int):
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining_ms


class _Clock:
    def __init__(self, now: float):
        self.now = now
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_every_readiness_phase_receives_one_lambda_bounded_deadline(monkeypatch):
    """A short Lambda budget must shorten the whole sequence, not just one wait.

    The exact reserve is intentionally not prescribed.  It must merely be
    positive so the handler has time to persist/raise after readiness expires.
    The same absolute value must reach all three phases; three independently
    computed relative timeouts can still add up to more than the invocation.
    """

    _wire_handler(monkeypatch)
    now = 1_000.0
    monkeypatch.setattr(mcp_server_step.time, "monotonic", lambda: now)

    runtime_wait = MagicMock(return_value={"success": True})
    endpoint_wait = MagicMock(return_value={"success": True})
    prewarm = MagicMock(return_value=True)
    monkeypatch.setattr(mcp_server_step, "wait_for_runtime_ready", runtime_wait)
    monkeypatch.setattr(
        mcp_server_step,
        "wait_for_default_endpoint_ready",
        endpoint_wait,
    )
    monkeypatch.setattr(mcp_server_step, "_prewarm_mcp_runtime", prewarm)

    mcp_server_step.handler(_handler_event(), _LambdaContext(90_000))

    runtime_deadline = runtime_wait.call_args.kwargs.get("deadline_monotonic")
    endpoint_deadline = endpoint_wait.call_args.kwargs.get("deadline_monotonic")
    prewarm_deadline = prewarm.call_args.kwargs.get("deadline_monotonic")

    assert runtime_deadline is not None, (
        "runtime readiness received no absolute deadline; its independent timeout "
        "can consume the whole Lambda before endpoint readiness even starts"
    )
    assert runtime_deadline == endpoint_deadline == prewarm_deadline
    assert now < runtime_deadline < now + 90, (
        "the shared deadline must be bounded by the Lambda's remaining time and "
        "leave a positive completion/error-persistence reserve"
    )


def test_endpoint_not_ready_is_a_deployment_failure(monkeypatch):
    """Runtime READY with an unusable DEFAULT endpoint is not a usable runtime."""

    _wire_handler(monkeypatch)
    monkeypatch.setattr(
        mcp_server_step,
        "wait_for_runtime_ready",
        lambda *_args, **_kwargs: {"success": True},
    )
    monkeypatch.setattr(
        mcp_server_step,
        "wait_for_default_endpoint_ready",
        lambda *_args, **_kwargs: {
            "success": False,
            "status": "CREATING",
            "error": "DEFAULT endpoint did not become READY",
        },
    )
    prewarm = MagicMock(return_value=True)
    monkeypatch.setattr(mcp_server_step, "_prewarm_mcp_runtime", prewarm)

    with pytest.raises(RuntimeError, match="DEFAULT endpoint"):
        mcp_server_step.handler(_handler_event(), _LambdaContext(600_000))

    prewarm.assert_not_called()


def test_failed_prewarm_is_a_deployment_failure(monkeypatch):
    """A gateway discovery probe must not be aimed at a runtime we failed to warm."""

    _wire_handler(monkeypatch)
    monkeypatch.setattr(
        mcp_server_step,
        "wait_for_runtime_ready",
        lambda *_args, **_kwargs: {"success": True},
    )
    monkeypatch.setattr(
        mcp_server_step,
        "wait_for_default_endpoint_ready",
        lambda *_args, **_kwargs: {"success": True},
    )
    prewarm = MagicMock(return_value=False)
    monkeypatch.setattr(mcp_server_step, "_prewarm_mcp_runtime", prewarm)

    with pytest.raises(RuntimeError, match="pre-warm"):
        mcp_server_step.handler(_handler_event(), _LambdaContext(600_000))

    prewarm.assert_called_once()


def test_prewarm_does_not_sleep_after_its_last_attempt(monkeypatch):
    """Sleeping after the final failure spends deadline without another retry."""

    calls = []
    sleeps = []

    def _fail(request, timeout):
        calls.append((request, timeout))
        # A permanent rejection: since 2026-09-29 a TRANSPORT error under a generous deadline is
        # retried until the deadline (see test_transport_failures_stop_at_the_reserve_without_a_trailing_sleep).
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", _fail)
    monkeypatch.setattr(mcp_server_step.time, "sleep", sleeps.append)

    ok = mcp_server_step._prewarm_mcp_runtime(
        "us-east-1",
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/mcp",
        "https://pool.auth.us-east-1.amazoncognito.com/oauth2/token",
        "client-id",
        "client-secret",
        "resource/invoke",
        attempts=3,
        deadline_monotonic=mcp_server_step.time.monotonic() + 60,
    )

    assert ok is False
    assert len(calls) == 3
    assert sleeps == [5, 5]


def test_transport_failures_stop_at_the_reserve_without_a_trailing_sleep(monkeypatch):
    """Transport errors extend past ``attempts`` only while more than the reserve remains, and the
    loop never sleeps after the attempt it will not follow with another. Fake clock: a stubbed sleep
    with a real clock would spin (the mutant-wedge lesson of 2026-09-28)."""

    clock = {"t": 1000.0}
    calls = []
    sleeps = []
    monkeypatch.setattr(mcp_server_step.time, "monotonic", lambda: clock["t"])

    def _sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(mcp_server_step.time, "sleep", _sleep)

    def _fail(request, timeout):
        calls.append(request)
        raise urllib.error.URLError(OSError(99, "Cannot assign requested address"))

    monkeypatch.setattr(urllib.request, "urlopen", _fail)

    ok = mcp_server_step._prewarm_mcp_runtime(
        "us-east-1",
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/mcp",
        "https://pool.auth.us-east-1.amazoncognito.com/oauth2/token",
        "client-id",
        "client-secret",
        "resource/invoke",
        attempts=2,
        deadline_monotonic=1000.0 + 30.0,
    )

    assert ok is False
    # t=0 fail (30 s left > 20 reserve: extend, sleep 5); t=5 fail (25 > 20: sleep 5); t=10 fail
    # (20 left, not > reserve; attempts already met): stop with NO trailing sleep.
    assert len(calls) == 3
    assert sleeps == [5.0, 5.0]
    assert clock["t"] == 1010.0


def test_prewarm_caps_each_network_call_to_the_shared_deadline(monkeypatch):
    """The old 30s/120s URL timeouts could run beyond the Lambda budget."""

    timeouts = []

    class _Response:
        def __init__(self, body: bytes):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return self._body

    responses = [
        _Response(b'{"access_token":"token"}'),
        _Response(b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'),
    ]

    def _open(_request, timeout):
        timeouts.append(timeout)
        return responses.pop(0)

    now = 2_000.0
    monkeypatch.setattr(urllib.request, "urlopen", _open)
    monkeypatch.setattr(mcp_server_step.time, "monotonic", lambda: now)

    ok = mcp_server_step._prewarm_mcp_runtime(
        "us-east-1",
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/mcp",
        "https://pool.auth.us-east-1.amazoncognito.com/oauth2/token",
        "client-id",
        "client-secret",
        "resource/invoke",
        attempts=1,
        deadline_monotonic=now + 3,
    )

    assert ok is True
    assert len(timeouts) == 2
    assert all(0 < timeout <= 3 for timeout in timeouts)


def test_expired_prewarm_deadline_makes_no_network_call_or_sleep(monkeypatch):
    calls = []
    sleeps = []
    now = 3_000.0

    monkeypatch.setattr(mcp_server_step.time, "monotonic", lambda: now)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: calls.append((_args, _kwargs)),
    )
    monkeypatch.setattr(mcp_server_step.time, "sleep", sleeps.append)

    ok = mcp_server_step._prewarm_mcp_runtime(
        "us-east-1",
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/mcp",
        "https://pool.auth.us-east-1.amazoncognito.com/oauth2/token",
        "client-id",
        "client-secret",
        "resource/invoke",
        attempts=4,
        deadline_monotonic=now,
    )

    assert ok is False
    assert calls == []
    assert sleeps == []


def test_runtime_poll_sleep_is_capped_by_the_shared_deadline(monkeypatch):
    clock = _Clock(4_000.0)
    ctrl = MagicMock()
    ctrl.get_agent_runtime.return_value = {"status": "CREATING"}
    monkeypatch.setattr(runtime_deployer.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(runtime_deployer.time, "sleep", clock.sleep)

    result = runtime_deployer.wait_for_runtime_ready(
        ctrl,
        "runtime-id",
        timeout=300,
        deadline_monotonic=clock.now + 3,
    )

    assert result["success"] is False
    assert ctrl.get_agent_runtime.call_count == 1
    assert clock.sleeps == [3]


def test_endpoint_poll_sleep_is_capped_by_the_shared_deadline(monkeypatch):
    clock = _Clock(5_000.0)
    ctrl = MagicMock()
    ctrl.list_agent_runtime_endpoints.return_value = {"runtimeEndpoints": [{"name": "DEFAULT", "status": "CREATING"}]}
    monkeypatch.setattr(runtime_deployer.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(runtime_deployer.time, "sleep", clock.sleep)

    result = runtime_deployer.wait_for_default_endpoint_ready(
        ctrl,
        "runtime-id",
        timeout=180,
        deadline_monotonic=clock.now + 2,
    )

    assert result["success"] is False
    assert ctrl.list_agent_runtime_endpoints.call_count == 1
    assert clock.sleeps == [2]
