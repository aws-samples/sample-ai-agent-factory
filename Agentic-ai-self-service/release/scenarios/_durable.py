"""Shared primitives for the durable-control live scenarios (audit, cost, JIT approval).

Each durable family binds to the deployed ``acf-production`` stack through :mod:`_live`, then
exercises an append-only / authorization / approval control directly against the production
DynamoDB tables and, for the fail-closed proofs, a **private owner-tagged clone** of the deployed
``DeploymentLambda`` whose only change is a store table redirected to a proven-absent name. The
clone is created from the exact bytes the binding already verified as the published candidate asset,
its ``CodeSha256`` is re-checked after creation, and it is torn down only when its tags still prove
this run owns it.

Nothing here reads credential files. boto3 sessions come from :func:`_live.boto3_session`, which
re-resolves the broker's short-lived vend so a long journey survives a re-vend.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.request
import uuid
from datetime import UTC, datetime
from typing import Any

from _live import STACK_NAME, ScenarioFailed

DEPLOYMENT_LAMBDA_LOGICAL = "DeploymentLambda5F4766EE"
_CLONE_TAGS_BASE = {"ManagedBy": "agentcore-flows-tests", "Purpose": "durable-control-live-gate"}

BUDGET_UNIT_MICRO_USD = 1_000_000

# A thin wrapper module added to the candidate ZIP for the cost family. It calls the deployed
# backend's own reserve/settle invocation-budget functions directly, holding the reservation for a
# bounded window so concurrent callers genuinely contend, and reports whether it crossed the
# dispatch boundary. It adds no behaviour of its own — the authorization is the product's.
WRAPPER_MEMBER = "durable_fixture.py"
WRAPPER_HANDLER = "durable_fixture.handler"
WRAPPER_SOURCE = """\
from __future__ import annotations

import time
from fastapi import HTTPException
from app.services.invocation_budget import reserve_invocation_budget, settle_invocation_budget


def handler(event, _context):
    owner_sub = str(event.get("ownerSub") or "")
    runtime_name = str(event.get("runtimeName") or "")
    hold_seconds = max(0.0, min(float(event.get("holdSeconds") or 0), 10.0))
    dispatch_boundary_crossed = False
    reservation = None
    try:
        reservation = reserve_invocation_budget(owner_sub=owner_sub, runtime_name=runtime_name)
        if reservation is None:
            raise RuntimeError("production authorization unexpectedly returned no reservation")
        if hold_seconds:
            time.sleep(hold_seconds)
        dispatch_boundary_crossed = True
        settle_invocation_budget(reservation, dispatched=True)
        return {
            "statusCode": 200,
            "dispatchBoundaryCrossed": True,
            "reservationId": reservation.reservation_id,
            "amountMicrousd": reservation.amount_microusd,
            "counterSks": list(reservation.counter_sks),
        }
    except HTTPException as exc:
        return {
            "statusCode": int(exc.status_code),
            "detail": str(exc.detail),
            "dispatchBoundaryCrossed": dispatch_boundary_crossed,
        }
    except Exception as exc:
        if reservation is not None and not dispatch_boundary_crossed:
            settle_invocation_budget(reservation, dispatched=False)
        return {
            "statusCode": 500,
            "detail": "fixture failure: " + type(exc).__name__,
            "dispatchBoundaryCrossed": dispatch_boundary_crossed,
        }
