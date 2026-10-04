"""The emitted template must be a template CloudFormation will accept.

Every other test on this generator asserts something about the *content* of the template:
that a field is honoured, that a tag is present, that a policy is scoped. None of them
asked the prior question -- whether the artifact is even loadable -- and one component
combination shipped a template that was not.

MEASURED, on a live export from ``acfe2e-p0920``. A canvas with ``mcpServerConfig`` and no
gateway returned HTTP 200 and a downloadable bundle whose ``template.yaml`` drew three
cfn-lint errors:

    E6101  Outputs.McpServerRuntimeId    GetAtt McpServerRuntime      -- not a resource
    E1010  IAM policy statement          GetAtt McpCognitoUserPool    -- not a resource
    E3005  AgentCoreRuntime.DependsOn    McpServerGatewayTarget       -- not a resource

The cause is two different conditions for one feature: the MCP resources are emitted under
``has_mcp_server and has_gateway``, while four reference sites are gated on
``has_mcp_server`` alone. CloudFormation rejects that at validate time, so the customer
downloaded a bundle that could not deploy at all -- and nothing objected, because no test
in the repo had ever linted the artifact we ship. That canvas is now refused with a reason;
this file is the guard that stops the next one.

TWO INDEPENDENT ORACLES, on purpose.

1. ``TestNoTemplateReferencesAResourceItNeverCreated`` resolves every ``Ref``, ``GetAtt``
   and ``DependsOn`` against the template's own Resources, Parameters and the CloudFormation
   pseudo-parameters. It needs no external tool, so it runs everywhere and cannot be
   skipped away.
2. ``TestCfnLintAcceptsEveryCombination`` runs the real validator, which knows things the
   first cannot: property types, required properties, tag shapes, return-value validity.

Each has a positive control, because a checker that finds nothing is indistinguishable from
a checker that checks nothing.

cfn-lint is scoped to ONE region deliberately. ``lint_all`` checks every region cfn-lint
knows and AgentCore does not exist in about twenty of them, which yields ~60 E3006
"resource type does not exist" errors that say nothing about the template. That the export
is deployable only where AgentCore exists is true, and is not what this file measures.
"""

from __future__ import annotations

import re

import pytest
import yaml
from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services.cfn_template_generator import CfnExportUnsupportedError, CfnTemplateGenerator

#: Pseudo-parameters are always resolvable and are never declared in the template.
_PSEUDO = {
    "AWS::AccountId",
    "AWS::NotificationARNs",
    "AWS::NoValue",
    "AWS::Partition",
    "AWS::Region",
    "AWS::StackId",
    "AWS::StackName",
    "AWS::URLSuffix",
}

_AGENTCORE_GATEWAY = {"enabled": True, "name": "gw"}

#: Every component combination reachable from the deploy panel, including the ones that
#: only exist as a *combination* -- which is where the shipped defect lived.
_COMBINATIONS = {
    "minimal": {},
    "gateway": {"gateway_config": _AGENTCORE_GATEWAY},
    "memory": {"memory_config": {"enabled": True}},
    "gateway+memory": {"gateway_config": _AGENTCORE_GATEWAY, "memory_config": {"enabled": True}},
    "gateway+mcp": {
        "gateway_config": _AGENTCORE_GATEWAY,
        "mcp_server_config": {"enabled": True, "name": "m"},
    },
    "mcp-template-id": {"template_id": "mcp-server-gateway-target", "gateway_config": _AGENTCORE_GATEWAY},
    "everything": {
        "gateway_config": _AGENTCORE_GATEWAY,
        "memory_config": {"enabled": True},
        "mcp_server_config": {"enabled": True, "name": "m"},
    },
    "everything+tags": {
        "gateway_config": _AGENTCORE_GATEWAY,
        "memory_config": {"enabled": True},
        "mcp_server_config": {"enabled": True, "name": "m"},
        "resource_tags": {"platform:application": "acf", "CostCentre": "ECB-42"},
    },
}


def _template(**overrides) -> dict:
    request = DeployRequest(
        node_id="n1",
        config=RuntimeConfig(
            name="agent",
            model={"model_id": "us.anthropic.claude-sonnet-5"},
            system_prompt="hi",
            entrypoint="agent.py",
        ),
        **overrides,
    )
    return yaml.safe_load(CfnTemplateGenerator().generate(request).template_yaml)


