"""Property-based tests for DynamoDB storage adapter.

These tests verify the correctness of the DynamoDB storage layer using
Hypothesis to generate random WorkflowDefinition objects and moto to
mock DynamoDB locally.

Requirements: 4.3, 4.4, 4.5, 4.6, 4.7, 4.9
"""

import sys

sys.path.insert(0, "src")

import os
from datetime import datetime, timezone

import boto3
import pytest
from app.models import (
    AgentCoreComponentType,
    AgentFramework,
    BrowserConfiguration,
    CodeInterpreterConfiguration,
    ComponentNode,
    ConnectionEdge,
    ConnectionType,
    MemoryConfiguration,
    ModelConfiguration,
    ModelProvider,
    ObservabilityConfiguration,
    Position,
    RuntimeConfiguration,
    Viewport,
    WorkflowDefinition,
    WorkflowMetadata,
)
from app.services.dynamodb_storage import DynamoDBWorkflowStorage
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws

# ============================================================================
# Constants
# ============================================================================

TABLE_NAME = "test-workflows"
REGION = "us-east-1"


# ============================================================================
# Hypothesis Strategies
# ============================================================================

valid_id_st = st.text(
    min_size=1,
    max_size=40,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
).filter(lambda x: len(x.strip()) > 0)

valid_name_st = st.text(
    min_size=1,
    max_size=100,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters=" -_"),
).filter(lambda x: len(x.strip()) > 0)

valid_description_st = st.text(min_size=0, max_size=200)

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

valid_author_st = st.text(
    min_size=1,
    max_size=50,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters=" -_"),
).filter(lambda x: len(x.strip()) > 0)

position_st = st.builds(
    Position,
    x=st.floats(
        min_value=-5000.0,
        max_value=5000.0,
        allow_nan=False,
        allow_infinity=False,
        allow_subnormal=False,
    ),
    y=st.floats(
        min_value=-5000.0,
        max_value=5000.0,
        allow_nan=False,
        allow_infinity=False,
        allow_subnormal=False,
    ),
)

viewport_st = st.builds(
    Viewport,
    x=st.floats(
        min_value=-5000.0,
        max_value=5000.0,
        allow_nan=False,
        allow_infinity=False,
        allow_subnormal=False,
    ),
    y=st.floats(
        min_value=-5000.0,
        max_value=5000.0,
        allow_nan=False,
        allow_infinity=False,
        allow_subnormal=False,
    ),
    zoom=st.floats(
        min_value=0.1,
        max_value=4.0,
        allow_nan=False,
        allow_infinity=False,
        allow_subnormal=False,
    ),
)

metadata_st = st.builds(
    lambda author, tags, aws_region: WorkflowMetadata(
        author=author,
        tags=tags,
        aws_region=aws_region,
    ),
    author=valid_author_st,
    tags=st.lists(valid_name_st, min_size=0, max_size=3),
    aws_region=valid_aws_region_st,
)

# Model configuration strategy
model_config_st = st.builds(
    ModelConfiguration,
    provider=st.sampled_from(list(ModelProvider)),
    model_id=st.text(min_size=1, max_size=50).filter(lambda x: len(x.strip()) > 0),
    temperature=st.floats(
        min_value=0.0,
        max_value=2.0,
        allow_nan=False,
        allow_infinity=False,
        allow_subnormal=False,
    ),
    top_p=st.floats(
        min_value=0.0,
        max_value=1.0,
        allow_nan=False,
        allow_infinity=False,
        allow_subnormal=False,
    ),
)

# Component configuration strategies (simple ones that don't require complex nested configs)
runtime_config_st = st.builds(
    RuntimeConfiguration,
    name=valid_name_st,
    framework=st.sampled_from(list(AgentFramework)),
    model=model_config_st,
    system_prompt=st.text(min_size=1, max_size=500).filter(lambda x: len(x.strip()) > 0),
)

memory_config_st = st.builds(
    MemoryConfiguration,
    name=valid_name_st,
)

code_interpreter_config_st = st.builds(
    CodeInterpreterConfiguration,
    name=valid_name_st,
)

browser_config_st = st.builds(
    BrowserConfiguration,
    name=valid_name_st,
)

observability_config_st = st.builds(
    ObservabilityConfiguration,
    name=valid_name_st,
)


def _build_node(node_id, component_type, config, position):
    """Build a ComponentNode with matching type and config."""
    return ComponentNode(
        id=node_id,
        type=component_type,
        position=position,
        data=config,
    )


