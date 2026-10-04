"""P0-C, infra half: no platform caller holds a credential that reaches the artifacts bucket.

A browser caller reaches the platform through the HTTP API with a Cognito JWT. The one
other ingress is the stream Lambda's Function URL, which is AWS_IAM, so it is reachable
only by a principal that already holds AWS credentials. No caller is given AWS
credentials. Read from the SYNTHESIZED template:

  * every API route but ``GET /health`` and the HMAC-authenticated trigger webhook
    has a JWT authorizer, including both exports, and there is no ``$default`` catch-all;
  * every ``AWS::Lambda::Url`` is AWS_IAM, and every ``AWS::Lambda::Permission`` names
    an AWS service principal and a SourceArn. No public permission exists;
  * no JWT becomes AWS credentials. The proof is the combination: there is no Cognito
    identity pool, AND every IAM role trusts an AWS service principal only. The second
    half also closes a direct ``AssumeRoleWithWebIdentity`` through a role that trusts
    an OIDC or Cognito federated principal;
  * the artifacts bucket policy allows nothing to anyone but the CDK auto-delete
    provider role. No target account is granted anything: cross-account deploys use a
    bucket in the target account, and the legacy ``deploy_target_accounts`` context that
    used to add a target-role grant is refused at synth;
  * all four PublicAccessBlock flags are set on the artifacts bucket. It has no website
    configuration and no access point or multi-region access point that could carry a
    second policy;
  * the logging layers a bundle request can reach are pinned exactly. The API stage
    has no access log. The artifacts bucket DOES log S3 server access to the logging
    bucket under ``s3-artifacts/``. Those records carry the object key and the request
    URI, with ``X-Amz-Signature`` redacted by S3. So the key is logged, but the bearer
    capability is not. The redaction is S3's behaviour, which a template cannot
    express; it is proven live by searching the delivered records for the signature.

The backend half (route table, S3 read inventory, URL use) is
backend/tests/test_export_bundle_isolation.py. Each checker has a positive control
that injects the violation it exists to catch into a copy of the template.
"""

import copy

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

EXPORT_ROUTE_KEYS = {"POST /api/generate-cfn-template", "POST /api/export-python"}
UNAUTHENTICATED_ROUTE_KEYS = {
    "GET /health",
    "POST /hooks/{runtime_name}/{trigger_id}",
}


def _synth(context: dict) -> dict:
    app = cdk.App(context=context)
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name="test",
        project_name="acf",
        env=cdk.Environment(region="us-east-1", account="123456789012"),
    )
    return Template.from_stack(stack).to_json()


@pytest.fixture(scope="module")
def template():
    return _synth({})


def _of_type(template: dict, rtype: str) -> dict[str, dict]:
    return {lid: r for lid, r in template["Resources"].items() if r["Type"] == rtype}


def route_auth_violations(template: dict) -> list[str]:
    out = []
    for r in _of_type(template, "AWS::ApiGatewayV2::Route").values():
        p = r["Properties"]
        if p["RouteKey"] == "$default":
            out.append("$default")
        elif p["RouteKey"] not in UNAUTHENTICATED_ROUTE_KEYS and not (
            p.get("AuthorizationType") == "JWT" and p.get("AuthorizerId")
        ):
            out.append(p["RouteKey"])
    return sorted(out)


def credential_path_violations(template: dict) -> list[str]:
    out = [f"identity pool {lid}" for lid in _of_type(template, "AWS::Cognito::IdentityPool")]
    for lid, r in _of_type(template, "AWS::IAM::Role").items():
        for st in r["Properties"]["AssumeRolePolicyDocument"]["Statement"]:
            principal = st.get("Principal")
            if st.get("Effect") != "Allow":
                continue
            if not (isinstance(principal, dict) and set(principal) == {"Service"}):
                out.append(f"role {lid} trusts {principal}")
    return out


