"""The trigger integration harness promotes staging through real release gates."""

from __future__ import annotations

import inspect
import json
import re
from typing import Any

import pytest

from tests.integration import test_trigger_delivery_matrix as matrix

RUNTIME = "trigger_release_runtime"
VERSION = "0" * 32
RUNTIME_ID = "trigger_release_runtime_1234567890"
AGENTCORE_RUNTIME_NAME = "trigger_release_runtime_deadbeef"


class _Response:
    def __init__(self, body: Any, status_code: int = 200):
        self._body = body
        self.status_code = status_code
        self.text = str(body)

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")


class _Session:
    base_url = "https://agents.example.com"

    def __init__(
        self,
        batch_states: list[str] | None = None,
        *,
        production_ready: bool = False,
        slot_statuses: list[int] | None = None,
    ):
        self.batch_states = list(batch_states or ["COMPLETED"])
        self.production_ready = production_ready
        self.slot_statuses = list(slot_statuses or [200])
        self.budget_active = False
        self.calls: list[tuple[str, str]] = []
        self.evaluation_sessions: dict[str, tuple[str, str]] = {}

    def get(self, url: str, **_kwargs) -> _Response:
        self.calls.append(("GET", url))
        if url.endswith("/api/cost/budgets"):
            budgets = (
                [{"scope": "agent", "key": RUNTIME, "limit_usd": 100.0, "warn_pct": 100}] if self.budget_active else []
            )
            return _Response(budgets)
        if url.endswith("/versions"):
            return _Response(
                [
                    {
                        "runtime_name": RUNTIME,
                        "version_id": VERSION,
                        "agentcore_runtime_name": AGENTCORE_RUNTIME_NAME,
                    }
                ]
            )
        if url.endswith("/slots"):
            status = self.slot_statuses.pop(0) if len(self.slot_statuses) > 1 else self.slot_statuses[0]
            if status != 200:
                return _Response({"detail": "Not found" if status == 404 else "request failed"}, status)
            return _Response(
                {
                    "production_version_id": VERSION if self.production_ready else None,
                    "staging_version_id": VERSION,
                }
            )
        if url.endswith("/promotion-evidence"):
            return _Response(
                {
                    "production_gate_required": True,
                    "promotion_policy": {"minimum_sessions": 2},
                    "promotion_evidence": None,
                }
            )
        if "/promotion-evaluation/" in url:
            state = self.batch_states.pop(0) if len(self.batch_states) > 1 else self.batch_states[0]
            return _Response({"status": state})
        raise AssertionError(f"Unexpected GET {url}")

    def post(self, url: str, *, json: dict[str, Any], **_kwargs) -> _Response:
        self.calls.append(("POST", url))
        if url.endswith("/api/cost/budgets"):
            assert json == {
                "scope": "agent",
                "key": RUNTIME,
                "limit_usd": 100.0,
                "warn_pct": 100,
            }
            self.budget_active = True
            return _Response(json)
        if url.endswith("/slots/staging/invoke"):
            case = next(case for case in matrix._RELEASE_EVALUATION_CASES if case["prompt"] == json["input"])
            self.evaluation_sessions[json["sessionId"]] = (
                str(case["prompt"]),
                str(case["expected_response"]),
            )
            return _Response(
                {
                    "success": True,
                    "slot": "staging",
                    "versionId": VERSION,
                    "sessionId": json["sessionId"],
                    "response": case["expected_response"],
                }
            )
        if url.endswith("/promotion-evaluation/start"):
            assert len(json["scenarios"]) == 2
            return _Response({"batch_evaluation_id": "batch-release-1", "status": "PENDING"})
        if url.endswith("/promotion-evaluation/attest"):
            assert json == {"batchEvaluationId": "batch-release-1"}
            return _Response({"promotion_evidence": {"verdict": "pass"}})
        if url.endswith("/promote"):
            self.production_ready = True
            return _Response({"promoted_version_id": VERSION})
        raise AssertionError(f"Unexpected POST {url}")

    def delete(self, url: str, **_kwargs) -> _Response:
        self.calls.append(("DELETE", url))
        if url.endswith(f"/api/cost/budgets/agent/{RUNTIME}"):
            self.budget_active = False
            return _Response({"deleted": {"scope": "agent", "key": RUNTIME}})
        raise AssertionError(f"Unexpected DELETE {url}")


