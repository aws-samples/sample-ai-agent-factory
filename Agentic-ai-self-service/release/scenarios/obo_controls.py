"""``identity.delegated_obo`` — delegated workforce on-behalf-of identity on the deployed stack.

One journey deploys a private RFC 8693 token-exchange/whoami fixture Lambda (``obo_fixture_lambda``)
behind a Function URL, then deploys, through the product ``/api/deploy``, an OBO gateway+runtime whose
``generic_openapi`` connector uses ``delegationMode=obo`` / ``oboGrantType=TOKEN_EXCHANGE`` against the
fixture. With two distinct valid workforce users it proves:

* **workforce-token-exchange-whoami** — the owner's workforce JWT reaches the gateway token exchange
  and downstream ``whoami`` both directly (MCP ``tools/call`` → 200, principal/issuer/client_id match)
  and through the product's staging-slot runtime path (``identityEvidence`` shows
  ``ON_BEHALF_OF_TOKEN_EXCHANGE`` for the owner subject and a successful whoami tool receipt).
* **substituted-subject-denied** — a second valid workforce user's token is refused by the gateway
  custom-claim authorization rule (403 insufficient_scope — the live behaviour; the stale audited
  constant said 401), with a ``Bearer`` challenge and the bearer never echoed.
* **direct-runtime-bypass-denied** — the owner's bearer cannot directly invoke the IAM-only runtime
  (403), bearer never echoed.

Teardown removes the runtime (through the product), the fixture Lambda + Function URL + IAM role +
SSM secret + log group, and both synthetic users; secrets and tokens stay process-local.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from _live import (
    REQUIRED_USER_GROUPS,
    BearerSession,
    ObservedStack,
    RunBinding,
    ScenarioFailed,
    boto3_session,
    bound_deployment,
    family_journey,
    redact,
    with_synthetic_user,
    write_json,
)

FAMILY = "obo-controls"
OBO_GROUPS = REQUIRED_USER_GROUPS | {"g-admins-cost"}
FIXTURE_TAGS = {"ManagedBy": "agentcore-flows-tests", "Purpose": "delegated-obo-live-gate"}
DIRECT_RUNTIME_DENIAL_STATUS = 403
SUBSTITUTED_GATEWAY_DENIAL_STATUS = 403  # live: valid-but-unauthorized subject => 403 insufficient_scope
FIXTURE_SOURCE = Path(__file__).resolve().parent / "obo_fixture_lambda.py"


def _claims(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(__import__("base64").urlsafe_b64decode(payload))


def _subject_hash(subject: str) -> str:
    return hashlib.sha256(subject.encode()).hexdigest()[:16]


def _fixture_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("fixture.py")
        info.external_attr = 0o644 << 16
        archive.writestr(info, FIXTURE_SOURCE.read_text())
    return buffer.getvalue()


def _role_policy(names: dict[str, str], region: str, account_id: str) -> dict[str, Any]:
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
            {
                "Sid": "ReadOnlyOwnClientSecret",
                "Effect": "Allow",
                "Action": "ssm:GetParameter",
                "Resource": parameter_arn,
            },
            {
                "Sid": "DecryptOnlyOwnSsmParameter",
                "Effect": "Allow",
                "Action": "kms:Decrypt",
                "Resource": f"arn:aws:kms:{region}:{account_id}:key/*",
                "Condition": {"StringEquals": {"kms:ViaService": f"ssm.{region}.amazonaws.com"}},
            },
            {
                "Sid": "ValidateOnlyWorkforceAccessToken",
                "Effect": "Allow",
                "Action": "cognito-idp:GetUser",
                "Resource": "*",
            },
        ],
    }


def _openapi(url: str) -> str:
    return json.dumps(
        {
            "openapi": "3.0.1",
            "info": {"title": "Synthetic OBO whoami", "version": "1.0.0"},
            "servers": [{"url": url}],
            "paths": {
                "/whoami": {
                    "get": {
                        "operationId": "whoami",
                        "description": "Return the authenticated downstream workforce principal.",
                        "responses": {
                            "200": {
                                "description": "Authenticated principal",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "required": ["principal", "sub", "issuer", "client_id"],
                                            "properties": {
                                                "principal": {"type": "string"},
                                                "sub": {"type": "string"},
                                                "issuer": {"type": "string"},
                                                "client_id": {"type": "string"},
                                                "username": {"type": "string"},
                                            },
                                        }
                                    }
                                },
                            }
                        },
                    }
                }
            },
        },
        separators=(",", ":"),
    )


def _runtime_config(runtime_name: str) -> dict[str, Any]:
    return {
        "runtimeName": runtime_name,
        "template": "strands-gateway-agent",
        "systemPrompt": "You are an OBO certification agent. When asked, call the whoami tool exactly once.",
        "modelId": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    }


# --------------------------------------------------------------------------- fixture lifecycle


def _create_fixture(session: Any, names: dict[str, str], region: str, account_id: str, client_secret: str) -> str:
    from botocore.exceptions import ClientError

    iam, logs, ssm, lam = (session.client(s) for s in ("iam", "logs", "ssm", "lambda"))
    tags = {**FIXTURE_TAGS, "FixtureId": names["token"]}
    logs.create_log_group(logGroupName=names["log_group"], tags=tags)
    logs.put_retention_policy(logGroupName=names["log_group"], retentionInDays=1)
    ssm.put_parameter(Name=names["parameter"], Value=client_secret, Type="SecureString", Overwrite=False)
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
        PolicyName=names["role_policy"],
        PolicyDocument=json.dumps(_role_policy(names, region, account_id), separators=(",", ":")),
    )
    create = {
        "FunctionName": names["function"],
        "Runtime": "python3.13",
        "Role": role_arn,
        "Handler": "fixture.handler",
        "Code": {"ZipFile": _fixture_zip()},
        "Timeout": 30,
        "MemorySize": 128,
        "Architectures": ["arm64"],
        "Environment": {
            "Variables": {
                "FIXTURE_CLIENT_ID": names["client_id"],
                "FIXTURE_SECRET_PARAMETER": names["parameter"],
                "WORKFORCE_ISSUER": names["workforce_issuer"],
                "WORKFORCE_CLIENT_ID": names["workforce_client_id"],
                "WORKFORCE_REGION": region,
            }
        },
        "Tags": tags,
    }
    deadline = time.monotonic() + 90
    while True:
        try:
            lam.create_function(**create)
            break
        except ClientError as error:
            if (
                error.response.get("Error", {}).get("Code") != "InvalidParameterValueException"
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(5)
    waiter = lam.get_waiter("function_active_v2")
    waiter.wait(FunctionName=names["function"], WaiterConfig={"Delay": 3, "MaxAttempts": 40})
    url = lam.create_function_url_config(FunctionName=names["function"], AuthType="NONE")["FunctionUrl"]
    lam.add_permission(
        FunctionName=names["function"],
        StatementId="public-function-url",
        Action="lambda:InvokeFunctionUrl",
        Principal="*",
        FunctionUrlAuthType="NONE",
    )
    return url.rstrip("/")


def _delete_fixture(session: Any, names: dict[str, str]) -> dict[str, bool]:
    from botocore.exceptions import ClientError

    iam, logs, ssm, lam = (session.client(s) for s in ("iam", "logs", "ssm", "lambda"))
    result: dict[str, bool] = {}

    def _try(label: str, fn) -> None:
        try:
            fn()
            result[label] = True
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            result[label] = code in {"ResourceNotFoundException", "NoSuchEntity", "ParameterNotFound", "404"}

    _try("function", lambda: lam.delete_function(FunctionName=names["function"]))
    _try("role_policy", lambda: iam.delete_role_policy(RoleName=names["role"], PolicyName=names["role_policy"]))
    _try("role", lambda: iam.delete_role(RoleName=names["role"]))
    _try("parameter", lambda: ssm.delete_parameter(Name=names["parameter"]))
    _try("log_group", lambda: logs.delete_log_group(logGroupName=names["log_group"]))
    return result


# --------------------------------------------------------------------------- MCP + facet proofs


def _mcp_post(gateway_url: str, token: str, payload: dict[str, Any], timeout: int = 60) -> requests.Response:
    return requests.post(
        gateway_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        data=json.dumps(payload),
        timeout=timeout,
    )


def _walk(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(value, dict):
        out.append(value)
        for v in value.values():
            out.extend(_walk(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(_walk(v))
    return out


def _mcp_objects(response: requests.Response) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for line in response.text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line:
            continue
        try:
            objects.append(json.loads(line))
        except ValueError:
            continue
    return objects


def _wait_tools(gateway_url: str, token: str) -> str:
    deadline = time.monotonic() + 300
    last = ""
    while time.monotonic() < deadline:
        response = _mcp_post(gateway_url, token, {"jsonrpc": "2.0", "id": "list", "method": "tools/list", "params": {}})
        last = f"{response.status_code}: {response.text[:200]}"
        if response.status_code == 200:
            for obj in _mcp_objects(response):
                for tool in _walk(obj):
                    name = str(tool.get("name") or "")
                    if name.endswith("whoami") or name == "whoami":
                        return name
        time.sleep(10)
    raise ScenarioFailed(f"gateway whoami tool never became listable: {last}")


def _call_whoami(
    gateway_url: str, token: str, tool_name: str, expected_sub: str, issuer: str, client_id: str
) -> dict[str, Any]:
    response = _mcp_post(
        gateway_url,
        token,
        {"jsonrpc": "2.0", "id": "call", "method": "tools/call", "params": {"name": tool_name, "arguments": {}}},
    )
    if response.status_code != 200:
        raise ScenarioFailed(f"owner whoami returned {response.status_code}: {response.text[:300]}")
    identities = [
        obj
        for value in _mcp_objects(response)
        for obj in _walk(value)
        if obj.get("principal") == expected_sub
        and obj.get("sub") == expected_sub
        and obj.get("issuer") == issuer
        and obj.get("client_id") == client_id
    ]
    if not identities:
        raise ScenarioFailed(f"gateway whoami omitted the expected principal: {response.text[:400]}")
    return {"status": 200, "toolName": tool_name, "principal": expected_sub, "issuer": issuer, "clientId": client_id}


def _substituted_denial(gateway_url: str, attacker_token: str) -> dict[str, Any]:
    response = _mcp_post(
        gateway_url, attacker_token, {"jsonrpc": "2.0", "id": "sub", "method": "tools/list", "params": {}}, timeout=30
    )
    if response.status_code != SUBSTITUTED_GATEWAY_DENIAL_STATUS:
        raise ScenarioFailed(
            f"substituted subject returned {response.status_code}, expected {SUBSTITUTED_GATEWAY_DENIAL_STATUS}: {response.text[:300]}"
        )
    if attacker_token in response.text:
        raise ScenarioFailed("substituted-subject denial echoed the bearer token")
    challenge = str(response.headers.get("www-authenticate") or "")
    return {
        "status": response.status_code,
        "wwwAuthenticate": challenge[:60],
        "bearerEchoed": False,
        "bodySha256": hashlib.sha256(response.content).hexdigest(),
    }


def _direct_runtime_bypass(region: str, runtime_arn: str, owner_token: str) -> dict[str, Any]:
    url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{quote(runtime_arn, safe='')}/invocations?qualifier=DEFAULT"
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {owner_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=json.dumps({"prompt": "Call whoami for bypass marker"}),
        timeout=30,
    )
    if response.status_code != DIRECT_RUNTIME_DENIAL_STATUS:
        raise ScenarioFailed(
            f"direct bearer runtime request returned {response.status_code}, expected {DIRECT_RUNTIME_DENIAL_STATUS}: {response.text[:300]}"
        )
    if owner_token in response.text:
        raise ScenarioFailed("direct-runtime bypass echoed the bearer token")
    return {
        "status": response.status_code,
        "errorType": response.headers.get("x-amzn-errortype"),
        "bearerEchoed": False,
        "bodySha256": hashlib.sha256(response.content).hexdigest(),
    }


def _mediated_runtime(http: Any, runtime_name: str, version_id: str, claims: dict[str, Any]) -> dict[str, Any]:
    response = http.post(
        f"{http.base_url}/api/runtimes/{runtime_name}/slots/staging/invoke",
        json={
            "input": "Call the whoami tool exactly once now and return only its principal field.",
            "sessionId": f"obo-{uuid.uuid4()}",
        },
        timeout=180,
    )
    if response.status_code != 200:
        raise ScenarioFailed(f"mediated runtime invoke returned {response.status_code}: {response.text[:300]}")
    body = response.json()
    evidence = body.get("identityEvidence") or {}
    receipts = [
        r
        for r in (body.get("toolReceipts") or [])
        if isinstance(r, dict)
        and (r.get("name") == "whoami" or str(r.get("name") or "").endswith("whoami"))
        and r.get("status") == "success"
    ]
    ok = (
        body.get("success") is True
        and body.get("slot") == "staging"
        and body.get("versionId") == version_id
        and evidence.get("subject") == claims["sub"]
        and evidence.get("issuer") == claims["iss"]
        and evidence.get("gateway_exchange") == "ON_BEHALF_OF_TOKEN_EXCHANGE"
        and len(receipts) == 1
        and str(claims["sub"]) in str(body.get("response") or "")
    )
    if not ok:
        raise ScenarioFailed(f"mediated runtime OBO evidence incomplete: evidence={evidence} receipts={len(receipts)}")
    return {
        "status": 200,
        "slot": "staging",
        "versionId": version_id,
        "identityEvidence": evidence,
        "whoamiReceipt": receipts[0],
        "responseContainsExactPrincipal": True,
    }


# --------------------------------------------------------------------------- journey


def _gateway_url(http: Any, runtime_name: str, status: dict[str, Any]) -> tuple[str, bool]:
    if status.get("gateway_url"):
        return str(status["gateway_url"]).rstrip("/"), bool(status.get("obo_required"))
    response = http.get(f"{http.base_url}/api/runtimes/{runtime_name}", timeout=60)
    response.raise_for_status()
    info = response.json()
    gateway = info.get("gateway") or info
    url = str(gateway.get("gateway_url") or gateway.get("gatewayUrl") or "")
    if not url.startswith("https://"):
        raise ScenarioFailed(f"could not resolve the OBO gateway URL for {runtime_name}")
    return url.rstrip("/"), gateway.get("obo_required") is True


def _journey(args: argparse.Namespace, binding: RunBinding, observed: ObservedStack, artifacts: Path) -> dict[str, Any]:
    import sys as _sys

    from _live import PROJECT_ROOT as _ROOT

    _backend = str(_ROOT / "backend")
    if _backend not in _sys.path:
        _sys.path.insert(0, _backend)
    from tests.integration.conftest import DeploymentCleanupTracker, wait_for_deployment_terminal

    region, account_id = binding.region, str(args.account_id)
    token = uuid.uuid4().hex[:12]
    client_secret = __import__("secrets").token_urlsafe(48)
    names = {
        "token": token,
        "function": f"acfpr-obo-fixture-{token}",
        "role": f"AcfprOboFixture{token}",
        "role_policy": "fixture",
        "parameter": f"/acfpr/obo/{token}/client-secret",
        "log_group": f"/aws/lambda/acfpr-obo-fixture-{token}",
        "client_id": f"acfpr-obo-{token}",
        "node_id": f"it-obo-{token}",
        "runtime_name": f"itobo{token}",
        "workforce_issuer": f"https://cognito-idp.{region}.amazonaws.com/{observed.outputs['UserPoolId']}",
        "workforce_client_id": observed.outputs["UserPoolClientId"],
    }
    outcome: dict[str, Any] = {"region": region, "run_token": token}
    session = boto3_session(region)
    secrets_to_hide = [client_secret]
    fixture_made = False
    with (
        with_synthetic_user(observed, label="obo-owner", groups=OBO_GROUPS) as owner,
        with_synthetic_user(observed, label="obo-attacker", groups=OBO_GROUPS) as attacker,
    ):
        owner_claims, attacker_claims = _claims(owner.token), _claims(attacker.token)
        secrets_to_hide += [owner.token, attacker.token]
        if owner_claims["sub"] == attacker_claims["sub"]:
            raise ScenarioFailed("owner and attacker resolved to the same subject")
        http = BearerSession(owner, observed.cloudfront_url)
        tracker = DeploymentCleanupTracker(http, logs_client=session.client("logs"))
        try:
            fixture_url = _create_fixture(session, names, region, account_id, client_secret)
            fixture_made = True
            outcome["fixture_url_host"] = fixture_url.split("//", 1)[-1].split("/", 1)[0]
            body = {
                "nodeId": names["node_id"],
                "config": _runtime_config(names["runtime_name"]),
                "connectedTools": ["gateway"],
                "gatewayConfig": {"name": names["runtime_name"]},
                "connectors": [
                    {
                        "connectorId": "generic_openapi",
                        "authMethod": "oauth2_cc",
                        "secretValue": client_secret,
                        "specInline": _openapi(fixture_url),
                        "scopes": ["whoami.read"],
                        "clientId": names["client_id"],
                        "oauthVendor": "CustomOauth2",
                        "discoveryUrl": f"{fixture_url}/.well-known/openid-configuration",
                        "delegationMode": "obo",
                        "oboGrantType": "TOKEN_EXCHANGE",
                    }
                ],
            }
            deploy = http.post(f"{http.base_url}/api/deploy", json=body, timeout=90)
            if deploy.status_code != 202:
                raise ScenarioFailed(f"OBO deploy returned {deploy.status_code}: {deploy.text[:300]}")
            deployment_id = deploy.json()["deploymentId"]
            record = tracker.track(deployment_id)
            status = wait_for_deployment_terminal(http, deployment_id, timeout=30 * 60)
            record.last_status = status
            if status.get("status") != "succeeded":
                raise ScenarioFailed(f"OBO deploy ended {status.get('status')}")
            tracker.bind_runtime(record, status["runtime_id"])
            runtime_arn, version_id = str(status["runtime_arn"]), str(status["version_id"])
            gateway_url, obo_required = _gateway_url(http, names["runtime_name"], status)
            outcome["gateway_config"] = {
                "obo_required": obo_required,
                "gateway_url_host": gateway_url.split("//", 1)[-1].split("/", 1)[0],
            }

            tool_name = _wait_tools(gateway_url, owner.token)
            direct = _call_whoami(
                gateway_url,
                owner.token,
                tool_name,
                str(owner_claims["sub"]),
                owner_claims["iss"],
                owner_claims["client_id"],
            )
            mediated = _mediated_runtime(http, names["runtime_name"], version_id, owner_claims)
            outcome["workforce-token-exchange-whoami"] = {"directGatewayWhoami": direct, "mediatedRuntime": mediated}
            outcome["substituted-subject-denied"] = _substituted_denial(gateway_url, attacker.token)
            outcome["direct-runtime-bypass-denied"] = _direct_runtime_bypass(region, runtime_arn, owner.token)

            serialized = json.dumps(outcome)
            if owner.token in serialized or attacker.token in serialized or client_secret in serialized:
                raise ScenarioFailed("a secret leaked into the recorded outcome")
        finally:
            cleanup: dict[str, Any] = {}
            try:
                cleanup["deployments"] = tracker.cleanup()
            except Exception as error:  # noqa: BLE001
                cleanup["deployments_error"] = redact(str(error), *secrets_to_hide)
            if fixture_made:
                cleanup["fixture"] = _delete_fixture(session, names)
            outcome["cleanup"] = cleanup
            http.close()
    write_json(artifacts / "journey-outcome.json", outcome)
    return outcome


def _scenario(args: argparse.Namespace, artifact_dir: Path, claim: str) -> dict:
    binding, observed, comparison = bound_deployment(args, artifact_dir)
    journey = family_journey(
        binding,
        observed,
        family=FAMILY,
        run=lambda artifacts: _journey(args, binding, observed, artifacts),
        artifact_dir=artifact_dir,
    )
    outcome = journey["outcome"]
    facet = outcome.get(claim) or {}
    if claim == "workforce-token-exchange-whoami":
        holds = (facet.get("directGatewayWhoami") or {}).get("status") == 200 and (
            facet.get("mediatedRuntime") or {}
        ).get("responseContainsExactPrincipal") is True
    elif claim == "substituted-subject-denied":
        holds = facet.get("status") == SUBSTITUTED_GATEWAY_DENIAL_STATUS and facet.get("bearerEchoed") is False
    elif claim == "direct-runtime-bypass-denied":
        holds = facet.get("status") == DIRECT_RUNTIME_DENIAL_STATUS and facet.get("bearerEchoed") is False
    else:
        raise ScenarioFailed(f"unknown OBO claim {claim}")
    report = {
        "scenario": args.scenario_id,
        "region": binding.region,
        "deployment_binding": comparison,
        "journey": {k: v for k, v in journey.items() if k != "outcome"},
        "claim": claim,
        "claim_holds": holds,
        "facet": facet,
        "cleanup": outcome.get("cleanup"),
    }
    write_json(artifact_dir / "report", report)
    if not holds:
        raise ScenarioFailed(f"{claim}: {json.dumps(facet)[:300]}")
    return report


def workforce_token_exchange_whoami(args, artifact_dir):
    return _scenario(args, artifact_dir, "workforce-token-exchange-whoami")


def substituted_subject_denied(args, artifact_dir):
    return _scenario(args, artifact_dir, "substituted-subject-denied")


def direct_runtime_bypass_denied(args, artifact_dir):
    return _scenario(args, artifact_dir, "direct-runtime-bypass-denied")
