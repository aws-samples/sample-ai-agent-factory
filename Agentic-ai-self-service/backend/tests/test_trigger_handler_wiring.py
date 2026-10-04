"""The deployment Lambda's additive trigger ingress seams."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

sys.path.insert(0, "src")


def test_webhook_router_is_mounted_once():
    import app.deployment_handler as handler

    mounted = [
        route
        for route in handler.deployment_app.routes
        if getattr(route, "original_router", None) is handler.webhook_router
    ]
    assert len(mounted) == 1
    assert [(route.path, route.methods) for route in handler.webhook_router.routes] == [
        ("/hooks/{runtime_name}/{trigger_id}", {"POST"})
    ]


def test_sqs_trigger_batch_bypasses_mangum(monkeypatch):
    import app.deployment_handler as handler
    from app.services import trigger_runtime

    event = {
        "Records": [
            {
                "messageId": "m1",
                "eventSource": "aws:sqs",
                "body": "{}",
            }
        ]
    }
    dispatch = MagicMock(return_value={"batchItemFailures": []})
    mangum = MagicMock(side_effect=AssertionError("SQS must not reach Mangum"))
    monkeypatch.setattr(trigger_runtime, "dispatch_sqs_batch", dispatch)
    monkeypatch.setattr(handler, "_mangum_handler", mangum)

    assert handler.handler(event, None) == {"batchItemFailures": []}
    dispatch.assert_called_once_with(event)
    mangum.assert_not_called()


def test_non_sqs_records_are_not_claimed_by_trigger_dispatch(monkeypatch):
    import app.deployment_handler as handler
    from app.services import trigger_runtime

    event = {
        "Records": [
            {
                "eventSource": "aws:s3",
            }
        ]
    }
    dispatch = MagicMock()
    mangum = MagicMock(return_value={"statusCode": 400})
    monkeypatch.setattr(trigger_runtime, "dispatch_sqs_batch", dispatch)
    monkeypatch.setattr(handler, "_mangum_handler", mangum)

    assert handler.handler(event, None) == {"statusCode": 400}
    dispatch.assert_not_called()
    mangum.assert_called_once_with(event, None)