"""


def add_wrapper(payload: bytes, member_name: str, source: str) -> bytes:
    """Return the candidate ZIP with exactly one extra module added; never shadow an existing one."""
    import io
    import zipfile

    target = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload), "r") as original, zipfile.ZipFile(target, "w") as wrapped:
        if member_name in set(original.namelist()):
            raise ScenarioFailed(f"deployed ZIP already contains {member_name}")
        for info in original.infolist():
            wrapped.writestr(info, original.read(info.filename))
        wrapped.writestr(member_name, source)
    return target.getvalue()


# The exact product oracles the deployed backend must return. Any drift is a release blocker.
EXACT_ORACLES = {
    "auditOutageStatus": 503,
    "auditOutageDetail": (
        "Required audit evidence is unavailable, so this write was refused before any application "
        "side effect. Retry shortly."
    ),
    "auditReadStatus": 503,
    "auditReadDetail": "Audit evidence is unavailable or failed integrity verification.",
    "budgetExhaustedStatus": 429,
    "budgetExhaustedDetail": "Invocation denied: an applicable monthly authorization limit has no remaining capacity.",
    "budgetOutageStatus": 503,
    "budgetOutageDetail": "Invocation budget authorization is temporarily unavailable; no runtime call was made.",
}


def table_names() -> dict[str, str]:
    """The production control tables, derived from the single stack-name constant."""
    return {
        "audit": f"{STACK_NAME}-audit",
        "budget": f"{STACK_NAME}-budget",
        "deployments": f"{STACK_NAME}-deployments",
        "permission": f"{STACK_NAME}-permission-requests",
        "triggers": f"{STACK_NAME}-triggers",
    }


# --------------------------------------------------------------------------- small utilities


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def aws_code(error: BaseException) -> str:
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code") or "")
    return ""


def is_not_found(error: BaseException) -> bool:
    return aws_code(error) in {"ResourceNotFoundException", "404"}


def _serializers():
    from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

    return TypeSerializer(), TypeDeserializer()


def ddb_map(value: dict[str, Any]) -> dict[str, Any]:
    serializer, _ = _serializers()
    return {str(key): serializer.serialize(item) for key, item in value.items()}


def from_ddb(value: dict[str, Any]) -> dict[str, Any]:
    _, deserializer = _serializers()
    return {str(key): deserializer.deserialize(item) for key, item in value.items()}


# --------------------------------------------------------------------------- DynamoDB


def ddb_get(session: Any, table: str, key: dict[str, Any]) -> dict[str, Any] | None:
    result = session.client("dynamodb").get_item(TableName=table, Key=ddb_map(key), ConsistentRead=True)
    item = result.get("Item")
    return from_ddb(item) if isinstance(item, dict) else None


def ddb_query(
    session: Any,
    table: str,
    *,
    expression: str,
    values: dict[str, Any],
    names: dict[str, str] | None = None,
    max_items: int = 20_000,
) -> list[dict[str, Any]]:
    client = session.client("dynamodb")
    request: dict[str, Any] = {
        "TableName": table,
        "KeyConditionExpression": expression,
        "ExpressionAttributeValues": ddb_map(values),
        "ConsistentRead": True,
    }
    if names:
        request["ExpressionAttributeNames"] = names
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _page in range(100):
        response = client.query(**request)
        items.extend(from_ddb(item) for item in response.get("Items", []))
        if len(items) > max_items:
            raise ScenarioFailed(f"Bounded query exceeded {max_items} rows on {table}")
        last = response.get("LastEvaluatedKey")
        if not last:
            return items
        marker = canonical_json(last)
        if marker in seen:
            raise ScenarioFailed(f"DynamoDB pagination did not advance on {table}")
        seen.add(marker)
        request["ExclusiveStartKey"] = last
    raise ScenarioFailed(f"DynamoDB query exceeded 100 pages on {table}")


def ddb_put(session: Any, table: str, item: dict[str, Any], *, condition: str | None = None) -> None:
    request: dict[str, Any] = {"TableName": table, "Item": ddb_map(item)}
    if condition:
        request["ConditionExpression"] = condition
    session.client("dynamodb").put_item(**request)


def ddb_update(
    session: Any,
    table: str,
    key: dict[str, Any],
    *,
    update: str,
    condition: str,
    values: dict[str, Any],
    names: dict[str, str] | None = None,
) -> None:
    request: dict[str, Any] = {
        "TableName": table,
        "Key": ddb_map(key),
        "UpdateExpression": update,
        "ConditionExpression": condition,
        "ExpressionAttributeValues": ddb_map(values),
    }
    if names:
        request["ExpressionAttributeNames"] = names
    session.client("dynamodb").update_item(**request)


def ddb_delete(
    session: Any,
    table: str,
    key: dict[str, Any],
    *,
    condition: str,
    values: dict[str, Any],
    names: dict[str, str] | None = None,
) -> None:
    from botocore.exceptions import ClientError

    request: dict[str, Any] = {
        "TableName": table,
        "Key": ddb_map(key),
        "ConditionExpression": condition,
        "ExpressionAttributeValues": ddb_map(values),
    }
    if names:
        request["ExpressionAttributeNames"] = names
    try:
        session.client("dynamodb").delete_item(**request)
    except ClientError as error:
        if aws_code(error) == "ConditionalCheckFailedException" and ddb_get(session, table, key) is None:
            return
        raise


# --------------------------------------------------------------------------- Lambda / HTTP events


def api_gateway_event(
    path: str,
    body: dict[str, Any],
    *,
    owner_sub: str,
    session_id: str,
    account_id: str,
    groups: list[str] | None = None,
) -> dict[str, Any]:
    """An API-Gateway v2 (payload 2.0) event equivalent to the product's authenticated POST route."""
    return {
        "version": "2.0",
        "routeKey": f"POST {path}",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"content-type": "application/json", "x-session-id": session_id},
        "requestContext": {
            "accountId": account_id,
            "apiId": "durablefixture",
            "authorizer": {
                "jwt": {
                    "claims": {"sub": owner_sub, "cognito:groups": groups or ["g-admins-super", "t-user"]},
                    "scopes": [],
                }
            },
            "domainName": "durablefixture.invalid",
            "domainPrefix": "durablefixture",
            "http": {"method": "POST", "path": path, "protocol": "HTTP/1.1", "sourceIp": "127.0.0.1"},
            "requestId": str(uuid.uuid4()),
            "routeKey": f"POST {path}",
            "stage": "$default",
            "time": datetime.now(UTC).strftime("%d/%b/%Y:%H:%M:%S +0000"),
            "timeEpoch": int(time.time() * 1000),
        },
        "body": json.dumps(body, separators=(",", ":")),
        "isBase64Encoded": False,
    }