class _Logs:
    def __init__(self, session: _Session) -> None:
        self.session = session

    def filter_log_events(self, **kwargs) -> dict[str, list[dict[str, str]]]:
        session_id = str(kwargs["filterPattern"]).strip('"')
        assert kwargs["logGroupName"] == matrix.BATCH_EVALUATION_SPANS_LOG_GROUP
        prompt, response = self.session.evaluation_sessions[session_id]
        return {
            "events": [
                {
                    "message": json.dumps(
                        {
                            "traceId": "a" * 32,
                            "spanId": "b" * 16,
                            "resource": {"attributes": {"service.name": f"{AGENTCORE_RUNTIME_NAME}.DEFAULT"}},
                            "attributes": {
                                "session.id": session_id,
                                "gen_ai.operation.name": "invoke_agent",
                                "gen_ai.task.input": prompt,
                                "gen_ai.task.output": response,
                            },
                        }
                    )
                }
            ]
        }


def _run(session: _Session) -> None:
    matrix._promote_staging_version_for_triggers(
        session,
        _Logs(session),
        runtime_name=RUNTIME,
        status={"version_id": VERSION, "runtime_id": RUNTIME_ID},
    )


def test_staging_sessions_are_evaluated_attested_and_promoted(monkeypatch) -> None:
    monkeypatch.setattr(matrix.time, "sleep", lambda _seconds: None)
    session = _Session(batch_states=["PENDING", "COMPLETED"])

    _run(session)

    posts = [url for method, url in session.calls if method == "POST"]
    invoke_positions = [index for index, url in enumerate(posts) if url.endswith("/slots/staging/invoke")]
    start = next(index for index, url in enumerate(posts) if url.endswith("/promotion-evaluation/start"))
    attest = next(index for index, url in enumerate(posts) if url.endswith("/promotion-evaluation/attest"))
    promote = next(index for index, url in enumerate(posts) if url.endswith("/promote"))
    assert len(invoke_positions) == 2
    assert max(invoke_positions) < start < attest < promote
    assert session.production_ready is True


def test_slot_publication_retries_only_404_until_exact_staging_exists(monkeypatch) -> None:
    monkeypatch.setattr(matrix.time, "sleep", lambda _seconds: None)
    session = _Session(slot_statuses=[404, 404, 200])

    _run(session)

    slot_positions = [
        index for index, (method, url) in enumerate(session.calls) if method == "GET" and url.endswith("/slots")
    ]
    promote_position = next(
        index for index, (method, url) in enumerate(session.calls) if method == "POST" and url.endswith("/promote")
    )
    assert len(slot_positions) == 4
    assert slot_positions[2] < promote_position < slot_positions[3]
    assert session.production_ready is True


@pytest.mark.parametrize("status", [401, 403, 500])
def test_slot_publication_does_not_retry_auth_or_server_errors(monkeypatch, status: int) -> None:
    monkeypatch.setattr(matrix.time, "sleep", lambda _seconds: None)
    session = _Session(slot_statuses=[status, 200])

    with pytest.raises(RuntimeError, match=rf"HTTP {status}"):
        _run(session)

    slot_gets = [url for method, url in session.calls if method == "GET" and url.endswith("/slots")]
    assert len(slot_gets) == 1


def test_slot_publication_404_wait_is_bounded(monkeypatch) -> None:
    original_timeout = matrix._integration_timeout
    monkeypatch.setattr(
        matrix,
        "_integration_timeout",
        lambda name, default: (
            1 if name == "INTEGRATION_SLOT_PUBLICATION_TIMEOUT_SECONDS" else original_timeout(name, default)
        ),
    )
    ticks = iter([0.0, 0.0, 1.0])
    monkeypatch.setattr(matrix.time, "monotonic", lambda: next(ticks, 1.0))
    monkeypatch.setattr(matrix.time, "sleep", lambda _seconds: None)
    session = _Session(slot_statuses=[404])

    with pytest.raises(AssertionError, match="within the bounded wait"):
        _run(session)

    slot_gets = [url for method, url in session.calls if method == "GET" and url.endswith("/slots")]
    assert len(slot_gets) == 1


