"""Proof that naming/tag governance is durable workflow state, not modal state."""

from datetime import datetime, timezone

import pytest
from app.models.flow import Flow, FlowUpdateRequest
from app.models.workflow import DeploymentGovernanceV1, WorkflowDefinition
from app.services.dynamodb_storage import _deserialize_workflow, _serialize_workflow
from pydantic import ValidationError

NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)
GOVERNANCE = {
    "version": 1,
    "namingProfile": {
        "prefix": "ecb",
        "resourceNames": {
            "gateway": "{prefix}-{deployment}-gw",
            "runtime": "{prefix}_{deployment}_agent",
        },
    },
    "tags": {
        "explicitValues": {"cost:center": "4711"},
        "effectiveValues": {
            "cost:center": "4711",
            "platform:application": "payments",
        },
        "profile": {
            "name": "regulated",
            "updatedAt": "2026-09-23T12:00:00Z",
        },
        "policyRevision": "sha256:policy-v7",
    },
}


def _workflow_dict(*, governance=...):
    value = {
        "id": "workflow-governance",
        "name": "Governed workflow",
        "description": "",
        "version": "1.0.0",
        "nodes": [],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
        "metadata": {
            "author": "owner",
            "tags": [],
            "aws_region": "eu-west-1",
            "deployment_status": "not_deployed",
        },
        "created_at": NOW,
        "updated_at": NOW,
    }
    if governance is not ...:
        value["governance"] = governance
    return value


def test_legacy_workflow_migrates_to_an_explicit_empty_v1():
    workflow = WorkflowDefinition.model_validate(_workflow_dict())

    assert workflow.governance == DeploymentGovernanceV1()
    assert workflow.model_dump(mode="json")["governance"] == {
        "version": 1,
        "naming_profile": None,
        "tags": {
            "explicit_values": {},
            "effective_values": {},
            "profile": None,
            "policy_revision": "",
        },
    }


def test_governance_survives_the_real_dynamodb_serializer_round_trip():
    workflow = WorkflowDefinition.model_validate(_workflow_dict(governance=GOVERNANCE))

    restored = _deserialize_workflow(_serialize_workflow(workflow))

    assert restored == workflow
    assert restored.governance.naming_profile is not None
    assert restored.governance.naming_profile.prefix == "ecb"
    assert restored.governance.tags.effective_values["platform:application"] == "payments"
    assert restored.governance.tags.profile is not None
    assert restored.governance.tags.profile.name == "regulated"


def test_the_captured_profile_timestamp_is_stored_in_the_catalogs_spelling():
    """A ``Z``-spelled capture must come back as ``+00:00`` (the catalog's ``isoformat``), so the
    stored value compares equal to the catalog by string as well as by instant."""
    workflow = WorkflowDefinition.model_validate(_workflow_dict(governance=GOVERNANCE))

    dumped = workflow.model_dump(mode="json")["governance"]["tags"]["profile"]["updated_at"]
    assert dumped == "2026-09-23T12:00:00+00:00"
    item = _serialize_workflow(workflow)
    assert item["governance"]["tags"]["profile"]["updated_at"] == "2026-09-23T12:00:00+00:00"


def test_loose_flow_workflow_validates_and_canonicalizes_only_governance():
    flow = Flow(
        id="flow-1",
        name="Future canvas",
        workflow={
            "nodes": [{"future_node_shape": {"is": "still accepted"}}],
            "governance": GOVERNANCE,
        },
        created_at=NOW,
        updated_at=NOW,
    )

    assert flow.workflow["nodes"] == [{"future_node_shape": {"is": "still accepted"}}]
    assert flow.workflow["governance"]["namingProfile"]["prefix"] == "ecb"
    assert flow.workflow["governance"]["tags"]["policyRevision"] == "sha256:policy-v7"
    assert "naming_profile" not in flow.workflow["governance"]


def test_legacy_flow_workflow_is_migrated_on_read():
    flow = Flow(
        id="flow-legacy",
        name="Legacy",
        workflow={"nodes": [], "edges": []},
        created_at=NOW,
        updated_at=NOW,
    )

    assert flow.workflow["governance"] == {
        "version": 1,
        "namingProfile": None,
        "tags": {
            "explicitValues": {},
            "effectiveValues": {},
            "profile": None,
            "policyRevision": "",
        },
    }


def test_flow_update_refuses_malformed_governance_before_storage():
    with pytest.raises(ValidationError, match="policyRevision"):
        FlowUpdateRequest(
            workflow={
                "nodes": [{"still": "loose"}],
                "governance": {
                    **GOVERNANCE,
                    "tags": {
                        **GOVERNANCE["tags"],
                        "policyRevision": "",
                    },
                },
            }
        )


def test_explicit_empty_v1_clears_prior_governance():
    request = FlowUpdateRequest(
        workflow={
            "nodes": [],
            "governance": {},
        }
    )

    assert request.workflow is not None
    assert request.workflow["governance"] == {
        "version": 1,
        "namingProfile": None,
        "tags": {
            "explicitValues": {},
            "effectiveValues": {},
            "profile": None,
            "policyRevision": "",
        },
    }


@pytest.mark.parametrize(
    "bad_governance",
    [
        None,
        {"version": 2},
        {"version": 1, "unknown": True},
        {
            "version": 1,
            "tags": {
                "explicitValues": {},
                "effectiveValues": {"owner": ""},
                "profile": None,
                "policyRevision": "rev",
            },
        },
    ],
)
def test_present_invalid_governance_is_never_silently_dropped(bad_governance):
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(_workflow_dict(governance=bad_governance))
