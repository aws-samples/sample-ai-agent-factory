"""Same-account deployments must be able to reach target resources in another region.

The UI and deployment API accept ``target_region`` without ``target_account_id``.  In
that mode the step Lambdas keep their platform-account credentials and construct service
clients in the requested region.  A role policy synthesized with the platform stack's
home region in every target-resource ARN therefore denies an otherwise supported deploy.

This file deliberately synthesizes the HOME stack.  Synthesizing a second stack in the
target region cannot answer the product question: customers do not deploy another copy
of the platform before choosing a non-home deployment target in the existing UI.

The inverse boundary matters just as much.  Platform configuration (SSM), platform data
(DynamoDB), the registry/trigger secrets, the deployment Lambda itself, and the shared
Cognito pool remain home control-plane resources.  Making every ARN region ``*`` would
make the feature work by unnecessarily widening those resources.  The tests below
therefore classify both sides and resolve statements by ROLE attachment, including CDK's
overflow managed policies.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform import tool_lambda_names
from stacks.platform_stack import PlatformStack

from tests.iam_attachment import statements_by_role

HOME_REGION = "us-east-1"
TARGET_REGION = "eu-west-1"
ACCOUNT = "123456789012"
PROJECT = "acf"
ENVIRONMENT = "f41"

# Every service here has at least one resource created, adopted, invoked, or deleted in
# ``event.target_region`` by DeploymentLambdaRole or a Step*Role.  Keeping the set
# explicit makes dropping an entire service's grants fail instead of shrinking the scan
# until it passes vacuously.
TARGET_RESOURCE_SERVICES = {
    "aoss",
    "bedrock",
    "bedrock-agentcore",
    "cognito-idp",
    "kms",
    "lambda",
    "logs",
    "rds",
    "s3vectors",
    "secretsmanager",
}

EXPECTED_TARGET_ROLE_PREFIXES = {
    "DeploymentLambdaRole",
    "StepGatewayRole",
    "StepGuardrailsRole",
    "StepHarnessRole",
    "StepKnowledgeBaseRole",
    "StepMcpServerRole",
    "StepPolicyRole",
    "StepRuntimeConfigureRole",
    "StepStatusUpdateRole",
}

DYNAMODB_DATA_ACTIONS = {
    "dynamodb:BatchGetItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:ConditionCheckItem",
    "dynamodb:DeleteItem",
    "dynamodb:DescribeTable",
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:UpdateItem",
}


@pytest.fixture(scope="module")
def template_json() -> dict:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "F41Probe",
        environment_name=ENVIRONMENT,
        project_name=PROJECT,
        env=cdk.Environment(region=HOME_REGION, account=ACCOUNT),
    )
    return Template.from_stack(stack).to_json()


def _list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _audited_role(role_logical_id: str) -> bool:
    return role_logical_id.startswith("DeploymentLambdaRole") or role_logical_id.startswith("Step")


def _role_prefix(role_logical_id: str) -> str:
    for prefix in ("DeploymentLambdaRole", *sorted(EXPECTED_TARGET_ROLE_PREFIXES - {"DeploymentLambdaRole"})):
        if role_logical_id.startswith(prefix):
            return prefix
    return role_logical_id


def _literal_arn_rows(template: dict) -> list[tuple[str, str, tuple[str, ...], str]]:
    """``(role, source policy, actions, ARN)`` for relevant attached Allow statements."""
    rows: list[tuple[str, str, tuple[str, ...], str]] = []
    for role, statements in statements_by_role(template).items():
        if not _audited_role(role):
            continue
        for source, statement in statements:
            if statement.get("Effect") != "Allow":
                continue
            actions = tuple(str(action) for action in _list(statement.get("Action")))
            for resource in _list(statement.get("Resource")):
                if isinstance(resource, str) and resource.startswith("arn:"):
                    rows.append((role, source, actions, resource))
    assert rows, "no literal ARNs were resolved from DeploymentLambdaRole or Step*Role"
    return rows


def _arn_fields(arn: str) -> tuple[str, str, str, str]:
    parts = arn.split(":", 5)
    assert len(parts) == 6, f"not a six-field ARN: {arn!r}"
    _arn, _partition, service, region, account, resource = parts
    return service, region, account, resource


def _is_home_control_plane_arn(arn: str) -> bool:
    service, _region, _account, resource = _arn_fields(arn)
    if service == "ssm":
        return resource == f"parameter/agentcore-workflow/{ENVIRONMENT}/*"
    if service == "secretsmanager":
        return resource in {
            "secret:agentcore-trigger/*",
            "secret:agentcore-registry/*",
        }
    if service == "lambda":
        return resource == f"function:{PROJECT}-{ENVIRONMENT}-deployment"
    return False


#: The two shapes of a stack-token tool-Lambda grant: the function (F-7d), and the log
#: group Lambda writes it to, which the gateway step governs with retention.
_STACK_TOKEN_RESOURCES = {
    "lambda": rf"function:{tool_lambda_names.FUNCTION_NAME_PREFIX}-([0-9a-f]{{10}})-\*",
    "logs": rf"log-group:/aws/lambda/{tool_lambda_names.FUNCTION_NAME_PREFIX}-([0-9a-f]{{10}})-\*",
}


def _stack_token(arn: str) -> str | None:
    service, _region, _account, resource = _arn_fields(arn)
    pattern = _STACK_TOKEN_RESOURCES.get(service)
    match = re.fullmatch(pattern, resource) if pattern else None
    return match.group(1) if match else None


def _is_stack_token_lambda_arn(arn: str) -> bool:
    """A tool-Lambda grant of the form ``function:AgentCore-<stack token>-*`` (F-7d), or
    its log group ``log-group:/aws/lambda/AgentCore-<stack token>-*``.

    These are the one family of target-resource ARNs that is deliberately REGION-EXACT
    rather than region-``*``: the token itself is derived from the target region, so the
    grant enumerates one ARN per supported region and a wildcard region would be authority
    over names this platform never creates there. They are pinned by their own test below
    rather than by the region-``*`` rule.
    """
    return _stack_token(arn) is not None


def _is_target_resource_arn(arn: str) -> bool:
    service, _region, _account, _resource = _arn_fields(arn)
    return (
        service in TARGET_RESOURCE_SERVICES
        and not _is_home_control_plane_arn(arn)
        and not _is_stack_token_lambda_arn(arn)
    )


def _format_rows(rows: Iterable[tuple[str, str, tuple[str, ...], str]]) -> list[dict]:
    return [
        {
            "role": _role_prefix(role),
            "policy": source,
            "actions": list(actions),
            "resource": arn,
        }
        for role, source, actions, arn in rows
    ]


def test_target_resource_grants_reach_any_region_but_only_this_account(template_json):
    """The HOME stack's roles can operate on resources in ``TARGET_REGION``.

    The account stays exact.  Cross-account deployments assume the documented target
    role; widening these platform-role ARNs to another account would add authority that
    the same-account regional path neither needs nor uses.
    """
    rows = [row for row in _literal_arn_rows(template_json) if _is_target_resource_arn(row[3])]
    assert rows, "the target-resource classifier found nothing"

    services = {_arn_fields(arn)[0] for _role, _source, _actions, arn in rows}
    assert services == TARGET_RESOURCE_SERVICES, (
        f"target-service coverage drifted: missing={sorted(TARGET_RESOURCE_SERVICES - services)}, "
        f"unexpected={sorted(services - TARGET_RESOURCE_SERVICES)}"
    )

    role_prefixes = {_role_prefix(role) for role, _source, _actions, _arn in rows}
    assert EXPECTED_TARGET_ROLE_PREFIXES <= role_prefixes, (
        "an entire target-capable principal disappeared from the audit: "
        f"{sorted(EXPECTED_TARGET_ROLE_PREFIXES - role_prefixes)}"
    )

    offenders = []
    for row in rows:
        _role, _source, _actions, arn = row
        _service, region, account, _resource = _arn_fields(arn)
        if region != "*" or account != ACCOUNT:
            offenders.append(row)

    assert not offenders, (
        f"the platform is synthesized in {HOME_REGION}, but these attached grants cannot "
        f"reach a same-account target in {TARGET_REGION}; target-resource ARNs must use "
        f"region '*' and account {ACCOUNT}, never a home-region literal or account wildcard:\n"
        f"{json.dumps(_format_rows(offenders), indent=2, sort_keys=True)}"
    )


def test_only_declared_control_plane_arns_remain_home_region_pinned(template_json):
    """Regionalization must not become a blanket ``stack.region -> '*'`` rewrite."""
    rows = _literal_arn_rows(template_json)
    home_rows = [
        row for row in rows if _arn_fields(row[3])[1] == HOME_REGION and not _is_stack_token_lambda_arn(row[3])
    ]
    assert home_rows, "no home-region control-plane ARN survived; the fix over-widened the policy"

    unexpected = [row for row in home_rows if not _is_home_control_plane_arn(row[3])]
    assert not unexpected, (
        "these home-region ARNs are not platform control-plane resources and still block "
        f"the {TARGET_REGION} deployment path:\n"
        f"{json.dumps(_format_rows(unexpected), indent=2, sort_keys=True)}"
    )

    expected_resources = {
        f"parameter/agentcore-workflow/{ENVIRONMENT}/*",
        "secret:agentcore-trigger/*",
        "secret:agentcore-registry/*",
        f"function:{PROJECT}-{ENVIRONMENT}-deployment",
    }
    actual_resources = {_arn_fields(row[3])[3] for row in home_rows}
    assert actual_resources == expected_resources, (
        "the home-control-plane allowlist changed. Add a resource only when its client "
        "deliberately uses platform/home credentials rather than event.target_region: "
        f"missing={sorted(expected_resources - actual_resources)}, "
        f"unexpected={sorted(actual_resources - expected_resources)}"
    )
    for _role, _source, _actions, arn in home_rows:
        _service, region, account, _resource = _arn_fields(arn)
        assert region == HOME_REGION
        assert account == ACCOUNT


def test_target_resource_owner_conditions_follow_the_requested_region(template_json):
    """A wildcard ARN with a home-only owner-tag condition is still an outage.

    Cognito's dedicated-pool ``DescribeUserPoolClient`` grant is the concrete instance:
    the target pool is stamped ``{project}-{env}-{target_region}``, so a policy condition
    frozen to the platform region fails even after its Resource ARN becomes region-wide.
    The IAM global condition variable keeps the ownership value exact for the region in
    which the API request is made.
    """
    expected_owner = f"{PROJECT}-{ENVIRONMENT}-${{aws:RequestedRegion}}"
    owner_values: list[tuple[str, str, str, object]] = []
    conditioned_target_statements = 0

    for role, statements in statements_by_role(template_json).items():
        if not _audited_role(role):
            continue
        for source, statement in statements:
            target_arns = [
                resource
                for resource in _list(statement.get("Resource"))
                if isinstance(resource, str) and resource.startswith("arn:") and _is_target_resource_arn(resource)
            ]
            if not target_arns:
                continue
            condition = statement.get("Condition") or {}
            if condition:
                conditioned_target_statements += 1
            for operator, clauses in condition.items():
                if not isinstance(clauses, dict):
                    continue
                for key, value in clauses.items():
                    if str(key).endswith("AgentCoreStack"):
                        owner_values.append(
                            (
                                _role_prefix(role),
                                source,
                                f"{operator}/{key}",
                                value,
                            )
                        )

    assert conditioned_target_statements >= 5, (
        "too few conditioned target-resource statements were audited; attachment "
        f"resolution or the target classifier drifted ({conditioned_target_statements})"
    )
    assert owner_values, "no target-resource AgentCoreStack condition was audited"

    offenders = [row for row in owner_values if row[3] != expected_owner]
    assert not offenders, (
        "these target-resource owner conditions remain tied to the platform region. "
        f"They must equal {expected_owner!r}, which IAM resolves to the API request's "
        f"region:\n{json.dumps(offenders, indent=2, sort_keys=True)}"
    )


def test_dynamodb_data_access_stays_on_the_home_stack_tables(template_json):
    """Target-region support must not widen the platform's tenant/state tables."""
    table_ids = {
        logical_id
        for logical_id, resource in template_json["Resources"].items()
        if resource["Type"] == "AWS::DynamoDB::Table"
    }
    assert table_ids, "the synthesized platform has no DynamoDB tables"

    examined = 0
    for role, statements in statements_by_role(template_json).items():
        if not _audited_role(role):
            continue
        for source, statement in statements:
            actions = {str(action) for action in _list(statement.get("Action"))}
            if not actions & DYNAMODB_DATA_ACTIONS:
                continue
            examined += 1
            resources = _list(statement.get("Resource"))
            assert resources and "*" not in resources, (
                f"{role}/{source} grants DynamoDB data access without naming a platform table"
            )
            rendered = json.dumps(resources, sort_keys=True)
            assert "arn:aws:dynamodb:" not in rendered, (
                f"{role}/{source} replaced stack-table references with a regional wildcard ARN: {rendered}"
            )
            assert any(table_id in rendered for table_id in table_ids), (
                f"{role}/{source} does not resolve to a DynamoDB table in this stack: {rendered}"
            )

    assert examined >= 20, f"only {examined} DynamoDB data statements were audited"


