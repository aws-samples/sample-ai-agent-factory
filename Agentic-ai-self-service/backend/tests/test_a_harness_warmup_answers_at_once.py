"""The deploy-time warm-up of a harness answers at once and runs on a session of its own.

DeployPanel.warmupRuntime pings every HTTP deployment the moment it succeeds, fire-and-forget,
through POST /api/test-runtime with ``warmup: true``. Measured live 2026-10-02 (G10 extra run):
a cold harness's first turn ran past the HTTP API's 30 s integration limit, so the browser got a
504 while the Lambda finished the turn. The harness branch also ignored ``warmup``, so the ping
was stored as a conversation turn in the session named after the harness. Warm turns afterwards
took 3.5-5 s, for fresh and continuing sessions alike. The route now hands the ping to an
asynchronous self-invoke (the tool-test route's pattern) on a session no conversation uses, and
answers at once.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import app.deployment_handler as dh
import pytest
from app.models.deployment_models import TestRequest
from app.services.runtime_invocation import invoke_verified_http_runtime

HARNESS_ARN = "arn:aws:bedrock-agentcore:eu-west-1:123456789012:harness/mx_harness_ab12-AbCdEf1234"
STATE = {
    "deployment_mode": "harness",
    "harness_arn": HARNESS_ARN,
    "target_account_id": "123456789012",
    "target_region": "eu-west-1",
    "target_role_arn": "arn:aws:iam::123456789012:role/Target",
}


def _invoke(request: TestRequest, *, starter=None, invoke_harness=None):
    invoke_harness = invoke_harness or MagicMock(return_value={"success": True, "output": "pong", "trace_id": None})
    response = invoke_verified_http_runtime(
        request,
        caller_sub="sub-1",
        deployment_state=dict(STATE),
        runtime_id="mx_harness_ab12-AbCdEf1234",
        runtime_arn=HARNESS_ARN,
        region="eu-west-1",
        target_session=MagicMock(),
        promote_policy=lambda *_a: False,
        invoke_harness=invoke_harness,
        resolve_memory_identity=lambda *_a: None,
        gateway_session=MagicMock(),
        get_gateway_token=lambda *_a: "token",
        start_harness_warmup=starter,
    )
    return response, invoke_harness


def test_a_warmup_is_handed_off_and_answered_at_once():
    starter = MagicMock()

    response, invoke_harness = _invoke(TestRequest(input="ping", runtimeId="h", warmup=True), starter=starter)

    assert response.success is True and response.arn == HARNESS_ARN
    starter.assert_called_once()
    state, region, arn = starter.call_args.args
    assert (state["harness_arn"], region, arn) == (HARNESS_ARN, "eu-west-1", HARNESS_ARN)
    invoke_harness.assert_not_called()


def test_a_conversation_turn_still_invokes_the_harness():
    starter = MagicMock()

    response, invoke_harness = _invoke(TestRequest(input="hello", runtimeId="h", sessionId="s" * 40), starter=starter)

    assert response.success is True and response.response == "pong"
    invoke_harness.assert_called_once()
    starter.assert_not_called()


def test_a_warmup_that_cannot_start_is_reported_not_raised():
    starter = MagicMock(side_effect=RuntimeError("lambda throttled"))

    response, invoke_harness = _invoke(TestRequest(input="ping", runtimeId="h", warmup=True), starter=starter)

    assert response.success is False and response.error == "Harness warm-up could not be started"
    assert "throttled" not in (response.error or "")
    invoke_harness.assert_not_called()


def test_without_a_starter_the_warmup_keeps_its_old_path():
    """The versions router passes no starter and never sets warmup; nothing about it changes."""
    response, invoke_harness = _invoke(TestRequest(input="ping", runtimeId="h", warmup=True), starter=None)

    assert response.success is True
    invoke_harness.assert_called_once()


def test_the_self_invoke_carries_only_the_arn_and_the_target(monkeypatch):
    lam = MagicMock()
    monkeypatch.setattr(dh.boto3, "client", lambda service, **_kw: lam if service == "lambda" else MagicMock())
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "acf-test-deployment")

    dh._start_harness_warmup(dict(STATE), "eu-west-1", HARNESS_ARN)

    kwargs = lam.invoke.call_args.kwargs
    assert kwargs["FunctionName"] == "acf-test-deployment" and kwargs["InvocationType"] == "Event"
    payload = json.loads(kwargs["Payload"])
    assert payload == {
        "_async_harness_warmup": True,
        "harness_arn": HARNESS_ARN,
        "target_event": {
            "target_account_id": "123456789012",
            "target_region": "eu-west-1",
            "target_role_arn": "arn:aws:iam::123456789012:role/Target",
        },
    }


def test_the_background_turn_runs_in_the_target_on_its_own_session(monkeypatch):
    from app.services import step_clients

    data_client = MagicMock()
    session = MagicMock()
    session.client.return_value = data_client
    seen_events: list = []
    monkeypatch.setattr(step_clients, "session_for_event", lambda event: seen_events.append(event) or session)
    calls: list = []
    monkeypatch.setattr(
        dh,
        "invoke_harness",
        lambda region, arn, prompt, session_id, **kw: (
            calls.append((region, arn, prompt, session_id, kw)) or {"success": True}
        ),
    )

    out = dh._handle_async_harness_warmup(
        {"_async_harness_warmup": True, "harness_arn": HARNESS_ARN, "target_event": {"target_region": "eu-west-1"}}
    )

    assert out == {"warmed": True}
    [(region, arn, prompt, session_id, kw)] = calls
    assert (region, arn, prompt) == ("eu-west-1", HARNESS_ARN, "ping")
    assert session_id.startswith(dh.HARNESS_WARMUP_SESSION_PREFIX) and len(session_id) >= 33
    assert kw["agentcore_data_client"] is data_client
    assert seen_events == [{"target_region": "eu-west-1"}]
    session.client.assert_called_once_with("bedrock-agentcore", region_name="eu-west-1")


@pytest.mark.parametrize("warmed", [True, False])
def test_the_lambda_entry_point_routes_the_warmup_before_the_api(monkeypatch, warmed):
    monkeypatch.setattr(
        dh, "_handle_async_harness_warmup", lambda event: {"warmed": warmed, "arn": event["harness_arn"]}
    )

    assert dh.handler({"_async_harness_warmup": True, "harness_arn": HARNESS_ARN}, None) == {
        "warmed": warmed,
        "arn": HARNESS_ARN,
    }
