"""Customer naming-profile contract for the exported CloudFormation bundle.

The profile is deliberately tested as an artifact contract rather than as a helper:
resource names, IAM grants, OAuth scopes, endpoint outputs and log-group qualifiers
must move together. A green stack whose role still names the legacy prefix is a
runtime failure, not a successful naming feature.
"""

import json
import re
import subprocess
import tempfile
import zipfile
from base64 import b64decode
from io import BytesIO
from pathlib import Path

import pytest
import yaml
from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services.cfn_template_generator import (
    _NAMING_PROFILE_ALLOWED_FIELDS,
    _NAMING_PROFILE_RULES,
    _NAMING_PROFILE_TEMPLATES,
    _NAMING_PROPERTY_FAMILIES,
    _NAMING_ROLE_KEYS,
    CfnExportUnsupportedError,
    CfnTemplateGenerator,
    _explicit_role_stack_name_limit,
    _legacy_deployment_name_limit,
    _name_expression_templates,
    _render_name_limit_sample,
    content_digest,
)
from fastapi.testclient import TestClient
from pydantic import ValidationError

MODEL_ID = "us.anthropic.claude-sonnet-5"
KB_CONFIG = {
    "kbMode": "create_new",
    "kbName": "kb",
    "embeddingModelId": "amazon.titan-embed-text-v2:0",
    "vectorStoreType": "s3_vectors",
    "dataSourceType": "s3",
    "s3BucketUri": "s3://example-bucket/docs",
}
VALID_POLICY_STATEMENT = (
    "permit(principal is AgentCore::OAuthUser, action in "
    '[AgentCore::Action::"DynamicTools___get_customer"], '
    'resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:'
    'us-east-1:123456789012:gateway/example");'
)


def _request(**kwargs) -> DeployRequest:
    kwargs.setdefault("nodeId", "node-1")
    kwargs.setdefault("config", RuntimeConfig(name="Export Test", model={"modelId": MODEL_ID}))
    return DeployRequest(**kwargs)


def _generate(**kwargs):
    return CfnTemplateGenerator().generate(_request(**kwargs))


def _template(**kwargs):
    return yaml.safe_load(_generate(**kwargs).template_yaml)


def _full_component_kwargs() -> dict:
    return {
        "connectedTools": ["gateway", "memory", "guardrails"],
        "gatewayConfig": {"gateway_provider": "agentcore", "targetType": "lambda"},
        "gatewayTools": ["get_customer"],
        "customTools": [
            {
                "toolName": "AReallyLongCustomToolNameForCollisionChecks",
                "displayName": "Long tool",
                "description": "Exercises the Lambda name ceiling",
                "lambdaCode": "def handler(event, context): return {'statusCode': 200}",
                "inputSchema": {},
            }
        ],
        "memoryConfig": {"enabled": True},
        "mcpServerConfig": {"tools": ["get_customer"]},
        "evaluationConfig": {"enabled": True},
        "knowledgeBaseConfig": KB_CONFIG,
    }


def _full_profile_bundle(**profile):
    return _generate(
        **_full_component_kwargs(),
        namingProfile=profile or {"prefix": "ecb"},
    )


def _full_legacy_bundle(**overrides):
    kwargs = _full_component_kwargs()
    kwargs.update(overrides)
    return _generate(
        **kwargs,
    )


def _statement(template: dict, role_id: str, sid: str) -> dict:
    for policy in template["Resources"][role_id]["Properties"].get("Policies", []):
        for statement in policy.get("PolicyDocument", {}).get("Statement", []):
            if statement.get("Sid") == sid:
                return statement
    raise AssertionError(f"{role_id}/{sid} not found")


def _sub(value):
    """The string half of either short or long-form Fn::Sub."""
    expression = value["Fn::Sub"]
    return expression[0] if isinstance(expression, list) else expression


def test_absent_profile_preserves_the_legacy_contract():
    template = _template(gatewayConfig={"gateway_provider": "agentcore"})

    assert "Metadata" not in template or "AgentCoreFlowsNamingProfile" not in template["Metadata"]
    assert template["Parameters"]["DeploymentName"]["AllowedPattern"] == "^[a-z][a-z0-9]{0,39}$"
    assert template["Parameters"]["DeploymentName"]["MaxLength"] == 39
    assert template["Resources"]["CognitoUserPool"]["Properties"]["UserPoolName"] == {
        "Fn::Sub": "AgentCore-${DeploymentName}"
    }
    assert template["Resources"]["AgentCoreGateway"]["Properties"]["Name"] == {"Fn::Sub": "${DeploymentName}-gateway"}
    role_name = template["Resources"]["RuntimeExecutionRole"]["Properties"]["RoleName"]
    assert role_name["Fn::If"][1] == {"Fn::Sub": "AgentCoreRuntime-${AWS::StackName}"}


