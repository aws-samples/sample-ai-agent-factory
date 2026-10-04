"""The model-capable shared runtime role must read the artifact bucket with exact
S3 verbs, never grant_read()'s wildcard action families.

AgentCore fetches the staged runtime ZIP as this role. The obvious way to grant that
-- artifacts_bucket.grant_read(role) -- emits wildcard ACTIONS (s3:GetObject*,
s3:GetBucket*, s3:List*). That is both broader than fetching a ZIP needs AND makes the
role's AwsSolutions-IAM5 suppression rationale ("wildcard RESOURCES only, actions are
exact") factually false, since the suppression silences the resource-level findings and
would then be hiding wildcard actions too. This test is the action-level lock that keeps
the suppression honest: every S3 action on the role must be an exact verb, and the S3
action set must be exactly the read set. A future grant_read() regression -- on the
current bucket or the regional buckets -- fails here even though the role's IAM5
findings are construct-suppressed in nag_suppressions.py.

The sibling model-free MCP role has its own equivalent lock in
test_mcp_shared_runtime_role.py; this file is the model-capable counterpart.
"""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform.regional_artifact_bucket_grant import BUCKET_NAMESPACE_PREFIX
from stacks.platform_stack import PlatformStack

from tests.iam_attachment import statements_for_role

REGION = "us-east-1"
ACCOUNT = "123456789012"
PROJECT = "agentcore-workflow"
ENVIRONMENT = "test"

MODEL_ROLE_NAME = f"AgentCoreRuntime-{PROJECT}-{ENVIRONMENT}-shared"

# The COMPLETE set of S3 actions the shared runtime role may hold: exact read verbs
# for listing the bucket and fetching a specific (optionally versioned) object. Every
# one is a discrete action; a wildcard family such as s3:GetObject* or s3:List* is NOT
# in this set and would fail the equality assertion below.
EXPECTED_S3_ACTIONS = {
    "s3:ListBucket",
    "s3:GetBucketLocation",
    "s3:GetObject",
    "s3:GetObjectVersion",
}
BUCKET_LEVEL_ACTIONS = {"s3:ListBucket", "s3:GetBucketLocation"}
OBJECT_LEVEL_ACTIONS = {"s3:GetObject", "s3:GetObjectVersion"}


@pytest.fixture(scope="module")
def template_json() -> dict:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name=ENVIRONMENT,
        project_name=PROJECT,
        env=cdk.Environment(region=REGION, account=ACCOUNT),
    )
    return Template.from_stack(stack).to_json()


def _role_logical_id(template_json: dict, role_name: str) -> str:
    matches = [
        logical_id
        for logical_id, resource in template_json["Resources"].items()
        if resource["Type"] == "AWS::IAM::Role" and resource.get("Properties", {}).get("RoleName") == role_name
    ]
    assert len(matches) == 1, f"expected exactly one AWS::IAM::Role named {role_name!r}, found {matches}"
    return matches[0]


def _s3_statements(template_json: dict, logical_id: str) -> list[dict]:
    """Every identity-attached statement whose action list touches S3."""
    out: list[dict] = []
    for _source, statement in statements_for_role(template_json, logical_id):
        value = statement.get("Action") or []
        actions = [value] if isinstance(value, str) else value
        if any(action.startswith("s3:") for action in actions):
            out.append(statement)
    return out


def test_the_shared_runtime_role_reads_s3_with_exact_actions(template_json):
    role = _role_logical_id(template_json, MODEL_ROLE_NAME)
    statements = _s3_statements(template_json, role)
    assert statements, (
        "the shared runtime role has no S3 read statement; AgentCore fetches the staged "
        "ZIP as this role, so a role without artifact read authority is a dead container"
    )

    all_s3_actions: set[str] = set()
    for statement in statements:
        value = statement.get("Action") or []
        all_s3_actions.update([value] if isinstance(value, str) else value)

    # No wildcard action family anywhere -- this is exactly what grant_read() would
    # reintroduce (s3:GetObject*, s3:GetBucket*, s3:List*).
    wildcard = sorted(a for a in all_s3_actions if a.endswith("*"))
    assert not wildcard, (
        f"the shared runtime role holds wildcard S3 action families {wildcard}. "
        "artifacts_bucket.grant_read() was almost certainly reintroduced; use the "
        "explicit READ_BUCKET_ACTIONS / READ_OBJECT_ACTIONS lists instead. The role's "
        "IAM5 suppression claims 'actions are exact', so a wildcard action here makes "
        "that rationale false."
    )

    # Exact set: only the read verbs, nothing broader (no s3:PutObject, s3:DeleteObject,
    # etc. leaking onto the role via a future grant).
    assert all_s3_actions == EXPECTED_S3_ACTIONS, (
        f"the shared runtime role's S3 action set drifted. Unexpected extra: "
        f"{sorted(all_s3_actions - EXPECTED_S3_ACTIONS)}; missing: "
        f"{sorted(EXPECTED_S3_ACTIONS - all_s3_actions)}."
    )