def _span_message(
    *,
    session_id: str = "session-1",
    service_name: str = f"{AGENTCORE_RUNTIME_NAME}.DEFAULT",
    trace_id: str = "a" * 32,
    span_id: str = "b" * 16,
    prompt: str = "What is two plus two?",
    response: str = "The answer is 4.",
) -> str:
    return json.dumps(
        {
            "traceId": trace_id,
            "spanId": span_id,
            "resource": {"attributes": {"service.name": service_name}},
            "attributes": {
                "session.id": session_id,
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.task.input": prompt,
                "gen_ai.task.output": response,
            },
        }
    )


def test_evaluation_span_requires_exact_session_service_ids_and_content() -> None:
    expected = {
        "prompt": "What is two plus two?",
        "accepted_answers": ("4",),
    }
    message = _span_message()
    assert matrix._is_evaluation_span(
        message,
        session_id="session-1",
        service_name=f"{AGENTCORE_RUNTIME_NAME}.DEFAULT",
        **expected,
    )
    assert not matrix._is_evaluation_span(
        message,
        session_id="another-session",
        service_name=f"{AGENTCORE_RUNTIME_NAME}.DEFAULT",
        **expected,
    )
    assert not matrix._is_evaluation_span(
        message,
        session_id="session-1",
        service_name="another-runtime.DEFAULT",
        **expected,
    )
    assert not matrix._is_evaluation_span(
        _span_message(trace_id="not-a-trace"),
        session_id="session-1",
        service_name=f"{AGENTCORE_RUNTIME_NAME}.DEFAULT",
        **expected,
    )
    assert not matrix._is_evaluation_span(
        _span_message(prompt="another prompt"),
        session_id="session-1",
        service_name=f"{AGENTCORE_RUNTIME_NAME}.DEFAULT",
        **expected,
    )
    assert not matrix._is_evaluation_span(
        _span_message(response="[Assistant output redacted.]"),
        session_id="session-1",
        service_name=f"{AGENTCORE_RUNTIME_NAME}.DEFAULT",
        **expected,
    )
    assert not matrix._is_evaluation_span(
        "session-1 appeared in an ordinary application log",
        session_id="session-1",
        service_name=f"{AGENTCORE_RUNTIME_NAME}.DEFAULT",
        **expected,
    )


class _ProductionPrivacySession:
    base_url = "https://agents.example.com"

    def __init__(self) -> None:
        self.invocation_payload: dict[str, Any] | None = None

    def get(self, url: str, **_kwargs) -> _Response:
        assert url.endswith("/versions")
        return _Response(
            [
                {
                    "runtime_name": RUNTIME,
                    "version_id": VERSION,
                    "agentcore_runtime_name": AGENTCORE_RUNTIME_NAME,
                }
            ]
        )

    def post(self, url: str, *, json: dict[str, Any], **_kwargs) -> _Response:
        assert url.endswith("/slots/production/invoke")
        assert "_agentcore_evaluation_capture" not in json
        self.invocation_payload = dict(json)
        return _Response(
            {
                "success": True,
                "slot": "production",
                "versionId": VERSION,
                "sessionId": json["sessionId"],
                "response": "Paris is the capital of France, and the Seine runs through it.",
            }
        )


class _ProductionPrivacyLogs:
    trace_id = "c" * 32

    def __init__(self, session: _ProductionPrivacySession, *, leak_on_trace: bool = False) -> None:
        self.session = session
        self.leak_on_trace = leak_on_trace
        self.filters: list[str] = []

    def filter_log_events(self, **kwargs) -> dict[str, list[dict[str, str]]]:
        assert kwargs["logGroupName"] == matrix.BATCH_EVALUATION_SPANS_LOG_GROUP
        value = str(kwargs["filterPattern"]).strip('"')
        self.filters.append(value)
        payload = self.session.invocation_payload or {}
        if value == payload.get("sessionId"):
            return {
                "events": [
                    {
                        "message": json.dumps(
                            {
                                "traceId": self.trace_id,
                                "spanId": "d" * 16,
                                "resource": {"attributes": {"service.name": f"{AGENTCORE_RUNTIME_NAME}.DEFAULT"}},
                                "attributes": {"session.id": value},
                            }
                        )
                    }
                ]
            }
        if value == self.trace_id:
            message = (
                "split event leaked Seine"
                if self.leak_on_trace
                else json.dumps({"traceId": self.trace_id, "body": {"redacted": True}})
            )
            return {"events": [{"message": message}]}
        raise AssertionError(f"Unexpected filter {value}")