def test_legacy_parameter_limit_is_derived_from_every_emitted_physical_name():
    template = yaml.safe_load(_full_legacy_bundle().template_yaml)
    parameter = template["Parameters"]["DeploymentName"]

    assert parameter["AllowedPattern"] == "^[a-z][a-z0-9]{0,39}$"
    assert parameter["MaxLength"] == 18
    assert len(parameter["Default"]) <= 18
    assert "1-18" in parameter["ConstraintDescription"]

    checked = 0
    one_character_beyond_the_contract: list[str] = []
    for logical_id, resource in template["Resources"].items():
        properties = resource.get("Properties", {})
        resource_type = resource.get("Type")
        for (mapped_type, property_name), family in _NAMING_PROPERTY_FAMILIES.items():
            if resource_type != mapped_type or property_name not in properties:
                continue
            max_length, pattern = _NAMING_PROFILE_RULES[family]
            for expression in _name_expression_templates(properties[property_name]):
                if "${DeploymentName}" not in expression:
                    continue
                checked += 1
                at_limit = _render_name_limit_sample(expression, parameter["MaxLength"])
                assert len(at_limit) <= max_length, (
                    f"{logical_id}.{property_name} is {len(at_limit)} characters "
                    f"at the advertised limit; {family} permits {max_length}"
                )
                assert pattern.fullmatch(at_limit), (
                    f"{logical_id}.{property_name} produces invalid {family} name {at_limit!r}"
                )

                above_limit = _render_name_limit_sample(
                    expression,
                    parameter["MaxLength"] + 1,
                )
                if len(above_limit) > max_length or not pattern.fullmatch(above_limit):
                    one_character_beyond_the_contract.append(f"{logical_id}.{property_name}")

    assert checked, "the test did not inspect any DeploymentName-bearing physical names"
    assert any(name.startswith("CustomTool") for name in one_character_beyond_the_contract), (
        "the 18-character ceiling is no longer pinned to the long custom-tool Lambda"
    )


def test_legacy_limit_refuses_a_new_intrinsic_shape_it_cannot_measure():
    synthetic = {
        "Resources": {
            "RuntimeEndpoint": {
                "Type": "AWS::BedrockAgentCore::RuntimeEndpoint",
                "Properties": {
                    "Name": {
                        "Fn::Join": [
                            "",
                            ["${DeploymentName}", "_endpoint"],
                        ]
                    }
                },
            }
        }
    }

    with pytest.raises(
        CfnExportUnsupportedError,
        match="intrinsic form the legacy name-limit calculator does not understand",
    ):
        _legacy_deployment_name_limit(synthetic)


def test_prefix_profile_moves_names_and_every_coupled_consumer_together():
    bundle = _full_profile_bundle(prefix="ecb")
    template = yaml.safe_load(bundle.template_yaml)
    resources = template["Resources"]

    assert template["Parameters"]["DeploymentName"]["AllowedPattern"] == "^[a-z][a-z0-9]{0,19}$"
    assert template["Metadata"]["AgentCoreFlowsNamingProfile"]["Prefix"] == "ecb"

    assert _sub(resources["CognitoUserPool"]["Properties"]["UserPoolName"]) == "ecb-${DeploymentName}"
    assert _sub(resources["AgentCoreGateway"]["Properties"]["Name"]) == "ecb-${DeploymentName}-gateway"
    assert _sub(resources["KBVectorBucket"]["Properties"]["VectorBucketName"]) == (
        "ecb-${DeploymentName}-kbvec-${StackSuffix}"
    )
    assert _sub(resources["KBVectorIndex"]["Properties"]["IndexName"]) == ("ecb-${DeploymentName}-kb-index")
    assert _sub(resources["AgentCoreMemory"]["Properties"]["Name"]) == "ecb_${DeploymentName}_memory"
    assert _sub(resources["McpServerRuntime"]["Properties"]["AgentRuntimeName"]) == ("ecb_${DeploymentName}_mcp_server")
    assert _sub(resources["AgentCoreRuntime"]["Properties"]["AgentRuntimeName"]) == "ecb_${DeploymentName}_runtime"
    assert _sub(resources["RuntimeEndpoint"]["Properties"]["Name"]) == "ecb_${DeploymentName}_endpoint"

    gateway_scope = "ecb-${DeploymentName}/invoke"
    assert _sub(resources["CognitoUserPoolClient"]["Properties"]["AllowedOAuthScopes"][0]) == gateway_scope
    assert _sub(resources["AgentCoreRuntime"]["Properties"]["EnvironmentVariables"]["COGNITO_SCOPE"]) == gateway_scope
    assert _sub(template["Outputs"]["CognitoScope"]["Value"]) == gateway_scope
    assert _sub(template["Outputs"]["EndpointName"]["Value"]) == "ecb_${DeploymentName}_endpoint"

    gateway_grant = json.dumps(_statement(template, "GatewayRole", "AgentCoreGatewayOps")["Resource"])
    assert "gateway/ecb-${DeploymentName}-gateway-*" in gateway_grant
    assert "gateway/${DeploymentName}-gateway-*" not in gateway_grant

    lambda_grants = _statement(template, "GatewayRole", "LambdaInvoke")["Resource"]
    function_names = {
        _sub(resources[logical_id]["Properties"]["FunctionName"])
        for logical_id in resources
        if resources[logical_id]["Type"] == "AWS::Lambda::Function"
        and logical_id
        in {
            "DynamicToolsLambda",
            "KBToolLambda",
            next(k for k in resources if k.startswith("CustomTool") and k.endswith("Lambda")),
        }
    }
    granted_names = {entry["Fn::Sub"].split("function:", 1)[1] for entry in lambda_grants}
    assert granted_names == function_names
    assert all(len(name.replace("${DeploymentName}", "d" * 20)) <= 64 for name in function_names)

    for sid in ("MemoryDataPlane", "MemoryControlPlane"):
        grant = json.dumps(_statement(template, "MemoryExecutionRole", sid)["Resource"])
        assert "memory/ecb_${DeploymentName}_memory-*" in grant
        assert "memory/${DeploymentName}_memory-*" not in grant

    runtime_role = resources["RuntimeExecutionRole"]["Properties"]["RoleName"]["Fn::If"][1]
    assert _sub(runtime_role) == "ecb-${DeploymentName}-${StackSuffix}-runtime-role"
    assert "StackSuffix" in runtime_role["Fn::Sub"][1]

    # Runtime log retention is one Custom::RuntimeLogGroup per group (the endpoint's group has its
    # own resource) plus a per-runtime sweeper that names no group; the prefix must have moved
    # every governed name, so collect them across every non-sweeper resource of the family.
    def _governed_log_group_names(logical_prefix: str) -> list:
        names: list = []
        for logical_id, resource in resources.items():
            if resource["Type"] != "Custom::RuntimeLogGroup" or not logical_id.startswith(logical_prefix):
                continue
            props = resource["Properties"]
            if props.get("Mode") == "sweeper":
                continue
            names.extend(props.get("LogGroupNames") or [])
        return names

    runtime_logs = _governed_log_group_names("AgentCoreRuntimeLogGroup")
    mcp_logs = _governed_log_group_names("McpServerRuntimeLogGroup")
    assert runtime_logs and mcp_logs, "the governed log groups must be emitted before their names can be checked"
    assert any("ecb_${DeploymentName}_endpoint" in _sub(item) for item in runtime_logs)
    assert any("ecb_${DeploymentName}_mcp_endpoint" in _sub(item) for item in mcp_logs)

    target_names = {
        _sub(resource["Properties"]["Name"])
        for resource in resources.values()
        if resource["Type"] == "AWS::BedrockAgentCore::GatewayTarget"
    }
    assert {
        "ecb-${DeploymentName}-KBTool",
        "ecb-${DeploymentName}-MCPServerRuntime",
    } <= target_names
    assert any("CT-AReallyLongCustomToolNameForColl" in name for name in target_names)

    assert "## Customer naming profile" in bundle.readme
    assert "Metadata.AgentCoreFlowsNamingProfile" in bundle.readme