def parse_lambda_http(value: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, str]]:
    status = int(value.get("statusCode", 0))
    body = value.get("body")
    parsed = json.loads(body) if isinstance(body, str) and body else {}
    headers = {str(key).lower(): str(item) for key, item in (value.get("headers") or {}).items()}
    return status, parsed if isinstance(parsed, dict) else {"value": parsed}, headers


def invoke_lambda_json(session: Any, function_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = session.client("lambda").invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload, separators=(",", ":")).encode(),
    )
    stream = response.get("Payload")
    raw = stream.read() if hasattr(stream, "read") else stream
    if response.get("FunctionError"):
        raise ScenarioFailed(f"Fixture Lambda {function_name} returned FunctionError: {(raw or b'')[:300]!r}")
    value = json.loads(raw or b"{}")
    if not isinstance(value, dict):
        raise ScenarioFailed(f"Fixture Lambda {function_name} returned a non-object")
    return value


def _function_tags(session: Any, function: dict[str, Any]) -> dict[str, str]:
    arn = function["Configuration"]["FunctionArn"]
    return session.client("lambda").list_tags(Resource=arn).get("Tags", {})


def clone_deployment_lambda(
    session: Any,
    observed: Any,
    *,
    run_id: str,
    commit: str,
    env_overrides: dict[str, str],
    suffix: str,
    wrapper: tuple[str, str] | None = None,
    handler: str | None = None,
) -> str:
    """Create a private clone of the deployed DeploymentLambda with ``env_overrides`` applied.

    The base ZIP is byte-identical to the published candidate asset (the binding already proved the
    live function's ``CodeSha256`` is that asset); this re-verifies that digest on the download.
    When ``wrapper=(member_name, source)`` is given, that one extra module is added to the ZIP and
    ``handler`` points at it — used by the cost family to drive the real reservation code directly.
    The clone's ``CodeSha256`` is re-checked after creation against the exact bytes deployed, and the
    clone is tagged with the run id and candidate so teardown can prove ownership. Returns the name.
    """
    from botocore.exceptions import ClientError

    physical = observed.lambda_physical.get(DEPLOYMENT_LAMBDA_LOGICAL)
    expected_sha = observed.lambda_code_sha256.get(DEPLOYMENT_LAMBDA_LOGICAL)
    if not physical or not expected_sha:
        raise ScenarioFailed(f"{DEPLOYMENT_LAMBDA_LOGICAL} is not in the observed binding")
    lam = session.client("lambda")
    source = lam.get_function(FunctionName=physical)
    config = source["Configuration"]
    if config.get("CodeSha256") != expected_sha:
        raise ScenarioFailed("Deployment Lambda moved after the exact-candidate binding")
    location = str(source.get("Code", {}).get("Location") or "")
    if not location.startswith("https://"):
        raise ScenarioFailed("GetFunction returned no HTTPS code location")
    with urllib.request.urlopen(location, timeout=120) as handle:  # noqa: S310 - AWS-signed Lambda code URL
        original = handle.read()
    if base64.b64encode(hashlib.sha256(original).digest()).decode() != expected_sha:
        raise ScenarioFailed("Downloaded Deployment Lambda ZIP digest differs from the binding")

    code = add_wrapper(original, wrapper[0], wrapper[1]) if wrapper else original
    code_sha = base64.b64encode(hashlib.sha256(code).digest()).decode()
    name = f"acfpr-durable-{run_id}-{suffix}"[:64]
    tags = {**_CLONE_TAGS_BASE, "FixtureId": run_id, "CandidateCommit": commit}
    environment = {str(k): str(v) for k, v in config.get("Environment", {}).get("Variables", {}).items()}
    environment.update(env_overrides)
    request: dict[str, Any] = {
        "FunctionName": name,
        "Runtime": config["Runtime"],
        "Role": config["Role"],
        "Handler": handler or config["Handler"],
        "Code": {"ZipFile": code},
        "Description": f"Temporary durable-control fixture {run_id}",
        "Timeout": min(int(config.get("Timeout", 600)), 600),
        "MemorySize": int(config.get("MemorySize", 512)),
        "Environment": {"Variables": environment},
        "Architectures": list(config.get("Architectures") or ["x86_64"]),
        "EphemeralStorage": config.get("EphemeralStorage") or {"Size": 512},
        "TracingConfig": config.get("TracingConfig") or {"Mode": "PassThrough"},
        "Tags": tags,
    }
    layers = [str(item.get("Arn")) for item in config.get("Layers", []) if item.get("Arn")]
    if layers:
        request["Layers"] = layers
    try:
        lam.create_function(**request)
    except ClientError:
        existing = lam.get_function(FunctionName=name)
        if _function_tags(session, existing) != tags:
            raise ScenarioFailed(f"Accepted-but-response-lost Lambda {name} has foreign tags") from None
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        observed_fn = lam.get_function(FunctionName=name)
        current = observed_fn["Configuration"]
        if current.get("State") == "Active" and current.get("LastUpdateStatus") in {None, "Successful"}:
            if current.get("CodeSha256") != code_sha or _function_tags(session, observed_fn) != tags:
                raise ScenarioFailed(f"Lambda clone {name} differs from its sealed bytes/tags")
            return name
        if current.get("State") == "Failed" or current.get("LastUpdateStatus") == "Failed":
            raise ScenarioFailed(f"Lambda clone {name} entered a failed state")
        time.sleep(3)
    raise ScenarioFailed(f"Lambda clone {name} did not become active")


def delete_clone(session: Any, name: str, *, run_id: str, commit: str) -> bool:
    """Delete a clone and its log group, but only if its tags still prove this run owns it."""
    from botocore.exceptions import ClientError

    lam = session.client("lambda")
    try:
        existing = lam.get_function(FunctionName=name)
    except ClientError as error:
        if is_not_found(error):
            return False
        raise
    expected = {**_CLONE_TAGS_BASE, "FixtureId": run_id, "CandidateCommit": commit}
    if _function_tags(session, existing) != expected:
        raise ScenarioFailed(f"Refusing to delete unowned Lambda clone {name}")
    lam.delete_function(FunctionName=name)
    logs = session.client("logs")
    log_group = f"/aws/lambda/{name}"
    # Lambda creates the log group lazily on first invocation, so it can materialise moments after
    # the function is gone. Poll briefly and delete it whenever it appears, leaving zero residue.
    for _attempt in range(6):
        groups = logs.describe_log_groups(logGroupNamePrefix=log_group).get("logGroups", [])
        if any(item.get("logGroupName") == log_group for item in groups):
            logs.delete_log_group(logGroupName=log_group)
            return True
        time.sleep(2)
    return True
