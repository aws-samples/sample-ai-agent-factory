"""Region-agnostic deployment: WAF scope and account-global resource naming.

The stack must synthesize for any region, not just us-east-1. Two things vary:

  * **WAF scope.** A CloudFront distribution accepts ONLY a ``scope=CLOUDFRONT``
    WebACL and AWS creates those exclusively in us-east-1. Outside us-east-1 the
    same rule set is applied REGIONALly to the Cognito user pool instead.
  * **Account-global names.** IAM roles and CloudFront resources share one
    account-wide namespace, so a second region has to qualify its names or the
    deploy collides with an existing us-east-1 deployment.

us-east-1 is the incumbent and must be byte-for-byte unchanged — several of
these tests exist specifically to pin that.
"""

import aws_cdk as cdk
import pytest
from stacks.platform_stack import PlatformStack

HOME_REGION = "us-east-1"
OTHER_REGION = "eu-central-1"


def _synth(region: str) -> dict:
    app = cdk.App()
    PlatformStack(
        app,
        "TestStack",
        environment_name="test",
        project_name="agentcore-workflow",
        env=cdk.Environment(region=region, account="123456789012"),
    )
    return app.synth().get_stack_by_name("TestStack").template


@pytest.fixture(scope="module")
def home_template() -> dict:
    return _synth(HOME_REGION)


@pytest.fixture(scope="module")
def other_template() -> dict:
    return _synth(OTHER_REGION)


def _resources(template: dict, res_type: str) -> dict:
    return {lid: r for lid, r in template["Resources"].items() if r["Type"] == res_type}


def _only(template: dict, res_type: str) -> dict:
    found = _resources(template, res_type)
    assert len(found) == 1, f"expected exactly one {res_type}, found {sorted(found)}"
    return next(iter(found.values()))


def _distribution_config(template: dict) -> dict:
    return _only(template, "AWS::CloudFront::Distribution")["Properties"]["DistributionConfig"]


# ---------------------------------------------------------------
# WAF — scope follows the region
# ---------------------------------------------------------------


class TestWafScope:
    """First-ever assertions on the WAF; it was previously untested entirely."""

    def test_home_region_web_acl_is_cloudfront_scoped(self, home_template):
        acl = _only(home_template, "AWS::WAFv2::WebACL")
        assert acl["Properties"]["Scope"] == "CLOUDFRONT"
        assert acl["Properties"]["Name"] == "agentcore-workflow-test-cloudfront-waf"

    def test_home_region_attaches_web_acl_to_the_distribution(self, home_template):
        acl_logical_id = next(iter(_resources(home_template, "AWS::WAFv2::WebACL")))
        assert acl_logical_id == "CloudFrontWebACL", (
            "logical ID is load-bearing — the us-east-1 stack is deployed live and a rename would replace the WebACL"
        )
        assert _distribution_config(home_template)["WebACLId"] == {"Fn::GetAtt": [acl_logical_id, "Arn"]}

    def test_home_region_has_no_web_acl_association(self, home_template):
        """CLOUDFRONT ACLs attach via the distribution, never via an association."""
        assert _resources(home_template, "AWS::WAFv2::WebACLAssociation") == {}

    def test_other_region_web_acl_is_regional(self, other_template):
        acl = _only(other_template, "AWS::WAFv2::WebACL")
        assert acl["Properties"]["Scope"] == "REGIONAL"
        assert acl["Properties"]["Name"] == "agentcore-workflow-test-regional-waf"

    def test_other_region_associates_the_web_acl_with_the_user_pool(self, other_template):
        assoc = _only(other_template, "AWS::WAFv2::WebACLAssociation")["Properties"]
        acl_logical_id = next(iter(_resources(other_template, "AWS::WAFv2::WebACL")))
        pool_logical_id = next(iter(_resources(other_template, "AWS::Cognito::UserPool")))
        assert assoc["WebACLArn"] == {"Fn::GetAtt": [acl_logical_id, "Arn"]}
        assert assoc["ResourceArn"] == {"Fn::GetAtt": [pool_logical_id, "Arn"]}

    def test_other_region_distribution_has_no_web_acl(self, other_template):
        """CloudFront rejects a REGIONAL ACL, so there is nothing to attach."""
        assert _distribution_config(other_template).get("WebACLId") is None

    @pytest.mark.parametrize("region", [HOME_REGION, OTHER_REGION])
    def test_rule_set_is_identical_in_both_scopes(self, region, home_template, other_template):
        """Region changes the scope and the attachment point, never the rules."""
        template = home_template if region == HOME_REGION else other_template
        rules = _only(template, "AWS::WAFv2::WebACL")["Properties"]["Rules"]
        assert [r["Name"] for r in rules] == [
            "AWSManagedRulesCommonRuleSet",
            "AWSManagedRulesKnownBadInputsRuleSet",
            "RateLimitRule",
        ]
        rate_limit = next(r for r in rules if r["Name"] == "RateLimitRule")
        assert rate_limit["Statement"]["RateBasedStatement"]["Limit"] == 2000

    def test_supplied_cloudfront_acl_arn_is_attached_outside_us_east_1(self):
        """The escape hatch: bring your own us-east-1 edge ACL."""
        arn = "arn:aws:wafv2:us-east-1:123456789012:global/webacl/byo/abc"
        app = cdk.App(context={"cloudfront_web_acl_arn": arn})
        PlatformStack(
            app,
            "TestStack",
            environment_name="test",
            project_name="agentcore-workflow",
            env=cdk.Environment(region=OTHER_REGION, account="123456789012"),
        )
        template = app.synth().get_stack_by_name("TestStack").template
        assert _distribution_config(template)["WebACLId"] == arn
        # The in-region REGIONAL ACL still exists and still guards Cognito.
        assert _only(template, "AWS::WAFv2::WebACL")["Properties"]["Scope"] == "REGIONAL"


