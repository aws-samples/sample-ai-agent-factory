"""An ownership read is TWO calls, and only the second one was ever granted.

``assert_agentcore_resource_owned`` (services/resource_ownership.py:410-440) cannot start
from an id. ``ListTagsForResource`` needs an ARN, so the function first calls the resource
type's own getter to resolve one, and only then reads the tags. The getter is therefore a
hard prerequisite: without it the ownership read fails before it can reach the tag call.

Found live. CloudTrail, 2026-09-22T15:35:52+01:00, during the automatic cleanup of a
failed deploy:

    StepStatusUpdateRole -> GetMemory(memoryId=mem_d47c3d4e52-XJDiI2HKWr) -> AccessDenied

Read off the DEPLOYED effective policy (inline + attached managed, so overflow-safe), the
role held three getters out of the seven types ``status_update`` declares it reads:

    HELD     runtime                   GetAgentRuntime
    HELD     gateway                   GetGateway
    HELD     harness                   GetHarness
    MISSING  memory                    GetMemory
    MISSING  policy-engine             GetPolicyEngine
    MISSING  oauth2credentialprovider  GetOauth2CredentialProvider
    MISSING  apikeycredentialprovider  GetApiKeyCredentialProvider

Four of seven reachable types could not complete an ownership read at all. That is why
auto-cleanup died and left the operator a ``delete_failed`` record for a resource that
still existed.

WHY THE EXISTING SUITE COULD NOT FIND IT, which is the defect this file exists to fix:
``test_agentcore_ownership_read_and_container_arns.py`` carries a single module constant,
``READ_ACTION = "bedrock-agentcore:ListTagsForResource"``, and derives every assertion
from it. It models one half of a two-call sequence, so it is *structurally* incapable of
detecting a missing prerequisite -- no amount of care in those tests would have caught
this. The fix is an added assertion axis, not a stricter version of the existing one.

The two surfaces are NOT the same and must be checked separately: read live,
``DeploymentLambdaRole`` already held all seven getters while ``StepStatusUpdateRole`` held
three. A test that checked one surface and generalised would have reported this clean.
"""

import pathlib
import re
import sys

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform.step_lambdas import (
    AGENTCORE_OWNERSHIP_GETTER,
    AGENTCORE_OWNERSHIP_READ_TYPES,
    AGENTCORE_TYPE_ARN_TAIL,
)
from stacks.platform_stack import PlatformStack

from tests.iam_attachment import statements_for_role

TAG_READ_ACTION = "bedrock-agentcore:ListTagsForResource"

PROJECT = "ownprobe"
ENVIRONMENT = "t0922"
ACCOUNT = "123456789012"
REGION = "us-east-1"

BACKEND_SRC = pathlib.Path(__file__).resolve().parents[2] / "backend" / "src"