def test_per_family_overrides_drive_the_same_references():
    template = yaml.safe_load(
        _full_profile_bundle(
            prefix="ecb",
            resourceNames={
                "gateway": "{prefix}-{deployment}-gw",
                "cognitoResourceServer": "{prefix}-{deployment}-api",
                "cognitoDomain": "{prefix}-auth-{deployment}-{suffix}",
                "runtime": "{prefix}_{deployment}_agent",
                "runtimeEndpoint": "{prefix}_{deployment}_live",
                "gatewayRole": "{prefix}-{deployment}-{suffix}-gw-role",
            },
        ).template_yaml
    )

    assert _sub(template["Resources"]["AgentCoreGateway"]["Properties"]["Name"]) == "ecb-${DeploymentName}-gw"
    grant = json.dumps(_statement(template, "GatewayRole", "AgentCoreGatewayOps")["Resource"])
    assert "gateway/ecb-${DeploymentName}-gw-*" in grant

    scope = "ecb-${DeploymentName}-api/invoke"
    assert _sub(template["Resources"]["CognitoUserPoolClient"]["Properties"]["AllowedOAuthScopes"][0]) == scope
    assert _sub(template["Outputs"]["CognitoScope"]["Value"]) == scope
    assert (
        _sub(template["Resources"]["AgentCoreRuntime"]["Properties"]["EnvironmentVariables"]["COGNITO_SCOPE"]) == scope
    )

    domain = template["Resources"]["CognitoUserPoolDomain"]["Properties"]["Domain"]["Fn::If"]
    assert _sub(domain[1]) == "ecb-auth-${DeploymentName}-${CognitoDomainSuffix}"
    assert _sub(domain[2]) == "ecb-auth-${DeploymentName}-${StackSuffix}"

    assert _sub(template["Resources"]["AgentCoreRuntime"]["Properties"]["AgentRuntimeName"]) == (
        "ecb_${DeploymentName}_agent"
    )
    assert _sub(template["Resources"]["RuntimeEndpoint"]["Properties"]["Name"]) == ("ecb_${DeploymentName}_live")
    assert _sub(template["Outputs"]["EndpointName"]["Value"]) == "ecb_${DeploymentName}_live"
    role = template["Resources"]["GatewayRole"]["Properties"]["RoleName"]["Fn::If"][1]
    assert _sub(role) == "ecb-${DeploymentName}-${StackSuffix}-gw-role"


def test_gateway_target_and_policy_names_move_with_generated_cedar_actions():
    template = _template(
        connectedTools=["gateway"],
        gatewayConfig={"gateway_provider": "agentcore", "targetType": "lambda"},
        gatewayTools=["get_customer"],
        policyConfig={},
        namingProfile={
            "prefix": "ecb",
            "resourceNames": {
                "gatewayTarget": "{prefix}-{deployment}-target-{component}",
                "policy": "{prefix}_{deployment}_policy_{component}",
            },
        },
    )

    target_name = _sub(template["Resources"]["DynamicToolsTarget"]["Properties"]["Name"])
    assert target_name == "ecb-${DeploymentName}-target-DynamicTools"

    policy = template["Resources"]["DefaultPolicy"]["Properties"]
    assert _sub(policy["Name"]) == "ecb_${DeploymentName}_policy_default_permit"
    statement = policy["Statement"]["Fn::Sub"]
    assert ('AgentCore::Action::"ecb-${DeploymentName}-target-DynamicTools___get_customer"') in statement
    assert 'AgentCore::Action::"DynamicTools___get_customer"' not in statement