# ---------------------------------------------------------------
# Account-global names — region-qualified only outside us-east-1
# ---------------------------------------------------------------


class TestAccountGlobalNames:
    """These names live in one account-wide namespace, so two regional
    deployments in the same account collide unless the region is in the name.

    us-east-1 keeps the legacy un-qualified name on purpose: it is deployed
    live, and renaming would replace the resource for no benefit.
    """

    def _role_names(self, template: dict) -> set[str]:
        return {
            r["Properties"]["RoleName"]
            for r in _resources(template, "AWS::IAM::Role").values()
            if r["Properties"].get("RoleName")
        }

    def _rhp_name(self, template: dict) -> str:
        rhp = _only(template, "AWS::CloudFront::ResponseHeadersPolicy")
        return rhp["Properties"]["ResponseHeadersPolicyConfig"]["Name"]

    def _oac_name(self, template: dict) -> str:
        oac = _only(template, "AWS::CloudFront::OriginAccessControl")
        return oac["Properties"]["OriginAccessControlConfig"]["Name"]

    def test_home_region_names_are_unchanged(self, home_template):
        assert self._role_names(home_template) == {"AgentCoreRuntime-agentcore-workflow-test-shared"}
        assert self._rhp_name(home_template) == "agentcore-workflow-test-security-headers"

    def test_other_region_qualifies_the_shared_runtime_role(self, other_template):
        assert self._role_names(other_template) == {f"AgentCoreRuntime-agentcore-workflow-test-{OTHER_REGION}-shared"}

    def test_other_region_qualifies_the_response_headers_policy(self, other_template):
        assert self._rhp_name(other_template) == f"agentcore-workflow-test-{OTHER_REGION}-security-headers"

    def test_origin_access_control_names_differ_across_regions(self, home_template, other_template):
        """CDK's generated OAC name embeds no region, so the second region must
        supply its own or the account-global name collides."""
        assert self._oac_name(home_template) != self._oac_name(other_template)
        assert OTHER_REGION in self._oac_name(other_template)

    def test_home_region_origin_access_control_logical_id_is_unchanged(self, home_template):
        """Pinning the CDK-generated construct path: introducing an explicit OAC
        in us-east-1 would replace the live one."""
        assert list(_resources(home_template, "AWS::CloudFront::OriginAccessControl")) == [
            "FrontendDistributionOrigin1S3OriginAccessControl51A3EFC6"
        ]

    def test_cloudfront_function_name_already_carries_the_region(self, home_template, other_template):
        """CDK auto-generates this one WITH the region, so we deliberately do not
        set it — asserted so a future CDK change that drops the region is caught."""
        home = _only(home_template, "AWS::CloudFront::Function")["Properties"]["Name"]
        other = _only(other_template, "AWS::CloudFront::Function")["Properties"]["Name"]
        assert HOME_REGION in home
        assert home != other

    def test_regional_namespaces_are_not_region_qualified(self, other_template):
        """Lambda/DynamoDB/state-machine names live in per-region namespaces.
        Adding the region there would be churn with no collision to fix."""
        fn_names = {
            r["Properties"]["FunctionName"]
            for r in _resources(other_template, "AWS::Lambda::Function").values()
            if r["Properties"].get("FunctionName")
        }
        assert "agentcore-workflow-test-deployment" in fn_names
        assert not any(OTHER_REGION in n for n in fn_names)

        table_names = {
            r["Properties"]["TableName"]
            for r in _resources(other_template, "AWS::DynamoDB::Table").values()
            if r["Properties"].get("TableName")
        }
        assert "agentcore-workflow-test-deployments" in table_names
        assert not any(OTHER_REGION in n for n in table_names)


# ---------------------------------------------------------------
# Region threading
# ---------------------------------------------------------------


class TestRegionThreading:
    def test_lambdas_receive_the_deployment_region(self, other_template):
        for r in _resources(other_template, "AWS::Lambda::Function").values():
            env = r["Properties"].get("Environment", {}).get("Variables", {})
            if "APP_AWS_REGION" in env:
                assert env["APP_AWS_REGION"] == OTHER_REGION

    def test_tool_generator_model_uses_the_regional_inference_prefix(self, other_template):
        """A ``us.`` inference profile does not exist in eu-central-1 — the tool
        generator would fail on every invoke."""
        models = {
            r["Properties"]["Environment"]["Variables"]["TOOL_GENERATOR_MODEL_ID"]
            for r in _resources(other_template, "AWS::Lambda::Function").values()
            if "TOOL_GENERATOR_MODEL_ID" in r["Properties"].get("Environment", {}).get("Variables", {})
        }
        assert models
        assert all(m.startswith("eu.") for m in models), models

    def test_csp_names_the_deployment_regions_cognito_endpoint(self, other_template):
        rhp = _only(other_template, "AWS::CloudFront::ResponseHeadersPolicy")
        csp = rhp["Properties"]["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"]["ContentSecurityPolicy"][
            "ContentSecurityPolicy"
        ]
        assert f"https://cognito-idp.{OTHER_REGION}.amazonaws.com" in csp
        assert f"cognito-idp.{HOME_REGION}.amazonaws.com" not in csp