def test_production_privacy_canary_checks_session_and_trace_records(monkeypatch) -> None:
    monkeypatch.setenv(matrix.EVALUATION_INDEX_SETTLE_ENV, "0")
    monkeypatch.setattr(matrix.time, "sleep", lambda _seconds: None)
    session = _ProductionPrivacySession()
    logs = _ProductionPrivacyLogs(session)

    matrix._assert_production_content_is_redacted(
        session,
        logs,
        runtime_name=RUNTIME,
        version_id=VERSION,
    )

    assert session.invocation_payload is not None
    prompt = str(session.invocation_payload["input"]).casefold()
    assert "capital of france" in prompt and "river" in prompt
    assert "finish the answer" not in prompt and "return only" not in prompt
    assert any(value.startswith("trigger-production-privacy-") for value in logs.filters)
    assert logs.trace_id in logs.filters


def test_production_privacy_canary_rejects_guardrail_redaction(monkeypatch) -> None:
    class RedactedPrivacySession(_ProductionPrivacySession):
        def post(self, url: str, *, json: dict[str, Any], **kwargs) -> _Response:
            response = super().post(url, json=json, **kwargs)
            response._body["response"] = "[Assistant output redacted.]"
            return response

    monkeypatch.setenv(matrix.EVALUATION_INDEX_SETTLE_ENV, "0")
    monkeypatch.setattr(matrix.time, "sleep", lambda _seconds: None)
    session = RedactedPrivacySession()

    with pytest.raises(AssertionError, match="success.*response|response.*redacted|Paris|paris"):
        matrix._assert_production_content_is_redacted(
            session,
            _ProductionPrivacyLogs(session),
            runtime_name=RUNTIME,
            version_id=VERSION,
        )


def test_production_privacy_canary_catches_split_trace_content(monkeypatch) -> None:
    monkeypatch.setenv(matrix.EVALUATION_INDEX_SETTLE_ENV, "0")
    monkeypatch.setattr(matrix.time, "sleep", lambda _seconds: None)
    session = _ProductionPrivacySession()
    logs = _ProductionPrivacyLogs(session, leak_on_trace=True)

    with pytest.raises(AssertionError, match="split-telemetry"):
        matrix._assert_production_content_is_redacted(
            session,
            logs,
            runtime_name=RUNTIME,
            version_id=VERSION,
        )


def test_filtered_span_reader_collects_every_page() -> None:
    class PagedLogs:
        def filter_log_events(self, **kwargs):
            token = kwargs.get("nextToken")
            if token is None:
                return {
                    "events": [{"message": "first"}],
                    "nextToken": "page-2",
                }
            assert token == "page-2"
            return {"events": [{"message": "second"}]}

    assert matrix._filtered_log_messages(PagedLogs(), '"session"') == [
        "first",
        "second",
    ]


def test_failed_batch_evaluation_never_promotes(monkeypatch) -> None:
    monkeypatch.setattr(matrix.time, "sleep", lambda _seconds: None)
    session = _Session(batch_states=["FAILED"])

    with pytest.raises(AssertionError):
        _run(session)

    assert not any(url.endswith("/attest") or url.endswith("/promote") for _method, url in session.calls)
    assert session.production_ready is False


def test_release_scenarios_are_guardrail_safe_and_semantically_oracled() -> None:
    cases = matrix._RELEASE_EVALUATION_CASES
    assert len(cases) >= 5
    assert len({case["prompt"] for case in cases}) == len(cases)
    for case in cases:
        prompt = str(case["prompt"])
        expected = str(case["expected_response"])
        accepted = tuple(str(answer).casefold() for answer in case["accepted_answers"])
        assert "reply with exactly" not in prompt.casefold()
        assert not re.search(r"\b[0-9a-f]{8,}\b", f"{prompt} {expected}", re.IGNORECASE)
        assert accepted
        assert any(answer in expected.casefold() for answer in accepted)
        assert case["assertion"]


