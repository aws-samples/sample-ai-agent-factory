"""F-03: a tag-gated delete grant is only a gate if the same principal cannot stamp the tag.

The deployment Lambda's cleanup grant on ``role/*-role`` (Get/Detach/Delete/List/DeleteRolePolicy)
is conditioned on ``aws:ResourceTag/ManagedBy=agentcore-flows`` -- and the statement right
below it granted ``iam:TagRole`` on the same ``role/*-role`` with no condition. The comment said
"a role we didn't create can never carry the tag"; the TagRole grant made that false:
``TagRole(prod-app-role, ManagedBy=agentcore-flows)`` then ``DeleteRole(prod-app-role)``.

The fix pins three things on every TagRole this role holds (all keys confirmed against the
Service Reference feed: aws:RequestTag/aws:TagKeys on the action, aws:ResourceTag on the role
resource):

* ``aws:RequestTag/ManagedBy = agentcore-flows`` -- it may only ever write the product value
  it already governs, never a different one;
* ``ForAllValues:StringLike aws:TagKeys`` -- an allowlist of the keys the backend actually
  sends on this path (``governed_tag_list``: ManagedBy, AgentCoreStack, namespaced governance
  keys), so no other key can be written;
* on ``role/*-role`` only, ``aws:ResourceTag/ManagedBy = agentcore-flows`` -- the ONLY roles it
  may re-tag are ones that already carry the tag. The one live caller on that path is the
  "reused role" branch of ``runtime_deployer.create_runtime_iam_role``, which runs AFTER
  ``assert_this_deployment_may_mutate`` has proven the tags exist, so the repair still works
  and an untagged role can never be claimed. Nothing in the deployment Lambda CREATES a
  ``*-role`` role (its CreateRole is scoped to the sandbox prefix), so no create-with-tags path
  needs the untagged form.

Memory says: conditioning a tag grant splits by prefix, and a read in the same statement fails
closed -- TagRole is therefore in statements of its own. ARCC cnt_SFJJhkOueCPRkd.
"""

from __future__ import annotations

import pytest
from stacks.platform.config import governance_tag_key_globs

from tests.iam_attachment import role_logical_id, statements_for_role
from tests.p1_synth import ENVIRONMENT, PROJECT, actions, all_statements, resources_text, synth

TAG = "iam:TagRole"
GATED_DELETE_RESOURCE = "role/*-role"


@pytest.fixture(scope="module")
def tpl() -> dict:
    return synth("F03TagRoleGateStack")


@pytest.fixture(scope="module")
def deployment_statements(tpl) -> list[tuple[str, dict]]:
    return statements_for_role(tpl, role_logical_id(tpl, "DeploymentLambdaRole"))


def _tag_grants(statements):
    return [(pid, st) for pid, st in statements if st.get("Effect") == "Allow" and TAG in actions(st)]


def test_the_gated_delete_grant_is_still_there(deployment_statements):
    """Regression guard: the fix must not have "solved" F-03 by dropping the cleanup path."""
    gated = [
        st
        for _pid, st in deployment_statements
        if "iam:DeleteRole" in actions(st)
        and any(GATED_DELETE_RESOURCE in r for r in resources_text(st))
        and (st.get("Condition") or {}).get("StringEquals", {}).get("aws:ResourceTag/ManagedBy") == "agentcore-flows"
    ]
    assert gated, "the ManagedBy-gated DeleteRole grant on role/*-role is gone"


def test_the_walk_finds_the_tag_grants(deployment_statements):
    assert _tag_grants(deployment_statements), "no iam:TagRole grant resolves to the deployment Lambda; walk broken"


def test_tag_role_on_the_gated_prefix_requires_the_tag_to_already_be_there(deployment_statements):
    """The assertion that fails on the pre-fix tree: stamping an untagged *-role is denied."""
    on_prefix = [
        (pid, st)
        for pid, st in _tag_grants(deployment_statements)
        if any(GATED_DELETE_RESOURCE in r for r in resources_text(st))
    ]
    assert on_prefix, "no iam:TagRole grant on role/*-role found; the repair path lost its grant"
    for pid, st in on_prefix:
        cond = st.get("Condition") or {}
        assert cond.get("StringEquals", {}).get("aws:ResourceTag/ManagedBy") == "agentcore-flows", (
            f"{pid}: iam:TagRole on role/*-role without aws:ResourceTag/ManagedBy -- the DeleteRole gate is "
            "self-satisfiable"
        )


def test_every_tag_role_grant_pins_the_product_value_and_an_allowlist_of_keys(deployment_statements):
    for pid, st in _tag_grants(deployment_statements):
        cond = st.get("Condition") or {}
        assert cond.get("StringEquals", {}).get("aws:RequestTag/ManagedBy") == "agentcore-flows", (pid, cond)
        assert cond.get("StringLike", {}).get("aws:RequestTag/AgentCoreStack") == f"{PROJECT}-{ENVIRONMENT}-*", (
            pid,
            cond,
        )
        keys = cond.get("ForAllValues:StringLike", {}).get("aws:TagKeys")
        assert keys, f"{pid}: no aws:TagKeys allowlist on iam:TagRole"
        assert "ManagedBy" in keys and "AgentCoreStack" in keys, keys
        if any(GATED_DELETE_RESOURCE in r for r in resources_text(st)):
            # The repair path sends governed_tag_list, which carries namespaced governance keys.
            assert set(governance_tag_key_globs()) <= set(keys), f"the *-role allowlist must admit them: {keys}"
        else:
            # The sandbox create sends owner_tag_list: exactly the two ownership keys.
            assert set(keys) == {"ManagedBy", "AgentCoreStack"}, keys
        assert set(actions(st)) == {TAG}, (
            f"{pid}: iam:TagRole shares a statement with {actions(st)}; the request-tag condition would deny "
            "every action in it that carries no tags"
        )


def test_no_role_in_the_template_may_untag_a_role(tpl):
    """Nothing in the backend calls untag_role; a strip primitive with no caller stays absent."""
    for pid, st in all_statements(tpl):
        if st.get("Effect") == "Allow":
            assert "iam:UntagRole" not in actions(st), pid
