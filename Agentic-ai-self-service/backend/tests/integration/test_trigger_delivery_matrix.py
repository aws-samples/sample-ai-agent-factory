"""Real-AWS delivery and cleanup matrix for all four runtime trigger types.

This is deliberately one customer journey over one HTTP runtime:

1. Deploy through the authenticated product API and wait for success.
2. Create cron, custom EventBridge, S3, and signed-webhook triggers through
   the same API used by the UI.
3. Fire every real source. No trigger service is mocked.
4. Require a completed DynamoDB delivery item for every trigger. The product
   writes that marker only after ``InvokeAgentRuntime`` returns.
5. Delete through the product API and prove the trigger row plus its
   EventBridge rule or webhook secret is no longer active.
6. Remove the temporary S3 source and require the runtime's durable
   ``delete_status=deleted`` tombstone.

Completed delivery rows intentionally remain for the seven-day deduplication
window and carry DynamoDB TTL. They are bounded tombstones, not live trigger
resources, and this test checks that distinction explicitly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

import boto3
import pytest
import requests
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from tests.integration.conftest import (
    DeploymentCleanupTracker,
    TrackedDeployment,
)

TRIGGER_TYPES_UNDER_TEST = (
    "cron",
    "eventbridge",
    "s3",
    "webhook",
)
DELIVERY_PARTITION_PREFIX = "!delivery#"

# Production Guardrails deliberately flag prompt-injection-shaped commands and
# credential-like random tokens. Release evaluation therefore uses ordinary,
# low-entropy factual questions with semantic answer oracles; session IDs, not
# model output, provide per-run correlation.
_RELEASE_EVALUATION_CASES: tuple[dict[str, Any], ...] = (
    {
        "prompt": "What is two plus two?",
        "expected_response": "Four.",
        "accepted_answers": ("4", "four"),
        "assertion": "The response correctly states that two plus two equals four.",
    },
    {
        "prompt": "What color is a clear daytime sky?",
        "expected_response": "Blue.",
        "accepted_answers": ("blue",),
        "assertion": "The response correctly identifies blue as the usual color of a clear daytime sky.",
    },
    {
        "prompt": "How many days are in one week?",
        "expected_response": "Seven.",
        "accepted_answers": ("7", "seven"),
        "assertion": "The response correctly states that one week has seven days.",
    },
    {
        "prompt": "What is the opposite of hot?",
        "expected_response": "Cold.",
        "accepted_answers": ("cold",),
        "assertion": "The response correctly identifies cold as the opposite of hot.",
    },
    {
        "prompt": "Which planet do humans live on?",
        "expected_response": "Earth.",
        "accepted_answers": ("earth",),
        "assertion": "The response correctly identifies Earth as the planet humans live on.",
    },
    {
        "prompt": "How many months are in one year?",
        "expected_response": "Twelve.",
        "accepted_answers": ("12", "twelve"),
        "assertion": "The response correctly states that one year has twelve months.",
    },
    {
        "prompt": "What shape has three sides?",
        "expected_response": "A triangle.",
        "accepted_answers": ("triangle",),
        "assertion": "The response correctly identifies a triangle as a three-sided shape.",
    },
    {
        "prompt": "Which animal is known for barking?",
        "expected_response": "A dog.",
        "accepted_answers": ("dog",),
        "assertion": "The response correctly identifies a dog as an animal known for barking.",
    },
)
EVENT_BUS_NAME = "default"
TRIGGERS_TABLE_ENV = "INTEGRATION_TRIGGERS_TABLE_NAME"
BATCH_EVALUATION_SPANS_LOG_GROUP = "aws/spans"
EVALUATION_INDEX_SETTLE_ENV = "INTEGRATION_EVALUATION_INDEX_SETTLE_SECONDS"
DEFAULT_EVALUATION_INDEX_SETTLE_SECONDS = 180
DELIVERY_TIMEOUT_ENV = "INTEGRATION_TRIGGER_TIMEOUT_SECONDS"
DEFAULT_DELIVERY_TIMEOUT_SECONDS = 12 * 60
DELETE_TIMEOUT_SECONDS = 12 * 60
POLL_INTERVAL_SECONDS = 10
EXPECTED_DELIVERY_RETENTION_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class CreatedTrigger:
    trigger_type: str
    trigger_id: str
    row: dict[str, Any]
    expected_delivery_id: str | None = None


@pytest.fixture(scope="session")
def integration_triggers_table_name() -> str:
    """Exact platform table used for durable delivery verification."""

    value = os.environ.get(TRIGGERS_TABLE_ENV, "").strip()
    if not value:
        pytest.skip(
            f"{TRIGGERS_TABLE_ENV} is required for the live trigger matrix; "
            "API registration alone is not delivery evidence"
        )
    return value


def _base_url(api_session: requests.Session) -> str:
    value = getattr(api_session, "base_url", "")
    assert isinstance(value, str) and value
    return value.rstrip("/")


def _delivery_timeout() -> int:
    raw = os.environ.get(
        DELIVERY_TIMEOUT_ENV,
        str(DEFAULT_DELIVERY_TIMEOUT_SECONDS),
    )
    try:
        value = int(raw)
    except ValueError:
        pytest.fail(f"{DELIVERY_TIMEOUT_ENV} must be an integer", pytrace=False)
    if value < 60:
        pytest.fail(
            f"{DELIVERY_TIMEOUT_ENV} must be at least 60 seconds",
            pytrace=False,
        )
    return value


def _start_http_runtime(
    api_session: requests.Session,
    deployment_cleanup: DeploymentCleanupTracker,
    wait_for_deployment,
) -> tuple[TrackedDeployment, str, dict[str, Any]]:
    """Deploy one known HTTP runtime and register cleanup before polling."""

    token = uuid.uuid4().hex[:12]
    runtime_name = f"it_trigger_{token}"
    node_id = f"it-trigger-{token}-{uuid.uuid4().hex[:8]}"
    payload = {
        "nodeId": node_id,
        "config": {
            "name": runtime_name,
            "entrypoint": "agent.py",
            "framework": "strands_agents",
            "model": {
                "modelId": os.environ.get(
                    "INTEGRATION_MODEL_ID",
                    "us.anthropic.claude-sonnet-5",
                )
            },
            "systemPrompt": (
                "Answer ordinary factual integration-test questions concisely. Do not call external tools."
            ),
            "deploymentType": "direct_code_deploy",
            "pythonRuntime": "PYTHON_3_13",
            "protocol": "HTTP",
            "idleTimeout": 300,
            "maxLifetime": 3600,
            "enableOtel": False,
            "multiAgentPattern": "none",
        },
    }
    try:
        response = api_session.post(
            f"{_base_url(api_session)}/api/deploy",
            json=payload,
            timeout=60,
        )
    except requests.RequestException:
        # An accepted request can outlive a lost HTTP response. Register the
        # recovered row before re-raising so fixture teardown still owns it.
        deployment_cleanup.recover_by_node_id(node_id)
        raise

    assert response.status_code == 202, f"POST /api/deploy returned {response.status_code}: {response.text}"
    body = response.json()
    deployment_id = body.get("deploymentId")
    assert isinstance(deployment_id, str) and deployment_id
    record = deployment_cleanup.track(deployment_id)

    status = wait_for_deployment(deployment_id)
    record.last_status = status
    assert status.get("status") == "succeeded", (
        f"Trigger-matrix runtime deployment failed: {status.get('error_details') or status}"
    )
    assert status.get("runtime_protocol") == "HTTP"
    assert isinstance(status.get("runtime_id"), str) and status["runtime_id"]
    assert isinstance(status.get("runtime_arn"), str) and status["runtime_arn"]
    assert isinstance(status.get("runtime_endpoint"), str) and status["runtime_endpoint"]
    assert isinstance(status.get("created_resources"), list)
    assert status["created_resources"], "Succeeded runtime has no teardown manifest"
    deployment_cleanup.bind_runtime(record, status["runtime_id"])
    return record, runtime_name, status


def _create_source_bucket(region: str) -> tuple[Any, str, str]:
    """Create an encrypted, private S3 source with EventBridge notifications."""

    account_id = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    bucket_name = f"agentcore-trigger-it-{account_id}-{uuid.uuid4().hex[:12]}"
    object_prefix = f"events/{uuid.uuid4().hex[:12]}/"
    client = boto3.client("s3", region_name=region)
    create_args: dict[str, Any] = {"Bucket": bucket_name}
    if region != "us-east-1":
        create_args["CreateBucketConfiguration"] = {
            "LocationConstraint": region,
        }
    try:
        client.create_bucket(**create_args)
        client.put_public_access_block(
            Bucket=bucket_name,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        client.put_bucket_encryption(
            Bucket=bucket_name,
            ServerSideEncryptionConfiguration={
                "Rules": [
                    {
                        "ApplyServerSideEncryptionByDefault": {
                            "SSEAlgorithm": "AES256",
                        }
                    }
                ]
            },
        )
        client.put_bucket_tagging(
            Bucket=bucket_name,
            Tagging={
                "TagSet": [
                    {"Key": "ManagedBy", "Value": "agentcore-flows-tests"},
                    {"Key": "Purpose", "Value": "trigger-integration-source"},
                ]
            },
        )
        client.put_bucket_notification_configuration(
            Bucket=bucket_name,
            NotificationConfiguration={"EventBridgeConfiguration": {}},
        )
        notification = client.get_bucket_notification_configuration(Bucket=bucket_name)
        assert notification.get("EventBridgeConfiguration") == {}
    except BaseException as exc:
        # CreateBucket can succeed before a transport error reaches the caller.
        # Always attempt the deterministic-name cleanup, even when setup did not
        # return far enough for the test's outer finally block to own it.
        try:
            _remove_source_bucket(client, bucket_name)
        except Exception as cleanup_exc:  # noqa: BLE001 - preserve leak detail
            exc.add_note(f"Temporary S3 source cleanup also failed: {type(cleanup_exc).__name__}: {cleanup_exc}")
        raise
    return client, bucket_name, object_prefix


def _remove_source_bucket(client: Any, bucket_name: str) -> None:
    """Delete every test object, then the temporary bucket, and wait for absence."""

    try:
        client.put_bucket_notification_configuration(
            Bucket=bucket_name,
            NotificationConfiguration={},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {
            "NoSuchBucket",
            "NoSuchBucketException",
        }:
            raise

    keys: list[dict[str, str]] = []
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket_name):
            keys.extend({"Key": item["Key"]} for item in page.get("Contents", []) if item.get("Key"))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {
            "NoSuchBucket",
            "NoSuchBucketException",
        }:
            return
        raise

    for start in range(0, len(keys), 1000):
        client.delete_objects(
            Bucket=bucket_name,
            Delete={"Objects": keys[start : start + 1000], "Quiet": True},
        )
    client.delete_bucket(Bucket=bucket_name)
    client.get_waiter("bucket_not_exists").wait(
        Bucket=bucket_name,
        WaiterConfig={"Delay": 2, "MaxAttempts": 30},
    )


def _trigger_row(table: Any, runtime_name: str, trigger_id: str) -> dict[str, Any] | None:
    return table.get_item(
        Key={
            "runtime_name": runtime_name,
            "trigger_id": trigger_id,
        },
        ConsistentRead=True,
    ).get("Item")


def _create_trigger(
    api_session: requests.Session,
    table: Any,
    *,
    runtime_name: str,
    runtime_arn: str,
    trigger_type: str,
    payload: dict[str, Any],
) -> CreatedTrigger:
    response = api_session.post(
        f"{_base_url(api_session)}/api/runtimes/{runtime_name}/triggers",
        json=payload,
        timeout=90,
    )
    assert response.status_code == 200, (
        f"Creating {trigger_type} trigger returned {response.status_code}: {response.text}"
    )
    body = response.json()
    assert body.get("type") == trigger_type
    assert body.get("status") == "active"
    assert body.get("runtime_name") == runtime_name
    assert body.get("target_runtime_arn") == runtime_arn
    trigger_id = body.get("trigger_id")
    assert isinstance(trigger_id, str) and trigger_id

    row = _trigger_row(table, runtime_name, trigger_id)
    assert row is not None, f"{trigger_type} trigger became active without a durable row"
    assert row.get("status") == "active"
    assert row.get("type") == trigger_type
    assert row.get("target_runtime_arn") == runtime_arn
    if trigger_type == "webhook":
        assert response.headers.get("Cache-Control") == "no-store"
        webhook_path = body.get("webhook_path")
        assert isinstance(webhook_path, str) and webhook_path
        assert row.get("webhook_path") == webhook_path
        secret = body.get("webhook_signing_secret")
        assert isinstance(secret, str) and len(secret) == 64
        assert isinstance(row.get("webhook_secret_ref"), str)
        assert "webhook_signing_secret" not in row
        # Keep the one-time value only in this process-local response copy so
        # the signed-webhook leg can exercise the user-visible contract.
        row["_one_time_signing_secret"] = secret
    else:
        assert isinstance(row.get("eventbridge_rule_arn"), str)
        assert body.get("webhook_signing_secret") is None
    return CreatedTrigger(
        trigger_type=trigger_type,
        trigger_id=trigger_id,
        row=row,
    )


def _delivery_partition(runtime_name: str, trigger_id: str) -> str:
    return f"{DELIVERY_PARTITION_PREFIX}{runtime_name}#{trigger_id}"


def _completed_delivery(
    table: Any,
    trigger: CreatedTrigger,
    *,
    runtime_name: str,
) -> dict[str, Any] | None:
    partition = _delivery_partition(runtime_name, trigger.trigger_id)
    if trigger.expected_delivery_id:
        items = [
            table.get_item(
                Key={
                    "runtime_name": partition,
                    "trigger_id": trigger.expected_delivery_id,
                },
                ConsistentRead=True,
            ).get("Item")
        ]
    else:
        items = table.query(
            KeyConditionExpression=Key("runtime_name").eq(partition),
            ConsistentRead=True,
        ).get("Items", [])

    for item in items:
        if not item or item.get("delivery_status") != "completed":
            continue
        assert item.get("item_kind") == "trigger_delivery"
        assert item.get("source_runtime_name") == runtime_name
        assert item.get("source_trigger_id") == trigger.trigger_id
        completed_at = int(item.get("completed_at") or 0)
        ttl = int(item.get("ttl") or 0)
        assert completed_at > 0
        assert ttl - completed_at == EXPECTED_DELIVERY_RETENTION_SECONDS, (
            "Completed trigger delivery must retain exactly seven days of "
            f"deduplication evidence, got {ttl - completed_at} seconds"
        )
        assert "claim_token" not in item
        assert "claim_expires_at" not in item
        return item
    return None


def _wait_for_all_deliveries(
    table: Any,
    triggers: list[CreatedTrigger],
    *,
    runtime_name: str,
) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + _delivery_timeout()
    pending = {trigger.trigger_id: trigger for trigger in triggers}
    completed: dict[str, dict[str, Any]] = {}
    while time.monotonic() < deadline and pending:
        for trigger_id, trigger in list(pending.items()):
            item = _completed_delivery(
                table,
                trigger,
                runtime_name=runtime_name,
            )
            if item is not None:
                completed[trigger_id] = item
                pending.pop(trigger_id)
        if pending:
            time.sleep(POLL_INTERVAL_SECONDS)

    assert not pending, (
        "The following real trigger sources never reached a completed "
        f"AgentCore delivery: "
        f"{sorted(trigger.trigger_type for trigger in pending.values())}"
    )
    return completed


def _fire_custom_event(
    events_client: Any,
    *,
    source: str,
    detail_type: str,
    nonce: str,
) -> str:
    result = events_client.put_events(
        Entries=[
            {
                "EventBusName": EVENT_BUS_NAME,
                "Source": source,
                "DetailType": detail_type,
                "Detail": json.dumps(
                    {"nonce": nonce},
                    separators=(",", ":"),
                ),
            }
        ]
    )
    assert result.get("FailedEntryCount") == 0, result.get("Entries")
    event_id = result["Entries"][0].get("EventId")
    assert isinstance(event_id, str) and event_id
    return event_id


def _fire_signed_webhook(
    api_session: requests.Session,
    trigger: CreatedTrigger,
    *,
    nonce: str,
) -> str:
    body = json.dumps(
        {"nonce": nonce, "source": "integration-test"},
        separators=(",", ":"),
    ).encode("utf-8")
    timestamp = str(int(time.time()))
    delivery_id = f"integration-webhook-{uuid.uuid4().hex}"
    secret = trigger.row.get("_one_time_signing_secret")
    path = trigger.row.get("webhook_path")
    assert isinstance(secret, str) and secret
    assert isinstance(path, str) and path
    message = timestamp.encode("ascii") + b"." + delivery_id.encode("utf-8") + b"." + body
    signature = (
        "v1="
        + hmac.new(
            secret.encode("ascii"),
            message,
            hashlib.sha256,
        ).hexdigest()
    )
    # Deliberately use an unsigned session. The public route authenticates the
    # body HMAC, not the browser's Cognito bearer token.
    headers = {
        "Content-Type": "application/json",
        "X-AgentCore-Timestamp": timestamp,
        "X-AgentCore-Delivery-Id": delivery_id,
        "X-AgentCore-Signature": signature,
    }
    response = requests.post(
        f"{_base_url(api_session)}{path}",
        data=body,
        headers=headers,
        timeout=60,
    )
    assert response.status_code == 202, f"Signed webhook returned {response.status_code}: {response.text}"
    result = response.json()
    assert result == {
        "accepted": True,
        "duplicate": False,
        "trigger_id": trigger.trigger_id,
        "delivery_id": delivery_id,
    }

    replay = requests.post(
        f"{_base_url(api_session)}{path}",
        data=body,
        headers=headers,
        timeout=60,
    )
    assert replay.status_code == 202, f"Signed webhook replay returned {replay.status_code}: {replay.text}"
    assert replay.json() == {
        "accepted": True,
        "duplicate": True,
        "trigger_id": trigger.trigger_id,
        "delivery_id": delivery_id,
    }

    conflict_body = json.dumps(
        {"nonce": nonce + "-conflict", "source": "integration-test"},
        separators=(",", ":"),
    ).encode("utf-8")
    conflict_message = timestamp.encode("ascii") + b"." + delivery_id.encode("utf-8") + b"." + conflict_body
    conflict_headers = {
        **headers,
        "X-AgentCore-Signature": (
            "v1="
            + hmac.new(
                secret.encode("ascii"),
                conflict_message,
                hashlib.sha256,
            ).hexdigest()
        ),
    }
    conflict = requests.post(
        f"{_base_url(api_session)}{path}",
        data=conflict_body,
        headers=conflict_headers,
        timeout=60,
    )
    assert conflict.status_code == 409, (
        f"Signed delivery-id/body conflict returned {conflict.status_code}: {conflict.text}"
    )
    assert conflict.json().get("detail") == ("Webhook delivery ID was already used for a different payload")
    return delivery_id


def _rule_name(rule_arn: str) -> str:
    value = rule_arn.rsplit("/", 1)[-1]
    assert value
    return value


def _wait_for_rule_ready(
    events_client: Any,
    trigger: CreatedTrigger,
    *,
    runtime_name: str,
) -> None:
    """Require the enabled, tagged rule and its dispatch target before firing."""

    rule_arn = trigger.row.get("eventbridge_rule_arn")
    assert isinstance(rule_arn, str) and rule_arn
    rule_name = _rule_name(rule_arn)
    deadline = time.monotonic() + 60
    last_observed: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            rule = events_client.describe_rule(
                Name=rule_name,
                EventBusName=EVENT_BUS_NAME,
            )
            targets = events_client.list_targets_by_rule(
                Rule=rule_name,
                EventBusName=EVENT_BUS_NAME,
            ).get("Targets", [])
            tags = {
                item.get("Key"): item.get("Value")
                for item in events_client.list_tags_for_resource(ResourceARN=rule_arn).get("Tags", [])
            }
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == ("ResourceNotFoundException"):
                time.sleep(2)
                continue
            raise
        last_observed = {
            "state": rule.get("State"),
            "arn": rule.get("Arn"),
            "target_ids": [target.get("Id") for target in targets],
            "tags": tags,
        }
        if (
            rule.get("State") == "ENABLED"
            and rule.get("Arn") == rule_arn
            and any(
                target.get("Id") == f"dispatch-{trigger.trigger_id}" and str(target.get("Arn") or "").startswith("arn:")
                for target in targets
            )
            and tags.get("ManagedBy") == "agentcore-flows"
            and tags.get("Purpose") == "runtime-trigger"
            and tags.get("TriggerId") == trigger.trigger_id
            and tags.get("RuntimeName") == runtime_name
            and tags.get("AgentCoreStack")
        ):
            return
        time.sleep(2)
    raise AssertionError(f"{trigger.trigger_type} rule {rule_name} never became dispatch-ready: {last_observed}")


def _assert_webhook_secret_owned(
    secrets_client: Any,
    trigger: CreatedTrigger,
) -> None:
    secret_ref = trigger.row.get("webhook_secret_ref")
    assert isinstance(secret_ref, str) and secret_ref
    metadata = secrets_client.describe_secret(SecretId=secret_ref)
    tags = {item.get("Key"): item.get("Value") for item in metadata.get("Tags", [])}
    assert str(metadata.get("Name") or "").startswith("agentcore-trigger/")
    assert tags.get("ManagedBy") == "agentcore-flows"
    assert tags.get("Purpose") == "trigger-webhook-hmac"
    assert tags.get("owner_sub")
    assert metadata.get("DeletedDate") is None


def _wait_for_rule_absence(events_client: Any, rule_arn: str) -> None:
    deadline = time.monotonic() + 60
    rule_name = _rule_name(rule_arn)
    while time.monotonic() < deadline:
        try:
            events_client.describe_rule(
                Name=rule_name,
                EventBusName=EVENT_BUS_NAME,
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == ("ResourceNotFoundException"):
                return
            raise
        time.sleep(2)
    raise AssertionError(f"EventBridge rule {rule_name} still exists after delete")


def _wait_for_secret_inactive(secrets_client: Any, secret_ref: str) -> None:
    """Require the force-deleted key to be absent or already inaccessible."""

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            metadata = secrets_client.describe_secret(SecretId=secret_ref)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == ("ResourceNotFoundException"):
                return
            raise

        if metadata.get("DeletedDate") is not None:
            try:
                secrets_client.get_secret_value(SecretId=secret_ref)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in {
                    "InvalidRequestException",
                    "ResourceNotFoundException",
                }:
                    return
                raise
            raise AssertionError("A webhook secret scheduled for deletion is still readable")
        time.sleep(2)
    raise AssertionError("Webhook secret is still active after trigger deletion")


def _delete_trigger_and_verify(
    api_session: requests.Session,
    table: Any,
    events_client: Any,
    secrets_client: Any,
    *,
    runtime_name: str,
    trigger: CreatedTrigger,
    allow_absent: bool = False,
) -> None:
    deadline = time.monotonic() + DELETE_TIMEOUT_SECONDS
    while True:
        response = api_session.delete(
            f"{_base_url(api_session)}/api/runtimes/{runtime_name}/triggers/{trigger.trigger_id}",
            timeout=90,
        )
        if response.status_code == 409 and time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        if allow_absent and response.status_code == 404:
            break
        assert response.status_code == 200, (
            f"Deleting {trigger.trigger_type} trigger returned {response.status_code}: {response.text}"
        )
        result = response.json()
        assert result.get("success") is True
        assert result.get("trigger_id") == trigger.trigger_id
        break

    assert _trigger_row(table, runtime_name, trigger.trigger_id) is None
    rule_arn = trigger.row.get("eventbridge_rule_arn")
    if isinstance(rule_arn, str) and rule_arn:
        _wait_for_rule_absence(events_client, rule_arn)
    secret_ref = trigger.row.get("webhook_secret_ref")
    if isinstance(secret_ref, str) and secret_ref:
        _wait_for_secret_inactive(secrets_client, secret_ref)


def _create_integration_budget(
    api_session: requests.Session,
    runtime_name: str,
) -> str:
    """Create bounded authorization capacity for this one synthetic runtime."""

    response = api_session.post(
        f"{_base_url(api_session)}/api/cost/budgets",
        json={
            "scope": "agent",
            "key": runtime_name,
            "limit_usd": 100.0,
            "warn_pct": 100,
        },
        timeout=60,
    )
    assert response.status_code == 200, (
        f"Creating temporary trigger budget returned {response.status_code}: {response.text}"
    )
    body = response.json()
    assert body.get("scope") == "agent"
    assert body.get("key") == runtime_name
    assert float(body.get("limit_usd") or 0) == 100.0
    return runtime_name


def _delete_integration_budget(
    api_session: requests.Session,
    budget_key: str,
) -> None:
    """Delete the matrix's runtime-specific budget definition idempotently."""

    response = api_session.delete(
        f"{_base_url(api_session)}/api/cost/budgets/agent/{budget_key}",
        timeout=60,
    )
    assert response.status_code == 200, (
        f"Deleting temporary trigger budget returned {response.status_code}: {response.text}"
    )
    assert response.json().get("deleted") == {
        "scope": "agent",
        "key": budget_key,
    }

    listed = api_session.get(
        f"{_base_url(api_session)}/api/cost/budgets",
        timeout=60,
    )
    assert listed.status_code == 200, (
        f"Listing budgets after temporary-budget deletion returned {listed.status_code}: {listed.text}"
    )
    listed_body = listed.json()
    assert isinstance(listed_body, list), f"Budget inventory returned a non-list body: {listed_body!r}"
    assert not any(
        item.get("scope") == "agent" and item.get("key") == budget_key for item in listed_body if isinstance(item, dict)
    ), f"Temporary trigger budget agent/{budget_key} is still visible after deletion"


