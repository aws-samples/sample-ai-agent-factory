"""F-41b: every role with a home-bucket grant can reach the regional artifact buckets.

A same-account deploy to a non-home region stages code in that region's
``agentcore-flows-artifacts-{account}-{region}`` bucket. Region registration failed live
with HeadBucket 403 because each role held S3 grants on the home ArtifactsBucket only
(stacks/platform/regional_artifact_bucket_grant.py).
"""

import ast
import fnmatch
import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform.regional_artifact_bucket_grant import (
    BUCKET_NAMESPACE_PREFIX,
    DELETE_OBJECT_ACTIONS,
    LIST_VERSION_ACTIONS,
    READ_BUCKET_ACTIONS,
    READ_OBJECT_ACTIONS,
    READ_TAG_ACTIONS,
    WRITE_OBJECT_ACTIONS,
    WRITE_TAG_ACTIONS,
)
from stacks.platform_stack import PlatformStack

ACCOUNT = "123456789012"
NAMESPACE = f"arn:aws:s3:::{BUCKET_NAMESPACE_PREFIX}-{ACCOUNT}-*"
DEPLOY_TARGET = Path(__file__).resolve().parents[2] / "backend" / "src" / "app" / "services" / "deploy_target.py"

READ = set(READ_BUCKET_ACTIONS), set(READ_OBJECT_ACTIONS)
READ_WRITE = set(READ_BUCKET_ACTIONS), set(READ_OBJECT_ACTIONS + WRITE_OBJECT_ACTIONS)
DELETE = set(), set(DELETE_OBJECT_ACTIONS)
TAGS = set(WRITE_TAG_ACTIONS + READ_TAG_ACTIONS)
# A tagged upload is authorized as PutObject AND PutObjectTagging; the first live
# non-home deploy failed on the second. The ownership proof before a delete reads tags.
# The version-aware delete (F-60) lists the key's versions and reads each one's tags.
READ_WRITE_TAGGED = READ_WRITE[0] | set(LIST_VERSION_ACTIONS), READ_WRITE[1] | TAGS
READ_WRITE_TAG_WRITE = READ_WRITE[0], READ_WRITE[1] | set(WRITE_TAG_ACTIONS)
DELETE_OWNED = set(LIST_VERSION_ACTIONS), DELETE[1] | set(READ_TAG_ACTIONS)

#: role logical-id prefix -> (bucket actions, object actions) it must hold on the namespace.
EXPECTED = {
    "StepCodegenRole": READ_WRITE_TAG_WRITE,
    "StepGatewayRole": READ_WRITE_TAGGED,
    "StepKnowledgeBaseRole": READ_WRITE,
    "StepMcpServerRole": READ_WRITE_TAG_WRITE,
    "StepRuntimeConfigureRole": READ,
    "StepRuntimeLaunchRole": READ,
    "StepStatusUpdateRole": DELETE_OWNED,
    "DeploymentLambdaRole": READ_WRITE_TAGGED,
    # AgentCore assumes this role to fetch the code zip from the runtime's own region.
    "SharedRuntimeExecRole": READ,
    # Standalone FastMCP runtimes fetch the same regional artifact with a model-free role.
    "SharedMcpRuntimeExecRole": READ,
}


@pytest.fixture(scope="module")
def template():
    app = cdk.App()
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name="test",
        project_name="acf",
        env=cdk.Environment(region="us-east-1", account=ACCOUNT),
    )
    return Template.from_stack(stack).to_json()


def _role_id(template: dict, prefix: str) -> str:
    ids = [k for k, v in template["Resources"].items() if v["Type"] == "AWS::IAM::Role" and k.startswith(prefix)]
    assert len(ids) == 1, (prefix, ids)
    return ids[0]


def _statements(template: dict, role_id: str) -> list[dict]:
    resources = template["Resources"]
    out = []
    for policy in resources[role_id]["Properties"].get("Policies", []) or []:
        out.extend(policy["PolicyDocument"].get("Statement", []) or [])
    for resource in resources.values():
        if resource["Type"] not in {"AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"}:
            continue
        roles = resource["Properties"].get("Roles", []) or []
        if any(isinstance(r, dict) and r.get("Ref") == role_id for r in roles):
            out.extend(resource["Properties"]["PolicyDocument"].get("Statement", []) or [])
    return out