def _walk(node):
    """Yield every ``(kind, target)`` reference in the template.

    ``Fn::Sub`` is included because ``${Thing}`` inside a Sub string is a reference just as
    much as a ``Ref`` is, and the shipped defect had one: the MCP OIDC discovery URL
    interpolated ``${McpCognitoUserPool}``.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Ref" and isinstance(value, str):
                yield "Ref", value
            elif key == "Fn::GetAtt":
                target = value[0] if isinstance(value, list) and value else value
                if isinstance(target, str):
                    yield "GetAtt", target.split(".")[0]
            elif key == "DependsOn":
                for dep in value if isinstance(value, list) else [value]:
                    if isinstance(dep, str):
                        yield "DependsOn", dep
            elif key == "Fn::Sub":
                text = value[0] if isinstance(value, list) and value else value
                local = set(value[1]) if isinstance(value, list) and len(value) > 1 else set()
                if isinstance(text, str):
                    for name in re.findall(r"\$\{([^}!][^}]*)\}", text):
                        base = name.split(".")[0].strip()
                        if base not in local:
                            yield "Sub", base
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _dangling(template: dict) -> list[str]:
    known = set(template.get("Resources") or {}) | set(template.get("Parameters") or {}) | _PSEUDO
    seen = {}
    for kind, target in _walk(template):
        if target not in known:
            seen.setdefault(target, kind)
    return [f"{target} (via {kind})" for target, kind in sorted(seen.items())]


@pytest.mark.parametrize("label", sorted(_COMBINATIONS))
class TestNoTemplateReferencesAResourceItNeverCreated:
    def test_every_reference_resolves(self, label):
        dangling = _dangling(_template(**_COMBINATIONS[label]))
        assert not dangling, (
            f"the {label!r} export references {dangling}, which the template does not "
            f"create or declare. CloudFormation rejects this at validate time, so the "
            f"customer downloads a bundle that cannot deploy at all. This is what two "
            f"different conditions for one feature looks like: the resource is emitted "
            f"under one boolean and referenced under another."
        )


class TestTheDanglingCheckReallyChecks:
    """Positive control. Without this, a bug in ``_walk`` makes every test above vacuous."""

    @pytest.mark.parametrize(
        "injector",
        [
            pytest.param(
                lambda t: (
                    t["Resources"]["AgentCoreRuntime"].setdefault("DependsOn", []).append("Ghost")
                    if isinstance(t["Resources"]["AgentCoreRuntime"].get("DependsOn"), list)
                    else t["Resources"]["AgentCoreRuntime"].update({"DependsOn": ["Ghost"]})
                ),
                id="DependsOn",
            ),
            pytest.param(lambda t: t.setdefault("Outputs", {}).update({"X": {"Value": {"Ref": "Ghost"}}}), id="Ref"),
            pytest.param(
                lambda t: t.setdefault("Outputs", {}).update({"X": {"Value": {"Fn::GetAtt": ["Ghost", "Arn"]}}}),
                id="GetAtt",
            ),
            pytest.param(
                lambda t: t.setdefault("Outputs", {}).update({"X": {"Value": {"Fn::Sub": "a-${Ghost}-b"}}}), id="Sub"
            ),
        ],
    )
    def test_an_injected_bad_reference_is_found(self, injector):
        template = _template()
        assert not _dangling(template), "the baseline template is already broken"
        injector(template)
        assert any(d.startswith("Ghost") for d in _dangling(template)), (
            "a reference to a resource that does not exist was NOT detected, so every "
            "assertion in this file passes for the wrong reason"
        )

    def test_a_sub_with_a_local_variable_is_not_a_false_positive(self):
        """``Fn::Sub`` with a second element declares its own names; treating those as
        references would flag a correct template and the suite would be muted to stop it."""
        template = _template()
        template.setdefault("Outputs", {})["X"] = {"Value": {"Fn::Sub": ["a-${Local}-b", {"Local": "v"}]}}
        assert not _dangling(template)


class TestAnMcpNodeWithNoGatewayIsRefused:
    """The defect this file was written for, pinned at its source."""

    def test_it_is_refused_with_an_actionable_reason(self):
        with pytest.raises(CfnExportUnsupportedError) as err:
            _template(mcp_server_config={"enabled": True, "name": "m"})
        message = str(err.value)
        assert "MCP Server" in message and "gateway" in message.lower(), message

    def test_with_a_gateway_it_still_exports(self):
        """Vacuity guard: the refusal must not be 'MCP never works'."""
        resources = _template(
            gateway_config=_AGENTCORE_GATEWAY,
            mcp_server_config={"enabled": True, "name": "m"},
        )["Resources"]
        for logical_id in ("McpServerRuntime", "McpCognitoUserPool", "McpServerGatewayTarget"):
            assert logical_id in resources, f"{logical_id} missing; the MCP path itself is now broken"


@pytest.mark.parametrize("label", sorted(_COMBINATIONS))
class TestCfnLintAcceptsEveryCombination:
    def test_no_error_level_findings(self, label):
        lint = pytest.importorskip("cfnlint.api", reason="cfn-lint not installed")
        template = _template(**_COMBINATIONS[label])
        found = lint.lint(yaml.safe_dump(template), regions=["us-east-1"])
        errors = [str(m) for m in found if str(m.rule.id).startswith("E")]
        assert not errors, f"the {label!r} export has CloudFormation errors: {errors}"


def test_cfn_lint_would_object_to_a_broken_template():
    """The other positive control: the clean runs above only mean something if the
    validator is actually validating. Reproduces the shipped defect exactly -- a reference
    to a resource that is not there."""
    lint = pytest.importorskip("cfnlint.api", reason="cfn-lint not installed")
    template = _template(gateway_config=_AGENTCORE_GATEWAY)
    template.setdefault("Outputs", {})["Ghost"] = {"Value": {"Fn::GetAtt": ["NotAResource", "Arn"]}}
    found = lint.lint(yaml.safe_dump(template), regions=["us-east-1"])
    assert [m for m in found if str(m.rule.id).startswith("E")], (
        "cfn-lint did not object to a GetAtt on a resource that does not exist, so a clean "
        "run proves nothing about whether the emitted template can deploy"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