def test_guardrail_redaction_placeholder_fails_the_semantic_oracle() -> None:
    class RedactedSession(_Session):
        def post(self, url: str, *, json: dict[str, Any], **kwargs) -> _Response:
            if url.endswith("/slots/staging/invoke"):
                self.calls.append(("POST", url))
                return _Response(
                    {
                        "success": True,
                        "slot": "staging",
                        "versionId": VERSION,
                        "sessionId": json["sessionId"],
                        "response": "[Assistant output redacted.]",
                    }
                )
            return super().post(url, json=json, **kwargs)

    with pytest.raises(AssertionError, match="semantic oracle"):
        matrix._release_scenarios(
            RedactedSession(),
            runtime_name=RUNTIME,
            version_id=VERSION,
            count=1,
        )


def test_promotion_policy_cannot_exceed_guardrail_safe_case_inventory() -> None:
    session = _Session()
    with pytest.raises(AssertionError, match="Guardrail-safe evaluation cases"):
        matrix._release_scenarios(
            session,
            runtime_name=RUNTIME,
            version_id=VERSION,
            count=len(matrix._RELEASE_EVALUATION_CASES) + 1,
        )
    assert session.calls == []


def test_existing_production_slot_needs_no_evaluation() -> None:
    session = _Session(production_ready=True)

    _run(session)

    assert [method for method, _url in session.calls] == ["GET"]


def _budget_fixture_violations(source: str) -> list[str]:
    """Return missing ordering/cleanup guarantees in the live matrix source."""

    violations: list[str] = []
    registration = source.find("budget_key = runtime_name")
    creation = source.find("_create_integration_budget(api_session, budget_key)")
    promotion = source.find("_promote_staging_version_for_triggers(")
    finally_block = source.find("finally:")
    cleanup = source.find("_delete_integration_budget(api_session, budget_key)")
    if min(registration, creation) < 0 or registration > creation:
        violations.append("cleanup is not registered before budget creation")
    if creation < 0 or promotion < 0 or creation > promotion:
        violations.append("budget is not created before staging invocation")
    if min(finally_block, cleanup) < 0 or cleanup < finally_block:
        violations.append("budget cleanup is not guaranteed by finally")
    return violations


def test_temporary_agent_budget_is_created_and_absence_checked_on_delete() -> None:
    budget_session = _Session()

    assert matrix._create_integration_budget(budget_session, RUNTIME) == RUNTIME
    assert budget_session.budget_active is True

    matrix._delete_integration_budget(budget_session, RUNTIME)

    assert budget_session.budget_active is False
    assert budget_session.calls == [
        ("POST", f"{budget_session.base_url}/api/cost/budgets"),
        ("DELETE", f"{budget_session.base_url}/api/cost/budgets/agent/{RUNTIME}"),
        ("GET", f"{budget_session.base_url}/api/cost/budgets"),
    ]


def test_temporary_budget_cleanup_fails_closed_on_malformed_inventory() -> None:
    class MalformedInventorySession(_Session):
        def get(self, url: str, **kwargs) -> _Response:
            if url.endswith("/api/cost/budgets"):
                self.calls.append(("GET", url))
                return _Response({"scope": "agent", "key": RUNTIME})
            return super().get(url, **kwargs)

    budget_session = MalformedInventorySession()
    matrix._create_integration_budget(budget_session, RUNTIME)

    with pytest.raises(AssertionError, match="non-list"):
        matrix._delete_integration_budget(budget_session, RUNTIME)


def test_budget_create_and_finally_cleanup_removal_mutations_are_caught() -> None:
    source = inspect.getsource(matrix.test_all_four_trigger_sources_invoke_and_cleanup)
    assert _budget_fixture_violations(source) == []

    without_create = source.replace(
        "_create_integration_budget(api_session, budget_key)",
        "_budget_creation_removed()",
    )
    assert "budget is not created before staging invocation" in _budget_fixture_violations(without_create)

    without_cleanup = source.replace(
        "_delete_integration_budget(api_session, budget_key)",
        "_budget_cleanup_removed()",
    )
    assert "budget cleanup is not guaranteed by finally" in _budget_fixture_violations(without_cleanup)
