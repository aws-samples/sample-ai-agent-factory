"""A delivery that loses a lease race is retried in seconds, not after the queue's hour.

Deliveries of one deployment exclude each other through its finalizer lease. The loser used to
fail its SQS record and wait out the dispatch queue's 3600-second visibility timeout, and five
losses dead-lettered it. Measured live 2026-10-01: an S3 upload that coincided with a cron
delivery on the same agent missed the live matrix's 12-minute delivery window entirely. A
contended delivery has invoked nothing, so it is now re-enqueued with a short jittered delay
and an attempt count that travels in the message.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest
from app.services import trigger_runtime as runtime

QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/111122223333/p-e-trigger-dispatch"


def _body(**extra) -> dict:
    return {
        "_agentcore_trigger_dispatch": runtime.DISPATCH_MARKER,
        "runtime_name": "runtime_one",
        "trigger_id": "trigger-one",
        "delivery_event": {"id": "delivery-one", "detail": {"k": "v"}},
        **extra,
    }


def _event(body: dict, message_id: str = "m1") -> dict:
    return {"Records": [{"messageId": message_id, "eventSource": "aws:sqs", "body": json.dumps(body)}]}


class _Sqs:
    def __init__(self, *, message_id: str | None = "requeued-1", error: Exception | None = None) -> None:
        self.sent: list[dict] = []
        self._message_id = message_id
        self._error = error

    def send_message(self, **kwargs):
        self.sent.append(kwargs)
        if self._error is not None:
            raise self._error
        return {"MessageId": self._message_id} if self._message_id else {}


@pytest.fixture(autouse=True)
def _queue_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIGGER_DISPATCH_QUEUE_URL", QUEUE_URL)


def _dispatch_raises(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    monkeypatch.setattr(runtime, "dispatch_trigger_event", MagicMock(side_effect=exc))


def _contended(monkeypatch: pytest.MonkeyPatch) -> None:
    _dispatch_raises(monkeypatch, runtime.TriggerDispatchContention("The trigger deployment is still finalizing"))


def test_a_contended_delivery_is_requeued_with_a_short_delay_and_acknowledged(monkeypatch):
    _contended(monkeypatch)
    sqs = _Sqs()

    result = runtime.dispatch_sqs_batch(_event(_body()), sqs_client=sqs)

    assert result == {"batchItemFailures": []}
    assert len(sqs.sent) == 1
    sent = sqs.sent[0]
    assert sent["QueueUrl"] == QUEUE_URL
    low, high = runtime.TRIGGER_CONTENTION_DELAY_SECONDS
    assert 0 < low <= sent["DelaySeconds"] <= high <= 900
    assert json.loads(sent["MessageBody"]) == _body(_contention_attempts=1)


def test_the_attempt_count_travels_with_the_message(monkeypatch):
    _contended(monkeypatch)
    sqs = _Sqs()

    runtime.dispatch_sqs_batch(_event(_body(_contention_attempts=7)), sqs_client=sqs)

    assert json.loads(sqs.sent[0]["MessageBody"])["_contention_attempts"] == 8


@pytest.mark.parametrize("attempts", [runtime.TRIGGER_CONTENTION_REQUEUE_LIMIT, 10**6, -1, "3", True, 2.0, None])
def test_an_exhausted_or_malformed_count_falls_back_to_the_queue_retry(monkeypatch, attempts):
    _contended(monkeypatch)
    sqs = _Sqs()

    result = runtime.dispatch_sqs_batch(_event(_body(_contention_attempts=attempts)), sqs_client=sqs)

    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    assert sqs.sent == []


@pytest.mark.parametrize("sqs", [_Sqs(error=RuntimeError("throttled")), _Sqs(message_id=None)])
def test_a_failed_requeue_keeps_the_original_for_the_queue_retry(monkeypatch, sqs):
    _contended(monkeypatch)

    result = runtime.dispatch_sqs_batch(_event(_body()), sqs_client=sqs)

    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}


def test_a_missing_queue_url_keeps_the_original(monkeypatch):
    _contended(monkeypatch)
    monkeypatch.delenv("TRIGGER_DISPATCH_QUEUE_URL")
    sqs = _Sqs()

    result = runtime.dispatch_sqs_batch(_event(_body()), sqs_client=sqs)

    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    assert sqs.sent == []


def test_other_dispatch_errors_are_not_requeued_and_name_their_reason(monkeypatch, caplog):
    _dispatch_raises(monkeypatch, runtime.TriggerDispatchError("The trigger coordinates are invalid"))
    sqs = _Sqs()

    with caplog.at_level(logging.ERROR, logger=runtime.logger.name):
        result = runtime.dispatch_sqs_batch(_event(_body()), sqs_client=sqs)

    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    assert sqs.sent == []
    assert "TriggerDispatchError: The trigger coordinates are invalid" in caplog.text


def test_an_unexpected_error_is_logged_by_type_alone(monkeypatch, caplog):
    _dispatch_raises(monkeypatch, ValueError("detail that may echo the delivery"))

    with caplog.at_level(logging.ERROR, logger=runtime.logger.name):
        result = runtime.dispatch_sqs_batch(_event(_body()), sqs_client=_Sqs())

    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    assert "ValueError" in caplog.text
    assert "detail that may echo the delivery" not in caplog.text


def test_contention_is_a_dispatch_error_for_every_existing_caller():
    assert issubclass(runtime.TriggerDispatchContention, runtime.TriggerDispatchError)