def test_caller_cedar_is_joined_with_the_new_target_without_becoming_fn_sub():
    statement = (
        "permit(principal is AgentCore::OAuthUser, action in "
        '[AgentCore::Action::"CustomerSupportTools___get_customer"], '
        'resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:'
        'us-east-1:123456789012:gateway/example");'
    )
    template = _template(
        connectedTools=["gateway"],
        gatewayConfig={"gateway_provider": "agentcore", "targetType": "lambda"},
        gatewayTools=["get_customer"],
        policyConfig={
            "policies": [
                {
                    "name": "customer_policy",
                    "statement": statement,
                }
            ]
        },
        namingProfile={"prefix": "ecb"},
    )

    rewritten = template["Resources"]["DefaultPolicy"]["Properties"]["Statement"]
    assert "Fn::Join" in rewritten
    parts = rewritten["Fn::Join"][1]
    assert {"Fn::Sub": "ecb-${DeploymentName}-DynamicTools"} in parts
    literal_text = "".join(part for part in parts if isinstance(part, str))
    assert 'AgentCore::Action::"___get_customer"' in literal_text
    assert "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/example" in literal_text
    assert "CustomerSupportTools" not in literal_text
    assert "DynamicTools" not in literal_text


def test_caller_cedar_rewrite_preserves_whitespace_and_literal_dollar_braces():
    statement = (
        "permit(principal is AgentCore::OAuthUser, action in "
        '[AgentCore :: Action :: "CustomerSupportTools___get_customer"], '
        'resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:'
        'us-east-1:123456789012:gateway/example"); // ${caller_text}'
    )
    template = _template(
        connectedTools=["gateway"],
        gatewayConfig={"gateway_provider": "agentcore", "targetType": "lambda"},
        gatewayTools=["get_customer"],
        policyConfig={
            "policies": [
                {
                    "name": "customer_policy",
                    "statement": statement,
                }
            ]
        },
        namingProfile={"prefix": "ecb"},
    )

    rewritten = template["Resources"]["DefaultPolicy"]["Properties"]["Statement"]
    assert "Fn::Join" in rewritten
    parts = rewritten["Fn::Join"][1]
    assert {"Fn::Sub": "ecb-${DeploymentName}-DynamicTools"} in parts
    literal_text = "".join(part for part in parts if isinstance(part, str))
    assert 'AgentCore :: Action :: "___get_customer"' in literal_text
    assert "${caller_text}" in literal_text
    assert "CustomerSupportTools" not in literal_text
    assert "DynamicTools" not in literal_text


@pytest.mark.parametrize(
    "tool_name",
    ["get_order", "get_customer", "list_orders", "process_refund"],
)
def test_historical_customer_support_aliases_follow_canonical_tools_to_dynamic_target(
    tool_name,
):
    """A saved pre-routing-fix policy must not silently stop authorizing its tool."""
    statement = (
        "permit(principal is AgentCore::OAuthUser, action in "
        f'[AgentCore::Action::"CustomerSupportTools___{tool_name}"], '
        'resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:'
        'us-east-1:123456789012:gateway/example"); '
        f"// literal CustomerSupportTools___{tool_name}"
    )

    template = _template(
        connectedTools=["gateway"],
        gatewayConfig={"gateway_provider": "agentcore", "targetType": "lambda"},
        gatewayTools=[tool_name],
        policyConfig={
            "policies": [
                {
                    "name": "historical_customer_policy",
                    "statement": statement,
                }
            ]
        },
    )

    rewritten = template["Resources"]["DefaultPolicy"]["Properties"]["Statement"]
    assert isinstance(rewritten, str)
    assert f'AgentCore::Action::"DynamicTools___{tool_name}"' in rewritten
    assert f'AgentCore::Action::"CustomerSupportTools___{tool_name}"' not in rewritten
    assert f"// literal CustomerSupportTools___{tool_name}" in rewritten


def test_genuine_legacy_customer_support_action_is_not_migrated_to_dynamic_tools():
    statement = (
        "permit(principal is AgentCore::OAuthUser, action in "
        '[AgentCore::Action::"CustomerSupportTools___check_order_status"], '
        'resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:'
        'us-east-1:123456789012:gateway/example");'
    )

    template = _template(
        connectedTools=["gateway"],
        gatewayConfig={"gateway_provider": "agentcore", "targetType": "lambda"},
        gatewayTools=["check_order_status"],
        policyConfig={
            "policies": [
                {
                    "name": "legacy_customer_policy",
                    "statement": statement,
                }
            ]
        },
        namingProfile={"prefix": "ecb"},
    )

    rewritten = template["Resources"]["DefaultPolicy"]["Properties"]["Statement"]
    assert "Fn::Join" in rewritten
    parts = rewritten["Fn::Join"][1]
    assert {"Fn::Sub": "ecb-${DeploymentName}-CustomerSupportTools"} in parts
    literal_text = "".join(part for part in parts if isinstance(part, str))
    assert 'AgentCore::Action::"___check_order_status"' in literal_text
    assert "DynamicTools" not in literal_text


@pytest.mark.parametrize(
    ("profile", "message"),
    [
        ({"prefix": "ecb", "resourceNames": {"unknown": "{prefix}-{deployment}"}}, "unknown resource families"),
        ({"prefix": "ecb", "resourceNames": {"gateway": "{prefix}-fixed"}}, "must include {deployment}"),
        (
            {"prefix": "ecb", "resourceNames": {"runtime": "{prefix}-{deployment}-runtime"}},
            "not valid for that AWS resource family",
        ),
        (
            {"prefix": "ecb", "resourceNames": {"cognitoDomain": "{prefix}-{deployment}"}},
            "must include {suffix}",
        ),
        (
            {"prefix": "ecb", "resourceNames": {"gateway": "{prefix}-{deployment}-{component}"}},
            "unsupported placeholder",
        ),
        (
            {
                "prefix": "ecb",
                "resourceNames": {
                    "vectorBucket": "{prefix}.{deployment}.{suffix}",
                },
            },
            "not valid for that AWS resource family",
        ),
        (
            {
                "prefix": "ecb",
                "resourceNames": {
                    "vectorIndex": "{deployment}",
                },
            },
            "1-character DeploymentName",
        ),
    ],
)
def test_invalid_or_incomplete_profiles_are_refused(profile, message):
    with pytest.raises(CfnExportUnsupportedError, match=re.escape(message)):
        _generate(namingProfile=profile)


