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
EVENT_BUS_NAME = "default"
TRIGGERS_TABLE_ENV = "INTEGRATION_TRIGGERS_TABLE_NAME"
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
            "systemPrompt": ("Acknowledge automated integration-test events concisely. Do not call external tools."),
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
    response = requests.post(
        f"{_base_url(api_session)}{path}",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-AgentCore-Timestamp": timestamp,
            "X-AgentCore-Delivery-Id": delivery_id,
            "X-AgentCore-Signature": signature,
        },
        timeout=60,
    )
    assert response.status_code == 202, f"Signed webhook returned {response.status_code}: {response.text}"
    result = response.json()
    assert result == {
        "accepted": True,
        "trigger_id": trigger.trigger_id,
        "delivery_id": delivery_id,
    }
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


@pytest.mark.integration
def test_all_four_trigger_sources_invoke_and_cleanup(
    api_session: requests.Session,
    aws_region: str,
    integration_triggers_table_name: str,
    deployment_cleanup: DeploymentCleanupTracker,
    wait_for_deployment,
) -> None:
    """Exercise cron, EventBridge, S3, and webhook through production paths."""

    table = boto3.resource(
        "dynamodb",
        region_name=aws_region,
    ).Table(integration_triggers_table_name)
    events_client = boto3.client("events", region_name=aws_region)
    secrets_client = boto3.client("secretsmanager", region_name=aws_region)
    s3_client, bucket_name, object_prefix = _create_source_bucket(aws_region)

    active_triggers: list[CreatedTrigger] = []
    primary_error: BaseException | None = None
    try:
        record, runtime_name, status = _start_http_runtime(
            api_session,
            deployment_cleanup,
            wait_for_deployment,
        )
        runtime_arn = status["runtime_arn"]
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

        if cleanup_errors:
            detail = "Trigger integration cleanup was not proven:\n  - " + ("\n  - ".join(cleanup_errors))
            if primary_error is not None:
                primary_error.add_note(detail)
            else:
                raise AssertionError(detail)
