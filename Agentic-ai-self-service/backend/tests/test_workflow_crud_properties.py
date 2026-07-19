"""Property-based tests for Workflow CRUD operations (Properties 1, 2, 3).

Feature: serverless-migration

These tests verify:
- Property 1: Workflow CRUD Round-Trip — create then retrieve returns equivalent workflow;
  delete then retrieve returns None (404 equivalent at storage level)
- Property 2: Partial Update Preserves Unmodified Fields — update subset of fields,
  verify other fields unchanged
- Property 3: Invalid Workflow Import Rejection — invalid dicts return 400 with validation errors

Tests run against in-memory WorkflowStorage (no DynamoDB needed).

**Validates: Requirements 2.1, 2.2, 2.4, 2.5, 2.7**
"""

import sys

sys.path.insert(0, "src")

from copy import deepcopy
from datetime import datetime, timezone

import pytest
from app.models.components import (
    ModelConfiguration,
    RuntimeConfiguration,
)
from app.models.enums import (
    AgentCoreComponentType,
    AgentFramework,
    AgentServerProtocol,
    DeploymentStatus,
    DeploymentType,
    ModelProvider,
    PythonRuntime,
)
from app.models.workflow import (
    ComponentNode,
    Position,
    Viewport,
    WorkflowDefinition,
    WorkflowMetadata,
)
from app.services.storage import WorkflowStorage
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

# ============================================================================
# Hypothesis Strategies
# ============================================================================

valid_id_st = st.text(
    min_size=1,
    max_size=50,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
).filter(lambda x: len(x.strip()) > 0)

valid_name_st = st.text(
    min_size=1,
    max_size=100,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters=" -_"),
).filter(lambda x: len(x.strip()) > 0)

valid_description_st = st.text(max_size=200)