def test_vector_index_allows_dots_even_though_vector_bucket_does_not():
    template = yaml.safe_load(
        _full_profile_bundle(
            prefix="ecb",
            resourceNames={
                "vectorIndex": "{prefix}.{deployment}.index",
            },
        ).template_yaml
    )
    assert _sub(template["Resources"]["KBVectorIndex"]["Properties"]["IndexName"]) == ("ecb.${DeploymentName}.index")


@pytest.mark.parametrize("prefix", ["ECB", "ecb-prod", "9ecb", "abcdefghijklmn"])
def test_prefix_uses_the_strictest_shared_aws_grammar(prefix):
    with pytest.raises(ValidationError):
        _request(namingProfile={"prefix": prefix})


def test_long_canvas_names_keep_distinct_collision_resistant_defaults():
    common = "customer-agent-platform-"
    first = yaml.safe_load(
        _generate(
            config=RuntimeConfig(
                name=common + "alpha",
                model={"modelId": MODEL_ID},
            ),
            namingProfile={"prefix": "ecb"},
        ).template_yaml
    )["Parameters"]["DeploymentName"]["Default"]
    second = yaml.safe_load(
        _generate(
            config=RuntimeConfig(
                name=common + "beta",
                model={"modelId": MODEL_ID},
            ),
            namingProfile={"prefix": "ecb"},
        ).template_yaml
    )["Parameters"]["DeploymentName"]["Default"]

    assert len(first) <= 20 and len(second) <= 20
    assert first != second
    assert re.fullmatch(r"[a-z][a-z0-9]{0,19}", first)
    assert re.fullmatch(r"[a-z][a-z0-9]{0,19}", second)


def test_legacy_long_canvas_names_remain_distinct_at_the_derived_limit():
    common = "customer-agent-platform-"
    first_template = yaml.safe_load(
        _full_legacy_bundle(
            config=RuntimeConfig(
                name=common + "alpha",
                model={"modelId": MODEL_ID},
            )
        ).template_yaml
    )
    second_template = yaml.safe_load(
        _full_legacy_bundle(
            config=RuntimeConfig(
                name=common + "beta",
                model={"modelId": MODEL_ID},
            )
        ).template_yaml
    )
    first_parameter = first_template["Parameters"]["DeploymentName"]
    second_parameter = second_template["Parameters"]["DeploymentName"]

    assert first_parameter["MaxLength"] == second_parameter["MaxLength"] == 18
    assert first_parameter["Default"] != second_parameter["Default"]
    assert re.fullmatch(r"[a-z][a-z0-9]{0,17}", first_parameter["Default"])
    assert re.fullmatch(r"[a-z][a-z0-9]{0,17}", second_parameter["Default"])