def _listify(value) -> list:
    return value if isinstance(value, list) else [value]


def _all_statements(template: dict) -> list[dict]:
    out = []
    for logical_id, resource in template["Resources"].items():
        if logical_id.startswith("AgentCoreRoleBoundary"):
            # AgentCoreRoleBoundary is the permissions boundary for roles the backend mints (F-06,
            # stacks/platform/role_boundary.py). It is attached to no principal, so it grants nothing:
            # it is the cap on what a CREATED role may be granted, and its wildcards are that ceiling.
            # test_f06_role_permissions_boundary.py pins that nothing references it and that it is the
            # only unattached managed policy, so this exemption cannot hide a real grant.
            continue
        props = resource.get("Properties", {})
        if resource["Type"] in {"AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"}:
            out.extend(props["PolicyDocument"].get("Statement", []) or [])
        if resource["Type"] == "AWS::IAM::Role":
            for policy in props.get("Policies", []) or []:
                out.extend(policy["PolicyDocument"].get("Statement", []) or [])
    return out


def _namespace_grants(statements: list[dict], resource: str) -> set[str]:
    granted: set[str] = set()
    for st in statements:
        if st.get("Effect") != "Allow" or resource not in _listify(st.get("Resource", [])):
            continue
        # Anti-squatting: S3 names are global, so a bucket in this namespace can belong
        # to another account. A grant without the owner condition does not count.
        assert st.get("Condition") == {"StringEquals": {"aws:ResourceAccount": ACCOUNT}}, st
        granted |= set(_listify(st["Action"]))
    return granted


@pytest.mark.parametrize("prefix", sorted(EXPECTED))
def test_role_can_reach_the_regional_artifact_buckets(template, prefix):
    statements = _statements(template, _role_id(template, prefix))
    bucket_actions, object_actions = EXPECTED[prefix]
    assert bucket_actions <= _namespace_grants(statements, NAMESPACE)
    assert object_actions <= _namespace_grants(statements, f"{NAMESPACE}/*")


@pytest.mark.parametrize("prefix", sorted(EXPECTED))
def test_role_gets_no_more_than_its_home_bucket_role_on_the_namespace(template, prefix):
    statements = _statements(template, _role_id(template, prefix))
    bucket_actions, object_actions = EXPECTED[prefix]
    assert _namespace_grants(statements, NAMESPACE) == bucket_actions
    assert _namespace_grants(statements, f"{NAMESPACE}/*") == object_actions


def test_expected_is_every_role_with_a_home_bucket_grant(template):
    """A tenth role granted the home bucket must be added to EXPECTED, not stay home-only."""
    resources = template["Resources"]
    (bucket_id,) = [
        k for k, v in resources.items() if v["Type"] == "AWS::S3::Bucket" and k.startswith("ArtifactsBucket")
    ]
    consumers = set()
    for role_id, role in resources.items():
        if role["Type"] != "AWS::IAM::Role":
            continue
        if bucket_id in json.dumps(_statements(template, role_id)):
            consumers.add(role_id)
    # CDK's BucketDeployment copies the frontend assets into the home bucket at deploy
    # time; it never touches a regional bucket.
    consumers = {r for r in consumers if not r.startswith("CustomCDKBucketDeployment")}
    assert consumers == {_role_id(template, prefix) for prefix in EXPECTED}


def test_no_role_can_write_or_delete_any_bucket(template):
    offenders = []
    for st in _all_statements(template):
        if st.get("Effect") != "Allow":
            continue
        resources = [r for r in _listify(st.get("Resource", [])) if isinstance(r, str)]
        wide = [r for r in resources if r in {"*", "arn:aws:s3:::*", "arn:aws:s3:::*/*"}]
        mutating = [a for a in _listify(st.get("Action", [])) if a.startswith(("s3:Put", "s3:Delete")) or a == "s3:*"]
        if wide and mutating:
            offenders.append(st)
    assert offenders == [], json.dumps(offenders, default=str)[:2000]


