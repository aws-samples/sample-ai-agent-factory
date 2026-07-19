"""Property-based tests for Pydantic model validation.

These tests verify that:
- Invalid configurations are rejected
- Valid configurations pass validation
- Requirements: 13.2
"""

import sys

sys.path.insert(0, "src")

from datetime import datetime, timezone

import pytest
from app.models import (
    # Enums
    AgentCoreComponentType,
    AgentFramework,
    APIKeyConfiguration,
    ComponentNode,
    ConnectionEdge,
    ConnectionType,
    IdentityConfiguration,
    LambdaTargetConfig,
    MemoryConfiguration,
    ModelConfiguration,
    ModelProvider,
    OAuth2Configuration,
    OAuth2Provider,
    OpenAPITargetConfig,
    Position,
    RuntimeConfiguration,
    SmithyTargetConfig,
    Viewport,
    WorkflowDefinition,
    WorkflowMetadata,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

# ============================================================================
# Hypothesis Strategies for Valid Data Generation
# ============================================================================

valid_name_st = st.text(
    min_size=1,
    max_size=100,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters=" -_"),
).filter(lambda x: len(x.strip()) > 0)

valid_id_st = st.text(
    min_size=1,
    max_size=50,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
).filter(lambda x: len(x.strip()) > 0)

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

valid_lambda_arn_st = st.builds(
    lambda region, account, name: f"arn:aws:lambda:{region}:{account}:function:{name}",
    region=valid_aws_region_st,
    account=st.from_regex(r"^\d{12}$", fullmatch=True),
    name=st.text(min_size=1, max_size=50, alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_").filter(
        lambda x: len(x.strip()) > 0
    ),
)


# Model Configuration strategy
model_config_st = st.builds(
    ModelConfiguration,
    provider=st.sampled_from(list(ModelProvider)),
    model_id=st.text(min_size=1, max_size=200).filter(lambda x: len(x.strip()) > 0),
    temperature=st.floats(min_value=0.0, max_value=2.0),
    top_p=st.floats(min_value=0.0, max_value=1.0),
)


# ============================================================================
# Model Configuration Tests
# ============================================================================


class TestModelConfiguration:
    """Tests for ModelConfiguration validation."""

    @given(model_config_st)
    @settings(max_examples=50)
    def test_valid_model_config(self, config: ModelConfiguration):
        """Valid model configurations should pass validation."""
        assert config.provider in ModelProvider
        assert 0.0 <= config.temperature <= 2.0
        assert 0.0 <= config.top_p <= 1.0

    def test_invalid_temperature_too_high(self):
        """Temperature above 2.0 should be rejected."""
        with pytest.raises(ValidationError):
            ModelConfiguration(
                provider=ModelProvider.ANTHROPIC,
                model_id="claude-3",
                temperature=2.5,
                top_p=0.9,
            )

    def test_invalid_temperature_negative(self):
        """Negative temperature should be rejected."""
        with pytest.raises(ValidationError):
            ModelConfiguration(
                provider=ModelProvider.ANTHROPIC,
                model_id="claude-3",
                temperature=-0.1,
                top_p=0.9,
            )

    def test_empty_model_id_rejected(self):
        """Empty model_id should be rejected."""
        with pytest.raises(ValidationError):
            ModelConfiguration(
                provider=ModelProvider.ANTHROPIC,
                model_id="",
                temperature=0.7,
                top_p=0.9,
            )


# ============================================================================
# Gateway Target Config Tests
# ============================================================================


class TestGatewayTargetConfigs:
    """Tests for Gateway target configurations."""

    def test_openapi_requires_spec(self):
        """OpenAPI config requires either spec_url or spec_content."""
        with pytest.raises(ValidationError):
            OpenAPITargetConfig()

    def test_openapi_with_url(self):
        """OpenAPI config with spec_url should be valid."""
        config = OpenAPITargetConfig(spec_url="https://api.example.com/openapi.json")
        assert config.spec_url == "https://api.example.com/openapi.json"

    def test_openapi_with_content(self):
        """OpenAPI config with spec_content should be valid."""
        config = OpenAPITargetConfig(spec_content='{"openapi": "3.0.0"}')
        assert config.spec_content == '{"openapi": "3.0.0"}'

    @given(valid_lambda_arn_st)
    @settings(max_examples=20)
    def test_valid_lambda_arn(self, arn: str):
        """Valid Lambda ARNs should be accepted."""
        config = LambdaTargetConfig(function_arn=arn)
        assert config.function_arn == arn

    def test_invalid_lambda_arn_rejected(self):
        """Invalid Lambda ARN format should be rejected."""
        with pytest.raises(ValidationError):
            LambdaTargetConfig(function_arn="invalid-arn")

    def test_smithy_default_model(self):
        """Smithy config should have default model name."""
        config = SmithyTargetConfig()
        assert config.model_name == "dynamodb"


# ============================================================================
# Identity Configuration Tests
# ============================================================================