def _artifacts_bucket_id(template: dict) -> str:
    ids = [lid for lid in _of_type(template, "AWS::S3::Bucket") if lid.startswith("ArtifactsBucket")]
    assert len(ids) == 1, ids
    return ids[0]


def bucket_policy_violations(template: dict) -> list[str]:
    bucket_id = _artifacts_bucket_id(template)
    policies = [
        r
        for r in _of_type(template, "AWS::S3::BucketPolicy").values()
        if r["Properties"]["Bucket"] == {"Ref": bucket_id}
    ]
    assert len(policies) == 1, policies
    provider = [
        lid
        for lid in _of_type(template, "AWS::IAM::Role")
        if lid.startswith("CustomS3AutoDeleteObjectsCustomResourceProviderRole")
    ]
    assert len(provider) == 1, provider
    auto_delete = {"AWS": {"Fn::GetAtt": [provider[0], "Arn"]}}
    out = []
    for st in policies[0]["Properties"]["PolicyDocument"]["Statement"]:
        if st["Effect"] == "Allow" and st.get("Principal") != auto_delete:
            out.append(f"{st.get('Sid', '?')} allows {st.get('Principal')}")
    return out


def lambda_ingress_violations(template: dict) -> list[str]:
    """Every way into a Lambda that is not a platform-owned service with a pinned source."""
    out = []
    for lid, r in _of_type(template, "AWS::Lambda::Url").items():
        if r["Properties"].get("AuthType") != "AWS_IAM":
            out.append(f"function url {lid} AuthType={r['Properties'].get('AuthType')}")
    for lid, r in _of_type(template, "AWS::Lambda::Permission").items():
        p = r["Properties"]
        principal = p.get("Principal")
        if not (isinstance(principal, str) and principal.endswith(".amazonaws.com") and p.get("SourceArn")):
            out.append(f"permission {lid} principal={principal} source={p.get('SourceArn')}")
        if p.get("FunctionUrlAuthType") == "NONE":
            out.append(f"permission {lid} FunctionUrlAuthType=NONE")
    return out


_PAB = {"BlockPublicAcls": True, "BlockPublicPolicy": True, "IgnorePublicAcls": True, "RestrictPublicBuckets": True}
_ACCESS_POINT_TYPES = (
    "AWS::S3::AccessPoint",
    "AWS::S3::AccessPointPolicy",
    "AWS::S3::MultiRegionAccessPoint",
    "AWS::S3::MultiRegionAccessPointPolicy",
    "AWS::S3ObjectLambda::AccessPoint",
    "AWS::S3ObjectLambda::AccessPointPolicy",
)


def public_exposure_violations(template: dict) -> list[str]:
    bucket = template["Resources"][_artifacts_bucket_id(template)]["Properties"]
    out = []
    if bucket.get("PublicAccessBlockConfiguration") != _PAB:
        out.append(f"public access block {bucket.get('PublicAccessBlockConfiguration')}")
    if "WebsiteConfiguration" in bucket:
        out.append("website configuration")
    out += [f"{r['Type']} {lid}" for lid, r in template["Resources"].items() if r["Type"] in _ACCESS_POINT_TYPES]
    return out


def test_only_health_and_hmac_webhook_skip_jwt_and_both_exports_exist(
    template,
):
    keys = {r["Properties"]["RouteKey"] for r in _of_type(template, "AWS::ApiGatewayV2::Route").values()}
    assert EXPORT_ROUTE_KEYS <= keys and len(keys) > 50, len(keys)  # reach
    assert route_auth_violations(template) == []


def test_no_caller_can_obtain_aws_credentials(template):
    assert _of_type(template, "AWS::IAM::Role")  # reach
    assert credential_path_violations(template) == []


def test_the_artifacts_bucket_policy_grants_no_caller_anything(template):
    assert bucket_policy_violations(template) == []


