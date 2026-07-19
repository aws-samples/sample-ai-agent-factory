"""Property-based tests for Validation Engine Consistency (Property 4).

Feature: serverless-migration

Property 4: Validation Engine Consistency
For any WorkflowDefinition, running the ValidationEngine should return a
ValidationResult where is_valid is True if and only if there are zero errors
in the errors list.

**Validates: Requirements 2.6**
"""

import sys

sys.path.insert(0, "src")

from datetime import datetime, timezone

from app.models.components import (
    A2AConfiguration,
    BrowserConfiguration,
    CodeInterpreterConfiguration,
    EvaluationConfiguration,
    GatewayConfiguration,
    GuardrailsConfiguration,
    IdentityConfiguration,
    LambdaTargetConfig,
    MemoryConfiguration,
    ModelConfiguration,
    ObservabilityConfiguration,
    PolicyConfiguration,
    RuntimeConfiguration,
    ToolConfiguration,
)
from app.models.enums import (
    A2ACommunicationPattern,
    AgentCoreComponentType,
    AgentFramework,
    AgentServerProtocol,
    ConnectionType,
    DeploymentStatus,
    DeploymentType,
    GatewayTargetType,
    ModelProvider,
    PythonRuntime,
)
from app.models.workflow import (
    ComponentNode,
    ConnectionEdge,
    Position,
    Viewport,
    WorkflowDefinition,
    WorkflowMetadata,
)
from app.services.validation import ValidationEngine
from hypothesis import given, settings
from hypothesis import strategies as st

# ============================================================================
# Hypothesis Strategies
# ============================================================================

valid_id_st = st.text(
    min_size=1,
    max_size=30,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
).filter(lambda x: len(x.strip()) > 0)

valid_name_st = st.text(
    min_size=1,
    max_size=50,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters=" -_"),
).filter(lambda x: len(x.strip()) > 0)

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

valid_aws_region_st = st.sampled_from(
    [
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
        "eu-west-1",
        "eu-central-1",
        "ap-southeast-1",
        "ap-northeast-1",
    ]
)

valid_version_st = st.from_regex(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$", fullmatch=True)


@st.composite
def valid_metadata_st(draw):
    return WorkflowMetadata(
        author=draw(
            st.text(min_size=1, max_size=30, alphabet="abcdefghijklmnopqrstuvwxyz0123456789").filter(
                lambda x: len(x.strip()) > 0
            )
        ),
        tags=draw(
            st.lists(
                st.text(min_size=1, max_size=15, alphabet="abcdefghijklmnopqrstuvwxyz"),
                max_size=3,
            )
        ),
        aws_region=draw(valid_aws_region_st),
        deployment_status=DeploymentStatus.NOT_DEPLOYED,
    )


# --- Component configuration factories ---


def _make_runtime_config(name: str) -> RuntimeConfiguration:
    """Valid RuntimeConfiguration."""
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


def _make_runtime_config_no_model(name: str) -> RuntimeConfiguration:
    """RuntimeConfiguration with None model — triggers ValidationEngine error on model.model_id."""
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
        system_prompt="",  # Empty system_prompt triggers required field validation error
        deployment_type=DeploymentType.DIRECT_CODE_DEPLOY,
        python_runtime=PythonRuntime.PYTHON_3_12,
        protocol=AgentServerProtocol.HTTP,
    )


def _make_memory_config(name: str) -> MemoryConfiguration:
    return MemoryConfiguration(component_type="memory", name=name, enabled=True)


def _make_browser_config(name: str) -> BrowserConfiguration:
    return BrowserConfiguration(component_type="browser", name=name, enabled=True)


def _make_code_interpreter_config(name: str) -> CodeInterpreterConfiguration:
    return CodeInterpreterConfiguration(component_type="code_interpreter", name=name, enabled=True)


def _make_observability_config(name: str) -> ObservabilityConfiguration:
    return ObservabilityConfiguration(component_type="observability", name=name, enabled=True)


def _make_gateway_config(name: str) -> GatewayConfiguration:
    return GatewayConfiguration(
        component_type="gateway",
        name=name,
        target_type=GatewayTargetType.LAMBDA,
        target_config=LambdaTargetConfig(type="lambda"),
    )


