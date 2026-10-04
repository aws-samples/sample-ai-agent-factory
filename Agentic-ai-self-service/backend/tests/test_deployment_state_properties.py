"""Property-based tests for Deployment State DynamoDB round-trip.

Feature: serverless-migration
Property 5: Deployment State DynamoDB Round-Trip

For any valid DeploymentState object, serializing it to a DynamoDB item
(with float-to-Decimal conversion and ISO 8601 datetime strings) then
deserializing back should produce an equivalent DeploymentState. Live records
must omit DynamoDB TTL; only successfully deleted tombstones expire.

**Validates: Requirements 3.7, 4.1, 4.3**
"""

import sys
from unittest.mock import MagicMock

sys.path.insert(0, "src")

from datetime import datetime, timezone

import app.services.deployment_state_store as state_store_module
import boto3
import pytest
from app.models.deployment_models import (
    DeploymentState,
    DeploymentStatusEnum,
    DeploymentStepName,
)
from app.services.deployment_state_store import (
    DeploymentLifecycleConflict,
    DeploymentStateStore,
    deserialize_deployment_state,
    serialize_deployment_state,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws

# ============================================================================
# Hypothesis Strategies
# ============================================================================

deployment_id_st = st.text(
    min_size=1,
    max_size=64,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
).filter(lambda s: len(s.strip()) > 0)

workflow_id_st = st.text(
    min_size=1,
    max_size=64,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
).filter(lambda s: len(s.strip()) > 0)

# Timestamps between 2020 and 2030, always UTC-aware
aware_datetime_st = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2030, 12, 31),
).map(lambda dt: dt.replace(tzinfo=timezone.utc))

optional_aware_datetime_st = st.none() | aware_datetime_st

status_st = st.sampled_from(list(DeploymentStatusEnum))
step_st = st.sampled_from(list(DeploymentStepName))
optional_step_st = st.none() | step_st

optional_short_string_st = st.none() | st.text(min_size=1, max_size=200)
optional_url_st = st.none() | st.text(
    min_size=8,
    max_size=200,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters=":/.-_"),
)
optional_arn_st = st.none() | st.text(
    min_size=10,
    max_size=200,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters=":/.-_"),
)


@st.composite
def deployment_state_st(draw):
    """Generate a random valid DeploymentState."""
    started_at = draw(aware_datetime_st)
    status = draw(status_st)

    # completed_at only makes sense for terminal states, but we generate it
    # for any state to test round-trip fidelity regardless
    completed_at = draw(optional_aware_datetime_st)

    return DeploymentState(
        deployment_id=draw(deployment_id_st),
        workflow_id=draw(workflow_id_st),
        execution_arn=draw(optional_arn_st),
        status=status,
        current_step=draw(optional_step_st),
        started_at=started_at,
        completed_at=completed_at,
        runtime_endpoint=draw(optional_url_st),
        runtime_id=draw(optional_short_string_st),
        runtime_protocol=draw(st.sampled_from(["HTTP", "MCP", "A2A"])),
        gateway_url=draw(optional_url_st),
        error_details=draw(optional_short_string_st),
        # Live deployment records deliberately have no TTL.
        ttl=None,
    )


# ============================================================================
# Property 5: Deployment State DynamoDB Round-Trip
# ============================================================================

_THIRTY_DAYS_SECONDS = 30 * 24 * 60 * 60
_ONE_HOUR_SECONDS = 3600


