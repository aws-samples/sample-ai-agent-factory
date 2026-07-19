"""Property-based tests for Deployment State DynamoDB round-trip.

Feature: serverless-migration
Property 5: Deployment State DynamoDB Round-Trip

For any valid DeploymentState object, serializing it to a DynamoDB item
(with float-to-Decimal conversion and ISO 8601 datetime strings) then
deserializing back should produce an equivalent DeploymentState. The TTL
field should be set to approximately 30 days (±1 hour) from started_at.

**Validates: Requirements 3.7, 4.1, 4.3**
"""

import sys

sys.path.insert(0, "src")

from datetime import datetime, timezone

from app.models.deployment_models import (
    DeploymentState,
    DeploymentStatusEnum,
    DeploymentStepName,
)
from app.services.deployment_state_store import (
    deserialize_deployment_state,
    serialize_deployment_state,
)
from hypothesis import given, settings
from hypothesis import strategies as st

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
        gateway_url=draw(optional_url_st),
        error_details=draw(optional_short_string_st),
        # ttl is computed by serialize, so we leave it as None here
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
        assert restored.gateway_url == state.gateway_url
        assert restored.error_details == state.error_details

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
        assert "ttl" in item

    @given(state=deployment_state_st())
    @settings(max_examples=100)
    def test_ttl_within_30_days_plus_minus_one_hour(self, state: DeploymentState):
        """TTL should be within ±1 hour of 30 days from started_at.

        **Validates: Requirements 3.7, 4.1, 4.3**
        """
        item = serialize_deployment_state(state)

        ttl_value = item["ttl"]
        # ttl may be Decimal after conversion, cast to int for comparison
        ttl_epoch = int(ttl_value)

        started_epoch = int(state.started_at.timestamp())
        expected_ttl = started_epoch + _THIRTY_DAYS_SECONDS

        assert abs(ttl_epoch - expected_ttl) <= _ONE_HOUR_SECONDS, (
            f"TTL {ttl_epoch} is not within ±1 hour of expected {expected_ttl} "
            f"(30 days from started_at={state.started_at.isoformat()})"
        )
