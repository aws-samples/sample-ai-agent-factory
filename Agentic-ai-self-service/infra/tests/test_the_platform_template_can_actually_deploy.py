"""The platform's OWN synthesized template must be a template CloudFormation accepts.

The customer export has had this guard since a shipped bundle turned out to be
undeployable (``backend/tests/test_the_exported_template_can_actually_deploy.py``). The
platform's own CDK output never did, on the reasonable-sounding assumption that CDK only
emits valid CloudFormation. That assumption held right up until something reached for a
``CfnResource`` escape hatch.

MEASURED, on a real deploy of ``acfe2e-p0920``::

    acfe2e-p0920: creating CloudFormation changeset...
    Template format error: Unrecognized resource types:
    [AWS::Route53Resolver::FirewallConfig]

The DNS-firewall work needed ``FirewallFailOpen: DISABLED``, found that
``aws_route53resolver`` has no ``CfnFirewallConfig`` class, read that as a gap in the CDK
bindings, and wrote the resource by hand. The type does not exist in CloudFormation at
all -- a firewall config is created implicitly with the VPC and is only reachable through
the ``UpdateFirewallConfig`` API. A missing L1 class was evidence about CloudFormation's
surface, not an invitation to route around it.

Five template-shape tests passed against that template, including one asserting
``FirewallFailOpen == "DISABLED"`` on the resource. Every one of them was reading a
resource CloudFormation would refuse to load. That is the gap this file closes: asserting
things *about* a template never asks whether the template is loadable, and an escape hatch
is exactly where CDK stops answering that question for us.

The oracle is ``E``-level cfn-lint findings, which is the class that stops a deploy.
``W``-level findings are deliberately not failed on, because the two kinds present here
are both false:

* ``W3037`` on ``bedrock:Converse`` and ``bedrock-agentcore:InvokeBrowser`` --
  cfn-lint's bundled action list is behind those services. The AWS Service Reference feed
  is the oracle for them.
* ``W3005`` on a redundant ``DependsOn`` that CDK generates itself for the
  ``AwsCustomResource`` singleton Lambda, which this repo cannot edit.

Failing on ``W`` would mean either a permanently red suite or suppressing real findings to
silence false ones. One region, for the reason the export test gives: ``lint_all`` walks
every region cfn-lint knows, AgentCore does not exist in about twenty of them, and the
resulting E3006 flood says nothing about the template.
"""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

REGION = "us-east-1"
ACCOUNT = "123456789012"


@pytest.fixture(scope="module")
def template_body() -> str:
    """The synthesized platform template, as the JSON CloudFormation would receive."""
    app = cdk.App()
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name="test",
        project_name="acf",
        env=cdk.Environment(region=REGION, account=ACCOUNT),
    )
    return json.dumps(Template.from_stack(stack).to_json())


def _errors(body: str) -> list[str]:
    lint = pytest.importorskip("cfnlint.api", reason="cfn-lint not installed")
    return [str(m) for m in lint.lint(body, regions=[REGION]) if str(m.rule.id).startswith("E")]


def test_the_platform_template_has_no_error_level_findings(template_body):
    errors = _errors(template_body)
    assert not errors, f"the platform template has CloudFormation errors: {errors}"


def test_every_resource_type_is_one_cloudformation_knows(template_body):
    """The specific failure, named.

    ``E3006`` is the rule CloudFormation's *"Unrecognized resource types"* corresponds to,
    and it is worth its own assertion rather than being folded into the sweep above: a
    hand-written ``CfnResource`` is the one construct in CDK that can invent a resource
    type, and this is the only thing standing between that and a rejected changeset.
    """
    lint = pytest.importorskip("cfnlint.api", reason="cfn-lint not installed")
    unknown = [str(m) for m in lint.lint(template_body, regions=[REGION]) if str(m.rule.id) == "E3006"]
    assert not unknown, f"resource types CloudFormation does not know: {unknown}"


def test_cfn_lint_would_object_to_an_invented_resource_type(template_body):
    """The positive control, reproducing the real defect.

    A clean run above only means something if the validator objects to the thing that was
    actually shipped. So inject the exact resource that CloudFormation rejected and require
    ``E3006`` back. Without this, deleting cfn-lint from the environment would turn both
    tests above green via ``importorskip`` and nothing would notice.
    """
    lint = pytest.importorskip("cfnlint.api", reason="cfn-lint not installed")
    template = json.loads(template_body)
    template["Resources"]["GhostFirewallConfig"] = {
        "Type": "AWS::Route53Resolver::FirewallConfig",
        "Properties": {"ResourceId": "vpc-0123456789abcdef0", "FirewallFailOpen": "DISABLED"},
    }
    found = [str(m) for m in lint.lint(json.dumps(template), regions=[REGION]) if str(m.rule.id) == "E3006"]
    assert found, (
        "cfn-lint did not object to AWS::Route53Resolver::FirewallConfig, the exact type "
        "CloudFormation rejected with 'Unrecognized resource types' -- so a clean run "
        "proves nothing about whether the platform template can deploy"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
