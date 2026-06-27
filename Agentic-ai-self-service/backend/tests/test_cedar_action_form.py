"""Bug 176: Cedar action heads must use the `action in [...]` LIST form, never a
singleton `action == "X"`.

Proven live against the AgentCore policy engine: a singleton action permit on a
gateway resource is rejected CREATE_FAILED "Overly Permissive: will allow every
request", but the list form `action in [AgentCore::Action::"X"]` validates to
ACTIVE — even for a single action. (The service's own StartPolicyGeneration emits
the == form and then flags it ALLOW_ALL.) These tests pin the list form so the
ENFORCE path stays valid and never silently degrades to LOG_ONLY.
"""

from __future__ import annotations

from app.step_handlers.policy_step import _cedar_action_ref


def test_single_prefixed_action_uses_list_form():
    out = _cedar_action_ref("CT-get-canary___get_canary", [])
    assert out.startswith("action in [")
    assert "action ==" not in out


def test_full_cedar_ref_uses_list_form():
    out = _cedar_action_ref('AgentCore::Action::"T___x"', [])
    assert out.startswith("action in [")
    assert "action ==" not in out


def test_bare_tool_single_target_uses_list_form():
    out = _cedar_action_ref("get_canary", ["CT-get-canary"])
    assert out == 'action in [AgentCore::Action::"CT-get-canary___get_canary"]'
    assert "action ==" not in out


def test_bare_tool_multi_target_uses_list_form():
    out = _cedar_action_ref("get_canary", ["T1", "T2"])
    assert out.startswith("action in [")
    assert "T1___get_canary" in out and "T2___get_canary" in out


def test_wildcard_action_is_unconstrained():
    assert _cedar_action_ref("*", []) == "action"
    assert _cedar_action_ref("", []) == "action"


def test_no_singleton_equals_anywhere():
    """Belt-and-suspenders: none of the action-ref shapes emit `action ==`."""
    for a, tn in [
        ("CT-x___y", []),
        ('AgentCore::Action::"a___b"', []),
        ("bare", ["TgtA"]),
        ("bare", ["TgtA", "TgtB"]),
    ]:
        assert "action ==" not in _cedar_action_ref(a, tn)