def _s3_resource_reprs(statement: dict) -> list[str]:
    """Each Resource entry on a statement as a comparable string: a literal ARN stays a
    string; an intrinsic (Fn::GetAtt / Fn::Join / Fn::Sub) is JSON-dumped so substring
    checks can inspect it. A str Resource stays raw so a mutation to Resource='*' is the
    exact string '*'."""
    resource = statement.get("Resource")
    elements = resource if isinstance(resource, list) else [resource]
    return [el if isinstance(el, str) else json.dumps(el) for el in elements]


def test_s3_resources_are_scoped_by_shape_and_never_wildcard(template_json):
    """Kill the 'widen a scoped bucket ARN to "*"' mutant, and pin every read shape.

    Asserting only action exactness (the test above) leaves the RESOURCE free to drift
    to '*': a mutation audit confirmed BOTH bucket-level statements survived being
    rewritten to Resource='*'. This test closes that gap. Every S3 resource must be a
    scoped ARN whose shape matches its action level -- object verbs on a `<bucket>/*`
    ARN, bucket verbs on the bucket ARN -- and never the literal '*' or the account-wide
    `arn:aws:s3:::*`. It also positively requires all four artifact-read shapes: the
    current bucket (Fn::GetAtt ARN, and that ARN + /*) AND the regional artifact
    namespace (arn:aws:s3:::<prefix>-<account>-*, and that + /*)."""
    role = _role_logical_id(template_json, MODEL_ROLE_NAME)
    statements = _s3_statements(template_json, role)
    assert statements, "no S3 read statement on the shared runtime role"

    saw = {
        "current_bucket": False,
        "current_object": False,
        "regional_bucket": False,
        "regional_object": False,
    }

    for statement in statements:
        value = statement.get("Action") or []
        actions = set([value] if isinstance(value, str) else value)
        for repr_ in _s3_resource_reprs(statement):
            assert repr_ != "*", (
                f"an S3 statement {sorted(actions)} uses the literal '*' resource; artifact "
                "reads must be scoped to the artifacts bucket / regional namespace. A "
                "Resource='*' mutation on a scoped bucket ARN must fail here."
            )
            assert "arn:aws:s3:::*" not in repr_, (
                f"an S3 statement {sorted(actions)} uses the account-wide bucket wildcard "
                f"arn:aws:s3:::* ({repr_}); scope it to the artifacts namespace"
            )

            is_object_resource = "/*" in repr_
            is_current_bucket = "Fn::GetAtt" in repr_
            is_regional = BUCKET_NAMESPACE_PREFIX in repr_

            if actions & OBJECT_LEVEL_ACTIONS:
                assert is_object_resource, (
                    f"object-level actions {sorted(actions & OBJECT_LEVEL_ACTIONS)} on a non-object resource: {repr_}"
                )
            if actions & BUCKET_LEVEL_ACTIONS:
                assert not is_object_resource, (
                    f"bucket-level actions {sorted(actions & BUCKET_LEVEL_ACTIONS)} on an "
                    f"object ('/*') resource: {repr_}"
                )

            if actions & BUCKET_LEVEL_ACTIONS and is_current_bucket and not is_object_resource:
                saw["current_bucket"] = True
            if actions & OBJECT_LEVEL_ACTIONS and is_current_bucket and is_object_resource:
                saw["current_object"] = True
            if actions & BUCKET_LEVEL_ACTIONS and is_regional and not is_object_resource:
                saw["regional_bucket"] = True
            if actions & OBJECT_LEVEL_ACTIONS and is_regional and is_object_resource:
                saw["regional_object"] = True

    missing = sorted(k for k, v in saw.items() if not v)
    assert not missing, (
        f"expected all four artifact-read shapes present, missing: {missing}. The role "
        "must read the current bucket (Fn::GetAtt ARN + that ARN/*) AND the regional "
        f"artifact namespace ({BUCKET_NAMESPACE_PREFIX}-<account>-* + that/*)."
    )
