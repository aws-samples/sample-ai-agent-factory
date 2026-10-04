"""P0-C: export bundles expire after a day, and the API role can write them tagged.

The two download routes stage a zip under ``cfn-templates/`` or ``python-exports/``
behind a one-hour presigned URL (backend deployment_handler._stage_export_bundle).
Read from the SYNTHESIZED template:

  * the artifacts bucket expires both prefixes after one day under stable rule ids,
    and ``deployments/`` -- workload code, a different class -- keeps its 90 days;
  * the prefixes are the ones the backend writes, parsed from its source, so a
    renamed prefix cannot leave its bundles outside every rule;
  * the deployment Lambda's role holds s3:PutObjectTagging on the bucket's objects,
    because a PutObject that carries ``Tagging`` is authorized as both actions.

ARCC guidance: cnt_sfiNQzRGEcegFL (purge timeboxed data via lifecycle).
"""

import ast
import fnmatch
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform.buckets import EXPORT_BUNDLE_LIFECYCLE_RULES
from stacks.platform_stack import PlatformStack

DEPLOYMENT_HANDLER = Path(__file__).resolve().parents[2] / "backend" / "src" / "app" / "deployment_handler.py"


@pytest.fixture(scope="module")
def template():
    app = cdk.App()
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name="test",
        project_name="acf",
        env=cdk.Environment(region="us-east-1", account="123456789012"),
    )
    return Template.from_stack(stack).to_json()


def _artifacts_bucket(template: dict) -> tuple[str, dict]:
    hits = [
        (lid, r)
        for lid, r in template["Resources"].items()
        if r["Type"] == "AWS::S3::Bucket" and lid.startswith("ArtifactsBucket")
    ]
    assert len(hits) == 1, [lid for lid, _ in hits]
    return hits[0]


def _backend_export_prefixes() -> set[str]:
    tree = ast.parse(DEPLOYMENT_HANDLER.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "EXPORT_BUNDLE_PREFIXES" for t in node.targets
        ):
            return {f"{v}/" for v in ast.literal_eval(node.value).values()}
    raise AssertionError("EXPORT_BUNDLE_PREFIXES not found in deployment_handler.py")


def test_the_artifacts_bucket_expires_both_export_prefixes_after_one_day(template):
    _lid, bucket = _artifacts_bucket(template)
    rules = bucket["Properties"]["LifecycleConfiguration"]["Rules"]

    # A list compare, not a dict keyed by prefix: that would hide a duplicate rule.
    expected = [
        {"Prefix": "deployments/", "ExpirationInDays": 90, "Status": "Enabled"},
        *(
            {"Id": rule_id, "Prefix": prefix, "ExpirationInDays": 1, "Status": "Enabled"}
            for rule_id, prefix in EXPORT_BUNDLE_LIFECYCLE_RULES.items()
        ),
    ]
    assert len(rules) == len(expected) == 3, rules
    assert sorted(rules, key=lambda r: r["Prefix"]) == sorted(expected, key=lambda r: r["Prefix"])


def test_a_versioned_bucket_would_also_expire_noncurrent_export_versions(template):
    """Expiration on a versioned bucket only adds a delete marker; the bundle stays."""
    _lid, bucket = _artifacts_bucket(template)
    if bucket["Properties"].get("VersioningConfiguration", {}).get("Status") != "Enabled":
        return
    rules = bucket["Properties"]["LifecycleConfiguration"]["Rules"]
    for prefix in EXPORT_BUNDLE_LIFECYCLE_RULES.values():
        assert any(r.get("Prefix") == prefix and r.get("NoncurrentVersionExpiration") for r in rules), prefix


def test_the_rules_cover_exactly_the_prefixes_the_backend_writes():
    assert set(EXPORT_BUNDLE_LIFECYCLE_RULES.values()) == _backend_export_prefixes()


def _role_statements(template: dict, prefix: str) -> list[dict]:
    """Inline policies on the role plus every Policy/ManagedPolicy attached to it.

    The ManagedPolicy branch matters: this role's artifacts grant lives in a CDK
    ``OverflowPolicy``, past the inline-policy size limit.
    """
    resources = template["Resources"]
    role_ids = [lid for lid, r in resources.items() if r["Type"] == "AWS::IAM::Role" and lid.startswith(prefix)]
    assert len(role_ids) == 1, role_ids
    out = []
    for pol in resources[role_ids[0]]["Properties"].get("Policies", []) or []:
        out.extend(pol["PolicyDocument"]["Statement"])
    for r in resources.values():
        if r["Type"] in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy") and any(
            isinstance(x, dict) and x.get("Ref") == role_ids[0] for x in r["Properties"].get("Roles", []) or []
        ):
            out.extend(r["Properties"]["PolicyDocument"]["Statement"])
    return out


def _object_pattern(resource, bucket_id: str) -> str | None:
    """The key pattern of an object ARN on *bucket_id*, or None for anything else.

    Only ``Fn::Join ["", [GetAtt <bucket>.Arn, "/<pattern>"]]`` is an object ARN; the
    bare ``GetAtt`` is the BUCKET, on which object actions authorize nothing.
    """
    if not (isinstance(resource, dict) and "Fn::Join" in resource):
        return None
    sep, parts = resource["Fn::Join"]
    if sep != "" or len(parts) != 2 or parts[0] != {"Fn::GetAtt": [bucket_id, "Arn"]}:
        return None
    tail = parts[1]
    return tail[1:] if isinstance(tail, str) and tail.startswith("/") else None


def test_the_deployment_lambda_can_put_a_tagged_object_and_presign_its_read(template):
    bucket_id, _ = _artifacts_bucket(template)
    statements = [st for st in _role_statements(template, "DeploymentLambdaRole") if st.get("Effect") == "Allow"]
    sample_keys = [f"{prefix}0123abcd/bundle-{'f' * 32}.zip" for prefix in EXPORT_BUNDLE_LIFECYCLE_RULES.values()]

    def _covered(action: str, key: str) -> bool:
        for st in statements:
            acts = [st["Action"]] if isinstance(st["Action"], str) else st["Action"]
            if not any(fnmatch.fnmatchcase(action, a) for a in acts):
                continue
            res = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
            patterns = [p for p in (_object_pattern(r, bucket_id) for r in res) if p is not None]
            if any(fnmatch.fnmatchcase(key, p) for p in patterns):
                return True
        return False

    missing = [
        (action, key)
        for action in ("s3:PutObject", "s3:PutObjectTagging", "s3:GetObject")
        for key in sample_keys
        if not _covered(action, key)
    ]
    assert not missing, f"DeploymentLambdaRole cannot perform these on artifacts-bucket OBJECTS: {missing}"
