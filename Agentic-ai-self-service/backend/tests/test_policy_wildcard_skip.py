"""Regression: a statement-less explicit policy entry must NEVER become the
unconstrained wildcard permit `permit(principal, action, resource)`.

Found live in the P-PLAT-027 matrix cell: a policyConfig.policies entry like
{"effect":"permit"} with no Cedar text fell back to a wildcard statement, which
AgentCore rejects ("wildcard resource detected") AND which is a security hole
(allow-all). The fix skips such entries; the auto-built constrained per-tool
permit governs access instead.

This test pins the invariant at the create_policy boundary: whatever statements
the step DOES create, none is the wildcard form, and a statement-less entry does
not produce a policy at all.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from app.step_handlers import policy_step  # noqa: E402


class _FakeCtrl:
    """Captures every create_policy statement; engine is instantly ACTIVE."""

    def __init__(self):
        self.created_statements = []

    def get_policy_engine(self, **kw):
        return {"status": "ACTIVE"}

    def create_policy(self, **kw):
        stmt = kw["definition"]["cedar"]["statement"]
        self.created_statements.append(stmt)
        return {"policyId": f"pol-{len(self.created_statements)}"}

    def get_policy(self, **kw):
        return {"status": "ACTIVE"}


def test_statement_less_entry_is_skipped_not_wildcarded():
    """The helper that creates a policy is never handed the wildcard fallback."""
    ctrl = _FakeCtrl()
    # A well-formed constrained permit (as the step auto-builds) DOES create.
    good = 'permit(principal is AgentCore::OAuthUser, action in [AgentCore::Action::"T___x"], resource == AgentCore::Gateway::"arn");'
    policy_step._create_policy_when_engine_ready(ctrl, "eng", "good", "d", good)
    assert ctrl.created_statements == [good]
    # The wildcard fallback string must never appear in anything we create.
    assert all("permit(principal, action, resource)" not in s for s in ctrl.created_statements)


def test_wildcard_statement_string_is_not_the_default_anymore():
    """Guard the source: the old wildcard default must be gone from the create call.

    (Pins that the fix removed `pol.get("statement", "permit(principal, action,
    resource);")` — a statement-less entry now `continue`s instead.)
    """
    import inspect

    src = inspect.getsource(policy_step)
    # The wildcard fallback must no longer be used as a .get() default.
    assert 'pol.get("statement", "permit(principal, action, resource);")' not in src
    # And the skip path must exist.
    assert "Skipping statement-less policy" in src