class TestDeploymentStateRoundTrip:
    """Property 5: Deployment State DynamoDB Round-Trip.

    **Validates: Requirements 3.7, 4.1, 4.3**
    """

    @given(state=deployment_state_st())
    @settings(max_examples=100)
    def test_serialize_deserialize_round_trip(self, state: DeploymentState):
        """Serializing then deserializing a DeploymentState produces an equivalent object.

        **Validates: Requirements 3.7, 4.1, 4.3**
        """
        item = serialize_deployment_state(state)
        restored = deserialize_deployment_state(item)

        assert restored.deployment_id == state.deployment_id
        assert restored.workflow_id == state.workflow_id
        assert restored.execution_arn == state.execution_arn
        assert restored.status == state.status
        assert restored.current_step == state.current_step
        assert restored.runtime_endpoint == state.runtime_endpoint
        assert restored.runtime_id == state.runtime_id
        assert restored.runtime_protocol == state.runtime_protocol
        assert restored.gateway_url == state.gateway_url
        assert restored.error_details == state.error_details
        assert restored.ttl is None

        # Datetime comparison: ISO 8601 round-trip may lose sub-second precision
        # depending on serialization, so compare to the second
        assert restored.started_at.replace(microsecond=0) == state.started_at.replace(microsecond=0)

        if state.completed_at is not None:
            assert restored.completed_at is not None
            assert restored.completed_at.replace(microsecond=0) == state.completed_at.replace(microsecond=0)
        else:
            assert restored.completed_at is None

    def test_serialize_omits_optional_none_fields_for_gsi_safety(self):
        """Bug 111 regression — serialized item must NOT contain NULL values for
        optional fields (runtime_id, gateway_url, completed_at, error_details).
        DynamoDB GSIs key on runtime_id and reject NULL writes."""
        from datetime import datetime, timezone

        from app.models.deployment_models import DeploymentStatusEnum

        state = DeploymentState(
            deployment_id="d-1234",
            workflow_id="w-1234",
            status=DeploymentStatusEnum.PENDING,
            current_step="validate",
            started_at=datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc),
            # runtime_id, gateway_url, completed_at, error_details, execution_arn,
            # runtime_endpoint all default to None — must NOT appear in serialized item
        )
        item = serialize_deployment_state(state)
        for key in ("runtime_id", "gateway_url", "completed_at", "error_details", "runtime_endpoint", "execution_arn"):
            assert key not in item, (
                f"serialize_deployment_state must omit None-valued '{key}' to avoid "
                f"DDB GSI NULL-key rejection (Bug 111)"
            )
        # required fields are still present
        assert item["deployment_id"] == "d-1234"
        assert item["workflow_id"] == "w-1234"
        assert "ttl" not in item

    @given(state=deployment_state_st())
    @settings(max_examples=100)
    def test_live_records_omit_even_a_legacy_ttl(self, state: DeploymentState):
        """A stale started_at-based TTL is removed from every live record.

        **Validates: Requirements 3.7, 4.1, 4.3**
        """
        legacy_state = state.model_copy(
            update={
                "ttl": int(state.started_at.timestamp()) + _THIRTY_DAYS_SECONDS,
                "delete_status": "delete_retained",
            }
        )
        item = serialize_deployment_state(legacy_state)
        assert "ttl" not in item

    def test_deleted_tombstone_gets_30_days_from_deletion_not_deployment_start(self):
        state = DeploymentState(
            deployment_id="d-deleted",
            workflow_id="w-deleted",
            status=DeploymentStatusEnum.SUCCEEDED,
            started_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            delete_status="deleted",
        )
        before = int(datetime.now(timezone.utc).timestamp()) + _THIRTY_DAYS_SECONDS
        item = serialize_deployment_state(state)
        after = int(datetime.now(timezone.utc).timestamp()) + _THIRTY_DAYS_SECONDS

        assert before - _ONE_HOUR_SECONDS <= int(item["ttl"]) <= after + _ONE_HOUR_SECONDS
        assert int(item["ttl"]) > int(state.started_at.timestamp()) + _THIRTY_DAYS_SECONDS

    def test_deleted_tombstone_preserves_an_existing_expiry(self):
        state = DeploymentState(
            deployment_id="d-deleted",
            workflow_id="w-deleted",
            status=DeploymentStatusEnum.SUCCEEDED,
            started_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            delete_status="deleted",
            ttl=1_900_000_000,
        )
        assert int(serialize_deployment_state(state)["ttl"]) == 1_900_000_000

    def test_create_clears_a_legacy_ttl(self, monkeypatch):
        store = object.__new__(DeploymentStateStore)
        store._table = object()
        put = MagicMock()
        monkeypatch.setattr(state_store_module, "_put_item", put)
        state = DeploymentState(
            deployment_id="d-create",
            workflow_id="w-create",
            status=DeploymentStatusEnum.PENDING,
            started_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            ttl=1,
        )

        created = store.create(state)

        assert created.ttl is None
        assert "ttl" not in put.call_args.args[1]
        assert put.call_args.kwargs["condition_expr"] == "attribute_not_exists(deployment_id)"

    def test_get_can_make_an_authority_read_strongly_consistent(self):
        store = object.__new__(DeploymentStateStore)
        store._table = MagicMock()
        store._table.get_item.return_value = {}

        assert store.get("d-authority", consistent=True) is None

        store._table.get_item.assert_called_once_with(
            Key={"deployment_id": "d-authority"},
            ConsistentRead=True,
        )

    def test_update_step_removes_legacy_ttl_and_requires_existing_row(self, monkeypatch):
        store = object.__new__(DeploymentStateStore)
        store._table = object()
        store.get = MagicMock(return_value=object())
        update = MagicMock()
        monkeypatch.setattr(state_store_module, "_update_item", update)

        store.update_step("d-step", DeploymentStepName.VALIDATE)

        kwargs = update.call_args.kwargs
        assert kwargs["update_expr"] == "SET current_step = :step, #s = :status REMOVE #t"
        assert ":ttl" not in kwargs["expr_values"]
        assert kwargs["expr_names"]["#t"] == "ttl"
        assert kwargs["expr_names"]["#delete_status"] == "delete_status"
        assert kwargs["expr_names"]["#fl"] == "finalizer_lease_expires_at"
        assert kwargs["expr_names"]["#ft"] == "finalizer_lease_token"
        assert kwargs["condition_expr"] == (
            "attribute_exists(deployment_id) AND "
            "attribute_not_exists(#delete_status) AND "
            "attribute_not_exists(#fl) AND attribute_not_exists(#ft)"
        )

    def test_update_status_removes_legacy_ttl_and_requires_existing_row(self, monkeypatch):
        store = object.__new__(DeploymentStateStore)
        store._table = object()
        store.get = MagicMock(return_value=object())
        update = MagicMock()
        monkeypatch.setattr(state_store_module, "_update_item", update)

        store.update_status("d-status", DeploymentStatusEnum.SUCCEEDED)

        kwargs = update.call_args.kwargs
        assert kwargs["update_expr"] == "SET #s = :status REMOVE #t"
        assert ":ttl" not in kwargs["expr_values"]
        assert kwargs["expr_names"]["#t"] == "ttl"
        assert kwargs["expr_names"]["#delete_status"] == "delete_status"
        assert kwargs["expr_names"]["#fl"] == "finalizer_lease_expires_at"
        assert kwargs["expr_names"]["#ft"] == "finalizer_lease_token"
        assert kwargs["condition_expr"] == (
            "attribute_exists(deployment_id) AND "
            "attribute_not_exists(#delete_status) AND "
            "attribute_not_exists(#fl) AND attribute_not_exists(#ft)"
        )

    @pytest.mark.parametrize(
        "delete_status",
        ["deleting", "deleted", "delete_failed", "delete_retained"],
    )
    def test_teardown_state_rejects_a_late_finalizer(self, delete_status):
        """The write condition, not a stale pre-read, owns the lifecycle decision."""
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
                    deployment_id="d-race",
                    workflow_id="w-race",
                    user_id="owner",
                    status=DeploymentStatusEnum.PENDING,
                    started_at=datetime.now(timezone.utc),
                )
            )
            store.update_delete_status("d-race", delete_status, "teardown")
            before = store._table.get_item(
                Key={"deployment_id": "d-race"},
                ConsistentRead=True,
            )["Item"]

            with pytest.raises(DeploymentLifecycleConflict):
                store.update_status(
                    "d-race",
                    DeploymentStatusEnum.SUCCEEDED,
                    completed_at=datetime.now(timezone.utc),
                )

            after = store._table.get_item(
                Key={"deployment_id": "d-race"},
                ConsistentRead=True,
            )["Item"]
            assert after["status"] == DeploymentStatusEnum.PENDING.value
            assert after["delete_status"] == delete_status
            assert ("ttl" in after) is (delete_status == "deleted")
            if delete_status == "deleted":
                assert after["ttl"] == before["ttl"]

    @pytest.mark.parametrize(
        "delete_status",
        ["deleting", "deleted", "delete_failed", "delete_retained"],
    )
    def test_teardown_state_rejects_a_late_step_update(self, delete_status):
        """The finalizer's first update cannot mutate or de-expire teardown state."""
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
                    deployment_id="d-step-race",
                    workflow_id="w-step-race",
                    user_id="owner",
                    status=DeploymentStatusEnum.PENDING,
                    started_at=datetime.now(timezone.utc),
                )
            )
            store.update_delete_status("d-step-race", delete_status, "teardown")
            before = store._table.get_item(
                Key={"deployment_id": "d-step-race"},
                ConsistentRead=True,
            )["Item"]

            with pytest.raises(DeploymentLifecycleConflict):
                store.update_step(
                    "d-step-race",
                    DeploymentStepName.STATUS_UPDATE,
                    DeploymentStatusEnum.IN_PROGRESS,
                )

            after = store._table.get_item(
                Key={"deployment_id": "d-step-race"},
                ConsistentRead=True,
            )["Item"]
            assert after == before

    def test_delete_status_only_expires_a_successful_tombstone(self, monkeypatch):
        store = object.__new__(DeploymentStateStore)
        store._table = object()
        update = MagicMock()
        monkeypatch.setattr(state_store_module, "_update_item", update)

        store.update_delete_status("d-status", "delete_retained", "kept")
        retained = update.call_args.kwargs
        assert retained["update_expr"] == ("SET delete_status = :ds, delete_message = :dm REMOVE #t, #claim")
        assert ":ttl" not in retained["expr_values"]
        assert retained["expr_names"]["#claim"] == "delete_claim_expires_at"
        assert retained["condition_expr"] == "attribute_exists(deployment_id)"

        store.update_delete_status("d-status", "deleted", "gone")
        deleted = update.call_args.kwargs
        assert deleted["update_expr"] == ("SET delete_status = :ds, delete_message = :dm, #t = :ttl REMOVE #claim")
        assert deleted["expr_values"][":ttl"] > int(datetime.now(timezone.utc).timestamp())
        assert deleted["expr_names"]["#claim"] == "delete_claim_expires_at"
        assert deleted["condition_expr"] == "attribute_exists(deployment_id)"