def test_deploy_script_hashes_stack_names_that_would_collide_when_clipped(tmp_path):
    bundle = _generate(namingProfile={"prefix": "ecb"})
    lines = [line for line in bundle.deploy_sh.splitlines() if line.startswith("DEPLOY_NAME=")]
    assert lines

    def derive(stack_name: str) -> str:
        probe = tmp_path / f"{stack_name[-5:]}.sh"
        probe.write_text(
            "\n".join(
                [
                    "set -euo pipefail",
                    f'STACK_NAME="{stack_name}"',
                    *lines,
                    'echo "$DEPLOY_NAME"',
                ]
            )
        )
        result = subprocess.run(
            ["bash", str(probe)],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin"},
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    first_stack = "customer-agent-platform-alpha"
    second_stack = "customer-agent-platform-beta"
    sanitized_first = re.sub(r"[^a-z0-9]", "", first_stack)
    sanitized_second = re.sub(r"[^a-z0-9]", "", second_stack)
    assert sanitized_first[:20] == sanitized_second[:20], "the control no longer demonstrates clipping"

    first = derive(first_stack)
    second = derive(second_stack)
    assert first != second
    assert re.fullmatch(r"[a-z][a-z0-9]{0,19}", first)
    assert re.fullmatch(r"[a-z][a-z0-9]{0,19}", second)


def test_legacy_deploy_script_uses_the_template_derived_limit(tmp_path):
    bundle = _full_legacy_bundle()
    parameter = yaml.safe_load(bundle.template_yaml)["Parameters"]["DeploymentName"]
    lines = [line for line in bundle.deploy_sh.splitlines() if line.startswith("DEPLOY_NAME=")]
    probe = tmp_path / "legacy-deployment-name.sh"
    probe.write_text(
        "\n".join(
            [
                "set -euo pipefail",
                'STACK_NAME="customer-agent-platform-alpha"',
                *lines,
                'echo "$DEPLOY_NAME"',
            ]
        )
    )
    result = subprocess.run(
        ["bash", str(probe)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert len(result.stdout.strip()) <= parameter["MaxLength"] == 18
    assert re.fullmatch(r"[a-z][a-z0-9]{0,17}", result.stdout.strip())


def test_generated_digest_handles_whitespace_and_glob_characters_in_file_names(tmp_path):
    """The archive and its integrity digest must cover the same files byte-for-byte."""
    bundle = _generate(namingProfile={"prefix": "ecb"})
    helpers = re.findall(
        r"^(?:sha256_stdin|content_digest)\(\).*?^\}$",
        bundle.deploy_sh,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert len(helpers) == 2, "deploy.sh no longer exposes both digest helpers"

    source_dir = tmp_path / "agent-code"
    (source_dir / "nested").mkdir(parents=True)
    files = {
        "agent.py": "agent",
        "customer prompts.py": "prompt",
        "nested/[draft] tool.py": "tool",
        "nested/-leading.py": "leading",
    }
    for name, body in files.items():
        (source_dir / name).write_text(body, encoding="utf-8")

    def bash_digest(*names: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "bash",
                "-c",
                "\n".join(helpers) + '\ncontent_digest "$@"',
                "_",
                str(source_dir),
                *names,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    discovered = bash_digest()
    assert discovered.returncode == 0, discovered.stderr
    assert discovered.stdout.strip() == content_digest(files)

    explicit_names = [
        "nested/[draft] tool.py",
        "customer prompts.py",
    ]
    explicit = bash_digest(*reversed(explicit_names))
    assert explicit.returncode == 0, explicit.stderr
    assert explicit.stdout.strip() == content_digest({name: files[name] for name in explicit_names})


def test_oversized_export_stages_its_template_before_cloudformation_deploy():
    bundle = _full_profile_bundle(prefix="ecb")
    assert len(bundle.template_yaml.encode("ascii")) > 51_200, (
        "the positive control no longer exercises CloudFormation's inline body limit"
    )

    assert '--s3-bucket "$BUCKET"' in bundle.deploy_sh
    assert '--s3-prefix "cfn-assets/${STACK_NAME}/cloudformation"' in bundle.deploy_sh
    assert bundle.deploy_sh.index("--template-file template.yaml") < bundle.deploy_sh.index('--s3-bucket "$BUCKET"')


def test_long_policy_names_are_stable_valid_and_collision_resistant():
    def emitted_name(tail: str) -> str:
        template = _template(
            connectedTools=["gateway"],
            gatewayConfig={
                "gateway_provider": "agentcore",
                "targetType": "lambda",
            },
            gatewayTools=["get_customer"],
            policyConfig={
                "policies": [
                    {
                        "name": "9" + ("same_prefix_" * 8) + tail,
                        "statement": VALID_POLICY_STATEMENT,
                    }
                ]
            },
        )
        return template["Resources"]["DefaultPolicy"]["Properties"]["Name"]

    first = emitted_name("alpha")
    second = emitted_name("beta")

    assert first != second
    for name in (first, second):
        assert len(name) == 48
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name)


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ({"not": "a string"}, "must be a string"),
        ("   ", "must not be empty"),
    ],
)
def test_malformed_policy_names_are_refused_with_an_actionable_error(name, message):
    with pytest.raises(CfnExportUnsupportedError, match=message):
        _generate(
            connectedTools=["gateway"],
            gatewayConfig={
                "gateway_provider": "agentcore",
                "targetType": "lambda",
            },
            gatewayTools=["get_customer"],
            policyConfig={
                "policies": [
                    {
                        "name": name,
                        "statement": VALID_POLICY_STATEMENT,
                    }
                ]
            },
        )


def test_policy_names_that_normalize_to_the_same_identifier_are_refused():
    with pytest.raises(CfnExportUnsupportedError, match="both resolve to"):
        _generate(
            connectedTools=["gateway"],
            gatewayConfig={
                "gateway_provider": "agentcore",
                "targetType": "lambda",
            },
            gatewayTools=["get_customer"],
            policyConfig={
                "policies": [
                    {
                        "name": "customer-policy",
                        "statement": VALID_POLICY_STATEMENT,
                    },
                    {
                        "name": "customer policy",
                        "statement": VALID_POLICY_STATEMENT,
                    },
                ]
            },
        )


def test_explicit_legacy_role_names_publish_their_independent_stack_name_limit():
    legacy = yaml.safe_load(_full_legacy_bundle().template_yaml)
    profiled = yaml.safe_load(_full_profile_bundle(prefix="ecb").template_yaml)

    assert _explicit_role_stack_name_limit(legacy) == 47
    assert _explicit_role_stack_name_limit(profiled) is None
    description = legacy["Parameters"]["UseExplicitRoleNames"]["Description"]
    assert "at most 47 characters" in description
    assert "long stack names remain deployable" in description


def test_deploy_script_auto_selects_safe_role_naming_for_long_stack_names(tmp_path):
    def run(stack_name: str, explicit: str | None = None, *, profiled: bool = False):
        bundle = _full_profile_bundle(prefix="ecb") if profiled else _full_legacy_bundle()
        script = bundle.deploy_sh
        guard_start = script.index("# Validate role-name mode")
        guard_end = script.index("# Derive a clean deployment name", guard_start)
        role_start = script.index("# Role governance.")
        role_end = script.index("# Optional customer-managed KMS key", role_start)
        probe = tmp_path / (f"roles-{'profile' if profiled else 'legacy'}-{explicit or 'auto'}.sh")
        probe.write_text(
            "\n".join(
                [
                    "set -euo pipefail",
                    'STACK_NAME="$PROBE_STACK_NAME"',
                    "PARAM_OVERRIDES=()",
                    script[guard_start:guard_end],
                    script[role_start:role_end],
                    'printf "RESULT=%s|%s|%s\\n" '
                    '"${USE_EXPLICIT_ROLE_NAMES:-unset}" '
                    '"$CAPABILITIES" "${PARAM_OVERRIDES[*]-}"',
                ]
            )
        )
        env = {
            "PATH": "/usr/bin:/bin",
            "PROBE_STACK_NAME": stack_name,
        }
        if explicit is not None:
            env["USE_EXPLICIT_ROLE_NAMES"] = explicit
        return subprocess.run(
            ["bash", str(probe)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    short_auto = run("s" * 47)
    assert short_auto.returncode == 0, short_auto.stderr
    assert short_auto.stdout.strip().endswith("RESULT=unset|CAPABILITY_NAMED_IAM|")

    long_auto = run("s" * 48)
    assert long_auto.returncode == 0, long_auto.stderr
    assert long_auto.stdout.strip().endswith("RESULT=false|CAPABILITY_IAM|UseExplicitRoleNames=false")

    long_forced = run("s" * 48, "true")
    assert long_forced.returncode != 0
    assert "support stack names up to 47 characters" in long_forced.stderr

    invalid_mode = run("short-stack", "TRUE")
    assert invalid_mode.returncode != 0
    assert "must be true or false" in invalid_mode.stderr

    long_profiled = run("s" * 128, profiled=True)
    assert long_profiled.returncode == 0, long_profiled.stderr
    assert long_profiled.stdout.strip().endswith("RESULT=unset|CAPABILITY_NAMED_IAM|")


def _route_body() -> dict:
    return {
        "nodeId": "node-1",
        "config": RuntimeConfig(
            name="Export Test",
            model={"modelId": MODEL_ID},
        ).model_dump(mode="json", by_alias=True),
        "namingProfile": {"prefix": "ecb"},
    }


def _authenticated_deployment_client(deployment_app) -> TestClient:
    """Exercise deployment routes with the JWT subject API Gateway supplies."""
    event = {
        "requestContext": {
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "naming-profile-test-owner",
                        "cognito:groups": ["g-users-default"],
                    }
                }
            }
        }
    }

    async def _inject(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "aws.event": event}
        await deployment_app(scope, receive, send)

    return TestClient(_inject, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("route", "operation"),
    [
        ("/api/deploy", "live platform deployment"),
        ("/api/export-python", "standalone Python export"),
    ],
)
def test_non_cfn_routes_refuse_the_profile_instead_of_silently_dropping_it(route, operation):
    import app.deployment_handler as deployment_handler

    response = _authenticated_deployment_client(deployment_handler.deployment_app).post(
        route,
        json=_route_body(),
    )

    assert response.status_code == 400, response.text[:400]
    detail = str(response.json().get("detail", ""))
    assert "namingProfile" in detail
    assert operation in detail
    assert "/api/generate-cfn-template" in detail


def test_cfn_route_accepts_the_profile_and_returns_it_in_the_bundle(monkeypatch):
    import app.deployment_handler as deployment_handler

    monkeypatch.delenv("ARTIFACTS_BUCKET_NAME", raising=False)
    response = TestClient(
        deployment_handler.deployment_app,
        raise_server_exceptions=False,
    ).post("/api/generate-cfn-template", json=_route_body())

    assert response.status_code == 200, response.text[:400]
    archive = zipfile.ZipFile(BytesIO(b64decode(response.json()["zip_base64"])))
    template_path = next(
        name for name in archive.namelist() if name == "template.yaml" or name.endswith("/template.yaml")
    )
    template = yaml.safe_load(archive.read(template_path))
    assert template["Metadata"]["AgentCoreFlowsNamingProfile"]["Prefix"] == "ecb"


def test_cfn_route_resolves_a_tag_profile_into_the_named_template(monkeypatch):
    import app.deployment_handler as deployment_handler
    import app.services.tag_policy_store as tag_policy_store

    # P0-B: the route resolves through ``resolve_governance`` so the staleness check and the
    # applied values come from one read, and it forwards the caller's captured revision and
    # profile timestamp -- both asserted here, because a route that dropped them would leave
    # the store unable to detect a stale request at all.
    revision = "sha256:" + "c" * 64
    profile_updated_at = "2026-09-23T10:00:00Z"

    class _ProfileStore:
        def ensure_platform_policies(self, tenant):
            assert tenant == "default"

        def resolve_governance(
            self,
            tenant,
            supplied=None,
            profile_name=None,
            *,
            expected_policy_revision=None,
            expected_profile_updated_at=None,
        ):
            assert tenant == "default"
            assert profile_name == "regulated"
            assert supplied == {"Owner": "operator"}
            assert expected_policy_revision == revision
            assert expected_profile_updated_at == profile_updated_at
            return tag_policy_store.ResolvedGovernance(
                tags={"Environment": "production", "Owner": "operator"},
                policy_revision=revision,
                profile_updated_at=profile_updated_at,
            )

    monkeypatch.setattr(tag_policy_store, "get_tag_policy_store", lambda: _ProfileStore())
    monkeypatch.delenv("ARTIFACTS_BUCKET_NAME", raising=False)
    response = TestClient(
        deployment_handler.deployment_app,
        raise_server_exceptions=False,
    ).post(
        "/api/generate-cfn-template",
        json={
            **_route_body(),
            "tagProfile": "regulated",
            "resourceTags": {"Owner": "operator"},
            "policyRevision": revision,
            "tagProfileUpdatedAt": profile_updated_at,
        },
    )

    assert response.status_code == 200, response.text[:400]
    archive = zipfile.ZipFile(BytesIO(b64decode(response.json()["zip_base64"])))
    template_path = next(
        name for name in archive.namelist() if name == "template.yaml" or name.endswith("/template.yaml")
    )
    template = yaml.safe_load(archive.read(template_path))

    assert template["Metadata"]["AgentCoreFlowsNamingProfile"]["Prefix"] == "ecb"
    expected_tags = {
        "Environment": "production",
        "Owner": "operator",
    }
    runtime_tags = template["Resources"]["AgentCoreRuntime"]["Properties"]["Tags"]
    assert expected_tags.items() <= runtime_tags.items()
    role_tags = template["Resources"]["RuntimeExecutionRole"]["Properties"]["Tags"]
    role_tag_map = {tag["Key"]: tag["Value"] for tag in role_tags}
    assert expected_tags.items() <= role_tag_map.items()


def test_frontend_and_backend_publish_the_same_naming_profile_contract():
    source = (
        Path(__file__).resolve().parents[2] / "frontend/src/components/deploy/ResourceNamingFields.tsx"
    ).read_text(encoding="utf-8")

    def string_set(constant: str) -> set[str]:
        match = re.search(
            rf"const {constant} = new Set\(\[(.*?)\]\);",
            source,
            flags=re.DOTALL,
        )
        assert match, f"{constant} is no longer a literal Set; update this contract test"
        values = set(re.findall(r"'([^']+)'", match.group(1)))
        assert values, f"{constant} parsed as an empty set"
        return values

    frontend_families = string_set("RESOURCE_FAMILIES")
    frontend_components = string_set("COMPONENT_FAMILIES")
    frontend_stack_unique = string_set("STACK_UNIQUE_FAMILIES")
    frontend_roles = string_set("ROLE_FAMILIES")
    frontend_limit = re.search(r"entries\.length > (\d+)", source)
    assert frontend_limit, "the frontend override limit is no longer explicit"

    assert frontend_families == set(_NAMING_PROFILE_TEMPLATES)
    assert int(frontend_limit.group(1)) >= len(frontend_families), (
        "the frontend cannot submit an override for every family it advertises"
    )
    assert frontend_components == {
        key for key, fields in _NAMING_PROFILE_ALLOWED_FIELDS.items() if "component" in fields
    }
    assert frontend_roles == set(_NAMING_ROLE_KEYS)
    assert frontend_stack_unique == {
        key
        for key, fields in _NAMING_PROFILE_ALLOWED_FIELDS.items()
        if "suffix" in fields and key not in _NAMING_ROLE_KEYS
    }


def test_one_profile_can_override_every_supported_resource_family():
    assert len(_NAMING_PROFILE_TEMPLATES) > 32, (
        "this regression only proves the old 32-entry ceiling was too small while "
        "the contract still has more than 32 families"
    )
    bundle = _generate(
        namingProfile={
            "prefix": "ecb",
            "resourceNames": dict(_NAMING_PROFILE_TEMPLATES),
        }
    )
    template = yaml.safe_load(bundle.template_yaml)
    effective = template["Metadata"]["AgentCoreFlowsNamingProfile"]["EffectiveResourceNames"]
    assert effective == dict(sorted(_NAMING_PROFILE_TEMPLATES.items()))


def test_profiled_template_has_no_cfn_lint_errors():
    bundle = _full_profile_bundle(prefix="ecb")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as template_file:
        template_file.write(bundle.template_yaml)
        template_file.flush()
        result = subprocess.run(
            ["cfn-lint", template_file.name, "--format", "json", "--ignore-checks", "W"],
            capture_output=True,
            text=True,
            check=False,
        )
    findings = json.loads(result.stdout or "[]")
    errors = [finding for finding in findings if finding.get("Level") == "Error"]
    assert not errors, json.dumps(errors, indent=2)


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        pytest.param("runtime-only", {"namingProfile": {"prefix": "ecb"}}, id="runtime-only"),
        pytest.param(
            "agentcore-gateway-policy",
            {
                "connectedTools": ["gateway"],
                "gatewayConfig": {
                    "gateway_provider": "agentcore",
                    "targetType": "lambda",
                },
                "gatewayTools": ["get_customer"],
                "policyConfig": {},
                "namingProfile": {"prefix": "ecb"},
            },
            id="agentcore-gateway-policy",
        ),
        pytest.param(
            "agentcore-full-tags",
            {
                "connectedTools": ["gateway", "memory", "guardrails"],
                "gatewayConfig": {
                    "gateway_provider": "agentcore",
                    "targetType": "lambda",
                },
                "gatewayTools": ["get_customer"],
                "memoryConfig": {"enabled": True},
                "mcpServerConfig": {"tools": ["get_customer"]},
                "evaluationConfig": {"enabled": True},
                "knowledgeBaseConfig": KB_CONFIG,
                "resourceTags": {
                    "CostCentre": "ECB-42",
                    "Environment": "test",
                },
                "namingProfile": {"prefix": "ecb"},
            },
            id="agentcore-full-tags",
        ),
        pytest.param(
            "litellm-memory",
            {
                "connectedTools": ["gateway", "memory"],
                "gatewayConfig": {
                    "gateway_provider": "litellm",
                    "litellm_base_url": "https://litellm.example.internal",
                    "litellm_servers": ["github", "jira"],
                    "litellm_api_key_ref": ("arn:aws:secretsmanager:us-east-1:123456789012:secret:litellm-key-AbCdEf"),
                },
                "memoryConfig": {"enabled": True},
                "namingProfile": {"prefix": "ecb"},
            },
            id="litellm-memory",
        ),
    ],
)
def test_profiled_export_matrix_is_lint_clean_and_scripts_parse(label, kwargs, tmp_path):
    lint = pytest.importorskip("cfnlint.api", reason="cfn-lint not installed")
    bundle = _generate(**kwargs)
    findings = lint.lint(bundle.template_yaml, regions=["us-east-1"])
    errors = [str(finding) for finding in findings if str(finding.rule.id).startswith("E")]
    assert not errors, f"{label}: {errors}"

    for filename, body in (
        ("deploy.sh", bundle.deploy_sh),
        ("teardown.sh", bundle.teardown_sh),
        ("build-dependency-bundle.sh", bundle.build_bundle_sh),
    ):
        script = tmp_path / filename
        script.write_text(body)
        result = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{filename}: {result.stderr}"