@pytest.fixture(scope="module")
def template_json() -> dict:
    app = cdk.App()
    stack = PlatformStack(
        app,
        f"{PROJECT}-{ENVIRONMENT}",
        environment_name=ENVIRONMENT,
        project_name=PROJECT,
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    return Template.from_stack(stack).to_json()


def _role_fragment(step_name: str) -> str:
    """``status_update`` -> ``StepStatusUpdateRole``."""
    return "Step" + "".join(part.capitalize() for part in step_name.split("_")) + "Role"


def _statements(template_json: dict, step_name: str) -> list[dict]:
    fragment = _role_fragment(step_name)
    resources = template_json["Resources"]
    lids = [lid for lid, res in resources.items() if res["Type"] == "AWS::IAM::Role" and fragment in lid]
    assert lids, (
        f"no IAM role logical id contains {fragment!r}. The step-name -> role-name "
        "convention changed, so every assertion below would be scoped to nothing"
    )
    assert len(lids) == 1, f"{fragment!r} matches {lids}; an ambiguous role makes this unattributable"
    return [st for _src, st in statements_for_role(template_json, lids[0])]


def _actions(statement: dict) -> list[str]:
    actions = statement.get("Action", [])
    return [actions] if isinstance(actions, str) else list(actions)


def _resources(statement: dict) -> list[str]:
    res = statement.get("Resource", [])
    res = [res] if not isinstance(res, list) else res
    return [r for r in res if isinstance(r, str)]


# The (step, type) pairs are generated so a type added to AGENTCORE_OWNERSHIP_READ_TYPES
# is covered without editing this file. A hand-written pair list is what let four types
# go ungranted while the suite stayed green.
REACHABLE_PAIRS = [
    (step, resource_type) for step, types in sorted(AGENTCORE_OWNERSHIP_READ_TYPES.items()) for resource_type in types
]


def test_the_pair_list_is_not_empty():
    """Vacuity guard. Every parametrized test below iterates this list, and a loop over
    nothing passes -- which is the exact shape that hid the original defect."""
    assert len(REACHABLE_PAIRS) >= 7, (
        f"only {len(REACHABLE_PAIRS)} (step, type) pairs derived; status_update alone "
        "declares seven types, so the derivation is broken"
    )


@pytest.mark.parametrize(("step_name", "resource_type"), REACHABLE_PAIRS)
def test_every_reachable_type_is_granted_both_halves_of_its_ownership_read(template_json, step_name, resource_type):
    """The assertion axis the previous suite lacked.

    Both actions must be granted ON THE SAME type-scoped resources. Asserting only that
    each action string appears somewhere in the role's document would pass on a getter
    granted against a different type's ARN, which authorizes nothing useful.
    """
    getter = AGENTCORE_OWNERSHIP_GETTER[resource_type]
    expected_arns = {f"arn:aws:bedrock-agentcore:*:{ACCOUNT}:{tail}" for tail in AGENTCORE_TYPE_ARN_TAIL[resource_type]}

    for action in (getter, TAG_READ_ACTION):
        covered: set[str] = set()
        for statement in _statements(template_json, step_name):
            if statement.get("Effect", "Allow") != "Allow":
                continue
            if action in _actions(statement):
                covered.update(_resources(statement))
        missing = expected_arns - covered
        assert not missing, (
            f"{_role_fragment(step_name)} does not grant {action} on {sorted(missing)}, "
            f"which it needs to complete the ownership read for a {resource_type}. "
            "An ownership read is two calls -- the getter resolves the ARN and "
            "ListTagsForResource reads the owner tag off it -- so granting one without "
            "the other leaves the read unable to run. This is the shape that produced "
            "GetMemory AccessDenied in live auto-cleanup on 2026-09-22."
        )


@pytest.mark.parametrize(("step_name", "resource_type"), REACHABLE_PAIRS)
def test_the_getter_is_not_satisfied_by_a_wildcard(template_json, step_name, resource_type):
    """A wildcard would make the test above unfalsifiable.

    ``bedrock-agentcore:Get*`` satisfies every per-type assertion whether or not the type
    was ever modelled, so the suite would go green on a type nobody had considered. Four
    missing actions is precisely when that shortcut is tempting; ARCC least-privilege
    guidance (cnt_BBrFTwAEgWxA30, cnt_AGx9pUNpmdOVZB) says to work upward from zero to the
    minimum set rather than downward from full access.
    """
    for statement in _statements(template_json, step_name):
        for action in _actions(statement):
            if not action.startswith("bedrock-agentcore:"):
                continue
            assert "*" not in action, (
                f"{_role_fragment(step_name)} grants {action!r}. A wildcard AgentCore "
                "action satisfies every assertion in this file without modelling any "
                "type, which converts the invariant into a no-op"
            )


def test_the_getter_table_covers_every_reachable_type():
    """A type in the read table with no getter row is an ungrantable ownership read."""
    reachable = {t for types in AGENTCORE_OWNERSHIP_READ_TYPES.values() for t in types}
    missing = reachable - set(AGENTCORE_OWNERSHIP_GETTER)
    assert not missing, (
        f"AGENTCORE_OWNERSHIP_READ_TYPES declares {sorted(missing)} reachable but "
        "AGENTCORE_OWNERSHIP_GETTER has no getter for them, so their ownership read can "
        "never be granted. Add the row with the call site that proves it."
    )


def test_the_getter_actions_agree_with_the_code_that_calls_them():
    """Reconciles this module's vocabulary against the backend's, by ACTION not by key.

    The two tables are keyed differently on purpose -- infra uses the ARN-tail spelling
    (``policy-engine``) and the backend uses the boto3 spelling (``policy_engine``) -- so
    comparing keys would need a hand-written translation, which is another table to drift.
    Comparing the derived ACTION STRINGS is the property that actually matters: the IAM
    action granted here must be the one the boto3 call will be authorized against.
    """
    added = str(BACKEND_SRC) not in sys.path
    if added:
        sys.path.insert(0, str(BACKEND_SRC))
    try:
        from app.services.resource_ownership import _AGENTCORE_OWNERSHIP_READS
    finally:
        if added:
            sys.path.remove(str(BACKEND_SRC))

    def action_for(method_name: str) -> str:
        # get_oauth2_credential_provider -> GetOauth2CredentialProvider. Note the digit:
        # a naive [a-z_]+ pattern silently drops this one row, which is how a seventh
        # getter goes missing from an audit that looks complete.
        parts = re.split(r"_", method_name)
        return "bedrock-agentcore:" + "".join(p.capitalize() for p in parts)

    from_code = {action_for(spec[0]) for spec in _AGENTCORE_OWNERSHIP_READS.values()}
    assert from_code, "parsed no getters out of _AGENTCORE_OWNERSHIP_READS -- the reconciliation is vacuous"

    declared = set(AGENTCORE_OWNERSHIP_GETTER.values())
    unknown = declared - from_code
    assert not unknown, (
        f"AGENTCORE_OWNERSHIP_GETTER grants {sorted(unknown)}, which no branch of "
        "assert_agentcore_resource_owned calls. Either the action is misspelled (and the "
        "grant authorizes nothing) or it is a capability nothing uses."
    )