@pytest.mark.parametrize("accounts", ["111111111111,222222222222", "111111111111"])
def test_the_legacy_cross_account_context_is_refused_not_ignored(accounts):
    # The key is snake_case on purpose: a camelCase key would be ignored and prove nothing.
    with pytest.raises(ValueError, match="deploy_target_accounts is no longer used"):
        _synth({"deploy_target_accounts": accounts})


def test_every_lambda_ingress_is_iam_or_a_pinned_service(template):
    assert len(_of_type(template, "AWS::Lambda::Url")) == 1  # reach: the stream Function URL
    assert _of_type(template, "AWS::Lambda::Permission")  # reach
    assert lambda_ingress_violations(template) == []


def test_the_artifacts_bucket_has_no_public_exposure_path(template):
    assert public_exposure_violations(template) == []


def test_the_api_stage_has_no_access_log_for_a_response_to_reach(template):
    stages = _of_type(template, "AWS::ApiGatewayV2::Stage")
    assert len(stages) == 1
    # If an access log is ever added, its format must be reviewed for response content,
    # and this test updated to pin that format instead.
    assert "AccessLogSettings" not in next(iter(stages.values()))["Properties"]


def test_the_artifacts_bucket_access_log_destination_is_exactly_known(template):
    bucket = template["Resources"][_artifacts_bucket_id(template)]["Properties"]
    logging_ids = [lid for lid in _of_type(template, "AWS::S3::Bucket") if lid.startswith("LoggingBucket")]
    assert len(logging_ids) == 1, logging_ids
    assert bucket["LoggingConfiguration"] == {
        "DestinationBucketName": {"Ref": logging_ids[0]},
        "LogFilePrefix": "s3-artifacts/",
    }


def _first(template: dict, rtype: str, prefix: str = "") -> dict:
    return next(r for lid, r in _of_type(template, rtype).items() if lid.startswith(prefix))


def test_positive_control_an_unauthenticated_export_route_is_caught(template):
    t = copy.deepcopy(template)
    route = next(
        r
        for r in _of_type(t, "AWS::ApiGatewayV2::Route").values()
        if r["Properties"]["RouteKey"] == "POST /api/export-python"
    )
    route["Properties"]["AuthorizationType"] = "NONE"
    route["Properties"].pop("AuthorizerId")
    t["Resources"]["RogueDefault"] = {"Type": "AWS::ApiGatewayV2::Route", "Properties": {"RouteKey": "$default"}}
    assert route_auth_violations(t) == ["$default", "POST /api/export-python"]


@pytest.mark.parametrize(
    "principal",
    [{"Federated": "cognito-identity.amazonaws.com"}, {"AWS": "arn:aws:iam::123456789012:root"}, "*"],
)
def test_positive_control_a_caller_assumable_role_is_caught(template, principal):
    t = copy.deepcopy(template)
    role = _first(t, "AWS::IAM::Role", "DeploymentLambdaRole")
    role["Properties"]["AssumeRolePolicyDocument"]["Statement"].append(
        {"Effect": "Allow", "Principal": principal, "Action": "sts:AssumeRoleWithWebIdentity"}
    )
    assert len(credential_path_violations(t)) == 1


def test_positive_control_an_identity_pool_is_caught(template):
    t = copy.deepcopy(template)
    t["Resources"]["RoguePool"] = {"Type": "AWS::Cognito::IdentityPool", "Properties": {}}
    assert credential_path_violations(t) == ["identity pool RoguePool"]


@pytest.mark.parametrize("principal", [{"AWS": "arn:aws:iam::123456789012:root"}, {"AWS": "*"}, "*"])
def test_positive_control_a_caller_grant_in_the_bucket_policy_is_caught(template, principal):
    t = copy.deepcopy(template)
    bucket_id = _artifacts_bucket_id(t)
    policy = next(
        r for r in _of_type(t, "AWS::S3::BucketPolicy").values() if r["Properties"]["Bucket"] == {"Ref": bucket_id}
    )
    policy["Properties"]["PolicyDocument"]["Statement"].append(
        {"Sid": "Rogue", "Effect": "Allow", "Principal": principal, "Action": ["s3:GetObject", "s3:ListBucket"]}
    )
    assert bucket_policy_violations(t) == [f"Rogue allows {principal}"]