@pytest.mark.parametrize("role_prefix", ["StepGatewayRole", "StepHarnessRole"])
def test_shared_pool_secret_read_remains_bound_to_the_platform_pool(template_json, role_prefix):
    """The shared pool is a home control-plane dependency, not a target-region pool."""
    matching_roles = [role for role in statements_by_role(template_json) if role.startswith(role_prefix)]
    assert len(matching_roles) == 1, f"expected one {role_prefix}, found {matching_roles}"

    exact_pool_reads = []
    for _source, statement in statements_by_role(template_json)[matching_roles[0]]:
        actions = {str(action) for action in _list(statement.get("Action"))}
        resource = statement.get("Resource")
        if "cognito-idp:DescribeUserPoolClient" not in actions or not isinstance(resource, dict):
            continue
        get_att = resource.get("Fn::GetAtt")
        if (
            isinstance(get_att, list)
            and len(get_att) == 2
            and str(get_att[0]).startswith("GatewayAuthUserPool")
            and get_att[1] == "Arn"
        ):
            exact_pool_reads.append(resource)

    assert len(exact_pool_reads) == 1, (
        f"{role_prefix} must keep one exact DescribeUserPoolClient grant on the retained "
        f"GatewayAuthUserPool, found {exact_pool_reads}"
    )


