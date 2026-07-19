"""Bug condition exploration tests for the 7 defects in the legacy deployment path.

These tests encode the EXPECTED (fixed) behavior. They are designed to FAIL on
unfixed code, proving the bugs exist. Once the fixes are applied, these same
tests will PASS, confirming the defects are resolved.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7**

Groups:
  1. Requirements & Bundling (Defects 1, 2, 5)
  2. Template Metadata & Code Generation (Defects 3, 4)
  3. CLI → boto3 (Defects 6, 7)
"""

import inspect

import pytest
from app.models.components import ModelConfiguration, RuntimeConfiguration
from app.models.enums import AgentFramework, AgentServerProtocol, ModelProvider
from app.services.deployment import (
    WorkflowExecutor,
    generate_requirements,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_runtime_config(framework: AgentFramework) -> RuntimeConfiguration:
    """Build a minimal RuntimeConfiguration for the given framework."""
    return RuntimeConfiguration(
        name="test_agent",
        framework=framework,
        model=ModelConfiguration(
            provider=ModelProvider.ANTHROPIC,
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            temperature=0.7,
            top_p=0.9,
        ),
        system_prompt="You are a test assistant.",
        protocol=AgentServerProtocol.HTTP,
    )


ALL_FRAMEWORKS = list(AgentFramework)


# ============================================================================
# Group 1 — Requirements & Bundling (Defects 1, 2, 5)
# ============================================================================


class TestGenerateRequirementsReturnsEmpty:
    """generate_requirements() should return empty string — deps are pre-bundled
    into code.zip via S3 dependency bundles.

    **Validates: Requirements 1.1**
    """

    @pytest.mark.parametrize("framework", ALL_FRAMEWORKS, ids=lambda fw: fw.value)
    def test_generate_requirements_returns_empty(self, framework: AgentFramework):
        """For each framework, generate_requirements() must return empty string."""
        config = _make_runtime_config(framework)
        result = generate_requirements(config)
        assert result == "", (
            f"generate_requirements({framework.value}) should return empty string "
            f"(deps are pre-bundled), got: {result!r}"
        )


class TestDeployBundlesDeps:
    """Defect 2: WorkflowExecutor.deploy() should download the pre-built
    dependency bundle from S3 and merge it into code.zip.

    The AgentCore Runtime does NOT install from requirements.txt — all
    dependencies must be pre-bundled in the zip.

    **Validates: Requirements 1.2**
    """

    def test_deploy_downloads_deps_bundle(self):
        """deploy() source must download deps bundle from S3."""
        source = inspect.getsource(WorkflowExecutor.deploy)
        assert "deps_bundle" in source, (
            "WorkflowExecutor.deploy() does not download deps_bundle. "
            "The AgentCore Runtime requires pre-bundled deps in code.zip."
        )


class TestCodegenStepBundlesDeps:
    """Defect 5: codegen_step must download S3 dep bundle and merge into code.zip.

    The AgentCore Runtime does NOT install from requirements.txt — all
    dependencies must be pre-bundled in the zip.

    **Validates: Requirements 1.5**
    """

    def test_codegen_step_downloads_bundle(self):
        """codegen_step must download dependency bundles from S3."""
        from app.step_handlers import codegen_step

        source = inspect.getsource(codegen_step)
        assert "_download_bundle" in source, (
            "codegen_step does not download dependency bundles. "
            "The AgentCore Runtime requires pre-bundled deps in code.zip."
        )

    def test_codegen_step_uploads_with_deps(self):
        """codegen_step source must pass deps_bundle to upload_code_to_s3."""
        from app.step_handlers import codegen_step

        source = inspect.getsource(codegen_step)
        assert "deps_bundle" in source, (
            "codegen_step does not pass deps_bundle to upload_code_to_s3(). "
            "Dependencies must be merged into the code.zip."
        )


# ============================================================================
# Group 2 — Template Metadata & Code Generation (Defects 3, 4)
# ============================================================================


class TestTemplate1Metadata:
    """Defect 3: Template 1 in templates.ts should have id='web-search-agent'
    and framework='custom', not 'langchain-web-search' / 'langgraph'.

    We read the TypeScript file and parse the relevant fields.

    **Validates: Requirements 1.3**
    """

    @pytest.fixture()
    def template1_source(self):
        """Read templates.ts and return its content."""
        import pathlib

        ts_path = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src" / "data" / "templates.ts"
        assert ts_path.exists(), f"templates.ts not found at {ts_path}"
        return ts_path.read_text()

    def test_template1_id_is_web_search_agent(self, template1_source: str):
        """Template 1 id must be 'web-search-agent'."""
        # The first template's id field
        assert "id: 'web-search-agent'" in template1_source, (
            "Template 1 id is not 'web-search-agent'. "
            "Found 'langchain-web-search' which is stale after the boto3 migration."
        )

    def test_template1_framework_is_strands(self, template1_source: str):
        """Template 1 framework must be 'strands_agents' (Strands-only migration)."""
        lines = template1_source.split("\n")
        in_template1 = False
        for line in lines:
            if "'langchain-web-search'" in line or "'web-search-agent'" in line:
                in_template1 = True
            if in_template1 and "framework:" in line:
                assert "'strands_agents'" in line, (
                    f"Template 1 framework is not 'strands_agents'. Line: {line.strip()}. "
                    "All templates should use 'strands_agents' after Strands-only migration."
                )
                return
        pytest.fail("Could not find framework field in Template 1")


class TestCodeGeneratorTemplateRouting:
    """Defect 4: code_generator.py should route template_id='web-search-agent'
    to _generate_langchain_web_search(), not 'langchain-web-search'.

    **Validates: Requirements 1.4**
    """

    def test_code_generator_routes_web_search_agent(self):
        """generate_agent_code() must check for 'web-search-agent' template_id."""
        from app.services import code_generator

        source = inspect.getsource(code_generator.generate_agent_code)
        assert '"web-search-agent"' in source, (
            "code_generator.generate_agent_code() does not check for "
            "'web-search-agent' template_id. Currently checks 'langchain-web-search' "
            "which is the stale ID."
        )


class TestBundleKeyForWebSearch:
    """codegen_step.py _needs_strands_bundle() correctly detects whether
    generated code needs the strands-mcp.zip or lighter base.zip bundle.

    **Validates: Requirements 1.4**
    """

    def test_boto3_only_code_uses_base_bundle(self):
        """Code without strands imports should use base.zip."""
        from app.step_handlers.codegen_step import _needs_strands_bundle

        code = "import boto3\nfrom bedrock_agentcore.runtime import BedrockAgentCoreApp"
        assert _needs_strands_bundle(code) is False

    def test_strands_code_uses_strands_bundle(self):
        """Code with strands imports should use strands-mcp.zip."""
        from app.step_handlers.codegen_step import _needs_strands_bundle

        code = "from strands import Agent\nfrom bedrock_agentcore.runtime import BedrockAgentCoreApp"
        assert _needs_strands_bundle(code) is True


# ============================================================================
# Group 3 — CLI → boto3 (Defects 6, 7)
# ============================================================================


class TestConfigureDoesNotUseSubprocess:
    """Defect 6: _run_agentcore_configure() should not exist or should not
    use subprocess to call the agentcore CLI.

    **Validates: Requirements 1.6**
    """

    def test_configure_no_subprocess(self):
        """_run_agentcore_configure must not exist or must not use subprocess."""
        has_method = hasattr(WorkflowExecutor, "_run_agentcore_configure")
        if not has_method:
            # Method deleted — that's the fix
            return

        source = inspect.getsource(WorkflowExecutor._run_agentcore_configure)
        uses_subprocess = (
            "create_subprocess_exec" in source
            or "subprocess" in source
            or '"agentcore"' in source
            or "'agentcore'" in source
        )
        assert not uses_subprocess, (
            "_run_agentcore_configure() uses subprocess to call 'agentcore' CLI. "
            "Should use boto3 create_agent_runtime() instead."
        )


class TestLaunchDoesNotUseSubprocess:
    """Defect 6 (continued): _run_agentcore_launch() should not exist or
    should not use subprocess to call the agentcore CLI.

    **Validates: Requirements 1.6**
    """

    def test_launch_no_subprocess(self):
        """_run_agentcore_launch must not exist or must not use subprocess."""
        has_method = hasattr(WorkflowExecutor, "_run_agentcore_launch")
        if not has_method:
            return

        source = inspect.getsource(WorkflowExecutor._run_agentcore_launch)
        uses_subprocess = (
            "create_subprocess_exec" in source
            or "subprocess" in source
            or '"agentcore"' in source
            or "'agentcore'" in source
        )
        assert not uses_subprocess, (
            "_run_agentcore_launch() uses subprocess to call 'agentcore' CLI. "
            "Should use boto3 wait_for_runtime_ready() instead."
        )


class TestRollbackDoesNotUseSubprocess:
    """Defect 7: rollback() should not use subprocess with 'agentcore'.

    **Validates: Requirements 1.7**
    """

    def test_rollback_no_agentcore_subprocess(self):
        """rollback() must not use subprocess to call 'agentcore destroy'."""
        source = inspect.getsource(WorkflowExecutor.rollback)
        # Check for the subprocess pattern with agentcore
        uses_agentcore_cli = ('"agentcore"' in source or "'agentcore'" in source) and (
            "create_subprocess_exec" in source or "subprocess" in source
        )
        assert not uses_agentcore_cli, (
            "WorkflowExecutor.rollback() uses subprocess to call 'agentcore destroy'. "
            "Should use runtime_deployer.destroy_runtime() via boto3 instead."
        )
