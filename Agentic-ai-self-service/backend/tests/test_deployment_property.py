"""Property-based tests for deployment engine.

These tests verify:
- Multi-Region Deployment Support
- Failed Deployment Rollback
- Deployment State Management

Requirements: 11.3, 11.4, 11.7
"""

import sys

sys.path.insert(0, "src")

from datetime import datetime, timezone

import pytest
from app.models import (
    AgentFramework,
    DeploymentConfig,
    ModelConfiguration,
    RuntimeConfiguration,
    Viewport,
    WorkflowDefinition,
    WorkflowMetadata,
)
from app.models.enums import StrandsModelProvider
from app.services.deployment import (
    VALID_AWS_REGIONS,
    DeploymentPhase,
    DeploymentState,
    WorkflowExecutor,
    generate_agent_code,
    generate_requirements,
)
from hypothesis import given, settings
from hypothesis import strategies as st

# ============================================================================
# Hypothesis Strategies
# ============================================================================

valid_workflow_id_st = st.text(
    min_size=1,
    max_size=50,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
).filter(lambda x: len(x.strip()) > 0)

valid_aws_region_st = st.sampled_from(VALID_AWS_REGIONS)


# ============================================================================
# Multi-Region Deployment Tests
# ============================================================================


class TestMultiRegionDeployment:
    """Tests for multi-region deployment support.

    Validates: Requirements 11.4
    """

    @given(region=valid_aws_region_st)
    @settings(max_examples=20)
    def test_executor_accepts_valid_regions(self, region: str):
        """WorkflowExecutor should accept all valid AWS regions."""
        executor = WorkflowExecutor(region=region)
        assert executor.region == region

    def test_executor_rejects_invalid_region(self):
        """WorkflowExecutor should reject invalid regions."""
        with pytest.raises(ValueError, match="Invalid AWS region"):
            WorkflowExecutor(region="invalid-region")

    @given(region=valid_aws_region_st)
    @settings(max_examples=10)
    def test_set_region_updates_executor(self, region: str):
        """set_region should update the executor's region."""
        executor = WorkflowExecutor(region="us-east-1")
        executor.set_region(region)
        assert executor.region == region

    def test_set_region_rejects_invalid(self):
        """set_region should reject invalid regions."""
        executor = WorkflowExecutor(region="us-east-1")
        with pytest.raises(ValueError, match="Invalid AWS region"):
            executor.set_region("not-a-region")


# ============================================================================
# Deployment State Tests
# ============================================================================


class TestDeploymentState:
    """Tests for deployment state management."""

    def test_initial_state(self):
        """New deployment state should be in INITIALIZING phase."""
        state = DeploymentState(
            deployment_id="test-123",
            workflow_id="workflow-456",
        )
        assert state.phase == DeploymentPhase.INITIALIZING
        assert state.error_message is None
        assert state.endpoint_url is None

    def test_state_tracks_timestamps(self):
        """Deployment state should track started_at timestamp."""
        state = DeploymentState(
            deployment_id="test-123",
            workflow_id="workflow-456",
        )
        assert state.started_at is not None
        assert state.completed_at is None


# ============================================================================
# Code Generation Tests
# ============================================================================