# Strategy for a single node with matching type/config
node_st = st.one_of(
    st.builds(
        _build_node,
        node_id=valid_id_st,
        component_type=st.just(AgentCoreComponentType.RUNTIME),
        config=runtime_config_st,
        position=position_st,
    ),
    st.builds(
        _build_node,
        node_id=valid_id_st,
        component_type=st.just(AgentCoreComponentType.MEMORY),
        config=memory_config_st,
        position=position_st,
    ),
    st.builds(
        _build_node,
        node_id=valid_id_st,
        component_type=st.just(AgentCoreComponentType.CODE_INTERPRETER),
        config=code_interpreter_config_st,
        position=position_st,
    ),
    st.builds(
        _build_node,
        node_id=valid_id_st,
        component_type=st.just(AgentCoreComponentType.BROWSER),
        config=browser_config_st,
        position=position_st,
    ),
    st.builds(
        _build_node,
        node_id=valid_id_st,
        component_type=st.just(AgentCoreComponentType.OBSERVABILITY),
        config=observability_config_st,
        position=position_st,
    ),
)


@st.composite
def unique_nodes_st(draw, min_size=0, max_size=5):
    """Generate a list of nodes with unique IDs."""
    count = draw(st.integers(min_value=min_size, max_value=max_size))
    nodes = []
    used_ids = set()
    for _ in range(count):
        node = draw(node_st)
        # Ensure unique ID
        while node.id in used_ids:
            new_id = draw(valid_id_st)
            node = node.model_copy(update={"id": new_id})
        used_ids.add(node.id)
        nodes.append(node)
    return nodes


@st.composite
def valid_edges_st(draw, nodes):
    """Generate valid edges between existing nodes (no self-loops)."""
    if len(nodes) < 2:
        return []
    node_ids = [n.id for n in nodes]
    count = draw(st.integers(min_value=0, max_value=min(3, len(nodes) - 1)))
    edges = []
    used_edge_ids = set()
    for _ in range(count):
        source = draw(st.sampled_from(node_ids))
        target = draw(st.sampled_from([nid for nid in node_ids if nid != source]))
        edge_id = draw(valid_id_st)
        while edge_id in used_edge_ids:
            edge_id = draw(valid_id_st)
        used_edge_ids.add(edge_id)
        edges.append(
            ConnectionEdge(
                id=edge_id,
                source=source,
                target=target,
                source_handle="output",
                target_handle="input",
                type=draw(st.sampled_from(list(ConnectionType))),
            )
        )
    return edges


@st.composite
def workflow_st(draw):
    """Generate a valid WorkflowDefinition with consistent nodes and edges."""
    nodes = draw(unique_nodes_st(min_size=0, max_size=4))
    edges = draw(valid_edges_st(nodes))
    now = datetime.now(timezone.utc)
    return WorkflowDefinition(
        id=draw(valid_id_st),
        name=draw(valid_name_st),
        description=draw(valid_description_st),
        version=draw(valid_version_st),
        nodes=nodes,
        edges=edges,
        viewport=draw(viewport_st),
        metadata=draw(metadata_st),
        created_at=now,
        updated_at=now,
    )


@st.composite
def distinct_workflows_st(draw, min_size=1, max_size=5):
    """Generate a list of workflows with distinct IDs."""
    count = draw(st.integers(min_value=min_size, max_value=max_size))
    workflows = []
    used_ids = set()
    for _ in range(count):
        wf = draw(workflow_st())
        while wf.id in used_ids:
            new_id = draw(valid_id_st)
            wf = wf.model_copy(update={"id": new_id})
        used_ids.add(wf.id)
        workflows.append(wf)
    return workflows


# ============================================================================
# Helper: Create mocked DynamoDB storage
# ============================================================================


def _create_mock_table():
    """Create the DynamoDB table in the moto mock environment."""
    # Set dummy AWS credentials for moto
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = REGION

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "workflow_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "workflow_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return DynamoDBWorkflowStorage(table_name=TABLE_NAME, region=REGION)


# ============================================================================
# Property 1: DynamoDB Storage Round-Trip
# Validates: Requirements 4.3, 4.4, 4.9
# ============================================================================