def test_namespace_matches_the_backend_default_bucket_prefix():
    tree = ast.parse(DEPLOY_TARGET.read_text())
    value = next(
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "DEFAULT_TARGET_ARTIFACT_BUCKET_PREFIX" for t in node.targets)
    )
    assert value == BUCKET_NAMESPACE_PREFIX


def test_status_update_can_prove_ownership_on_the_home_bucket(template):
    """Its cleanup reads tags before deleting; grant_delete alone cannot (measured live)."""
    statements = _statements(template, _role_id(template, "StepStatusUpdateRole"))
    (bucket_id,) = [
        k
        for k, v in template["Resources"].items()
        if v["Type"] == "AWS::S3::Bucket" and k.startswith("ArtifactsBucket")
    ]
    granted = set()
    for st in statements:
        if st.get("Effect") == "Allow" and bucket_id in json.dumps(st.get("Resource")):
            granted |= set(_listify(st["Action"]))
    assert "s3:GetObjectTagging" in granted


#: Every role that reads object tags, from the call graph: get_object_tagging is called
#: only by resource_ownership.assert_s3_object_owned, whose callers are gateway_deployer
#: (the gateway step's spec rollback), deployment_handler (teardown) and
#: status_update_step (failure cleanup). Exact, so a new reader is added on purpose.
TAG_READERS = {"StepGatewayRole", "DeploymentLambdaRole", "StepStatusUpdateRole"}
#: Every role that uploads with Tagging=: runtime_deployer.upload_code_to_s3 (codegen,
#: mcp_server, the in-process deployment path) and gateway spec staging.
TAG_WRITERS = {"StepCodegenRole", "StepMcpServerRole", "StepGatewayRole", "DeploymentLambdaRole"}


@pytest.mark.parametrize(
    ("action", "resource", "holders"),
    [
        ("s3:GetObjectTagging", f"{NAMESPACE}/*", TAG_READERS),
        ("s3:GetObjectVersionTagging", f"{NAMESPACE}/*", TAG_READERS),
        ("s3:ListBucketVersions", NAMESPACE, TAG_READERS),
        ("s3:PutObjectTagging", f"{NAMESPACE}/*", TAG_WRITERS),
    ],
)
def test_tag_actions_on_the_namespace_are_held_by_exactly_the_call_graph(template, action, resource, holders):
    have = {
        prefix
        for prefix in EXPECTED
        if action in _namespace_grants(_statements(template, _role_id(template, prefix)), resource)
    }
    assert have == holders


def _home_bucket_grants(template: dict, role_id: str, *, objects: bool) -> list[str]:
    """Action patterns the role is allowed on the home ArtifactsBucket (or its objects)."""
    (bucket_id,) = [
        k
        for k, v in template["Resources"].items()
        if v["Type"] == "AWS::S3::Bucket" and k.startswith("ArtifactsBucket")
    ]
    granted: list[str] = []
    for st in _statements(template, role_id):
        if st.get("Effect") != "Allow":
            continue
        for resource in _listify(st.get("Resource", [])):
            text = json.dumps(resource)
            if bucket_id not in text or ("/*" in text) != objects:
                continue
            granted += _listify(st["Action"])
    return granted


@pytest.mark.parametrize("prefix", sorted(TAG_READERS))
@pytest.mark.parametrize(
    ("action", "objects"),
    [("s3:ListBucketVersions", False), ("s3:GetObjectVersionTagging", True), ("s3:DeleteObjectVersion", True)],
)
def test_every_version_aware_deleter_can_run_it_on_the_home_bucket(template, prefix, action, objects):
    """Effective, wildcard-aware: CDK's grant_read_write grants s3:List* / s3:GetObject*."""
    patterns = _home_bucket_grants(template, _role_id(template, prefix), objects=objects)
    assert any(fnmatch.fnmatchcase(action, p) for p in patterns), (prefix, action, patterns)
