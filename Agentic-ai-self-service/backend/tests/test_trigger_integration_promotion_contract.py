"""The trigger integration harness promotes staging through real release gates."""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration import test_trigger_delivery_matrix as matrix

RUNTIME = "trigger_release_runtime"
VERSION = "0" * 32
RUNTIME_ID = "trigger_release_runtime_1234567890"


class _Response:
    def __init__(self, body: dict[str, Any], status_code: int = 200):
        self._body = body
        self.status_code = status_code
        self.text = str(body)

    def json(self) -> dict[str, Any]:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")


class _Session:
    base_url = "https://agents.example.com"

    def __init__(self, batch_states: list[str] | None = None, *, production_ready: bool = False):
        self.batch_states = list(batch_states or ["COMPLETED"])
        self.production_ready = production_ready
        self.calls: list[tuple[str, str]] = []

    def get(self, url: str, **_kwargs) -> _Response:
        self.calls.append(("GET", url))
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
