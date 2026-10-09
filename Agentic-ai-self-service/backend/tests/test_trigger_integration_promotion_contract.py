"""The trigger integration harness promotes staging through real release gates."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from tests.integration import test_trigger_delivery_matrix as matrix

RUNTIME = "trigger_release_runtime"
VERSION = "0" * 32
RUNTIME_ID = "trigger_release_runtime_1234567890"


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

    def __init__(self, batch_states: list[str] | None = None, *, production_ready: bool = False):
        self.batch_states = list(batch_states or ["COMPLETED"])
        self.production_ready = production_ready
        self.budget_active = False
        self.calls: list[tuple[str, str]] = []

    def get(self, url: str, **_kwargs) -> _Response:
        self.calls.append(("GET", url))
        if url.endswith("/api/cost/budgets"):
            budgets = (
                [{"scope": "agent", "key": RUNTIME, "limit_usd": 100.0, "warn_pct": 100}]
                if self.budget_active
                else []
            )
            return _Response(budgets)
        if url.endswith("/slots"):
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
            marker = str(json["input"]).removeprefix("Reply with exactly ").removesuffix(" and nothing else.")
            return _Response(
                {
                    "success": True,
                    "slot": "staging",
                    "versionId": VERSION,
                    "sessionId": json["sessionId"],
                    "response": marker,
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
    def filter_log_events(self, **_kwargs) -> dict[str, list[dict[str, str]]]:
        return {"events": [{"message": "session visible"}]}


def _run(session: _Session) -> None:
    matrix._promote_staging_version_for_triggers(
        session,
        _Logs(),
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


def test_failed_batch_evaluation_never_promotes(monkeypatch) -> None:
    monkeypatch.setattr(matrix.time, "sleep", lambda _seconds: None)
    session = _Session(batch_states=["FAILED"])

    with pytest.raises(AssertionError):
        _run(session)

    assert not any(url.endswith("/attest") or url.endswith("/promote") for _method, url in session.calls)
    assert session.production_ready is False


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
