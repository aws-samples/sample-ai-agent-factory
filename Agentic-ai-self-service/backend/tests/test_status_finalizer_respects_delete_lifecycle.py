"""A teardown-owned deployment is terminal to the status finalizer."""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import boto3
import pytest

sys.path.insert(0, "src")

pytest.importorskip("moto")

from app.models.deployment_models import DeploymentState, DeploymentStatusEnum  # noqa: E402
from app.services.deployment_state_store import DeploymentStateStore  # noqa: E402
from app.step_handlers import status_update_step as status_update  # noqa: E402
from moto import mock_aws  # noqa: E402


@pytest.mark.parametrize(
    "delete_status",
    ["deleting", "deleted", "delete_failed", "delete_retained"],
)
def test_finalizer_cancels_before_any_post_delete_write(monkeypatch, delete_status):
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName="Deployments",
            KeySchema=[{"AttributeName": "deployment_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "deployment_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        store = DeploymentStateStore("Deployments", "us-east-1")
        store.create(
            DeploymentState(
                deployment_id="d-finalizer-race",
                workflow_id="w-finalizer-race",
                user_id="owner",
                status=DeploymentStatusEnum.PENDING,
                started_at=datetime.now(timezone.utc),
            )
        )
        store.update_delete_status(
            "d-finalizer-race",
            delete_status,
            "teardown owns this row",
        )
        before = store._table.get_item(  # noqa: SLF001 - exact persisted-state assertion
            Key={"deployment_id": "d-finalizer-race"},
            ConsistentRead=True,
        )["Item"]

        monkeypatch.setattr(status_update, "_get_deployment_store", lambda: store)

        def forbidden(*args, **kwargs):
            raise AssertionError("the finalizer continued after the lifecycle fence")

        monkeypatch.setattr(status_update, "get_versions_store", forbidden)
        monkeypatch.setattr(status_update, "get_slots_store", forbidden)
        monkeypatch.setattr(status_update, "_auto_register_in_aws_registry", forbidden)

        result = status_update.handler(
            {
                "deployment_id": "d-finalizer-race",
                "version_id": "v1",
                "friendly_runtime_name": "orders",
                "owner_sub": "owner",
            },
            None,
        )

        after = store._table.get_item(  # noqa: SLF001 - exact persisted-state assertion
            Key={"deployment_id": "d-finalizer-race"},
            ConsistentRead=True,
        )["Item"]
        assert result == {
            "deployment_id": "d-finalizer-race",
            "status": "cancelled",
            "error_details": ("The deployment was deleted while its final status was being written."),
            "version_id": "v1",
        }
        assert after == before