def _integration_timeout(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        pytest.fail(f"{name} must be an integer", pytrace=False)
    if value < 60:
        pytest.fail(f"{name} must be at least 60 seconds", pytrace=False)
    return value


def _release_scenarios(
    api_session: requests.Session,
    *,
    runtime_name: str,
    version_id: str,
    count: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Create deterministic real staging sessions for production evaluation."""

    scenarios: list[dict[str, Any]] = []
    session_ids: list[str] = []
    assert count <= len(_RELEASE_EVALUATION_CASES), (
        f"Promotion policy requires {count} sessions but the trigger matrix has only "
        f"{len(_RELEASE_EVALUATION_CASES)} Guardrail-safe evaluation cases"
    )
    scenario_token = uuid.uuid4().hex[:12]
    for index, case in enumerate(_RELEASE_EVALUATION_CASES[:count]):
        session_id = f"trigger-eval-{uuid.uuid4()}"
        prompt = str(case["prompt"])
        response = api_session.post(
            f"{_base_url(api_session)}/api/runtimes/{runtime_name}/slots/staging/invoke",
            json={"input": prompt, "sessionId": session_id},
            timeout=180,
        )
        assert response.status_code == 200, (
            f"Staging evaluation session {index + 1} returned {response.status_code}: {response.text}"
        )
        body = response.json()
        assert body.get("success") is True, body
        assert body.get("slot") == "staging"
        assert body.get("versionId") == version_id
        assert body.get("sessionId") == session_id
        model_response = str(body.get("response") or "").strip()
        accepted_answers = tuple(str(answer).casefold() for answer in case["accepted_answers"])
        assert any(answer in model_response.casefold() for answer in accepted_answers), (
            f"Staging evaluation session {index + 1} did not satisfy its semantic oracle: "
            f"expected one of {accepted_answers!r}, got {model_response!r}"
        )
        session_ids.append(session_id)
        scenarios.append(
            {
                "sessionId": session_id,
                "testScenarioId": f"trigger-release-{scenario_token}-{index + 1}",
                "assertions": [str(case["assertion"]), "The response uses no external tools."],
                "expectedToolNames": [],
                "turns": [
                    {
                        "prompt": prompt,
                        "expectedResponse": str(case["expected_response"]),
                    }
                ],
            }
        )
    return scenarios, session_ids


def _evaluation_settle_seconds() -> int:
    raw = os.environ.get(
        EVALUATION_INDEX_SETTLE_ENV,
        str(DEFAULT_EVALUATION_INDEX_SETTLE_SECONDS),
    )
    try:
        seconds = int(raw)
    except ValueError:
        pytest.fail(f"{EVALUATION_INDEX_SETTLE_ENV} must be an integer", pytrace=False)
    if not 0 <= seconds <= 600:
        pytest.fail(
            f"{EVALUATION_INDEX_SETTLE_ENV} must be in 0..600 seconds",
            pytrace=False,
        )
    return seconds


def _filtered_log_messages(logs_client: Any, filter_pattern: str) -> list[str]:
    """Read every bounded page for one exact span correlation key."""

    messages: list[str] = []
    request: dict[str, Any] = {
        "logGroupName": BATCH_EVALUATION_SPANS_LOG_GROUP,
        "filterPattern": filter_pattern,
        "limit": 100,
    }
    seen_tokens: set[str] = set()
    for _page in range(20):
        response = logs_client.filter_log_events(**request)
        messages.extend(str(event.get("message") or "") for event in response.get("events", []))
        token = response.get("nextToken")
        if not token or token in seen_tokens:
            return messages
        seen_tokens.add(token)
        request["nextToken"] = token
    raise AssertionError(f"Span query exceeded 20 pages for filter {filter_pattern!r}; refusing partial evidence")


def _is_evaluation_span(
    message: str,
    *,
    session_id: str,
    service_name: str,
    prompt: str,
    accepted_answers: tuple[str, ...],
) -> bool:
    """Whether one record is the exact content-bearing staging evaluator span."""

    try:
        record = json.loads(message)
    except (TypeError, ValueError):
        return False
    if not isinstance(record, dict):
        return False
    attributes = record.get("attributes") or {}
    resource = record.get("resource") or {}
    resource_attributes = resource.get("attributes") if isinstance(resource, dict) else {}
    if not isinstance(attributes, dict) or not isinstance(resource_attributes, dict):
        return False
    observed_session = attributes.get("session.id") or record.get("attributes.session.id")
    observed_service = resource_attributes.get("service.name") or record.get("resource.attributes.service.name")
    trace_id = str(record.get("traceId") or "")
    span_id = str(record.get("spanId") or "")
    observed_prompt = str(
        attributes.get("gen_ai.task.input") or attributes.get("agentcore.invocation.user_prompt") or ""
    )
    observed_response = str(
        attributes.get("gen_ai.task.output") or attributes.get("agentcore.invocation.agent_response") or ""
    ).casefold()
    return (
        attributes.get("gen_ai.operation.name") == "invoke_agent"
        and observed_session == session_id
        and observed_service == service_name
        and observed_prompt == prompt
        and any(answer in observed_response for answer in accepted_answers)
        and re.fullmatch(r"[0-9a-f]{32}", trace_id) is not None
        and re.fullmatch(r"[0-9a-f]{16}", span_id) is not None
    )


def _wait_for_evaluation_sessions(
    logs_client: Any,
    *,
    agentcore_runtime_name: str,
    session_ids: list[str],
) -> None:
    """Wait until every staging session has an exact AgentCore evaluation span."""

    service_name = f"{agentcore_runtime_name}.DEFAULT"
    pending = set(session_ids)
    expected_by_session = {session_id: _RELEASE_EVALUATION_CASES[index] for index, session_id in enumerate(session_ids)}
    deadline = time.monotonic() + _integration_timeout(
        "INTEGRATION_EVALUATION_LOG_TIMEOUT_SECONDS",
        10 * 60,
    )
    while pending and time.monotonic() < deadline:
        for session_id in list(pending):
            try:
                messages = _filtered_log_messages(
                    logs_client,
                    f'"{session_id}"',
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                    continue
                raise
            case = expected_by_session[session_id]
            prompt = str(case["prompt"])
            accepted_answers = tuple(str(answer).casefold() for answer in case["accepted_answers"])
            # Content is permitted only on this explicitly marked staging
            # session. A prompt from any other release scenario is cross-session
            # leakage and must fail even if the expected span also exists.
            assert not any(
                str(other["prompt"]) in message
                for other in _RELEASE_EVALUATION_CASES
                if str(other["prompt"]) != prompt
                for message in messages
            ), f"Another staging prompt leaked into evaluation session {session_id}"
            if any(
                _is_evaluation_span(
                    message,
                    session_id=session_id,
                    service_name=service_name,
                    prompt=prompt,
                    accepted_answers=accepted_answers,
                )
                for message in messages
            ):
                pending.remove(session_id)
        if pending:
            time.sleep(POLL_INTERVAL_SECONDS)
    assert not pending, (
        f"Staging sessions have no exact content-bearing {service_name} evaluation spans in "
        f"{BATCH_EVALUATION_SPANS_LOG_GROUP}: {sorted(pending)}"
    )

    settle_seconds = _evaluation_settle_seconds()
    if settle_seconds:
        time.sleep(settle_seconds)


def _wait_for_staging_slot(
    api_session: requests.Session,
    *,
    runtime_name: str,
    version_id: str,
) -> dict[str, Any]:
    """Wait only for the status-update step's bounded cross-table publication."""

    url = f"{_base_url(api_session)}/api/runtimes/{runtime_name}/slots"
    deadline = time.monotonic() + _integration_timeout(
        "INTEGRATION_SLOT_PUBLICATION_TIMEOUT_SECONDS",
        60,
    )
    attempts = 0
    while time.monotonic() < deadline:
        response = api_session.get(url, timeout=60)
        attempts += 1
        if response.status_code == 404:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        response.raise_for_status()
        slots = response.json()
        assert isinstance(slots, dict), f"Slots publication returned a non-object: {slots!r}"
        if slots.get("production_version_id") == version_id or slots.get("staging_version_id") == version_id:
            return slots
        raise AssertionError(f"Slots published the wrong version after {attempts} attempt(s): {slots}")
    raise AssertionError(f"Slots did not publish version {version_id} within the bounded wait")


def _promote_staging_version_for_triggers(
    api_session: requests.Session,
    logs_client: Any,
    *,
    runtime_name: str,
    status: dict[str, Any],
) -> None:
    """Make the exact succeeded version production-ready through the real gate."""

    version_id = status.get("version_id")
    runtime_id = status.get("runtime_id")
    assert isinstance(version_id, str) and version_id
    assert isinstance(runtime_id, str) and runtime_id

    slots = _wait_for_staging_slot(
        api_session,
        runtime_name=runtime_name,
        version_id=version_id,
    )
    if slots.get("production_version_id") == version_id:
        return
    assert slots.get("staging_version_id") == version_id, slots

    versions_response = api_session.get(
        f"{_base_url(api_session)}/api/runtimes/{runtime_name}/versions",
        timeout=60,
    )
    versions_response.raise_for_status()
    version_record = next(
        (item for item in versions_response.json() if isinstance(item, dict) and item.get("version_id") == version_id),
        None,
    )
    assert version_record is not None, f"Version {version_id} is missing from {runtime_name}"
    agentcore_runtime_name = version_record.get("agentcore_runtime_name")
    assert isinstance(agentcore_runtime_name, str) and agentcore_runtime_name

    evidence_response = api_session.get(
        f"{_base_url(api_session)}/api/runtimes/{runtime_name}/versions/{version_id}/promotion-evidence",
        timeout=60,
    )
    evidence_response.raise_for_status()
    evidence = evidence_response.json()
    if evidence.get("production_gate_required"):
        policy = evidence.get("promotion_policy") or {}
        minimum_sessions = int(policy.get("minimum_sessions") or 0)
        assert minimum_sessions >= 1, policy
        scenarios, session_ids = _release_scenarios(
            api_session,
            runtime_name=runtime_name,
            version_id=version_id,
            count=minimum_sessions,
        )
        _wait_for_evaluation_sessions(
            logs_client,
            agentcore_runtime_name=agentcore_runtime_name,
            session_ids=session_ids,
        )
        start = api_session.post(
            f"{_base_url(api_session)}/api/runtimes/{runtime_name}/versions/{version_id}/promotion-evaluation/start",
            json={"scenarios": scenarios},
            timeout=90,
        )
        assert start.status_code == 200, f"Starting production evaluation returned {start.status_code}: {start.text}"
        batch_id = start.json().get("batch_evaluation_id")
        assert isinstance(batch_id, str) and batch_id

        deadline = time.monotonic() + _integration_timeout(
            "INTEGRATION_PROMOTION_TIMEOUT_SECONDS",
            30 * 60,
        )
        last_status = ""
        while time.monotonic() < deadline:
            batch = api_session.get(
                f"{_base_url(api_session)}/api/runtimes/{runtime_name}/versions/{version_id}/promotion-evaluation/{batch_id}",
                timeout=60,
            )
            batch.raise_for_status()
            last_status = str(batch.json().get("status") or "")
            if last_status == "COMPLETED":
                break
            assert last_status in {"PENDING", "IN_PROGRESS", "STOPPING"}, batch.json()
            time.sleep(POLL_INTERVAL_SECONDS)
        assert last_status == "COMPLETED", f"Batch evaluation ended/timed out in {last_status or 'UNKNOWN'}"

        attest = api_session.post(
            f"{_base_url(api_session)}/api/runtimes/{runtime_name}/versions/{version_id}/promotion-evaluation/attest",
            json={"batchEvaluationId": batch_id},
            timeout=90,
        )
        assert attest.status_code == 200, (
            f"Attesting production evaluation returned {attest.status_code}: {attest.text}"
        )
        assert (attest.json().get("promotion_evidence") or {}).get("verdict") == "pass"

    promote = api_session.post(
        f"{_base_url(api_session)}/api/runtimes/{runtime_name}/versions/{version_id}/promote",
        json={"slot": "production"},
        timeout=90,
    )
    assert promote.status_code == 200, f"Promoting trigger runtime returned {promote.status_code}: {promote.text}"
    assert promote.json().get("promoted_version_id") == version_id

    final_slots = api_session.get(
        f"{_base_url(api_session)}/api/runtimes/{runtime_name}/slots",
        timeout=60,
    )
    final_slots.raise_for_status()
    assert final_slots.json().get("production_version_id") == version_id


def _assert_production_content_is_redacted(
    api_session: requests.Session,
    logs_client: Any,
    *,
    runtime_name: str,
    version_id: str,
) -> None:
    """Invoke production and prove its safe canary never becomes span content."""

    versions_response = api_session.get(
        f"{_base_url(api_session)}/api/runtimes/{runtime_name}/versions",
        timeout=60,
    )
    versions_response.raise_for_status()
    version = next(
        (item for item in versions_response.json() if isinstance(item, dict) and item.get("version_id") == version_id),
        None,
    )
    assert version is not None, f"Version {version_id} disappeared before the privacy canary"
    agentcore_runtime_name = version.get("agentcore_runtime_name")
    assert isinstance(agentcore_runtime_name, str) and agentcore_runtime_name
    service_name = f"{agentcore_runtime_name}.DEFAULT"

    session_id = f"trigger-production-privacy-{uuid.uuid4()}"
    prompt_marker = "privacy check number 314159"
    response_marker = "lavender compass"
    prompt = f"Name the capital of France. This is {prompt_marker}. Finish the answer with the words {response_marker}."
    response = api_session.post(
        f"{_base_url(api_session)}/api/runtimes/{runtime_name}/slots/production/invoke",
        json={"input": prompt, "sessionId": session_id},
        timeout=180,
    )
    assert response.status_code == 200, f"Production privacy canary returned {response.status_code}: {response.text}"
    body = response.json()
    assert body.get("success") is True, body
    assert body.get("slot") == "production"
    assert body.get("versionId") == version_id
    model_response = str(body.get("response") or "").casefold()
    assert "paris" in model_response, body
    assert response_marker in model_response, body

    deadline = time.monotonic() + _integration_timeout(
        "INTEGRATION_EVALUATION_LOG_TIMEOUT_SECONDS",
        10 * 60,
    )
    exact_trace_id = ""
    while time.monotonic() < deadline:
        messages = _filtered_log_messages(logs_client, f'"{session_id}"')
        assert not any(
            prompt_marker in message or response_marker in message or prompt in message for message in messages
        ), "Production-slot prompt or response content reached the shared evaluation span group"
        for message in messages:
            try:
                record = json.loads(message)
            except (TypeError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            attributes = record.get("attributes") or {}
            resource = record.get("resource") or {}
            resource_attributes = resource.get("attributes") if isinstance(resource, dict) else {}
            if not isinstance(attributes, dict) or not isinstance(resource_attributes, dict):
                continue
            trace_id = str(record.get("traceId") or "")
            span_id = str(record.get("spanId") or "")
            if (
                (attributes.get("session.id") or record.get("attributes.session.id")) == session_id
                and (resource_attributes.get("service.name") or record.get("resource.attributes.service.name"))
                == service_name
                and re.fullmatch(r"[0-9a-f]{32}", trace_id)
                and re.fullmatch(r"[0-9a-f]{16}", span_id)
            ):
                exact_trace_id = trace_id
                break
        if exact_trace_id:
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    assert exact_trace_id, (
        f"Production privacy canary has no exact {service_name} metadata span in {BATCH_EVALUATION_SPANS_LOG_GROUP}"
    )
    # Split telemetry can put content in a correlated event record that has the
    # trace id but not the session id. Wait the documented indexing window, then
    # inspect every bounded page under BOTH keys before passing the privacy gate.
    settle_seconds = _evaluation_settle_seconds()
    if settle_seconds:
        time.sleep(settle_seconds)
    final_messages = _filtered_log_messages(
        logs_client,
        f'"{session_id}"',
    ) + _filtered_log_messages(
        logs_client,
        f'"{exact_trace_id}"',
    )
    assert not any(
        prompt_marker in message or response_marker in message or prompt in message for message in final_messages
    ), "Production-slot prompt or response content reached a delayed or split-telemetry record"


@pytest.mark.integration
def test_all_four_trigger_sources_invoke_and_cleanup(
    api_session: requests.Session,
    aws_region: str,
    integration_transaction_search,
    integration_triggers_table_name: str,
    deployment_cleanup: DeploymentCleanupTracker,
    wait_for_deployment,
) -> None:
    """Exercise cron, EventBridge, S3, and webhook through production paths."""

    assert integration_transaction_search is not None
    table = boto3.resource(
        "dynamodb",
        region_name=aws_region,
    ).Table(integration_triggers_table_name)
    events_client = boto3.client("events", region_name=aws_region)
    logs_client = boto3.client("logs", region_name=aws_region)
    secrets_client = boto3.client("secretsmanager", region_name=aws_region)
    s3_client, bucket_name, object_prefix = _create_source_bucket(aws_region)

    active_triggers: list[CreatedTrigger] = []
    budget_key: str | None = None
    primary_error: BaseException | None = None
    try:
        record, runtime_name, status = _start_http_runtime(
            api_session,
            deployment_cleanup,
            wait_for_deployment,
        )
        runtime_arn = status["runtime_arn"]
        budget_key = runtime_name
        _create_integration_budget(api_session, budget_key)
        _promote_staging_version_for_triggers(
            api_session,
            logs_client,
            runtime_name=runtime_name,
            status=status,
        )
        _assert_production_content_is_redacted(
            api_session,
            logs_client,
            runtime_name=runtime_name,
            version_id=status["version_id"],
        )
        nonce = uuid.uuid4().hex
        event_source = f"integration.agentcore.{nonce}"
        event_detail_type = f"AgentCore integration {nonce}"

        cron = _create_trigger(
            api_session,
            table,
            runtime_name=runtime_name,
            runtime_arn=runtime_arn,
            trigger_type="cron",
            payload={
                "type": "cron",
                "schedule": "cron(* * * * ? *)",
            },
        )
        active_triggers.append(cron)

        custom_event = _create_trigger(
            api_session,
            table,
            runtime_name=runtime_name,
            runtime_arn=runtime_arn,
            trigger_type="eventbridge",
            payload={
                "type": "eventbridge",
                "pattern": {
                    "source": [event_source],
                    "detail-type": [event_detail_type],
                    "detail": {"nonce": [nonce]},
                },
            },
        )
        active_triggers.append(custom_event)

        s3_trigger = _create_trigger(
            api_session,
            table,
            runtime_name=runtime_name,
            runtime_arn=runtime_arn,
            trigger_type="s3",
            payload={
                "type": "s3",
                "pattern": {
                    "source": ["aws.s3"],
                    "detail-type": ["Object Created"],
                    "detail": {
                        "bucket": {"name": [bucket_name]},
                        "object": {"key": [{"prefix": object_prefix}]},
                    },
                },
            },
        )
        active_triggers.append(s3_trigger)

        webhook = _create_trigger(
            api_session,
            table,
            runtime_name=runtime_name,
            runtime_arn=runtime_arn,
            trigger_type="webhook",
            payload={"type": "webhook"},
        )
        active_triggers.append(webhook)

        for trigger in active_triggers:
            if trigger.trigger_type != "webhook":
                _wait_for_rule_ready(
                    events_client,
                    trigger,
                    runtime_name=runtime_name,
                )
        _assert_webhook_secret_owned(secrets_client, webhook)

        listed = api_session.get(
            f"{_base_url(api_session)}/api/runtimes/{runtime_name}/triggers",
            timeout=60,
        )
        listed.raise_for_status()
        visible = {
            item["trigger_id"]: item for item in listed.json() if isinstance(item, dict) and item.get("trigger_id")
        }
        assert set(visible) == {trigger.trigger_id for trigger in active_triggers}
        assert {item.get("type") for item in visible.values()} == set(TRIGGER_TYPES_UNDER_TEST)
        assert all(item.get("status") == "active" for item in visible.values())
        assert all(item.get("webhook_signing_secret") is None for item in visible.values())

        event_delivery_id = _fire_custom_event(
            events_client,
            source=event_source,
            detail_type=event_detail_type,
            nonce=nonce,
        )
        s3_client.put_object(
            Bucket=bucket_name,
            Key=f"{object_prefix}{nonce}.json",
            Body=json.dumps({"nonce": nonce}).encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
        webhook_delivery_id = _fire_signed_webhook(
            api_session,
            webhook,
            nonce=nonce,
        )

        active_triggers[active_triggers.index(custom_event)] = CreatedTrigger(
            trigger_type=custom_event.trigger_type,
            trigger_id=custom_event.trigger_id,
            row=custom_event.row,
            expected_delivery_id=event_delivery_id,
        )
        active_triggers[active_triggers.index(webhook)] = CreatedTrigger(
            trigger_type=webhook.trigger_type,
            trigger_id=webhook.trigger_id,
            row=webhook.row,
            expected_delivery_id=webhook_delivery_id,
        )

        completed = _wait_for_all_deliveries(
            table,
            active_triggers,
            runtime_name=runtime_name,
        )
        assert len(completed) == len(TRIGGER_TYPES_UNDER_TEST)

        for trigger in list(reversed(active_triggers)):
            _delete_trigger_and_verify(
                api_session,
                table,
                events_client,
                secrets_client,
                runtime_name=runtime_name,
                trigger=trigger,
            )
            active_triggers.remove(trigger)

        final_list = api_session.get(
            f"{_base_url(api_session)}/api/runtimes/{runtime_name}/triggers",
            timeout=60,
        )
        final_list.raise_for_status()
        assert final_list.json() == []

        tombstone = deployment_cleanup.delete_and_verify(record)
        assert tombstone.get("delete_status") == "deleted"
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_errors: list[str] = []
        runtime_name_value = locals().get("runtime_name")
        if isinstance(runtime_name_value, str):
            for trigger in list(reversed(active_triggers)):
                try:
                    _delete_trigger_and_verify(
                        api_session,
                        table,
                        events_client,
                        secrets_client,
                        runtime_name=runtime_name_value,
                        trigger=trigger,
                        allow_absent=True,
                    )
                    active_triggers.remove(trigger)
                except Exception as exc:  # noqa: BLE001 - preserve every leak
                    cleanup_errors.append(f"{trigger.trigger_type}/{trigger.trigger_id}: {type(exc).__name__}: {exc}")
        try:
            _remove_source_bucket(s3_client, bucket_name)
        except Exception as exc:  # noqa: BLE001 - teardown is production evidence
            cleanup_errors.append(f"S3/{bucket_name}: {type(exc).__name__}: {exc}")
        if budget_key is not None:
            try:
                _delete_integration_budget(api_session, budget_key)
                budget_key = None
            except Exception as exc:  # noqa: BLE001 - teardown is production evidence
                cleanup_errors.append(f"budget/agent/{budget_key}: {type(exc).__name__}: {exc}")

        if cleanup_errors:
            detail = "Trigger integration cleanup was not proven:\n  - " + ("\n  - ".join(cleanup_errors))
            if primary_error is not None:
                primary_error.add_note(detail)
            else:
                raise AssertionError(detail)