def _make_identity_config(name: str) -> IdentityConfiguration:
    from app.models.components import APIKeyConfiguration

    return IdentityConfiguration(
        component_type="identity",
        name=name,
        credential_type="api_key",
        api_key_config=APIKeyConfiguration(
            key_name="X-API-Key",
            key_value_ref="arn:aws:secretsmanager:us-east-1:123456789:secret:key",
            header_name="X-API-Key",
        ),
    )


def _make_evaluation_config(name: str) -> EvaluationConfiguration:
    return EvaluationConfiguration(
        component_type="evaluation",
        name=name,
        evaluators=[],
    )


def _make_policy_config(name: str) -> PolicyConfiguration:
    return PolicyConfiguration(
        component_type="policy",
        name=name,
        rules=[],
    )


def _make_a2a_config(name: str) -> A2AConfiguration:
    return A2AConfiguration(
        component_type="a2a",
        name=name,
        communication_pattern=A2ACommunicationPattern.PEER_TO_PEER,
        agent_endpoints=[],
    )


def _make_guardrails_config(name: str) -> GuardrailsConfiguration:
    return GuardrailsConfiguration(component_type="guardrails", name=name, enabled=True)


def _make_tool_config(name: str) -> ToolConfiguration:
    return ToolConfiguration(component_type="tool", name=name, tool_id=name, enabled=True)


COMPONENT_CONFIG_FACTORIES = {
    AgentCoreComponentType.RUNTIME: _make_runtime_config,
    AgentCoreComponentType.MEMORY: _make_memory_config,
    AgentCoreComponentType.BROWSER: _make_browser_config,
    AgentCoreComponentType.CODE_INTERPRETER: _make_code_interpreter_config,
    AgentCoreComponentType.OBSERVABILITY: _make_observability_config,
    AgentCoreComponentType.GATEWAY: _make_gateway_config,
    AgentCoreComponentType.IDENTITY: _make_identity_config,
    AgentCoreComponentType.EVALUATION: _make_evaluation_config,
    AgentCoreComponentType.POLICY: _make_policy_config,
    AgentCoreComponentType.A2A: _make_a2a_config,
    AgentCoreComponentType.GUARDRAILS: _make_guardrails_config,
    AgentCoreComponentType.TOOL: _make_tool_config,
}


# ============================================================================
# Workflow Strategies
# ============================================================================


@st.composite
def workflow_with_varied_nodes_st(draw):
    """WorkflowDefinition with 0-5 nodes of random types, no edges.

    Tests component-level validation across diverse component types.
    """
    num_nodes = draw(st.integers(min_value=0, max_value=5))
    nodes = []
    for i in range(num_nodes):
        ctype = draw(st.sampled_from(list(AgentCoreComponentType)))
        factory = COMPONENT_CONFIG_FACTORIES[ctype]
        node = ComponentNode(
            id=f"node-{i}",
            type=ctype,
            position=draw(valid_position_st),
            data=factory(f"{ctype.value}-{i}"),
        )
        nodes.append(node)

    now = datetime.now(timezone.utc)
    return WorkflowDefinition(
        id=draw(valid_id_st),
        name=draw(valid_name_st),
        description=draw(st.text(max_size=100)),
        version=draw(valid_version_st),
        nodes=nodes,
        edges=[],
        viewport=draw(valid_viewport_st),
        metadata=draw(valid_metadata_st()),
        created_at=now,
        updated_at=now,
    )