class TestCodeGeneration:
    """Tests for agent code generation."""

    def test_generate_strands_agent_code(self):
        """Should generate valid Strands agent code with BedrockAgentCoreApp SDK."""
        config = RuntimeConfiguration(
            name="test-agent",
            framework=AgentFramework.STRANDS_AGENTS,
            model=ModelConfiguration(
                provider=StrandsModelProvider.BEDROCK,
                model_id="claude-3",
            ),
            system_prompt="You are helpful.",
        )
        code = generate_agent_code(config)
        assert "BedrockAgentCoreApp" in code
        assert "@app.entrypoint" in code
        assert "app.run()" in code
        assert "http.server" not in code
        assert "serve_forever()" not in code
        assert "strands" in code.lower() or "Agent" in code

    def test_generate_strands_agent_code_with_custom_model(self):
        """Should generate valid Strands agent code with a different model_id."""
        config = RuntimeConfiguration(
            name="test-agent",
            framework=AgentFramework.STRANDS_AGENTS,
            model=ModelConfiguration(
                provider=StrandsModelProvider.BEDROCK,
                model_id="us.amazon.nova-2-lite-v1:0",
            ),
            system_prompt="You are helpful.",
        )
        code = generate_agent_code(config)
        assert "BedrockAgentCoreApp" in code
        assert "@app.entrypoint" in code
        assert "app.run()" in code
        assert "http.server" not in code
        assert "serve_forever()" not in code
        assert "strands" in code.lower() or "Agent" in code

    def test_generate_strands_agent_code_custom_prompt(self):
        """Should generate valid Strands agent code with a custom system prompt."""
        config = RuntimeConfiguration(
            name="test-agent",
            framework=AgentFramework.STRANDS_AGENTS,
            model=ModelConfiguration(
                provider=StrandsModelProvider.BEDROCK,
                model_id="claude-3",
            ),
            system_prompt="You are a custom assistant.",
        )
        code = generate_agent_code(config)
        assert "BedrockAgentCoreApp" in code
        assert "@app.entrypoint" in code
        assert "app.run()" in code
        assert "http.server" not in code
        assert "serve_forever()" not in code


# ============================================================================
# Requirements Generation Tests
# ============================================================================


class TestRequirementsGeneration:
    """Tests for requirements.txt generation.

    generate_requirements() returns empty string — deps are pre-bundled.
    """

    def test_strands_requirements_empty(self):
        """Strands framework: requirements must be empty (deps pre-bundled)."""
        config = RuntimeConfiguration(
            name="test",
            framework=AgentFramework.STRANDS_AGENTS,
            model=ModelConfiguration(
                provider=StrandsModelProvider.BEDROCK,
                model_id="claude-3",
            ),
            system_prompt="test",
        )
        reqs = generate_requirements(config)
        assert reqs == ""

    def test_strands_requirements_empty_alt_model(self):
        """Strands framework with alt model: requirements must be empty (deps pre-bundled)."""
        config = RuntimeConfiguration(
            name="test",
            framework=AgentFramework.STRANDS_AGENTS,
            model=ModelConfiguration(
                provider=StrandsModelProvider.BEDROCK,
                model_id="us.amazon.nova-2-lite-v1:0",
            ),
            system_prompt="test",
        )
        reqs = generate_requirements(config)
        assert reqs == ""


# ============================================================================
# Workflow Executor Tests
# ============================================================================


class TestWorkflowExecutor:
    """Tests for WorkflowExecutor."""

    def test_get_deployment_status_not_found(self):
        """Should return None for unknown deployment ID."""
        executor = WorkflowExecutor(region="us-west-2")
        status = executor.get_deployment_status("unknown-id")
        assert status is None

    @pytest.mark.asyncio
    async def test_deploy_requires_runtime_component(self):
        """Deploy should fail if workflow has no Runtime component."""
        executor = WorkflowExecutor(region="us-west-2")

        workflow = WorkflowDefinition(
            id="test-workflow",
            name="Test",
            version="1.0.0",
            nodes=[],  # No runtime node
            edges=[],
            viewport=Viewport(x=0, y=0, zoom=1.0),
            metadata=WorkflowMetadata(
                author="test",
                aws_region="us-west-2",
            ),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        config = DeploymentConfig(
            aws_region="us-west-2",
        )

        result = await executor.deploy(workflow, config)
        assert result.status == "failed"
        assert "Runtime" in result.error_message

    @pytest.mark.asyncio
    async def test_rollback_unknown_deployment(self):
        """Rollback should fail for unknown deployment."""
        executor = WorkflowExecutor(region="us-west-2")
        result = await executor.rollback("unknown-id")
        assert result.success is False
        assert len(result.errors) > 0