def test_tool_lambda_grants_enumerate_every_supported_region_exactly(template_json):
    """F-7d: the tool-Lambda grants are region-EXACT, one per supported target region.

    Unlike every other target-resource ARN, these are not region-``*``: the name token is
    derived from the target region, so a token for region R granted in region S would be
    authority over a same-account name this platform never creates there. What must hold
    instead (peer a3): the set of ARN regions is exactly ``SUPPORTED_TARGET_REGIONS`` (no
    missing advertised region, no extra), each ARN's region field is the region its token
    was derived from (no cross-region token leakage), the account is exact, and there is no
    wildcard in either field. The token formula is the backend's, mirrored in
    ``stacks/platform/tool_lambda_names.py`` and pinned by ``test_tool_lambda_name_parity``.
    """
    rows = [row for row in _literal_arn_rows(template_json) if _is_stack_token_lambda_arn(row[3])]
    assert rows, "no stack-token tool-Lambda grant found; the F-7d prefix grants are missing"

    expected = {
        region: tool_lambda_names.stack_scope_token(PROJECT, ENVIRONMENT, region)
        for region in tool_lambda_names.SUPPORTED_TARGET_REGIONS
    }
    seen_regions: set[str] = set()
    offenders = []
    for row in rows:
        _role, _source, _actions, arn = row
        _service, region, account, _resource = _arn_fields(arn)
        token = _stack_token(arn)
        seen_regions.add(region)
        if region == "*" or account != ACCOUNT or expected.get(region) != token:
            offenders.append(row)
    assert not offenders, (
        "tool-Lambda grants must be region-exact, account-exact, and carry the token of their own "
        f"region:\n{json.dumps(_format_rows(offenders), indent=2, sort_keys=True)}"
    )
    assert seen_regions == set(tool_lambda_names.SUPPORTED_TARGET_REGIONS), (
        f"missing={sorted(set(tool_lambda_names.SUPPORTED_TARGET_REGIONS) - seen_regions)}, "
        f"extra={sorted(seen_regions - set(tool_lambda_names.SUPPORTED_TARGET_REGIONS))}"
    )
    # Every role that holds one token ARN of a family (function, log group) holds that whole
    # family: a role granted only the home region's token would pass the set check above
    # through another role, or through the other family, and still block F-41.
    per_role: dict[tuple[str, str], set[str]] = {}
    for role, _source, _actions, arn in rows:
        service, region, _account, _resource = _arn_fields(arn)
        per_role.setdefault((_role_prefix(role), service), set()).add(region)
    partial = {
        f"{role} {service}": sorted(regions) for (role, service), regions in per_role.items() if regions != seen_regions
    }
    assert not partial, f"roles holding only some regions' token grants: {partial}"
    assert {service for _role, service in per_role} == set(_STACK_TOKEN_RESOURCES), (
        f"a stack-token grant family disappeared: {sorted(set(_STACK_TOKEN_RESOURCES) - {s for _r, s in per_role})}"
    )