@st.composite
def workflow_with_edges_st(draw):
    """WorkflowDefinition with a runtime hub and tool nodes connected by edges.

    Edges connect existing nodes (Pydantic-valid), but some connections may be
    incompatible at the ValidationEngine level (e.g., memory→browser).
    """
    runtime_node = ComponentNode(
        id="runtime-0",
        type=AgentCoreComponentType.RUNTIME,
        position=draw(valid_position_st),
        data=_make_runtime_config("main-runtime"),
    )
    nodes = [runtime_node]

    tool_types = [
        AgentCoreComponentType.MEMORY,
        AgentCoreComponentType.BROWSER,
        AgentCoreComponentType.CODE_INTERPRETER,
        AgentCoreComponentType.OBSERVABILITY,
        AgentCoreComponentType.GATEWAY,
        AgentCoreComponentType.IDENTITY,
        AgentCoreComponentType.EVALUATION,
        AgentCoreComponentType.POLICY,
        AgentCoreComponentType.A2A,
        AgentCoreComponentType.GUARDRAILS,
        AgentCoreComponentType.TOOL,
    ]
    num_extra = draw(st.integers(min_value=1, max_value=4))
    for i in range(num_extra):
        ctype = draw(st.sampled_from(tool_types))
        factory = COMPONENT_CONFIG_FACTORIES[ctype]
        node = ComponentNode(
            id=f"tool-{i}",
            type=ctype,
            position=draw(valid_position_st),
            data=factory(f"{ctype.value}-{i}"),
        )
        nodes.append(node)

    # Generate edges between existing nodes (some may be incompatible)
    edges = []
    edge_ids_used = set()
    num_edges = draw(st.integers(min_value=1, max_value=len(nodes)))
    for e_idx in range(num_edges):
        src_idx = draw(st.integers(min_value=0, max_value=len(nodes) - 1))
        tgt_idx = draw(st.integers(min_value=0, max_value=len(nodes) - 1))
        if src_idx == tgt_idx:
            continue
        eid = f"edge-{e_idx}"
        if eid in edge_ids_used:
            continue
        edge_ids_used.add(eid)
        edges.append(
            ConnectionEdge(
                id=eid,
                source=nodes[src_idx].id,
                target=nodes[tgt_idx].id,
                source_handle=f"{nodes[src_idx].id}-out",
                target_handle=f"{nodes[tgt_idx].id}-in",
                type=ConnectionType.DATA,
            )
        )

    now = datetime.now(timezone.utc)
    return WorkflowDefinition(
        id=draw(valid_id_st),
        name=draw(valid_name_st),
        description=draw(st.text(max_size=100)),
        version=draw(valid_version_st),
        nodes=nodes,
        edges=edges,
        viewport=draw(valid_viewport_st),
        metadata=draw(valid_metadata_st()),
        created_at=now,
        updated_at=now,
    )


@st.composite
def workflow_with_empty_required_field_st(draw):
    """WorkflowDefinition with a runtime that has an empty system_prompt.

    The ValidationEngine should flag this as an error since system_prompt
    is a required field for runtime components.
    """
    node = ComponentNode(
        id="bad-runtime",
        type=AgentCoreComponentType.RUNTIME,
        position=draw(valid_position_st),
        data=_make_runtime_config_no_model("bad-runtime"),
    )

    now = datetime.now(timezone.utc)
    return WorkflowDefinition(
        id=draw(valid_id_st),
        name=draw(valid_name_st),
        description="",
        version="1.0.0",
        nodes=[node],
        edges=[],
        viewport=Viewport(x=0, y=0, zoom=1.0),
        metadata=draw(valid_metadata_st()),
        created_at=now,
        updated_at=now,
    )


# Combined strategy for maximum input diversity
any_workflow_st = st.one_of(
    workflow_with_varied_nodes_st(),
    workflow_with_edges_st(),
    workflow_with_empty_required_field_st(),
)


# ============================================================================
# Property 4: Validation Engine Consistency
# ============================================================================


class TestProperty4ValidationEngineConsistency:
    """Property 4: Validation Engine Consistency.

    For any WorkflowDefinition, running the ValidationEngine should return a
    ValidationResult where is_valid is True if and only if there are zero
    errors in the errors list.

    **Validates: Requirements 2.6**
    """

    @given(workflow=any_workflow_st)
    @settings(max_examples=100)
    def test_is_valid_equals_no_errors(self, workflow: WorkflowDefinition):
        """is_valid must be True iff errors list is empty.

        Property 4: Validation Engine Consistency
        **Validates: Requirements 2.6**
        """
        engine = ValidationEngine()
        result = engine.validate_workflow(workflow)

        # The core property: is_valid == (len(errors) == 0)
        assert result.is_valid == (len(result.errors) == 0), (
            f"Consistency violation: is_valid={result.is_valid} but "
            f"len(errors)={len(result.errors)}. "
            f"Errors: {[e.message for e in result.errors]}"
        )
