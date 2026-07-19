"""Property-based tests for serialization round-trips (Property 8).

Feature: serverless-migration

Property 8: WorkflowDefinition JSON Serialization Round-Trip
For any valid WorkflowDefinition object, calling model_dump(mode="json")
then model_validate on the result should produce an equivalent
WorkflowDefinition object.

**Validates: Requirements 11.1**
"""

import sys

sys.path.insert(0, "src")

from datetime import datetime, timezone

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
from hypothesis import given, settings
from hypothesis import strategies as st

# ============================================================================
# Hypothesis Strategies (adapted from test_workflow_crud_properties.py)
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

# Use floats that survive DynamoDB round-trip for model config too
_dynamo_safe_unit_float = st.floats(
    min_value=0.0,
    max_value=1.0,
    allow_nan=False,
    allow_infinity=False,
).filter(lambda x: x == 0.0 or abs(x) >= 1e-130)

valid_model_config_st = st.builds(
    ModelConfiguration,
    provider=st.sampled_from(list(ModelProvider)),
    model_id=st.text(
        min_size=1,
        max_size=50,
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_.",
    ).filter(lambda x: len(x.strip()) > 0),
    temperature=st.floats(
        min_value=0.0,
        max_value=2.0,
        allow_nan=False,
        allow_infinity=False,
    ).filter(lambda x: x == 0.0 or abs(x) >= 1e-130),
    top_p=_dynamo_safe_unit_float,
)