class TestStorageRoundTrip:
    """Property 1: DynamoDB Storage Round-Trip.

    For any valid WorkflowDefinition object, serializing it to DynamoDB
    format and then deserializing it back SHALL produce an object
    equivalent to the original.

    **Validates: Requirements 4.3, 4.4, 4.9**
    """

    @given(data=st.data())
    @settings(max_examples=100, deadline=None)
    def test_storage_round_trip(self, data):
        """Storing then retrieving a workflow produces an equivalent object."""
        workflow = data.draw(workflow_st())

        with mock_aws():
            storage = _create_mock_table()
            created = storage.create(workflow)
            retrieved = storage.get(created.id)

            assert retrieved is not None
            assert retrieved.id == created.id
            assert retrieved.name == created.name
            assert retrieved.description == created.description
            assert retrieved.version == created.version
            assert len(retrieved.nodes) == len(created.nodes)
            assert len(retrieved.edges) == len(created.edges)

            # Verify node data round-trips correctly
            created_node_ids = {n.id for n in created.nodes}
            retrieved_node_ids = {n.id for n in retrieved.nodes}
            assert created_node_ids == retrieved_node_ids

            # Verify edge data round-trips correctly
            created_edge_ids = {e.id for e in created.edges}
            retrieved_edge_ids = {e.id for e in retrieved.edges}
            assert created_edge_ids == retrieved_edge_ids

            # Verify metadata round-trips
            assert retrieved.metadata.author == created.metadata.author
            assert retrieved.metadata.aws_region == created.metadata.aws_region
            assert retrieved.metadata.tags == created.metadata.tags

            # Verify viewport round-trips
            assert retrieved.viewport.x == pytest.approx(created.viewport.x)
            assert retrieved.viewport.y == pytest.approx(created.viewport.y)
            assert retrieved.viewport.zoom == pytest.approx(created.viewport.zoom)


# ============================================================================
# Property 2: Update Preserves Identity and Advances Timestamp
# Validates: Requirements 4.5
# ============================================================================


class TestUpdatePreservesIdentity:
    """Property 2: Update Preserves Identity and Advances Timestamp.

    For any existing workflow and any valid update payload, after updating,
    the retrieved workflow SHALL have the same workflow_id and created_at
    as the original, and updated_at SHALL be >= the original updated_at.

    **Validates: Requirements 4.5**
    """

    @given(data=st.data())
    @settings(max_examples=100, deadline=None)
    def test_update_preserves_identity(self, data):
        """Update preserves workflow_id and created_at, advances updated_at."""
        original = data.draw(workflow_st())
        update_payload = data.draw(workflow_st())

        with mock_aws():
            storage = _create_mock_table()
            created = storage.create(original)
            original_id = created.id
            original_created_at = created.created_at
            original_updated_at = created.updated_at

            updated = storage.update(original_id, update_payload)

            assert updated is not None
            # Identity preserved
            assert updated.id == original_id
            assert updated.created_at == original_created_at
            # Timestamp advances (or stays equal if very fast)
            assert updated.updated_at >= original_updated_at

            # Verify the update persisted correctly
            retrieved = storage.get(original_id)
            assert retrieved is not None
            assert retrieved.id == original_id
            assert retrieved.name == update_payload.name


# ============================================================================
# Property 3: Delete Removes Workflow
# Validates: Requirements 4.6
# ============================================================================


class TestDeleteRemovesWorkflow:
    """Property 3: Delete Removes Workflow.

    For any workflow that has been created, after deleting it by ID,
    retrieving it by the same ID SHALL return None.

    **Validates: Requirements 4.6**
    """

    @given(data=st.data())
    @settings(max_examples=100, deadline=None)
    def test_delete_removes_workflow(self, data):
        """Deleting a workflow makes it unretrievable."""
        workflow = data.draw(workflow_st())

        with mock_aws():
            storage = _create_mock_table()
            created = storage.create(workflow)
            workflow_id = created.id

            # Verify it exists
            assert storage.get(workflow_id) is not None

            # Delete it
            result = storage.delete(workflow_id)
            assert result is True

            # Verify it's gone
            assert storage.get(workflow_id) is None

            # Deleting again should return False
            assert storage.delete(workflow_id) is False


# ============================================================================
# Property 4: List Returns All Created Workflows
# Validates: Requirements 4.7
# ============================================================================


class TestListReturnsAll:
    """Property 4: List Returns All Created Workflows.

    For any set of N distinct valid WorkflowDefinition objects created
    in the storage, calling list_all SHALL return exactly N items, and
    the set of returned workflow_ids SHALL equal the set of created
    workflow_ids.

    **Validates: Requirements 4.7**
    """

    @given(data=st.data())
    @settings(max_examples=100, deadline=None)
    def test_list_returns_all(self, data):
        """list_all returns exactly the workflows that were created."""
        workflows = data.draw(distinct_workflows_st(min_size=1, max_size=5))

        with mock_aws():
            storage = _create_mock_table()
            created_ids = set()
            for wf in workflows:
                created = storage.create(wf)
                created_ids.add(created.id)

            all_workflows = storage.list_all()
            listed_ids = {w.id for w in all_workflows}

            assert len(all_workflows) == len(workflows)
            assert listed_ids == created_ids
