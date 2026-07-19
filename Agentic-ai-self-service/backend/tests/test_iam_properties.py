"""Property-based tests for IAM policy scoping.

Property 7: IAM Policy Scoping for Tools
- For any set of connected tools, the generated IAM policy contains only
  the actions required by those specific tools and no additional service permissions.

Validates: Requirements 5.5, 6.4
"""

import sys

sys.path.insert(0, "src")

from app.services.iam_manager import (
    TOOL_POLICY_STATEMENTS,
    build_tool_policy_statements,
)
from hypothesis import given, settings
from hypothesis import strategies as st

# ============================================================================
# Hypothesis Strategies
# ============================================================================

SUPPORTED_TOOLS = ["browser", "code_interpreter", "memory", "gateway"]

# Generate random subsets of supported tools (including empty set)
tool_subset_st = st.lists(
    st.sampled_from(SUPPORTED_TOOLS),
    min_size=0,
    max_size=len(SUPPORTED_TOOLS),
    unique=True,
)


# ============================================================================
# Helpers
# ============================================================================


def _get_expected_actions_for_tool(tool: str) -> set[str]:
    """Get the set of IAM actions expected for a single tool."""
    actions: set[str] = set()
    stmts = TOOL_POLICY_STATEMENTS.get(tool, [])
    for stmt in stmts:
        actions.update(stmt["Action"])
    return actions


def _get_all_actions_from_statements(statements: list[dict]) -> set[str]:
    """Extract all IAM actions from a list of policy statements."""
    actions: set[str] = set()
    for stmt in statements:
        actions.update(stmt.get("Action", []))
    return actions


# ============================================================================
# Property 7: IAM Policy Scoping for Tools
# ============================================================================


class TestIAMPolicyScopingProperty:
    """Property 7: IAM Policy Scoping for Tools.

    **Validates: Requirements 5.5, 6.4**
    """

    @given(tools=tool_subset_st)
    @settings(max_examples=100)
    def test_policy_contains_only_requested_tool_actions(self, tools: list[str]):
        """Generated policy contains only actions for the requested tools."""
        statements = build_tool_policy_statements(tools)

        if not tools:
            assert statements == [], "Empty tool list should produce no statements"
            return

        actual_actions = _get_all_actions_from_statements(statements)

        # Build expected actions: union of all requested tools + bedrock model if needed
        expected_actions: set[str] = set()
        for tool in tools:
            expected_actions.update(_get_expected_actions_for_tool(tool))

        # browser and code_interpreter also get bedrock model access
        if "browser" in tools or "code_interpreter" in tools:
            expected_actions.update(
                [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ]
            )

        assert actual_actions == expected_actions, (
            f"For tools {tools}:\n"
            f"  Expected actions: {sorted(expected_actions)}\n"
            f"  Actual actions:   {sorted(actual_actions)}\n"
            f"  Extra:   {sorted(actual_actions - expected_actions)}\n"
            f"  Missing: {sorted(expected_actions - actual_actions)}"
        )

    @given(tools=tool_subset_st)
    @settings(max_examples=100)
    def test_no_wildcard_service_permissions(self, tools: list[str]):
        """No statement should grant broad service-level wildcards like s3:* or iam:*."""
        statements = build_tool_policy_statements(tools)
        for stmt in statements:
            for action in stmt.get("Action", []):
                # Actions should be specific, not service-level wildcards
                assert not action.endswith(":*"), f"Found overly broad action '{action}' for tools {tools}"

    @given(tools=tool_subset_st)
    @settings(max_examples=100)
    def test_all_statements_are_allow(self, tools: list[str]):
        """All generated statements should have Effect: Allow."""
        statements = build_tool_policy_statements(tools)
        for stmt in statements:
            assert stmt["Effect"] == "Allow", f"Expected Effect 'Allow', got '{stmt['Effect']}'"
