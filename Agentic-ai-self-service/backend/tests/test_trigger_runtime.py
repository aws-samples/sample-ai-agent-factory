"""Provisioning, durable delivery, and invocation tests for runtime triggers."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Iterator
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, "src")

moto = pytest.importorskip("moto")
from app.services import trigger_runtime as runtime  # noqa: E402
from app.services.deployment_state_store import FinalizerLeaseBusy  # noqa: E402
from app.services.trigger_store import (  # noqa: E402
    STATUS_ACTIVE,
    STATUS_DELETING,
    STATUS_ERROR,
    STATUS_PROVISIONING,
    TYPE_CRON,
    TYPE_EVENTBRIDGE,
    TYPE_S3,
    TYPE_WEBHOOK,
    Trigger,
    TriggerDeleteBusy,
    TriggerDeliveryBusy,
    TriggerDeliveryClaim,
    TriggerDeliveryInactive,
    TriggerStore,
)
from moto import mock_aws  # noqa: E402

REGION = "us-east-1"
ACCOUNT = "123456789012"
RUNTIME_NAME = "orders_bot"
TRIGGER_ID = "01a0d000000000000000000000000001"
DELIVERY_ID = "evt-123"
DEPLOYMENT_ID = "deploy-123"
VERSION_ID = "version-123"
RUNTIME_ID = "orders_bot_abcd1234-XyZ1234567"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME_ID}"
QUEUE_ARN = f"arn:aws:sqs:{REGION}:{ACCOUNT}:agentcore-trigger-dispatch"
QUEUE_URL = f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/agentcore-trigger-dispatch"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[TriggerStore]:
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName="Triggers",
            KeySchema=[
                {"AttributeName": "runtime_name", "KeyType": "HASH"},
                {"AttributeName": "trigger_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "runtime_name", "AttributeType": "S"},
                {"AttributeName": "trigger_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        value = TriggerStore(table_name="Triggers", region=REGION)
        monkeypatch.setattr(runtime, "get_trigger_store", lambda: value)
        yield value


@pytest.fixture
def trigger_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_AWS_REGION", REGION)
    monkeypatch.setenv("TRIGGER_RULE_PREFIX", "agentcore-trigger")
    monkeypatch.setenv("TRIGGER_DISPATCH_QUEUE_ARN", QUEUE_ARN)
    monkeypatch.setenv("TRIGGER_DISPATCH_QUEUE_URL", QUEUE_URL)


@pytest.fixture
def deployment_lease(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    store = MagicMock()
    store.acquire_finalizer_lease.return_value = "deployment-lease"
    monkeypatch.setattr(runtime, "_get_deployment_store", lambda: store)
    return store


def make_trigger(
    *,
    trigger_type: str = TYPE_CRON,
    status: str = STATUS_ACTIVE,
    trigger_id: str = TRIGGER_ID,
    schedule: str | None = "cron(0 12 * * ? *)",
    pattern: dict | None = None,
    provisioning_token: str | None = None,
    eventbridge_rule_arn: str | None = None,
    webhook_secret_ref: str | None = None,
    webhook_out_url: str | None = None,
) -> Trigger:
    return Trigger(
        runtime_name=RUNTIME_NAME,
        trigger_id=trigger_id,
        owner_sub="sub-alice",
        type=trigger_type,
        target_runtime_arn=RUNTIME_ARN,
        version_id=VERSION_ID,
        deployment_id=DEPLOYMENT_ID,
        status=status,
        schedule=schedule if trigger_type == TYPE_CRON else None,
        pattern=pattern,
        eventbridge_rule_arn=eventbridge_rule_arn,
        webhook_secret_ref=webhook_secret_ref,
        webhook_out_url=webhook_out_url,
        provisioning_token=provisioning_token,
        created_at=1,
        updated_at=1,
    )


def persist(store: TriggerStore, trigger: Trigger) -> Trigger:
    return store.put_trigger_unfenced(
        runtime_name=trigger.runtime_name,
        trigger_id=trigger.trigger_id,
        owner_sub=trigger.owner_sub,
        type=trigger.type,
        target_runtime_arn=trigger.target_runtime_arn,
        version_id=trigger.version_id,
        deployment_id=trigger.deployment_id,
        status=trigger.status,
        schedule=trigger.schedule,
        pattern=trigger.pattern,
        webhook_secret_ref=trigger.webhook_secret_ref,
        webhook_out_url=trigger.webhook_out_url,
        eventbridge_rule_arn=trigger.eventbridge_rule_arn,
        provisioning_token=trigger.provisioning_token,
    )


def client_error(code: str, operation: str = "DeleteRule") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "test"}},
        operation,
    )


def rule_arn() -> str:
    return f"arn:aws:events:{REGION}:{ACCOUNT}:rule/agentcore-trigger-{TRIGGER_ID}"


def owned_rule_tags() -> list[dict[str, str]]:
    return [
        {"Key": "ManagedBy", "Value": "agentcore-flows"},
        {"Key": "Purpose", "Value": "runtime-trigger"},
        {
            "Key": "AgentCoreStack",
            "Value": "unit-tests-local-us-east-1",
        },
        {"Key": "TriggerId", "Value": TRIGGER_ID},
        {"Key": "RuntimeName", "Value": RUNTIME_NAME},
    ]


def prepare_rule_create(events: MagicMock) -> None:
    events.describe_rule.side_effect = client_error(
        "ResourceNotFoundException",
        "DescribeRule",
    )
    events.put_rule.return_value = {"RuleArn": rule_arn()}
    events.list_tags_for_resource.return_value = {
        "Tags": owned_rule_tags(),
    }
    events.put_targets.return_value = {
        "FailedEntryCount": 0,
        "FailedEntries": [],
    }


def prepare_owned_rule(events: MagicMock) -> None:
    events.describe_rule.return_value = {"Arn": rule_arn()}
    events.list_tags_for_resource.return_value = {
        "Tags": owned_rule_tags(),
    }


# ---------------------------------------------------------------------------
# Lifecycle and delivery transactions
# ---------------------------------------------------------------------------


def test_provisioning_completion_is_token_fenced(store: TriggerStore):
    row = persist(
        store,
        make_trigger(
            status=STATUS_PROVISIONING,
            provisioning_token="creator-token",
        ),
    )

    assert (
        store.complete_provisioning(
            runtime_name=row.runtime_name,
            trigger_id=row.trigger_id,
            provisioning_token="wrong-token",
        )
        is None
    )
    completed = store.complete_provisioning(
        runtime_name=row.runtime_name,
        trigger_id=row.trigger_id,
        provisioning_token="creator-token",
        eventbridge_rule_arn=(f"arn:aws:events:{REGION}:{ACCOUNT}:rule/agentcore-trigger-{row.trigger_id}"),
    )
    assert completed is not None
    assert completed.status == STATUS_ACTIVE
    assert completed.provisioning_token is None


def test_delete_fences_an_in_flight_provisioner(store: TriggerStore):
    row = persist(
        store,
        make_trigger(
            status=STATUS_PROVISIONING,
            provisioning_token="creator-token",
        ),
    )
    claimed = store.claim_delete(
        runtime_name=row.runtime_name,
        trigger_id=row.trigger_id,
        owner_sub=row.owner_sub,
        delete_token="delete-token",
        now=100,
    )
    assert claimed is not None
    assert claimed.status == STATUS_DELETING
    assert (
        store.complete_provisioning(
            runtime_name=row.runtime_name,
            trigger_id=row.trigger_id,
            provisioning_token="creator-token",
        )
        is None
    )
    assert store.delete_claimed(
        runtime_name=row.runtime_name,
        trigger_id=row.trigger_id,
        delete_token="delete-token",
    )


def test_delivery_claim_serializes_and_suppresses_duplicates(store: TriggerStore):
    row = persist(store, make_trigger())
    first = store.acquire_delivery(
        trigger=row,
        delivery_id=DELIVERY_ID,
        lease_seconds=630,
        retention_seconds=86_400,
        now=100,
    )
    assert first is not None

    with pytest.raises(TriggerDeliveryBusy):
        store.acquire_delivery(
            trigger=row,
            delivery_id=DELIVERY_ID,
            lease_seconds=630,
            retention_seconds=86_400,
            now=101,
        )
    with pytest.raises(TriggerDeliveryBusy):
        store.acquire_delivery(
            trigger=row,
            delivery_id="another-event",
            lease_seconds=630,
            retention_seconds=86_400,
            now=101,
        )

    assert store.complete_delivery(
        first,
        retention_seconds=86_400,
        now=102,
    )
    assert (
        store.acquire_delivery(
            trigger=row,
            delivery_id=DELIVERY_ID,
            lease_seconds=630,
            retention_seconds=86_400,
            now=103,
        )
        is None
    )

    delivery_item = store._table.get_item(  # noqa: SLF001
        Key={
            "runtime_name": f"!delivery#{RUNTIME_NAME}#{TRIGGER_ID}",
            "trigger_id": DELIVERY_ID,
        },
        ConsistentRead=True,
    )["Item"]
    assert delivery_item["delivery_status"] == "completed"
    assert "owner_sub" not in delivery_item
    assert "claim_token" not in delivery_item
    assert "claim_expires_at" not in delivery_item
    assert delivery_item["ttl"] > delivery_item["completed_at"]


def test_completion_dedupe_survives_a_changed_trigger_lease(
    store: TriggerStore,
):
    row = persist(store, make_trigger())
    claim = store.acquire_delivery(
        trigger=row,
        delivery_id=DELIVERY_ID,
        lease_seconds=630,
        retention_seconds=86_400,
        now=100,
    )
    assert claim is not None
    store._table.update_item(  # noqa: SLF001
        Key={
            "runtime_name": row.runtime_name,
            "trigger_id": row.trigger_id,
        },
        UpdateExpression=("SET dispatch_token = :token, dispatch_expires_at = :expires"),
        ExpressionAttributeValues={
            ":token": "newer-worker-token",
            ":expires": 999,
        },
    )

    assert store.complete_delivery(
        claim,
        retention_seconds=86_400,
        now=102,
    )

    delivery_item = store._table.get_item(  # noqa: SLF001
        Key={
            "runtime_name": f"!delivery#{RUNTIME_NAME}#{TRIGGER_ID}",
            "trigger_id": DELIVERY_ID,
        },
        ConsistentRead=True,
    )["Item"]
    assert delivery_item["delivery_status"] == "completed"
    trigger_item = store._table.get_item(  # noqa: SLF001
        Key={
            "runtime_name": RUNTIME_NAME,
            "trigger_id": TRIGGER_ID,
        },
        ConsistentRead=True,
    )["Item"]
    assert trigger_item["dispatch_token"] == "newer-worker-token"
    assert (
        store.acquire_delivery(
            trigger=row,
            delivery_id=DELIVERY_ID,
            lease_seconds=630,
            retention_seconds=86_400,
            now=103,
        )
        is None
    )


def test_completion_reads_back_an_ambiguous_committed_write(
    store: TriggerStore,
    monkeypatch: pytest.MonkeyPatch,
):
    row = persist(store, make_trigger())
    claim = store.acquire_delivery(
        trigger=row,
        delivery_id=DELIVERY_ID,
        lease_seconds=630,
        retention_seconds=86_400,
        now=100,
    )
    assert claim is not None
    original_update = store._table.update_item  # noqa: SLF001
    delivery_update_calls = 0

    def update_then_lose_response(**kwargs):
        nonlocal delivery_update_calls
        if str(kwargs["Key"]["runtime_name"]).startswith("!delivery#"):
            delivery_update_calls += 1
            response = original_update(**kwargs)
            if delivery_update_calls == 1:
                raise TimeoutError("response was lost after commit")
            return response
        return original_update(**kwargs)

    monkeypatch.setattr(
        store._table,  # noqa: SLF001
        "update_item",
        update_then_lose_response,
    )

    assert store.complete_delivery(
        claim,
        retention_seconds=86_400,
        now=102,
    )
    assert delivery_update_calls == 1
    assert (
        store.acquire_delivery(
            trigger=row,
            delivery_id=DELIVERY_ID,
            lease_seconds=630,
            retention_seconds=86_400,
            now=103,
        )
        is None
    )


def test_completion_retries_a_transient_precommit_failure(
    store: TriggerStore,
    monkeypatch: pytest.MonkeyPatch,
):
    row = persist(store, make_trigger())
    claim = store.acquire_delivery(
        trigger=row,
        delivery_id=DELIVERY_ID,
        lease_seconds=630,
        retention_seconds=86_400,
        now=100,
    )
    assert claim is not None
    original_update = store._table.update_item  # noqa: SLF001
    delivery_update_calls = 0

    def fail_once_before_commit(**kwargs):
        nonlocal delivery_update_calls
        if str(kwargs["Key"]["runtime_name"]).startswith("!delivery#"):
            delivery_update_calls += 1
            if delivery_update_calls == 1:
                raise TimeoutError("request did not reach DynamoDB")
        return original_update(**kwargs)

    monkeypatch.setattr(
        store._table,  # noqa: SLF001
        "update_item",
        fail_once_before_commit,
    )

    assert store.complete_delivery(
        claim,
        retention_seconds=86_400,
        now=102,
    )
    assert delivery_update_calls == 2
    assert (
        store.acquire_delivery(
            trigger=row,
            delivery_id=DELIVERY_ID,
            lease_seconds=630,
            retention_seconds=86_400,
            now=103,
        )
        is None
    )


def test_completion_stays_terminal_when_lease_release_transport_fails(
    store: TriggerStore,
    monkeypatch: pytest.MonkeyPatch,
):
    row = persist(store, make_trigger())
    claim = store.acquire_delivery(
        trigger=row,
        delivery_id=DELIVERY_ID,
        lease_seconds=630,
        retention_seconds=86_400,
        now=100,
    )
    assert claim is not None
    original_update = store._table.update_item  # noqa: SLF001

    def fail_trigger_lease_release(**kwargs):
        if kwargs["Key"]["runtime_name"] == RUNTIME_NAME:
            raise TimeoutError("lease release response unavailable")
        return original_update(**kwargs)

    monkeypatch.setattr(
        store._table,  # noqa: SLF001
        "update_item",
        fail_trigger_lease_release,
    )

    assert store.complete_delivery(
        claim,
        retention_seconds=86_400,
        now=102,
    )
    delivery_item = store._table.get_item(  # noqa: SLF001
        Key={
            "runtime_name": f"!delivery#{RUNTIME_NAME}#{TRIGGER_ID}",
            "trigger_id": DELIVERY_ID,
        },
        ConsistentRead=True,
    )["Item"]
    assert delivery_item["delivery_status"] == "completed"


def test_failed_delivery_release_allows_a_retry(store: TriggerStore):
    row = persist(store, make_trigger())
    first = store.acquire_delivery(
        trigger=row,
        delivery_id=DELIVERY_ID,
        lease_seconds=630,
        retention_seconds=86_400,
        now=100,
    )
    assert first is not None
    assert store.release_delivery(first)

    retry = store.acquire_delivery(
        trigger=row,
        delivery_id=DELIVERY_ID,
        lease_seconds=630,
        retention_seconds=86_400,
        now=101,
    )
    assert retry is not None
    assert retry.token != first.token


def test_delivery_claim_fails_if_trigger_stopped_being_active(store: TriggerStore):
    row = persist(store, make_trigger())
    store.update_status(
        runtime_name=row.runtime_name,
        trigger_id=row.trigger_id,
        status=STATUS_ERROR,
    )

    with pytest.raises(TriggerDeliveryInactive):
        store.acquire_delivery(
            trigger=row,
            delivery_id=DELIVERY_ID,
            lease_seconds=630,
            retention_seconds=86_400,
            now=100,
        )
    assert (
        store._table.get_item(  # noqa: SLF001
            Key={
                "runtime_name": f"!delivery#{RUNTIME_NAME}#{TRIGGER_ID}",
                "trigger_id": DELIVERY_ID,
            },
            ConsistentRead=True,
        ).get("Item")
        is None
    )


def test_delete_waits_for_dispatch_lease_but_recovers_after_expiry(
    store: TriggerStore,
):
    row = persist(store, make_trigger())
    claim = store.acquire_delivery(
        trigger=row,
        delivery_id=DELIVERY_ID,
        lease_seconds=630,
        retention_seconds=86_400,
        now=100,
    )
    assert claim is not None

    with pytest.raises(TriggerDeleteBusy):
        store.claim_delete(
            runtime_name=row.runtime_name,
            trigger_id=row.trigger_id,
            owner_sub=row.owner_sub,
            delete_token="delete-before-expiry",
            now=729,
        )

    claimed = store.claim_delete(
        runtime_name=row.runtime_name,
        trigger_id=row.trigger_id,
        owner_sub=row.owner_sub,
        delete_token="delete-after-expiry",
        now=730,
    )
    assert claimed is not None
    assert claimed.status == STATUS_DELETING


def test_stale_delivery_token_cannot_complete_or_release(store: TriggerStore):
    row = persist(store, make_trigger())
    current = store.acquire_delivery(
        trigger=row,
        delivery_id=DELIVERY_ID,
        lease_seconds=630,
        retention_seconds=86_400,
        now=100,
    )
    assert current is not None
    stale = TriggerDeliveryClaim(
        runtime_name=current.runtime_name,
        trigger_id=current.trigger_id,
        delivery_id=current.delivery_id,
        token="stale-token",
    )
    assert not store.complete_delivery(
        stale,
        retention_seconds=86_400,
        now=101,
    )
    assert not store.release_delivery(stale)
    assert store.complete_delivery(
        current,
        retention_seconds=86_400,
        now=102,
    )


# ---------------------------------------------------------------------------
# AWS resource provisioning and cleanup
# ---------------------------------------------------------------------------


def test_cron_provisions_deterministic_eventbridge_rule_to_sqs(
    trigger_env: None,
):
    trigger = make_trigger(
        status=STATUS_PROVISIONING,
        provisioning_token="creator-token",
    )
    events = MagicMock()
    prepare_rule_create(events)

    resources = runtime.provision_trigger(trigger, events_client=events)

    assert resources.eventbridge_rule_arn == events.put_rule.return_value["RuleArn"]
    _, rule_kwargs = events.put_rule.call_args
    assert rule_kwargs["ScheduleExpression"] == "cron(0 12 * * ? *)"
    assert rule_kwargs["State"] == "ENABLED"
    assert rule_kwargs["EventBusName"] == "default"
    assert {tag["Key"]: tag["Value"] for tag in rule_kwargs["Tags"]} == {
        "ManagedBy": "agentcore-flows",
        "Purpose": "runtime-trigger",
        "AgentCoreStack": "unit-tests-local-us-east-1",
        "TriggerId": TRIGGER_ID,
        "RuntimeName": RUNTIME_NAME,
    }
    _, target_kwargs = events.put_targets.call_args
    target = target_kwargs["Targets"][0]
    assert target["Arn"] == QUEUE_ARN
    assert target["Id"] == f"dispatch-{TRIGGER_ID}"
    assert target["RetryPolicy"] == {
        "MaximumEventAgeInSeconds": 3600,
        "MaximumRetryAttempts": 5,
    }
    template = target["InputTransformer"]["InputTemplate"]
    assert f'"trigger_id":"{TRIGGER_ID}"' in template
    assert f'"runtime_name":"{RUNTIME_NAME}"' in template
    assert '"delivery_event":<aws_event>' in template


@pytest.mark.parametrize("trigger_type", [TYPE_EVENTBRIDGE, TYPE_S3])
def test_event_patterns_are_canonical_json(
    trigger_env: None,
    trigger_type: str,
):
    pattern = {"detail": {"state": ["ready"]}, "source": ["aws.s3"]}
    trigger = make_trigger(
        trigger_type=trigger_type,
        status=STATUS_PROVISIONING,
        schedule=None,
        pattern=pattern,
        provisioning_token="creator-token",
    )
    events = MagicMock()
    prepare_rule_create(events)

    runtime.provision_trigger(trigger, events_client=events)

    _, kwargs = events.put_rule.call_args
    assert json.loads(kwargs["EventPattern"]) == pattern
    assert "ScheduleExpression" not in kwargs


def test_webhook_provisioning_creates_only_a_static_path(trigger_env: None):
    trigger = make_trigger(
        trigger_type=TYPE_WEBHOOK,
        status=STATUS_PROVISIONING,
        schedule=None,
        provisioning_token="creator-token",
        webhook_secret_ref=(f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-trigger/alice"),
    )
    events = MagicMock()

    resources = runtime.provision_trigger(trigger, events_client=events)

    assert resources.webhook_path == f"/hooks/{RUNTIME_NAME}/{TRIGGER_ID}"
    assert resources.eventbridge_rule_arn is None
    events.assert_not_called()


def test_provisioning_rejects_noncanonical_runtime_before_aws(
    trigger_env: None,
):
    trigger = make_trigger(
        status=STATUS_PROVISIONING,
        provisioning_token="creator-token",
    )
    trigger.target_runtime_arn = "runtime-id-only"
    events = MagicMock()

    with pytest.raises(runtime.TriggerProvisioningError):
        runtime.provision_trigger(trigger, events_client=events)
    events.assert_not_called()


def test_put_targets_partial_failure_is_not_reported_active(trigger_env: None):
    trigger = make_trigger(
        status=STATUS_PROVISIONING,
        provisioning_token="creator-token",
    )
    events = MagicMock()
    prepare_rule_create(events)
    events.put_targets.return_value = {
        "FailedEntryCount": 1,
        "FailedEntries": [{"ErrorCode": "InternalException"}],
    }

    with pytest.raises(runtime.TriggerProvisioningError):
        runtime.provision_trigger(trigger, events_client=events)


def test_provisioning_refuses_an_existing_foreign_rule_before_mutation(
    trigger_env: None,
):
    trigger = make_trigger(
        status=STATUS_PROVISIONING,
        provisioning_token="creator-token",
    )
    events = MagicMock()
    events.describe_rule.return_value = {"Arn": rule_arn()}
    events.list_tags_for_resource.return_value = {
        "Tags": [
            {"Key": "ManagedBy", "Value": "another-system"},
            {"Key": "TriggerId", "Value": TRIGGER_ID},
        ]
    }

    with pytest.raises(runtime.TriggerProvisioningError):
        runtime.provision_trigger(trigger, events_client=events)

    events.put_rule.assert_not_called()
    events.put_targets.assert_not_called()


def test_provisioning_can_resume_only_its_exact_owned_rule(
    trigger_env: None,
):
    trigger = make_trigger(
        status=STATUS_PROVISIONING,
        provisioning_token="creator-token",
    )
    events = MagicMock()
    prepare_owned_rule(events)
    events.put_rule.return_value = {"RuleArn": rule_arn()}
    events.put_targets.return_value = {"FailedEntryCount": 0}

    resources = runtime.provision_trigger(trigger, events_client=events)

    assert resources.eventbridge_rule_arn == rule_arn()
    assert events.list_tags_for_resource.call_count == 2
    events.put_targets.assert_called_once()


def test_provisioning_detects_a_rule_created_by_somebody_else_in_the_race(
    trigger_env: None,
):
    trigger = make_trigger(
        status=STATUS_PROVISIONING,
        provisioning_token="creator-token",
    )
    events = MagicMock()
    events.describe_rule.side_effect = client_error(
        "ResourceNotFoundException",
        "DescribeRule",
    )
    events.put_rule.return_value = {"RuleArn": rule_arn()}
    events.list_tags_for_resource.return_value = {
        "Tags": [
            {"Key": "ManagedBy", "Value": "another-system"},
            {"Key": "TriggerId", "Value": TRIGGER_ID},
        ]
    }

    with pytest.raises(runtime.TriggerProvisioningError):
        runtime.provision_trigger(trigger, events_client=events)

    events.put_rule.assert_called_once()
    events.put_targets.assert_not_called()


def test_cleanup_removes_only_the_deterministic_target_and_rule(
    trigger_env: None,
):
    trigger = make_trigger(eventbridge_rule_arn=rule_arn())
    events = MagicMock()
    prepare_owned_rule(events)
    events.remove_targets.return_value = {"FailedEntryCount": 0}

    runtime.cleanup_eventbridge_trigger(trigger, events_client=events)

    events.remove_targets.assert_called_once_with(
        Rule=f"agentcore-trigger-{TRIGGER_ID}",
        EventBusName="default",
        Ids=[f"dispatch-{TRIGGER_ID}"],
    )
    events.delete_rule.assert_called_once_with(
        Name=f"agentcore-trigger-{TRIGGER_ID}",
        EventBusName="default",
    )
    assert events.describe_rule.call_count == 2
    assert events.list_tags_for_resource.call_count == 2


def test_cleanup_derives_name_after_crash_before_handle_publish(
    trigger_env: None,
):
    trigger = make_trigger(
        status=STATUS_ERROR,
        eventbridge_rule_arn=None,
    )
    events = MagicMock()
    prepare_owned_rule(events)
    events.remove_targets.return_value = {"FailedEntryCount": 0}

    runtime.cleanup_eventbridge_trigger(trigger, events_client=events)

    events.delete_rule.assert_called_once_with(
        Name=f"agentcore-trigger-{TRIGGER_ID}",
        EventBusName="default",
    )


def test_cleanup_refuses_a_rule_outside_the_platform_account(
    trigger_env: None,
):
    trigger = make_trigger(
        eventbridge_rule_arn=(f"arn:aws:events:{REGION}:999999999999:rule/agentcore-trigger-{TRIGGER_ID}")
    )
    events = MagicMock()

    with pytest.raises(runtime.TriggerCleanupRefused):
        runtime.cleanup_eventbridge_trigger(trigger, events_client=events)
    events.assert_not_called()


def test_cleanup_refuses_a_foreign_same_name_rule_before_any_mutation(
    trigger_env: None,
):
    trigger = make_trigger(eventbridge_rule_arn=rule_arn())
    events = MagicMock()
    events.describe_rule.return_value = {"Arn": rule_arn()}
    events.list_tags_for_resource.return_value = {
        "Tags": [
            {"Key": "ManagedBy", "Value": "another-system"},
            {"Key": "TriggerId", "Value": TRIGGER_ID},
        ]
    }

    with pytest.raises(runtime.TriggerCleanupRefused):
        runtime.cleanup_eventbridge_trigger(trigger, events_client=events)

    events.remove_targets.assert_not_called()
    events.delete_rule.assert_not_called()


def test_cleanup_rechecks_ownership_between_target_and_rule_mutations(
    trigger_env: None,
):
    trigger = make_trigger(eventbridge_rule_arn=rule_arn())
    events = MagicMock()
    events.describe_rule.return_value = {"Arn": rule_arn()}
    events.list_tags_for_resource.side_effect = [
        {"Tags": owned_rule_tags()},
        {
            "Tags": [
                {"Key": "ManagedBy", "Value": "another-system"},
                {"Key": "TriggerId", "Value": TRIGGER_ID},
            ]
        },
    ]
    events.remove_targets.return_value = {"FailedEntryCount": 0}

    with pytest.raises(runtime.TriggerCleanupRefused):
        runtime.cleanup_eventbridge_trigger(trigger, events_client=events)

    events.remove_targets.assert_called_once()
    events.delete_rule.assert_not_called()


def test_cleanup_is_idempotent_when_rule_is_already_absent(
    trigger_env: None,
):
    trigger = make_trigger(
        eventbridge_rule_arn=(f"arn:aws:events:{REGION}:{ACCOUNT}:rule/agentcore-trigger-{TRIGGER_ID}")
    )
    events = MagicMock()
    events.describe_rule.side_effect = client_error(
        "ResourceNotFoundException",
        "DescribeRule",
    )

    runtime.cleanup_eventbridge_trigger(trigger, events_client=events)
    events.remove_targets.assert_not_called()
    events.delete_rule.assert_not_called()


# ---------------------------------------------------------------------------
# Dispatch, invocation authority, and durable queue behavior
# ---------------------------------------------------------------------------


def delivery_event() -> dict:
    return {
        "version": "0",
        "id": DELIVERY_ID,
        "source": "orders.test",
        "detail-type": "Order ready",
        "detail": {"order_id": "o-123"},
    }


def dispatch_event() -> dict:
    return {
        "_agentcore_trigger_dispatch": runtime.DISPATCH_MARKER,
        "runtime_name": RUNTIME_NAME,
        "trigger_id": TRIGGER_ID,
        "delivery_event": delivery_event(),
    }


def test_dispatch_invokes_once_and_suppresses_completed_duplicate(
    store: TriggerStore,
    monkeypatch: pytest.MonkeyPatch,
    deployment_lease: MagicMock,
):
    persist(store, make_trigger())
    invoke = MagicMock(
        return_value=runtime.TriggerInvocation(
            trigger_id=TRIGGER_ID,
            delivery_id=DELIVERY_ID,
            response="done",
            session_id="trigger-session",
        )
    )
    callback = MagicMock()
    monkeypatch.setattr(runtime, "invoke_trigger", invoke)
    monkeypatch.setattr(runtime, "_post_callback", callback)

    first = runtime.dispatch_trigger_event(dispatch_event())
    duplicate = runtime.dispatch_trigger_event(dispatch_event())

    assert first["status"] == "invoked"
    assert duplicate == {
        "status": "duplicate",
        "trigger_id": TRIGGER_ID,
        "delivery_id": DELIVERY_ID,
    }
    invoke.assert_called_once()
    callback.assert_called_once()
    deployment_lease.acquire_finalizer_lease.assert_called_once_with(
        DEPLOYMENT_ID,
        seconds=runtime.TRIGGER_DELIVERY_LEASE_SECONDS,
    )
    deployment_lease.release_finalizer_lease.assert_called_once_with(
        DEPLOYMENT_ID,
        "deployment-lease",
    )


def test_failed_invoke_releases_claim_for_sqs_retry(
    store: TriggerStore,
    monkeypatch: pytest.MonkeyPatch,
    deployment_lease: MagicMock,
):
    persist(store, make_trigger())
    invoke = MagicMock(
        side_effect=[
            runtime.TriggerDispatchError("first attempt failed"),
            runtime.TriggerInvocation(
                trigger_id=TRIGGER_ID,
                delivery_id=DELIVERY_ID,
                response="done",
                session_id="trigger-session",
            ),
        ]
    )
    monkeypatch.setattr(runtime, "invoke_trigger", invoke)
    monkeypatch.setattr(runtime, "_post_callback", MagicMock())

    with pytest.raises(runtime.TriggerDispatchError):
        runtime.dispatch_trigger_event(dispatch_event())
    assert runtime.dispatch_trigger_event(dispatch_event())["status"] == "invoked"
    assert invoke.call_count == 2
    assert deployment_lease.release_finalizer_lease.call_count == 2


def test_teardown_winning_the_deployment_lease_prevents_invoke(
    store: TriggerStore,
    monkeypatch: pytest.MonkeyPatch,
    deployment_lease: MagicMock,
):
    persist(store, make_trigger())
    deployment_lease.acquire_finalizer_lease.side_effect = runtime.DeploymentLifecycleConflict(
        "teardown owns the deployment"
    )
    invoke = MagicMock()
    monkeypatch.setattr(runtime, "invoke_trigger", invoke)

    assert runtime.dispatch_trigger_event(dispatch_event()) == {
        "status": "inactive",
        "trigger_id": TRIGGER_ID,
        "delivery_id": DELIVERY_ID,
    }
    invoke.assert_not_called()
    # The first trigger claim was released, so a later SQS retry can classify
    # the now-deleted deployment instead of being stuck behind stale metadata.
    delivery_item = store._table.get_item(  # noqa: SLF001
        Key={
            "runtime_name": f"!delivery#{RUNTIME_NAME}#{TRIGGER_ID}",
            "trigger_id": DELIVERY_ID,
        },
        ConsistentRead=True,
    ).get("Item")
    assert delivery_item is None


def test_a_busy_deployment_lease_is_contention_and_releases_the_claim(
    store: TriggerStore,
    monkeypatch: pytest.MonkeyPatch,
    deployment_lease: MagicMock,
):
    """Another delivery holding the deployment's lease is retried soon (dispatch_sqs_batch
    re-enqueues contention); the claim is released so the retry is not blocked by it."""
    persist(store, make_trigger())
    deployment_lease.acquire_finalizer_lease.side_effect = FinalizerLeaseBusy("another delivery holds it")
    invoke = MagicMock()
    monkeypatch.setattr(runtime, "invoke_trigger", invoke)

    with pytest.raises(runtime.TriggerDispatchContention):
        runtime.dispatch_trigger_event(dispatch_event())

    invoke.assert_not_called()
    delivery_item = store._table.get_item(  # noqa: SLF001
        Key={
            "runtime_name": f"!delivery#{RUNTIME_NAME}#{TRIGGER_ID}",
            "trigger_id": DELIVERY_ID,
        },
        ConsistentRead=True,
    ).get("Item")
    assert delivery_item is None


def test_a_delivery_owned_by_another_worker_is_contention(
    store: TriggerStore,
    monkeypatch: pytest.MonkeyPatch,
    deployment_lease: MagicMock,
):
    persist(store, make_trigger())
    monkeypatch.setattr(store, "acquire_delivery", MagicMock(side_effect=TriggerDeliveryBusy("owned")))
    invoke = MagicMock()
    monkeypatch.setattr(runtime, "invoke_trigger", invoke)

    with pytest.raises(runtime.TriggerDispatchContention):
        runtime.dispatch_trigger_event(dispatch_event())

    invoke.assert_not_called()
    deployment_lease.acquire_finalizer_lease.assert_not_called()


def test_dispatch_noops_after_delete_wins(store: TriggerStore):
    row = persist(store, make_trigger(status=STATUS_DELETING))
    result = runtime.dispatch_trigger_event(dispatch_event())
    assert result == {
        "status": "inactive",
        "trigger_id": row.trigger_id,
        "trigger_status": STATUS_DELETING,
    }


def test_dispatch_noops_after_row_is_gone(store: TriggerStore):
    assert runtime.dispatch_trigger_event(dispatch_event()) == {
        "status": "gone",
        "trigger_id": TRIGGER_ID,
    }


def test_sqs_batch_reports_only_failed_message(
    monkeypatch: pytest.MonkeyPatch,
):
    dispatch = MagicMock(
        side_effect=[
            {"status": "invoked"},
            runtime.TriggerDispatchError("retry"),
        ]
    )
    monkeypatch.setattr(runtime, "dispatch_trigger_event", dispatch)
    event = {
        "Records": [
            {
                "messageId": "m1",
                "eventSource": "aws:sqs",
                "body": json.dumps(dispatch_event()),
            },
            {
                "messageId": "m2",
                "eventSource": "aws:sqs",
                "body": json.dumps(dispatch_event()),
            },
        ]
    }

    assert runtime.dispatch_sqs_batch(event) == {"batchItemFailures": [{"itemIdentifier": "m2"}]}


def test_deployment_authority_is_exact_and_cross_account_aware(
    monkeypatch: pytest.MonkeyPatch,
):
    trigger = make_trigger()
    deployment = {
        "deployment_id": DEPLOYMENT_ID,
        "version_id": VERSION_ID,
        "user_id": trigger.owner_sub,
        "status": "succeeded",
        "runtime_id": RUNTIME_ID,
        "runtime_arn": RUNTIME_ARN,
        "target_account_id": ACCOUNT,
        "target_region": REGION,
        "target_role_arn": f"arn:aws:iam::{ACCOUNT}:role/AgentCoreFlowsDeploymentRole",
    }
    deployment_store = MagicMock()
    deployment_store.get.return_value = deployment
    target_session = MagicMock()
    session_for_event = MagicMock(return_value=target_session)
    monkeypatch.setattr(runtime, "_get_deployment_store", lambda: deployment_store)
    monkeypatch.setattr(runtime.step_clients, "session_for_event", session_for_event)

    resolved, match, session = runtime._deployment_for_trigger(trigger)  # noqa: SLF001

    assert resolved == deployment
    assert match.group("runtime_id") == RUNTIME_ID
    assert session is target_session
    session_for_event.assert_called_once_with(
        {
            "target_account_id": ACCOUNT,
            "target_region": REGION,
            "target_role_arn": deployment["target_role_arn"],
        }
    )
    deployment_store.get.assert_called_once_with(
        DEPLOYMENT_ID,
        consistent=True,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_id", "sub-mallory"),
        ("status", "failed"),
        ("delete_status", "deleting"),
        ("version_id", "other-version"),
        ("runtime_id", "other-runtime"),
        ("runtime_arn", RUNTIME_ARN.replace(RUNTIME_ID, "other-runtime")),
        ("target_account_id", "999999999999"),
        ("target_region", "eu-west-1"),
        ("target_role_arn", None),
    ],
)
def test_deployment_authority_refuses_every_binding_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value,
):
    trigger = make_trigger()
    deployment = {
        "deployment_id": DEPLOYMENT_ID,
        "version_id": VERSION_ID,
        "user_id": trigger.owner_sub,
        "status": "succeeded",
        "runtime_id": RUNTIME_ID,
        "runtime_arn": RUNTIME_ARN,
        "target_account_id": ACCOUNT,
        "target_region": REGION,
        "target_role_arn": f"arn:aws:iam::{ACCOUNT}:role/AgentCoreFlowsDeploymentRole",
    }
    deployment[field] = value
    deployment_store = MagicMock()
    deployment_store.get.return_value = deployment
    session_for_event = MagicMock()
    monkeypatch.setattr(runtime, "_get_deployment_store", lambda: deployment_store)
    monkeypatch.setattr(runtime.step_clients, "session_for_event", session_for_event)

    with pytest.raises(runtime.TriggerDispatchError):
        runtime._deployment_for_trigger(trigger)  # noqa: SLF001
    session_for_event.assert_not_called()


def test_invoke_uses_stable_delivery_session_and_parses_response(
    monkeypatch: pytest.MonkeyPatch,
):
    trigger = make_trigger()
    agentcore = MagicMock()
    agentcore.invoke_agent_runtime.return_value = {"response": io.BytesIO(b'{"response":"processed"}')}
    session = MagicMock()
    session.client.return_value = agentcore
    deployment = {
        "deployment_id": DEPLOYMENT_ID,
        "user_id": trigger.owner_sub,
        "memory_result": None,
    }
    match = runtime._RUNTIME_ARN_RE.fullmatch(RUNTIME_ARN)  # noqa: SLF001
    assert match is not None
    monkeypatch.setattr(
        runtime,
        "_deployment_for_trigger",
        lambda _trigger: (deployment, match, session),
    )

    result = runtime.invoke_trigger(trigger, delivery_event())

    assert result.response == "processed"
    assert result.delivery_id == DELIVERY_ID
    assert 33 <= len(result.session_id) <= 100
    _, kwargs = agentcore.invoke_agent_runtime.call_args
    assert kwargs["agentRuntimeArn"] == RUNTIME_ARN
    assert kwargs["runtimeSessionId"] == result.session_id
    payload = json.loads(kwargs["payload"])
    assert payload["session_id"] == result.session_id
    assert DELIVERY_ID in payload["prompt"]


# ---------------------------------------------------------------------------
# Result callback transport
# ---------------------------------------------------------------------------


def test_callback_resolution_pins_the_exact_public_dns_answer(
    monkeypatch: pytest.MonkeyPatch,
):
    url = "https://hooks.example.com:8443/results?tenant=alice"
    monkeypatch.setattr(runtime, "_validate_outbound_url", lambda value, **_: value)
    getaddrinfo = MagicMock(
        return_value=[
            (2, 1, 6, "", ("93.184.216.34", 8443)),
        ]
    )
    monkeypatch.setattr(runtime.socket, "getaddrinfo", getaddrinfo)

    target = runtime._resolve_callback_target(url)  # noqa: SLF001

    assert target == runtime._CallbackTarget(  # noqa: SLF001
        host="hooks.example.com",
        port=8443,
        pinned_ip="93.184.216.34",
        request_target="/results?tenant=alice",
    )
    getaddrinfo.assert_called_once_with(
        "hooks.example.com",
        8443,
        runtime.socket.AF_UNSPEC,
        runtime.socket.SOCK_STREAM,
    )


@pytest.mark.parametrize(
    "addresses",
    [
        [(2, 1, 6, "", ("127.0.0.1", 443))],
        [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (2, 1, 6, "", ("169.254.169.254", 443)),
        ],
    ],
)
def test_callback_resolution_rejects_a_rebound_or_mixed_private_answer(
    monkeypatch: pytest.MonkeyPatch,
    addresses: list[tuple],
):
    # The first validation is deliberately mocked as successful to model DNS
    # rebinding between validation and the connection-time lookup.
    monkeypatch.setattr(runtime, "_validate_outbound_url", lambda value, **_: value)
    monkeypatch.setattr(runtime.socket, "getaddrinfo", lambda *_args: addresses)

    with pytest.raises(runtime._DiscoveryUrlBlocked):  # noqa: SLF001
        runtime._resolve_callback_target(  # noqa: SLF001
            "https://hooks.example.com/result"
        )


def test_pinned_https_connection_never_resolves_the_hostname_again(
    monkeypatch: pytest.MonkeyPatch,
):
    create_connection = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(runtime.socket, "create_connection", create_connection)
    connection = runtime._PinnedHTTPSConnection(  # noqa: SLF001
        "hooks.example.com",
        443,
        pinned_ip="93.184.216.34",
        timeout=10,
    )

    connection._create_connection(  # noqa: SLF001
        ("hooks.example.com", 443),
        10,
        None,
    )

    assert connection.host == "hooks.example.com"
    create_connection.assert_called_once_with(
        ("93.184.216.34", 443),
        10,
        None,
    )


def test_callback_posts_once_to_the_pinned_target(
    monkeypatch: pytest.MonkeyPatch,
):
    target = runtime._CallbackTarget(  # noqa: SLF001
        host="hooks.example.com",
        port=443,
        pinned_ip="93.184.216.34",
        request_target="/result?source=agent",
    )
    monkeypatch.setattr(runtime, "_resolve_callback_target", lambda _url: target)
    connection_class = MagicMock()
    connection = connection_class.return_value
    connection.getresponse.return_value.status = 204
    monkeypatch.setattr(runtime, "_PinnedHTTPSConnection", connection_class)
    trigger = make_trigger(webhook_out_url="https://hooks.example.com/result")
    invocation = runtime.TriggerInvocation(
        trigger_id=TRIGGER_ID,
        delivery_id=DELIVERY_ID,
        response="processed",
        session_id="trigger-session",
    )

    runtime._post_callback(trigger, invocation)  # noqa: SLF001

    connection_class.assert_called_once_with(
        "hooks.example.com",
        443,
        pinned_ip="93.184.216.34",
        timeout=10,
    )
    connection.request.assert_called_once()
    method, request_target = connection.request.call_args.args[:2]
    assert method == "POST"
    assert request_target == "/result?source=agent"
    posted = json.loads(connection.request.call_args.kwargs["body"])
    assert posted == {
        "trigger_id": TRIGGER_ID,
        "delivery_id": DELIVERY_ID,
        "session_id": "trigger-session",
        "response": "processed",
    }
    connection.getresponse.return_value.close.assert_called_once()
    connection.close.assert_called_once()


def test_callback_does_not_connect_when_dispatch_time_dns_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        runtime,
        "_resolve_callback_target",
        MagicMock(side_effect=runtime._DiscoveryUrlBlocked("blocked")),
    )
    connection_class = MagicMock()
    monkeypatch.setattr(runtime, "_PinnedHTTPSConnection", connection_class)

    runtime._post_callback(  # noqa: SLF001
        make_trigger(webhook_out_url="https://hooks.example.com/result"),
        runtime.TriggerInvocation(
            trigger_id=TRIGGER_ID,
            delivery_id=DELIVERY_ID,
            response="processed",
            session_id="trigger-session",
        ),
    )

    connection_class.assert_not_called()


# ---------------------------------------------------------------------------
# Public webhook authentication and durable enqueue
# ---------------------------------------------------------------------------


def webhook_trigger() -> Trigger:
    return make_trigger(
        trigger_type=TYPE_WEBHOOK,
        schedule=None,
        webhook_secret_ref=(f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-trigger/alice"),
    )


def webhook_secrets(secret: str) -> MagicMock:
    client = MagicMock()
    client.describe_secret.return_value = {
        "Name": "agentcore-trigger/alice",
        "Tags": [
            {"Key": "ManagedBy", "Value": "agentcore-flows"},
            {"Key": "Purpose", "Value": "trigger-webhook-hmac"},
            {"Key": "owner_sub", "Value": "sub-alice"},
        ],
    }
    client.get_secret_value.return_value = {"SecretString": secret}
    return client


def test_webhook_hmac_covers_timestamp_delivery_and_raw_body():
    trigger = webhook_trigger()
    secret = "a" * 64
    timestamp = "1700000000"
    body = b'{"order":"o-123"}'
    signature = runtime.webhook_signature(
        secret,
        timestamp=timestamp,
        delivery_id=DELIVERY_ID,
        body=body,
    )

    assert (
        runtime.authenticate_webhook(
            trigger,
            timestamp=timestamp,
            delivery_id=DELIVERY_ID,
            signature=signature,
            body=body,
            secrets_client=webhook_secrets(secret),
            now=1700000000,
        )
        == DELIVERY_ID
    )


@pytest.mark.parametrize(
    ("timestamp", "delivery_id", "signature", "body"),
    [
        ("1699999000", DELIVERY_ID, "v1=" + "0" * 64, b"body"),
        ("1700000000", "bad delivery id!", "v1=" + "0" * 64, b"body"),
        ("1700000000", DELIVERY_ID, "not-a-signature", b"body"),
        ("1700000000", DELIVERY_ID, "v1=" + "0" * 64, b"tampered"),
    ],
)
def test_webhook_rejects_stale_or_tampered_requests(
    timestamp: str,
    delivery_id: str,
    signature: str,
    body: bytes,
):
    with pytest.raises(runtime.WebhookAuthenticationError):
        runtime.authenticate_webhook(
            webhook_trigger(),
            timestamp=timestamp,
            delivery_id=delivery_id,
            signature=signature,
            body=body,
            secrets_client=webhook_secrets("a" * 64),
            now=1700000000,
        )


def test_webhook_refuses_a_secret_without_exact_provenance():
    trigger = webhook_trigger()
    secret_client = webhook_secrets("a" * 64)
    secret_client.describe_secret.return_value["Tags"][2]["Value"] = "sub-mallory"

    with pytest.raises(runtime.WebhookAuthenticationError):
        runtime.authenticate_webhook(
            trigger,
            timestamp="1700000000",
            delivery_id=DELIVERY_ID,
            signature="v1=" + "0" * 64,
            body=b"body",
            secrets_client=secret_client,
            now=1700000000,
        )
    secret_client.get_secret_value.assert_not_called()


def test_webhook_enqueues_to_sqs_and_requires_message_id(
    trigger_env: None,
):
    trigger = webhook_trigger()
    sqs = MagicMock()
    sqs.send_message.return_value = {"MessageId": "sqs-message"}

    runtime.enqueue_webhook_dispatch(
        trigger=trigger,
        delivery_event=delivery_event(),
        sqs_client=sqs,
    )

    _, kwargs = sqs.send_message.call_args
    assert kwargs["QueueUrl"] == QUEUE_URL
    body = json.loads(kwargs["MessageBody"])
    assert body["_agentcore_trigger_dispatch"] == runtime.DISPATCH_MARKER
    assert body["trigger_id"] == TRIGGER_ID

    sqs.send_message.return_value = {}
    with pytest.raises(runtime.TriggerDispatchError):
        runtime.enqueue_webhook_dispatch(
            trigger=trigger,
            delivery_event=delivery_event(),
            sqs_client=sqs,
        )