class TestIdentityConfiguration:
    """Tests for IdentityConfiguration validation."""

    def test_oauth2_requires_config(self):
        """OAuth2 credential type requires oauth2_config."""
        with pytest.raises(ValidationError):
            IdentityConfiguration(
                name="test-identity",
                credential_type="oauth2",
            )

    def test_api_key_requires_config(self):
        """API key credential type requires api_key_config."""
        with pytest.raises(ValidationError):
            IdentityConfiguration(
                name="test-identity",
                credential_type="api_key",
            )

    def test_valid_oauth2_identity(self):
        """Valid OAuth2 identity configuration should pass."""
        config = IdentityConfiguration(
            name="test-oauth2",
            credential_type="oauth2",
            oauth2_config=OAuth2Configuration(
                provider=OAuth2Provider.GOOGLE,
                client_id="client-123",
                client_secret_ref="secret-ref",
                scopes=["read", "write"],
            ),
        )
        assert config.credential_type == "oauth2"
        assert config.oauth2_config is not None

    def test_valid_api_key_identity(self):
        """Valid API key identity configuration should pass."""
        config = IdentityConfiguration(
            name="test-api-key",
            credential_type="api_key",
            api_key_config=APIKeyConfiguration(
                key_name="my-api-key",
                key_value_ref="secret-ref",
                header_name="X-API-Key",
            ),
        )
        assert config.credential_type == "api_key"
        assert config.api_key_config is not None


# ============================================================================
# Workflow Definition Tests
# ============================================================================


class TestWorkflowDefinition:
    """Tests for WorkflowDefinition validation."""

    def test_valid_empty_workflow(self):
        """Empty workflow with required fields should be valid."""
        workflow = WorkflowDefinition(
            id="test-workflow",
            name="Test Workflow",
            version="1.0.0",
            viewport=Viewport(x=0, y=0, zoom=1.0),
            metadata=WorkflowMetadata(
                author="test-author",
                aws_region="us-west-2",
            ),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        assert workflow.id == "test-workflow"
        assert len(workflow.nodes) == 0
        assert len(workflow.edges) == 0

    def test_invalid_version_format(self):
        """Invalid version format should be rejected."""
        with pytest.raises(ValidationError):
            WorkflowDefinition(
                id="test",
                name="Test",
                version="invalid",
                metadata=WorkflowMetadata(
                    author="test",
                    aws_region="us-west-2",
                ),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )

    def test_invalid_aws_region_format(self):
        """Invalid AWS region format should be rejected."""
        with pytest.raises(ValidationError):
            WorkflowMetadata(
                author="test",
                aws_region="invalid-region",
            )

    @given(valid_version_st)
    @settings(max_examples=20)
    def test_valid_version_formats(self, version: str):
        """Valid semantic versions should be accepted."""
        workflow = WorkflowDefinition(
            id="test",
            name="Test",
            version=version,
            viewport=Viewport(x=0, y=0, zoom=1.0),
            metadata=WorkflowMetadata(
                author="test",
                aws_region="us-west-2",
            ),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        assert workflow.version == version


# ============================================================================
# Connection Edge Tests
# ============================================================================


class TestConnectionEdge:
    """Tests for ConnectionEdge validation."""

    def test_self_connection_rejected(self):
        """Edge connecting node to itself should be rejected."""
        with pytest.raises(ValidationError):
            ConnectionEdge(
                id="edge-1",
                source="node-1",
                target="node-1",
                source_handle="output",
                target_handle="input",
                type=ConnectionType.DATA,
            )

    def test_valid_edge(self):
        """Valid edge between different nodes should pass."""
        edge = ConnectionEdge(
            id="edge-1",
            source="node-1",
            target="node-2",
            source_handle="output",
            target_handle="input",
            type=ConnectionType.DATA,
        )
        assert edge.source != edge.target


# ============================================================================
# Component Node Tests
# ============================================================================


class TestComponentNode:
    """Tests for ComponentNode validation."""

    def test_runtime_node_requires_runtime_config(self):
        """Runtime node must have RuntimeConfiguration data."""
        with pytest.raises(ValidationError):
            ComponentNode(
                id="node-1",
                type=AgentCoreComponentType.RUNTIME,
                position=Position(x=0, y=0),
                data=MemoryConfiguration(name="wrong-type"),
            )

    def test_valid_runtime_node(self):
        """Valid runtime node should pass validation."""
        node = ComponentNode(
            id="node-1",
            type=AgentCoreComponentType.RUNTIME,
            position=Position(x=100, y=200),
            data=RuntimeConfiguration(
                name="test-runtime",
                framework=AgentFramework.STRANDS_AGENTS,
                model=ModelConfiguration(
                    provider=ModelProvider.ANTHROPIC,
                    model_id="claude-3",
                ),
                system_prompt="You are a helpful assistant.",
            ),
        )
        assert node.type == AgentCoreComponentType.RUNTIME
        assert isinstance(node.data, RuntimeConfiguration)

    def test_valid_memory_node(self):
        """Valid memory node should pass validation."""
        node = ComponentNode(
            id="node-2",
            type=AgentCoreComponentType.MEMORY,
            position=Position(x=300, y=200),
            data=MemoryConfiguration(name="test-memory"),
        )
        assert node.type == AgentCoreComponentType.MEMORY
        assert isinstance(node.data, MemoryConfiguration)
