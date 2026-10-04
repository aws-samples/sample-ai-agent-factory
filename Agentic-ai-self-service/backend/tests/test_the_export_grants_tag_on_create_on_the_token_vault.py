"""The exported provider role may tag a credential provider it creates: container AND child.

A tagged ``CreateOauth2CredentialProvider`` is authorized for ``bedrock-agentcore:TagResource``
twice, once on the ``token-vault/default`` CONTAINER and once on the provider itself. The
platform measured that for its own roles on 2026-09-22 and records it in
``AGENTCORE_TYPE_ARN_TAIL`` (infra/stacks/platform/step_lambdas.py). The exported stack's
``CfnProviderRole`` kept the child ARN alone. Measured live 2026-10-01 (Stage 70, a governed
mcp-server-gateway-target export deployed by its own deploy.sh): the
``McpOAuth2CredentialProvider`` custom resource failed with AccessDeniedException two seconds
in, and CloudTrail showed ``CreateOauth2CredentialProvider`` denied to ``CfnProviderRole`` with
the governance tags in the request. The stack rolled back.

The same day a live oracle settled it. Two throwaway roles were built from the generator's
rendered statements and each called the create WITH tags. The role narrowed to the child ARN
was refused ("not authorized to perform: bedrock-agentcore:TagResource on resource:
...:token-vault/default"). The role exactly as rendered created the provider, which was then
deleted.
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml
from app.models.deployment_models import DeployRequest
from app.services.cfn_template_generator import CfnTemplateGenerator

STEP_LAMBDAS = Path(__file__).resolve().parents[2] / "infra" / "stacks" / "platform" / "step_lambdas.py"
PREFIX = "arn:${AWS::Partition}:bedrock-agentcore:${AWS::Region}:${AWS::AccountId}:"


def _gateway_target_template() -> dict:
    bundle = CfnTemplateGenerator().generate(
        DeployRequest.model_validate(
            {
                "nodeId": "tag-on-create",
                "config": {
                    "name": "tag_on_create",
                    "model": {"modelId": "us.anthropic.claude-sonnet-5"},
                    "modelProvider": "bedrock",
                    "protocol": "HTTP",
                    "enableOtel": False,
                },
                "templateId": "mcp-server-gateway-target",
                "gatewayConfig": {"gateway_provider": "agentcore", "targetType": "lambda"},
                "mcpServerConfig": {"name": "order_tools", "tools": []},
                "resourceTags": {"cost-center": "cc-1", "owner": "platform-team"},
            }
        )
    )
    return yaml.load(bundle.template_yaml, Loader=yaml.BaseLoader)


def _provider_tag_statement(template: dict) -> dict:
    assert "McpOAuth2CredentialProvider" in template["Resources"], "the canvas no longer emits the provider resource"
    statements = []
    for policy in template["Resources"]["CfnProviderRole"]["Properties"]["Policies"]:
        if "PolicyDocument" not in policy:  # an Fn::If-wrapped optional policy
            branch = (policy.get("Fn::If") or [None, {}])[1]
            policy = branch if isinstance(branch, dict) else {}
        statements += [
            st
            for st in (policy.get("PolicyDocument") or {}).get("Statement", [])
            if st.get("Sid") == "OAuth2CredentialProviderTags"
        ]
    assert len(statements) == 1, statements
    return statements[0]


def _tails(resources) -> set[str]:
    out = set()
    for resource in resources if isinstance(resources, list) else [resources]:
        value = resource["Fn::Sub"] if isinstance(resource, dict) else resource
        assert value.startswith(PREFIX), value
        out.add(value[len(PREFIX) :])
    return out


def _platform_tails(kind: str) -> set[str]:
    """The platform's measured table, read from source: importing the module would need CDK."""
    tree = ast.parse(STEP_LAMBDAS.read_text())
    for node in tree.body:
        targets = [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
        if any(isinstance(t, ast.Name) and t.id == "AGENTCORE_TYPE_ARN_TAIL" for t in targets):
            return set(ast.literal_eval(node.value)[kind])
    raise AssertionError("AGENTCORE_TYPE_ARN_TAIL not found in step_lambdas.py")


def test_the_provider_tag_grant_names_the_container_and_the_child():
    statement = _provider_tag_statement(_gateway_target_template())

    assert "bedrock-agentcore:TagResource" in statement["Action"]
    assert _tails(statement["Resource"]) == {
        "token-vault/default",
        "token-vault/default/oauth2credentialprovider/*",
    }


def test_the_export_and_the_platform_grant_the_same_two_shapes():
    statement = _provider_tag_statement(_gateway_target_template())

    assert _tails(statement["Resource"]) == _platform_tails("oauth2credentialprovider")