valid_version_st = st.from_regex(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$", fullmatch=True)

valid_aws_region_st = st.sampled_from(
    [
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
        "eu-west-1",
        "eu-west-2",
        "eu-central-1",
        "ap-southeast-1",
        "ap-southeast-2",
        "ap-northeast-1",
    ]
)

valid_position_st = st.builds(
    Position,
    x=st.floats(min_value=-10000, max_value=10000, allow_nan=False, allow_infinity=False),
    y=st.floats(min_value=-10000, max_value=10000, allow_nan=False, allow_infinity=False),
)

valid_viewport_st = st.builds(
    Viewport,
    x=st.floats(min_value=-10000, max_value=10000, allow_nan=False, allow_infinity=False),
    y=st.floats(min_value=-10000, max_value=10000, allow_nan=False, allow_infinity=False),
    zoom=st.floats(min_value=0.1, max_value=4.0, allow_nan=False, allow_infinity=False),
)

valid_model_config_st = st.builds(
    ModelConfiguration,
    provider=st.sampled_from(list(ModelProvider)),
    model_id=st.text(min_size=1, max_size=50, alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_.").filter(
        lambda x: len(x.strip()) > 0
    ),
    temperature=st.floats(min_value=0.0, max_value=2.0, allow_nan=False, allow_infinity=False),
    top_p=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
)


@st.composite
def valid_metadata_st(draw):
    """Generate a valid WorkflowMetadata."""
    return WorkflowMetadata(
        author=draw(
            st.text(min_size=1, max_size=50, alphabet="abcdefghijklmnopqrstuvwxyz0123456789").filter(
                lambda x: len(x.strip()) > 0
            )
        ),
        tags=draw(
            st.lists(
                st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnopqrstuvwxyz"),
                max_size=5,
            )
        ),
        aws_region=draw(valid_aws_region_st),
        deployment_status=DeploymentStatus.NOT_DEPLOYED,
    )


def _make_runtime_config(name: str) -> RuntimeConfiguration:
    """Create a valid RuntimeConfiguration for a node."""
    return RuntimeConfiguration(
        component_type="runtime",
        name=name,
        entrypoint="agent.py",
        framework=AgentFramework.STRANDS_AGENTS,
        model=ModelConfiguration(
            provider=ModelProvider.ANTHROPIC,
            model_id="claude-3-sonnet",
            temperature=0.7,
            top_p=0.9,
        ),
        system_prompt="You are a helpful assistant.",
        deployment_type=DeploymentType.DIRECT_CODE_DEPLOY,
        python_runtime=PythonRuntime.PYTHON_3_12,
        protocol=AgentServerProtocol.HTTP,
    )


# Strategy for a simple component node (runtime type, no edges needed)
def _runtime_node_st(node_id_st=None):
    """Strategy for generating a valid runtime ComponentNode."""
    if node_id_st is None:
        node_id_st = valid_id_st
    return st.builds(
        lambda nid, pos: ComponentNode(
            id=nid,
            type=AgentCoreComponentType.RUNTIME,
            position=pos,
            data=_make_runtime_config(f"runtime-{nid}"),
        ),
        nid=node_id_st,
        pos=valid_position_st,
    )


# Strategy for generating a list of unique runtime nodes
valid_nodes_st = st.lists(
    _runtime_node_st(),
    max_size=3,
).filter(lambda nodes: len({n.id for n in nodes}) == len(nodes))


# Strategy for a valid WorkflowDefinition (no edges for simplicity — edges require
# matching node IDs which adds complexity without testing CRUD logic)
@st.composite
def valid_workflow_st(draw):
    """Generate a valid WorkflowDefinition with unique ID."""
    wf_id = draw(valid_id_st)
    name = draw(valid_name_st)
    description = draw(valid_description_st)
    version = draw(valid_version_st)
    nodes = draw(valid_nodes_st)
    viewport = draw(valid_viewport_st)
    metadata = draw(valid_metadata_st())
    now = datetime.now(timezone.utc)

    return WorkflowDefinition(
        id=wf_id,
        name=name,
        description=description,
        version=version,
        nodes=nodes,
        edges=[],
        viewport=viewport,
        metadata=metadata,
        created_at=now,
        updated_at=now,
    )


# ============================================================================
# Property 1: Workflow CRUD Round-Trip
# ============================================================================


class TestProperty1WorkflowCRUDRoundTrip:
    """Property 1: Workflow CRUD Round-Trip.

    For any valid WorkflowDefinition, creating it via WorkflowStorage, then
    retrieving it by ID, should return an equivalent workflow. Subsequently
    deleting it and retrieving again should return None (404 at API level).

    **Validates: Requirements 2.1, 2.2, 2.5**
    """

    @given(workflow=valid_workflow_st())
    @settings(max_examples=100)
    def test_create_then_retrieve_returns_equivalent_workflow(self, workflow: WorkflowDefinition):
        """Create a workflow, then retrieve it — should be equivalent."""
        storage = WorkflowStorage()

        created = storage.create(workflow)

        # Retrieve by ID
        retrieved = storage.get(created.id)
        assert retrieved is not None, "Retrieved workflow should not be None after create"

        # Core fields should match (storage may update timestamps)
        assert retrieved.id == created.id
        assert retrieved.name == created.name
        assert retrieved.description == created.description
        assert retrieved.version == created.version
        assert len(retrieved.nodes) == len(created.nodes)
        assert len(retrieved.edges) == len(created.edges)
        assert retrieved.viewport == created.viewport
        assert retrieved.metadata.author == created.metadata.author
        assert retrieved.metadata.aws_region == created.metadata.aws_region
        assert retrieved.metadata.tags == created.metadata.tags

    @given(workflow=valid_workflow_st())
    @settings(max_examples=100)
    def test_delete_then_retrieve_returns_none(self, workflow: WorkflowDefinition):
        """Create a workflow, delete it, then retrieve — should return None (404)."""
        storage = WorkflowStorage()

        created = storage.create(workflow)
        assert storage.get(created.id) is not None

        # Delete
        deleted = storage.delete(created.id)
        assert deleted is True, "Delete should return True for existing workflow"

        # Retrieve after delete should return None (equivalent to 404)
        retrieved = storage.get(created.id)
        assert retrieved is None, "Workflow should not be found after deletion"

    @given(workflow=valid_workflow_st())
    @settings(max_examples=100)
    def test_delete_nonexistent_returns_false(self, workflow: WorkflowDefinition):
        """Deleting a non-existent workflow should return False."""
        storage = WorkflowStorage()

        # Delete without creating — should return False
        deleted = storage.delete(workflow.id)
        assert deleted is False, "Delete should return False for non-existent workflow"


# ============================================================================
# Property 2: Partial Update Preserves Unmodified Fields
# ============================================================================


# The updatable fields for a workflow
UPDATABLE_FIELDS = [
    "name",
    "description",
    "version",
    "nodes",
    "edges",
    "viewport",
    "metadata",
]


@st.composite
def partial_update_st(draw):
    """Generate a random subset of updatable fields with new values."""
    # Pick a non-empty subset of fields to update
    fields_to_update = draw(
        st.lists(
            st.sampled_from(UPDATABLE_FIELDS),
            min_size=1,
            max_size=len(UPDATABLE_FIELDS),
            unique=True,
        )
    )

    updates = {}
    for field in fields_to_update:
        if field == "name":
            updates["name"] = draw(valid_name_st)
        elif field == "description":
            updates["description"] = draw(valid_description_st)
        elif field == "version":
            updates["version"] = draw(valid_version_st)
        elif field == "nodes":
            updates["nodes"] = draw(valid_nodes_st)
        elif field == "edges":
            # Keep edges empty since we don't generate matching node IDs
            updates["edges"] = []
        elif field == "viewport":
            updates["viewport"] = draw(valid_viewport_st)
        elif field == "metadata":
            updates["metadata"] = draw(valid_metadata_st())

    return fields_to_update, updates


class TestProperty2PartialUpdatePreservesUnmodifiedFields:
    """Property 2: Partial Update Preserves Unmodified Fields.

    For any existing workflow and any subset of updatable fields, updating with
    only those fields should change exactly those fields and leave all other
    fields unchanged (except updated_at which is always refreshed).

    **Validates: Requirements 2.4**
    """

    @given(workflow=valid_workflow_st(), partial=partial_update_st())
    @settings(max_examples=100)
    def test_partial_update_preserves_unmodified_fields(
        self,
        workflow: WorkflowDefinition,
        partial: tuple,
    ):
        """Update a subset of fields — unmodified fields should remain unchanged."""
        storage = WorkflowStorage()
        fields_to_update, updates = partial

        # Create the original workflow
        created = storage.create(workflow)
        original = storage.get(created.id)
        assert original is not None

        # Build updated workflow using model_copy (same pattern as the router)
        updated_workflow = original.model_copy(update=updates)

        # Perform the update
        result = storage.update(created.id, updated_workflow)
        assert result is not None, "Update should return the updated workflow"

        # Verify updated fields changed
        for field in fields_to_update:
            if field == "name":
                assert result.name == updates["name"]
            elif field == "description":
                assert result.description == updates["description"]
            elif field == "version":
                assert result.version == updates["version"]
            elif field == "nodes":
                assert len(result.nodes) == len(updates["nodes"])
            elif field == "edges":
                assert result.edges == updates["edges"]
            elif field == "viewport":
                assert result.viewport == updates["viewport"]
            elif field == "metadata":
                assert result.metadata.author == updates["metadata"].author
                assert result.metadata.aws_region == updates["metadata"].aws_region

        # Verify unmodified fields are preserved
        unmodified_fields = set(UPDATABLE_FIELDS) - set(fields_to_update)
        for field in unmodified_fields:
            if field == "name":
                assert result.name == original.name
            elif field == "description":
                assert result.description == original.description
            elif field == "version":
                assert result.version == original.version
            elif field == "nodes":
                assert len(result.nodes) == len(original.nodes)
                for orig_node, res_node in zip(original.nodes, result.nodes, strict=True):
                    assert orig_node.id == res_node.id
            elif field == "edges":
                assert result.edges == original.edges
            elif field == "viewport":
                assert result.viewport == original.viewport
            elif field == "metadata":
                assert result.metadata.author == original.metadata.author
                assert result.metadata.aws_region == original.metadata.aws_region
                assert result.metadata.tags == original.metadata.tags

        # created_at should always be preserved
        assert result.created_at == original.created_at

        # updated_at should be refreshed (>= original)
        assert result.updated_at >= original.updated_at

        # ID should always be preserved
        assert result.id == original.id


# ============================================================================
# Property 3: Invalid Workflow Import Rejection
# ============================================================================


@st.composite
def invalid_workflow_dict_st(draw):
    """Generate dicts that violate the WorkflowDefinition Pydantic schema.

    Strategies:
    1. Missing required fields
    2. Wrong types for fields
    3. Invalid enum values
    4. Invalid version format
    5. Invalid AWS region format
    6. Completely empty dict
    """
    strategy_choice = draw(st.integers(min_value=0, max_value=5))

    now = datetime.now(timezone.utc).isoformat()
    base_valid = {
        "id": "test-id",
        "name": "Test Workflow",
        "description": "desc",
        "version": "1.0.0",
        "nodes": [],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "metadata": {
            "author": "test-author",
            "tags": [],
            "aws_region": "us-east-1",
            "deployment_status": "not_deployed",
        },
        "created_at": now,
        "updated_at": now,
    }

    if strategy_choice == 0:
        # Missing required fields — remove fields that the import endpoint
        # does NOT auto-fill. The import endpoint auto-fills id, created_at,
        # updated_at, so removing those won't cause a 400. We must remove
        # fields like name, version, or metadata which are truly required.
        required_field = draw(st.sampled_from(["name", "version", "metadata"]))
        d = deepcopy(base_valid)
        del d[required_field]
        return d

    elif strategy_choice == 1:
        # Wrong type for a field
        field_to_break = draw(st.sampled_from(["name", "version", "nodes", "edges", "viewport"]))
        d = deepcopy(base_valid)
        if field_to_break in ("name", "version"):
            d[field_to_break] = draw(st.integers())  # Should be string
        elif field_to_break in ("nodes", "edges"):
            d[field_to_break] = draw(st.text(min_size=1, max_size=10))  # Should be list
        elif field_to_break == "viewport":
            d[field_to_break] = "not-a-viewport"  # Should be dict
        return d

    elif strategy_choice == 2:
        # Invalid enum value in metadata
        d = deepcopy(base_valid)
        valid_statuses = {s.value for s in DeploymentStatus}
        invalid_status = draw(
            st.text(min_size=3, max_size=20, alphabet="abcdefghijklmnopqrstuvwxyz_").filter(
                lambda x: x not in valid_statuses
            )
        )
        d["metadata"]["deployment_status"] = invalid_status
        return d

    elif strategy_choice == 3:
        # Invalid version format
        d = deepcopy(base_valid)
        d["version"] = draw(
            st.sampled_from(
                [
                    "invalid",
                    "1.0",
                    "1",
                    "v1.0.0",
                    "1.0.0.0",
                    "abc.def.ghi",
                    "",
                    "1.0.0-beta",
                    "1.0.0+build",
                ]
            )
        )
        return d

    elif strategy_choice == 4:
        # Invalid AWS region format
        d = deepcopy(base_valid)
        d["metadata"]["aws_region"] = draw(
            st.sampled_from(
                [
                    "invalid-region",
                    "us-east",
                    "us_east_1",
                    "",
                    "123",
                    "US-EAST-1",
                    "us-east-1a",
                ]
            )
        )
        return d

    else:
        # Completely empty or minimal dict
        return draw(
            st.sampled_from(
                [
                    {},
                    {"name": "only-name"},
                    {"id": 12345},  # Wrong type for id
                    {"nodes": "not-a-list"},
                ]
            )
        )


class TestProperty3InvalidWorkflowImportRejection:
    """Property 3: Invalid Workflow Import Rejection.

    For any dict that does not conform to the WorkflowDefinition Pydantic schema
    (missing required fields, wrong types, invalid enum values), attempting to
    validate it should raise a ValidationError (which at the API level translates
    to a 400 status code with validation error details).

    **Validates: Requirements 2.7**
    """

    @given(invalid_dict=invalid_workflow_dict_st())
    @settings(max_examples=100)
    def test_invalid_dict_raises_validation_error(self, invalid_dict: dict):
        """Invalid workflow dicts should be rejected by Pydantic validation."""
        with pytest.raises((ValidationError, Exception)):
            WorkflowDefinition.model_validate(invalid_dict)

    @given(invalid_dict=invalid_workflow_dict_st())
    @settings(max_examples=100, deadline=None)
    def test_invalid_dict_via_api_returns_400(self, invalid_dict: dict):
        """Invalid workflow dicts imported via the API should return 400.

        This tests the full API path: POST /api/workflows/import with an
        invalid workflow_json should return HTTP 400 with error details.
        """
        from app.main import app
        from app.services.storage import get_workflow_storage
        from fastapi.testclient import TestClient

        client = TestClient(app)
        get_workflow_storage().clear()

        response = client.post(
            "/api/workflows/import",
            json={"workflow_json": invalid_dict},
        )

        assert response.status_code == 400, (
            f"Expected 400 for invalid dict, got {response.status_code}. Dict: {invalid_dict}"
        )
        data = response.json()
        # The API should return error details
        assert "detail" in data
        detail = data["detail"]
        assert "errors" in detail, "Response should contain validation errors"
        assert len(detail["errors"]) > 0, "Should have at least one error"
