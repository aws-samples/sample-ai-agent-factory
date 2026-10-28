"""``safety.guardrails`` + ``policy.cedar_enforce`` + ``connector.gateway_framework`` — gateway governance.

Foundation module (deterministic deploy/teardown + the deploy-body builder). The live drive and the
eleven exact oracles are added on top of this base. The family deploys, through the product
``/api/deploy``, a gateway+runtime whose ``generic_openapi`` connector points at a private fixture
exposing seven operations, with a Bedrock Guardrail (word filters) and a Cedar policy component
(forbidding the two "denied" probes). Each scenario drives the product runtime to call one operation
and asserts the contract's exact oracle (runtime success, fixture-dispatch count, response shape).

This base intentionally does NOT register scenario ids yet — the drive/oracle layer that follows
registers them, so no half-working id is dispatchable from this commit.
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path
from typing import Any

from _live import REQUIRED_USER_GROUPS

FAMILY = "gateway-governance"
GOV_GROUPS = REQUIRED_USER_GROUPS | {"g-admins-super"}
FIXTURE_SOURCE = Path(__file__).resolve().parent / "gateway_governance_fixture.py"
FIXTURE_TAGS = {"ManagedBy": "agentcore-flows-tests", "Purpose": "gateway-governance-live-gate"}

# The three guardrail word filters the fixture's blocked outputs / blocked arguments trip.
GUARDRAIL_WORD_FILTERS = [
    "ACFPR_BLOCK_ARGUMENT_B43627B",
    "ACFPR_BLOCK_RESULT_B43627B",
    "ACFPR_CONNECTOR_GUARDRAIL_B43627B",
]
BLOCKED_RESULT_VALUE = "ACFPR_BLOCK_RESULT_B43627B result payload"
# Cedar must FORBID these two probes; every other operation is permitted.
CEDAR_FORBIDDEN_OPERATIONS = ["policy_denied_probe", "connector_denied_probe"]

# Fixture path -> operationId, and the inverse used to build per-operation OpenAPI + prompts.
PATH_OPERATIONS = {
    "/guardrail-safe-echo": "guardrail_safe_echo",
    "/guardrail-blocked-result": "guardrail_blocked_result",
    "/policy-allowed": "policy_allowed_probe",
    "/policy-denied": "policy_denied_probe",
    "/connector-allowed": "connector_allowed_probe",
    "/connector-denied": "connector_denied_probe",
    "/connector-guardrail-denied": "connector_guardrail_denied",
}


def new_marker(token: str, slug: str) -> str:
    """A fixture marker matching the fixture's strict ^gg-<12hex>-<slug>$ format."""
    return f"gg-{token}-{slug}"


def openapi_spec(url: str) -> str:
    """OpenAPI 3.0.1 spec exposing the seven governance operations as POST tools with marker+value."""
    paths: dict[str, Any] = {}
    for path, operation in PATH_OPERATIONS.items():
        paths[path] = {
            "post": {
                "operationId": operation,
                "description": f"Governance probe {operation}; echoes a marker and optional value.",
                "parameters": [
                    {"name": "marker", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "value", "in": "query", "required": False, "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {
                        "description": "probe result",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["kind", "marker"],
                                    "properties": {
                                        "kind": {"type": "string"},
                                        "marker": {"type": "string"},
                                        "value": {"type": "string"},
                                    },
                                }
                            }
                        },
                    }
                },
            }
        }
    return json.dumps(
        {
            "openapi": "3.0.1",
            "info": {"title": "Gateway governance fixture", "version": "1.0.0"},
            "servers": [{"url": url}],
            "paths": paths,
        },
        separators=(",", ":"),
    )


def fixture_zip(region: str, secret_parameter: str, fixture_id: str) -> bytes:
    """Zip the tracked fixture module plus the sibling fixture_config.json it reads at import."""
    config = {
        "region": region,
        "secret_parameter": secret_parameter,
        "fixture_id": fixture_id,
        "blocked_result": BLOCKED_RESULT_VALUE,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        source = zipfile.ZipInfo("fixture.py")
        source.external_attr = 0o644 << 16
        archive.writestr(source, FIXTURE_SOURCE.read_text())
        cfg = zipfile.ZipInfo("fixture_config.json")
        cfg.external_attr = 0o644 << 16
        archive.writestr(cfg, json.dumps(config, separators=(",", ":")))
    return buffer.getvalue()


def fixture_role_policy(names: dict[str, str], region: str, account_id: str) -> dict[str, Any]:
    parameter_arn = f"arn:aws:ssm:{region}:{account_id}:parameter{names['parameter']}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "WriteOnlyOwnLogGroup",
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"arn:aws:logs:{region}:{account_id}:log-group:{names['log_group']}:*",
            },
            {"Sid": "ReadOnlyOwnSecret", "Effect": "Allow", "Action": "ssm:GetParameter", "Resource": parameter_arn},
            {
                "Sid": "DecryptOwnSsm",
                "Effect": "Allow",
                "Action": "kms:Decrypt",
                "Resource": f"arn:aws:kms:{region}:{account_id}:key/*",
                "Condition": {"StringEquals": {"kms:ViaService": f"ssm.{region}.amazonaws.com"}},
            },
        ],
    }