# The grant this stack used to add under -c deploy_target_accounts, verbatim from the synth.
_LEGACY_CROSS_ACCOUNT_GRANT = {
    "Sid": "CrossAccountDeployRoleArtifacts",
    "Effect": "Allow",
    "Principal": {
        "AWS": [
            f"arn:aws:iam::{a}:role/{r}"
            for a in ("111111111111", "222222222222")
            for r in ("AgentCoreFlowsDeploymentRole", "AgentCoreFlowsRuntimeRole")
        ]
    },
    "Action": ["s3:GetObject", "s3:PutObject"],
    "Resource": ["deployments/*", "agentcore-deps/*"],
}


def test_positive_control_the_removed_cross_account_grant_is_caught(template):
    t = copy.deepcopy(template)
    bucket_id = _artifacts_bucket_id(t)
    policy = next(
        r for r in _of_type(t, "AWS::S3::BucketPolicy").values() if r["Properties"]["Bucket"] == {"Ref": bucket_id}
    )
    policy["Properties"]["PolicyDocument"]["Statement"].append(copy.deepcopy(_LEGACY_CROSS_ACCOUNT_GRANT))
    assert bucket_policy_violations(t) == [
        f"CrossAccountDeployRoleArtifacts allows {_LEGACY_CROSS_ACCOUNT_GRANT['Principal']}"
    ]


def test_positive_control_a_public_function_url_is_caught(template):
    t = copy.deepcopy(template)
    next(iter(_of_type(t, "AWS::Lambda::Url").values()))["Properties"]["AuthType"] = "NONE"
    t["Resources"]["RoguePublicInvoke"] = {
        "Type": "AWS::Lambda::Permission",
        "Properties": {
            "Action": "lambda:InvokeFunctionUrl",
            "FunctionName": "x",
            "Principal": "*",
            "FunctionUrlAuthType": "NONE",
        },
    }
    t["Resources"]["RogueAccountInvoke"] = {
        "Type": "AWS::Lambda::Permission",
        "Properties": {"Action": "lambda:InvokeFunction", "FunctionName": "x", "Principal": "123456789012"},
    }
    t["Resources"]["RogueUnscopedService"] = {
        "Type": "AWS::Lambda::Permission",
        "Properties": {"Action": "lambda:InvokeFunction", "FunctionName": "x", "Principal": "apigateway.amazonaws.com"},
    }
    got = lambda_ingress_violations(t)
    assert [v.split(" ")[:2] for v in got] == [
        ["function", "url"],
        ["permission", "RoguePublicInvoke"],
        ["permission", "RoguePublicInvoke"],
        ["permission", "RogueAccountInvoke"],
        ["permission", "RogueUnscopedService"],
    ], got


@pytest.mark.parametrize("flag", sorted(_PAB))
def test_positive_control_a_cleared_public_access_block_flag_is_caught(template, flag):
    t = copy.deepcopy(template)
    t["Resources"][_artifacts_bucket_id(t)]["Properties"]["PublicAccessBlockConfiguration"][flag] = False
    assert len(public_exposure_violations(t)) == 1


@pytest.mark.parametrize("rtype", _ACCESS_POINT_TYPES)
def test_positive_control_an_access_point_or_website_is_caught(template, rtype):
    t = copy.deepcopy(template)
    t["Resources"]["RogueAp"] = {"Type": rtype, "Properties": {}}
    t["Resources"][_artifacts_bucket_id(t)]["Properties"]["WebsiteConfiguration"] = {"IndexDocument": "i.html"}
    assert public_exposure_violations(t) == ["website configuration", f"{rtype} RogueAp"]