@st.composite
def valid_metadata_st(draw):
    """Generate a valid WorkflowMetadata."""
    return WorkflowMetadata(
        author=draw(
            st.text(
                min_size=1,
                max_size=50,
                alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
            ).filter(lambda x: len(x.strip()) > 0)
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


valid_nodes_st = st.lists(
    _runtime_node_st(),
    max_size=3,
).filter(lambda nodes: len({n.id for n in nodes}) == len(nodes))


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
# Property 8: WorkflowDefinition JSON Serialization Round-Trip
# ============================================================================


class TestProperty8WorkflowDefinitionJSONRoundTrip:
    """Property 8: WorkflowDefinition JSON Serialization Round-Trip.

    For any valid WorkflowDefinition object, calling model_dump(mode="json")
    then model_validate on the result should produce an equivalent
    WorkflowDefinition object.

    **Validates: Requirements 11.1**
    """

    @given(workflow=valid_workflow_st())
    @settings(max_examples=100)
    def test_json_round_trip_produces_equivalent_object(self, workflow: WorkflowDefinition):
        """model_dump(mode='json') → model_validate() should produce equivalent object."""
        # Serialize to JSON-compatible dict
        json_data = workflow.model_dump(mode="json")

        # Deserialize back to WorkflowDefinition
        restored = WorkflowDefinition.model_validate(json_data)

        # Verify equivalence of all fields
        assert restored.id == workflow.id
        assert restored.name == workflow.name
        assert restored.description == workflow.description
        assert restored.version == workflow.version
        assert restored.created_at == workflow.created_at
        assert restored.updated_at == workflow.updated_at

        # Viewport
        assert restored.viewport.x == workflow.viewport.x
        assert restored.viewport.y == workflow.viewport.y
        assert restored.viewport.zoom == workflow.viewport.zoom

        # Metadata
        assert restored.metadata.author == workflow.metadata.author
        assert restored.metadata.tags == workflow.metadata.tags
        assert restored.metadata.aws_region == workflow.metadata.aws_region
        assert restored.metadata.deployment_status == workflow.metadata.deployment_status

        # Nodes
        assert len(restored.nodes) == len(workflow.nodes)
        for orig_node, restored_node in zip(workflow.nodes, restored.nodes, strict=True):
            assert restored_node.id == orig_node.id
            assert restored_node.type == orig_node.type
            assert restored_node.position.x == orig_node.position.x
            assert restored_node.position.y == orig_node.position.y
            assert restored_node.data.name == orig_node.data.name
            assert restored_node.data.component_type == orig_node.data.component_type

        # Edges
        assert len(restored.edges) == len(workflow.edges)

    @given(workflow=valid_workflow_st())
    @settings(max_examples=100)
    def test_json_round_trip_full_model_equality(self, workflow: WorkflowDefinition):
        """Full model equality check: the round-tripped object should equal the original."""
        json_data = workflow.model_dump(mode="json")
        restored = WorkflowDefinition.model_validate(json_data)

        # Use model_dump for deep equality comparison (avoids object identity issues)
        assert restored.model_dump() == workflow.model_dump()


# ============================================================================
# Property 9: ComponentConfiguration DynamoDB Serialization Round-Trip
# ============================================================================


from app.models.components import (
    A2AConfiguration,
    AdvancedMemoryConfiguration,
    AgentEndpoint,
    APIKeyConfiguration,
    BrowserConfiguration,
    CodeInterpreterConfiguration,
    ComponentConfiguration,
    EvaluationConfiguration,
    EvaluatorConfig,
    GatewayConfiguration,
    IdentityConfiguration,
    LambdaTargetConfig,
    MemoryConfiguration,
    ObservabilityConfiguration,
    OpenAPITargetConfig,
    PolicyCondition,
    PolicyConfiguration,
    PolicyRule,
    SmithyTargetConfig,
)
from app.models.enums import (
    A2ACommunicationPattern,
    EvaluatorType,
    ExtractionStrategy,
    GatewayTargetType,
    PolicyEffect,
)
from app.services.deployment_state_store import (
    _convert_decimals_to_floats,
    _convert_floats_to_decimals,
)

# --- Hypothesis strategies for each ComponentConfiguration variant ---

# Floats that survive DynamoDB round-trip: avoid subnormals that
# _convert_floats_to_decimals clamps to Decimal("0") when abs < 1e-130.
_safe_float = st.floats(
    min_value=0.0,
    max_value=2.0,
    allow_nan=False,
    allow_infinity=False,
).filter(lambda x: x == 0.0 or abs(x) >= 1e-130)

_comp_name_st = st.text(
    min_size=1,
    max_size=50,
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-",
).filter(lambda x: len(x.strip()) > 0)

_short_text_st = st.text(
    min_size=1,
    max_size=50,
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-.",
).filter(lambda x: len(x.strip()) > 0)

_url_st = st.from_regex(r"^https://[a-z]{3,12}\.[a-z]{2,6}/[a-z0-9]{1,20}$", fullmatch=True)


@st.composite
def runtime_config_st(draw):
    """Generate a random valid RuntimeConfiguration."""
    return RuntimeConfiguration(
        component_type="runtime",
        name=draw(_comp_name_st),
        entrypoint=draw(st.just("agent.py")),
        framework=draw(st.sampled_from(list(AgentFramework))),
        model=draw(valid_model_config_st),
        system_prompt=draw(st.text(min_size=1, max_size=200).filter(lambda x: len(x.strip()) > 0)),
        deployment_type=draw(st.sampled_from(list(DeploymentType))),
        python_runtime=draw(st.sampled_from(list(PythonRuntime))),
        protocol=draw(st.sampled_from(list(AgentServerProtocol))),
        idle_timeout=draw(st.integers(min_value=60, max_value=28800)),
        max_lifetime=draw(st.integers(min_value=60, max_value=28800)),
        enable_otel=draw(st.booleans()),
    )


@st.composite
def gateway_config_st(draw):
    """Generate a random valid GatewayConfiguration with matching target_config.

    Only the supported `lambda`, `smithy`, `openapi` target types are
    exercised. `api_gateway` and `prebuilt` are rejected at the API
    boundary (see backend/src/app/models/components.py
    validate_target_config_type and tasks/lessons.md Bug 106).
    """
    variant = draw(st.sampled_from(["lambda", "smithy", "openapi"]))
    if variant == "lambda":
        target_type = GatewayTargetType.LAMBDA
        target_config = LambdaTargetConfig(type="lambda")
    elif variant == "smithy":
        target_type = GatewayTargetType.SMITHY
        target_config = SmithyTargetConfig(type="smithy", model_name="dynamodb")
    else:
        target_type = GatewayTargetType.OPENAPI
        target_config = OpenAPITargetConfig(type="openapi", spec_url=draw(_url_st))

    return GatewayConfiguration(
        component_type="gateway",
        name=draw(_comp_name_st),
        target_type=target_type,
        target_config=target_config,
        enable_semantic_search=draw(st.booleans()),
    )


@st.composite
def memory_config_st(draw):
    return MemoryConfiguration(
        component_type="memory",
        name=draw(_comp_name_st),
        enabled=draw(st.booleans()),
    )


@st.composite
def code_interpreter_config_st(draw):
    return CodeInterpreterConfiguration(
        component_type="code_interpreter",
        name=draw(_comp_name_st),
        enabled=draw(st.booleans()),
    )


@st.composite
def browser_config_st(draw):
    return BrowserConfiguration(
        component_type="browser",
        name=draw(_comp_name_st),
        enabled=draw(st.booleans()),
    )


@st.composite
def observability_config_st(draw):
    return ObservabilityConfiguration(
        component_type="observability",
        name=draw(_comp_name_st),
        enable_otel=draw(st.booleans()),
    )


@st.composite
def identity_config_st(draw):
    """Generate a valid IdentityConfiguration (api_key variant for simplicity)."""
    return IdentityConfiguration(
        component_type="identity",
        name=draw(_comp_name_st),
        credential_type="api_key",
        api_key_config=APIKeyConfiguration(
            key_name=draw(_short_text_st),
            key_value_ref=draw(_short_text_st),
            header_name=draw(_short_text_st),
        ),
    )


@st.composite
def evaluation_config_st(draw):
    num_evaluators = draw(st.integers(min_value=0, max_value=3))
    evaluators = []
    for _ in range(num_evaluators):
        etype = draw(st.sampled_from([e for e in EvaluatorType if e != EvaluatorType.CUSTOM]))
        evaluators.append(
            EvaluatorConfig(
                evaluator_type=etype,
                enabled=draw(st.booleans()),
                threshold=draw(_dynamo_safe_unit_float),
            )
        )
    return EvaluationConfiguration(
        component_type="evaluation",
        name=draw(_comp_name_st),
        enabled=draw(st.booleans()),
        evaluators=evaluators,
        mode=draw(st.sampled_from(["on_demand", "continuous"])),
        sampling_rate=draw(_dynamo_safe_unit_float),
        enable_dashboard=draw(st.booleans()),
        extraction_strategy=draw(st.sampled_from(list(ExtractionStrategy))),
    )


@st.composite
def policy_config_st(draw):
    num_rules = draw(st.integers(min_value=0, max_value=3))
    rules = []
    for i in range(num_rules):
        num_conditions = draw(st.integers(min_value=0, max_value=2))
        conditions = [
            PolicyCondition(
                attribute=draw(_short_text_st),
                operator=draw(st.sampled_from(["==", "!=", "<", ">", "<=", ">=", "in", "contains"])),
                value=draw(_short_text_st),
            )
            for _ in range(num_conditions)
        ]
        rules.append(
            PolicyRule(
                rule_id=f"rule-{i}-{draw(_short_text_st)}",
                effect=draw(st.sampled_from(list(PolicyEffect))),
                conditions=conditions,
            )
        )
    return PolicyConfiguration(
        component_type="policy",
        name=draw(_comp_name_st),
        enabled=draw(st.booleans()),
        rules=rules,
        default_effect=draw(st.sampled_from(list(PolicyEffect))),
        enable_nl_authoring=draw(st.booleans()),
        strict_validation=draw(st.booleans()),
        enable_audit_log=draw(st.booleans()),
    )


@st.composite
def advanced_memory_config_st(draw):
    return AdvancedMemoryConfiguration(
        component_type="advanced_memory",
        name=draw(_comp_name_st),
        enabled=draw(st.booleans()),
        extraction_strategies=draw(
            st.lists(
                st.sampled_from(list(ExtractionStrategy)),
                min_size=1,
                max_size=3,
            )
        ),
        short_term_enabled=draw(st.booleans()),
        short_term_max_messages=draw(st.integers(min_value=1, max_value=1000)),
        long_term_enabled=draw(st.booleans()),
        session_timeout_minutes=draw(st.integers(min_value=1, max_value=10080)),
        enable_branching=draw(st.booleans()),
    )


@st.composite
def a2a_config_st(draw):
    num_endpoints = draw(st.integers(min_value=0, max_value=3))
    endpoints = [
        AgentEndpoint(
            agent_id=draw(_short_text_st),
            endpoint_url=draw(_url_st),
            protocol=draw(st.sampled_from(["HTTP", "MCP", "A2A"])),
        )
        for _ in range(num_endpoints)
    ]
    return A2AConfiguration(
        component_type="a2a",
        name=draw(_comp_name_st),
        enabled=draw(st.booleans()),
        pattern=draw(st.sampled_from(list(A2ACommunicationPattern))),
        agent_endpoints=endpoints,
        timeout_seconds=draw(st.integers(min_value=1, max_value=300)),
        max_retries=draw(st.integers(min_value=0, max_value=10)),
        enable_parallel_execution=draw(st.booleans()),
        enable_message_routing=draw(st.booleans()),
        routing_strategy=draw(st.sampled_from(["round_robin", "capability_based", "load_balanced"])),
        share_context=draw(st.booleans()),
        context_window_size=draw(st.integers(min_value=1, max_value=100)),
    )


# Combined strategy covering all discriminated union variants
any_component_config_st = st.one_of(
    runtime_config_st(),
    gateway_config_st(),
    memory_config_st(),
    code_interpreter_config_st(),
    browser_config_st(),
    observability_config_st(),
    identity_config_st(),
    evaluation_config_st(),
    policy_config_st(),
    advanced_memory_config_st(),
    a2a_config_st(),
)


def _dynamodb_round_trip(config):
    """Simulate DynamoDB serialization round-trip.

    1. model_dump(mode="json") to get a JSON-compatible dict
    2. _convert_floats_to_decimals to simulate DynamoDB write
    3. _convert_decimals_to_floats to simulate DynamoDB read
    4. Pydantic model_validate with the discriminator to reconstruct
    """
    from pydantic import TypeAdapter

    # Step 1: Serialize to JSON-compatible dict
    json_data = config.model_dump(mode="json")

    # Step 2: Convert floats → Decimal (DynamoDB write)
    dynamo_item = _convert_floats_to_decimals(json_data)

    # Step 3: Convert Decimal → float (DynamoDB read)
    restored_data = _convert_decimals_to_floats(dynamo_item)

    # Step 4: Deserialize back using the ComponentConfiguration union type
    adapter = TypeAdapter(ComponentConfiguration)
    return adapter.validate_python(restored_data)


class TestProperty9ComponentConfigDynamoDBRoundTrip:
    """Property 9: ComponentConfiguration DynamoDB Serialization Round-Trip.

    For any valid ComponentConfiguration object (any variant of the
    discriminated union), converting to a DynamoDB-compatible dict
    (with Decimal conversion) then converting back should produce an
    equivalent ComponentConfiguration object.

    **Validates: Requirements 11.2**
    """

    @given(config=any_component_config_st)
    @settings(max_examples=100)
    def test_dynamodb_round_trip_produces_equivalent_object(self, config):
        """DynamoDB round-trip (float→Decimal→float) preserves ComponentConfiguration.

        Property 9: ComponentConfiguration DynamoDB Serialization Round-Trip
        **Validates: Requirements 11.2**
        """
        restored = _dynamodb_round_trip(config)

        # Verify the discriminator matches
        assert restored.component_type == config.component_type

        # Deep equality via model_dump
        original_dump = config.model_dump(mode="json")
        restored_dump = restored.model_dump(mode="json")
        assert restored_dump == original_dump, (
            f"Round-trip mismatch for {config.component_type}:\n"
            f"  Original: {original_dump}\n"
            f"  Restored: {restored_dump}"
        )