def create_fixture(session: Any, names: dict[str, str], region: str, account_id: str, secret: str) -> str:
    """Deploy the governance fixture Lambda behind a public Function URL; return its URL."""
    from botocore.exceptions import ClientError

    iam, logs, ssm, lam = (session.client(s) for s in ("iam", "logs", "ssm", "lambda"))
    tags = {**FIXTURE_TAGS, "FixtureId": names["token"]}
    logs.create_log_group(logGroupName=names["log_group"], tags=tags)
    logs.put_retention_policy(logGroupName=names["log_group"], retentionInDays=1)
    ssm.put_parameter(Name=names["parameter"], Value=secret, Type="SecureString", Overwrite=False)
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}
        ],
    }
    role_arn = iam.create_role(
        RoleName=names["role"],
        AssumeRolePolicyDocument=json.dumps(trust, separators=(",", ":")),
        MaxSessionDuration=3600,
        Tags=[{"Key": k, "Value": v} for k, v in sorted(tags.items())],
    )["Role"]["Arn"]
    iam.put_role_policy(
        RoleName=names["role"],
        PolicyName="fixture",
        PolicyDocument=json.dumps(fixture_role_policy(names, region, account_id), separators=(",", ":")),
    )
    deadline = time.monotonic() + 90
    while True:
        try:
            lam.create_function(
                FunctionName=names["function"],
                Runtime="python3.13",
                Role=role_arn,
                Handler="fixture.handler",
                Code={"ZipFile": fixture_zip(region, names["parameter"], names["token"])},
                Timeout=30,
                MemorySize=128,
                Architectures=["arm64"],
                Tags=tags,
            )
            break
        except ClientError as error:
            if (
                error.response.get("Error", {}).get("Code") != "InvalidParameterValueException"
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(5)
    lam.get_waiter("function_active_v2").wait(
        FunctionName=names["function"], WaiterConfig={"Delay": 3, "MaxAttempts": 40}
    )
    url = lam.create_function_url_config(FunctionName=names["function"], AuthType="NONE")["FunctionUrl"]
    lam.add_permission(
        FunctionName=names["function"],
        StatementId="public-function-url",
        Action="lambda:InvokeFunctionUrl",
        Principal="*",
        FunctionUrlAuthType="NONE",
    )
    return url.rstrip("/")


def delete_fixture(session: Any, names: dict[str, str]) -> dict[str, bool]:
    from botocore.exceptions import ClientError

    iam, logs, ssm, lam = (session.client(s) for s in ("iam", "logs", "ssm", "lambda"))
    result: dict[str, bool] = {}

    def _try(label: str, fn) -> None:
        try:
            fn()
            result[label] = True
        except ClientError as error:
            result[label] = error.response.get("Error", {}).get("Code") in {
                "ResourceNotFoundException",
                "NoSuchEntity",
                "ParameterNotFound",
                "404",
            }

    _try("function", lambda: lam.delete_function(FunctionName=names["function"]))
    _try("role_policy", lambda: iam.delete_role_policy(RoleName=names["role"], PolicyName="fixture"))
    _try("role", lambda: iam.delete_role(RoleName=names["role"]))
    _try("parameter", lambda: ssm.delete_parameter(Name=names["parameter"]))
    _try("log_group", lambda: logs.delete_log_group(logGroupName=names["log_group"]))
    return result


def cedar_policy_component() -> dict[str, Any]:
    """A Cedar policy component permitting the allowed probes and forbidding the two denied ones."""
    rules = []
    for operation in CEDAR_FORBIDDEN_OPERATIONS:
        rules.append(
            {
                "rule_id": f"forbid-{operation}".replace("_", "-")[:100],
                "effect": "forbid",
                "principal": "*",
                "action": 'Action::"invoke_tool"',
                "resource": f'Tool::"{operation}"',
                "description": f"Deny the {operation} governance probe",
            }
        )
    return {
        "component_type": "policy",
        "name": "gateway-governance-cedar",
        "enabled": True,
        "rules": rules,
        "default_effect": "permit",
    }


def deploy_body(names: dict[str, str], fixture_url: str, secret: str, runtime_config: dict[str, Any]) -> dict[str, Any]:
    """The product /api/deploy body: runtime + m2m connector (the 7 ops) + guardrail word filters + Cedar."""
    return {
        "nodeId": names["node_id"],
        "config": runtime_config,
        "connectedTools": ["gateway"],
        "gatewayConfig": {"name": names["runtime_name"]},
        "guardrailsConfig": {"word_filters": GUARDRAIL_WORD_FILTERS},
        "policy": cedar_policy_component(),
        "connectors": [
            {
                "connectorId": "generic_openapi",
                "authMethod": "api_key",
                "secretValue": secret,
                "specInline": openapi_spec(fixture_url),
                # The gateway target injects "{credentialPrefix} {key}" at this header; AgentCore
                # supplies the separating space, so the prefix is bare "Bearer". This reproduces the
                # fixture's required `Authorization: Bearer <secret>` exactly.
                "credentialLocation": "HEADER",
                "credentialParameterName": "Authorization",
                "credentialPrefix": "Bearer",
            }
        ],
    }


__all__ = [
    "FAMILY",
    "GOV_GROUPS",
    "GUARDRAIL_WORD_FILTERS",
    "CEDAR_FORBIDDEN_OPERATIONS",
    "PATH_OPERATIONS",
    "new_marker",
    "openapi_spec",
    "fixture_zip",
    "create_fixture",
    "delete_fixture",
    "cedar_policy_component",
    "deploy_body",
]
