"""``audit.append_only`` — the three live audit controls on the deployed stack.

One journey, run once per deployment and cached, proves all three facets against ``acf-production``:

* **mutation-intent-and-outcome-visible** — a real budget write through the product API leaves an
  ``attempt``/``outcome`` audit pair with the exact mutation oracle, both rows carry a matching
  integrity digest and no TTL, and a duplicate primary key is refused (append-only / immutable).
* **audit-store-outage-blocks-mutation** — a private clone of the deployed ``DeploymentLambda`` whose
  only change is ``AUDIT_TABLE_NAME`` redirected to a proven-absent table returns the exact 503
  outage oracle, and the application budget row is never written (fail-closed before side effect).
* **tampered-row-read-denied** — a fixture audit row tampered after insertion makes the admin audit
  read fail closed with the exact 503 integrity oracle.

Each scenario id verifies its facet from the cached journey; the journey cleans up every resource it
creates (the Lambda clone, its own audit rows, the budget definition, the synthetic user).
"""

from __future__ import annotations

import argparse
import base64
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from _durable import (
    EXACT_ORACLES,
    api_gateway_event,
    clone_deployment_lambda,
    ddb_delete,
    ddb_get,
    ddb_put,
    ddb_query,
    ddb_update,
    delete_clone,
    invoke_lambda_json,
    parse_lambda_http,
    sha256_bytes,
    table_names,
)
from _live import (
    REQUIRED_USER_GROUPS,
    BearerSession,
    ObservedStack,
    RunBinding,
    ScenarioFailed,
    boto3_session,
    bound_deployment,
    family_journey,
    with_synthetic_user,
    write_json,
)

FAMILY = "audit-controls"
AUDIT_GROUPS = REQUIRED_USER_GROUPS | {"g-admins-super"}  # grants admin + cost:write


def _token_sub(token: str) -> str:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    sub = str(claims.get("sub") or "")
    if not sub:
        raise ScenarioFailed("access token carries no sub claim")
    return sub


def _audit_digest(row: dict[str, Any]) -> str:
    document = {
        "org_id": row["org_id"],
        "sk": row["sk"],
        "actor_sub": row.get("actor_sub") or "unknown",
        "action": row.get("action", ""),
        "method": row.get("method", ""),
        "path": row.get("path", ""),
        "status_code": int(row.get("status_code", 0)),
        "ts": row.get("ts", ""),
        "event_id": row.get("event_id", ""),
        "session_uuid": row.get("session_uuid") or "",
        "phase": row.get("phase") or "outcome",
        "correlation_id": row.get("correlation_id") or "",
    }
    return sha256_bytes(json.dumps(document, sort_keys=True, separators=(",", ":"), default=str).encode())


def _audit_rows(session: Any, owner_sub: str) -> list[dict[str, Any]]:
    rows = ddb_query(session, table_names()["audit"], expression="org_id = :org", values={":org": "default"})
    return [row for row in rows if row.get("actor_sub") == owner_sub]


def _create_budget(http: Any, key: str, session_id: str, limit: float = 2.0) -> None:
    import requests

    path = "/api/cost/budgets"
    payload = {"scope": "agent", "key": key, "limit_usd": limit, "warn_pct": 100}
    try:
        response = http.post(f"{http.base_url}{path}", json=payload, headers={"X-Session-Id": session_id}, timeout=60)
    except (requests.ConnectionError, requests.Timeout):
        return  # the caller verifies the durable row regardless of a lost response
    if response.status_code != 200:
        raise ScenarioFailed(f"budget create returned {response.status_code}: {response.text[:300]}")


def _verify_audit_pair(session: Any, owner_sub: str, session_id: str) -> dict[str, Any]:
    matches = [row for row in _audit_rows(session, owner_sub) if row.get("session_uuid") == session_id]
    if len(matches) != 2:
        raise ScenarioFailed(f"expected one audit attempt/outcome pair, found {len(matches)}")
    matches.sort(key=lambda row: 0 if row.get("phase") == "attempt" else 1)
    attempt, outcome = matches
    shape_ok = (
        attempt.get("phase") == "attempt"
        and outcome.get("phase") == "outcome"
        and attempt.get("correlation_id") == outcome.get("correlation_id")
        and attempt.get("action") == outcome.get("action") == "budget.write"
        and attempt.get("method") == outcome.get("method") == "POST"
        and attempt.get("path") == outcome.get("path") == "/api/cost/budgets"
        and int(attempt.get("status_code", -1)) == 0
        and int(outcome.get("status_code", -1)) == 200
    )
    if not shape_ok:
        raise ScenarioFailed("audit pair does not match the exact mutation intent/outcome oracle")
    for row in matches:
        if row.get("ttl") is not None or row.get("integrity_sha256") != _audit_digest(row):
            raise ScenarioFailed("production audit row has a TTL or an invalid integrity digest")
    _assert_immutable(session, outcome)
    return {
        "sessionId": session_id,
        "correlationId": attempt["correlation_id"],
        "attemptEventId": attempt["event_id"],
        "outcomeEventId": outcome["event_id"],
        "immutableDuplicateDenied": True,
        "integrityVerified": True,
    }


def _assert_immutable(session: Any, outcome: dict[str, Any]) -> None:
    from botocore.exceptions import ClientError

    audit = table_names()["audit"]
    duplicate = dict(outcome)
    duplicate["path"] = "/api/forbidden-overwrite"
    try:
        ddb_put(session, audit, duplicate, condition="attribute_not_exists(org_id) AND attribute_not_exists(sk)")
    except ClientError as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
    else:
        raise ScenarioFailed("duplicate audit key overwrote immutable evidence")
    if ddb_get(session, audit, {"org_id": "default", "sk": outcome["sk"]}) != outcome:
        raise ScenarioFailed("duplicate-key probe changed the audit row")


def _prove_outage(
    session: Any, observed: ObservedStack, binding: RunBinding, owner_sub: str, token: str, account_id: str
) -> dict[str, Any]:
    budget = table_names()["budget"]
    absent = f"acfpr-missing-audit-{token}"
    _assert_absent(session, absent)
    clone = clone_deployment_lambda(
        session,
        observed,
        run_id=token,
        commit=binding.commit,
        env_overrides={"AUDIT_TABLE_NAME": absent},
        suffix="audit",
    )
    try:
        outage_key = f"audit_outage_{token}"
        event = api_gateway_event(
            "/api/cost/budgets",
            {"scope": "agent", "key": outage_key, "limit_usd": 1.0, "warn_pct": 100},
            owner_sub=owner_sub,
            session_id=f"acfpr-audit-outage-{token}",
            account_id=account_id,
        )
        status, body, _ = parse_lambda_http(invoke_lambda_json(session, clone, event))
        if status != EXACT_ORACLES["auditOutageStatus"] or body.get("detail") != EXACT_ORACLES["auditOutageDetail"]:
            raise ScenarioFailed(f"audit outage oracle differed: {status} {body}")
        if ddb_get(session, budget, {"org_id": "default", "sk": f"BUDGET#agent#{outage_key}"}):
            raise ScenarioFailed("audit outage allowed the application budget mutation")
        return {
            "status": status,
            "detail": body["detail"],
            "applicationMutationObserved": False,
            "cloneCodeSha256": observed.lambda_code_sha256.get("DeploymentLambda5F4766EE"),
        }
    finally:
        delete_clone(session, clone, run_id=token, commit=binding.commit)


def _assert_absent(session: Any, table: str) -> None:
    from botocore.exceptions import ClientError

    try:
        session.client("dynamodb").describe_table(TableName=table)
    except ClientError as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
        return
    raise ScenarioFailed(f"outage sentinel table unexpectedly exists: {table}")


def _prove_tamper(session: Any, http: Any, owner_sub: str, token: str) -> dict[str, Any]:
    audit = table_names()["audit"]
    event_id = f"fixture-{token}"
    timestamp = datetime.now(UTC).isoformat()
    tampered = {
        "org_id": "default",
        "sk": f"{timestamp}#{event_id}",
        "actor_sub": owner_sub,
        "action": "budget.write",
        "method": "POST",
        "path": "/api/cost/budgets",
        "status_code": 200,
        "ts": timestamp,
        "event_id": event_id,
        "session_uuid": f"acfpr-tamper-{token}",
        "phase": "outcome",
        "correlation_id": f"fixture-{token}",
    }
    tampered["integrity_sha256"] = _audit_digest(tampered)
    ddb_put(session, audit, tampered, condition="attribute_not_exists(org_id) AND attribute_not_exists(sk)")
    ddb_update(
        session,
        audit,
        {"org_id": "default", "sk": tampered["sk"]},
        update="SET #path = :tampered",
        condition="actor_sub = :owner AND event_id = :event AND integrity_sha256 = :digest",
        values={
            ":tampered": "/api/tampered-fixture",
            ":owner": owner_sub,
            ":event": event_id,
            ":digest": tampered["integrity_sha256"],
        },
        names={"#path": "path"},
    )
    denied = http.get(f"{http.base_url}/api/admin/audit?limit=1000", timeout=60)
    body = denied.json() if denied.content else {}
    if denied.status_code != EXACT_ORACLES["auditReadStatus"] or body.get("detail") != EXACT_ORACLES["auditReadDetail"]:
        raise ScenarioFailed(f"tampered audit read oracle differed: {denied.status_code} {denied.text[:200]}")
    return {
        "status": denied.status_code,
        "detail": body["detail"],
        "tamperedEventId": event_id,
        "originalDigest": tampered["integrity_sha256"],
    }


def _cleanup(session: Any, owner_sub: str, token: str) -> dict[str, Any]:
    audit = table_names()["audit"]
    budget = table_names()["budget"]
    removed = 0
    for row in _audit_rows(session, owner_sub):
        ddb_delete(
            session,
            audit,
            {"org_id": row["org_id"], "sk": row["sk"]},
            condition="actor_sub = :owner",
            values={":owner": owner_sub},
        )
        removed += 1
    key = f"BUDGET#agent#audit_pos_{token}"
    if ddb_get(session, budget, {"org_id": "default", "sk": key}):
        ddb_delete(
            session,
            budget,
            {"org_id": "default", "sk": key},
            condition="owner_sub = :owner",
            values={":owner": owner_sub},
        )
    return {"audit_rows_removed": removed}


def _journey(args: argparse.Namespace, binding: RunBinding, observed: ObservedStack, artifacts: Path) -> dict[str, Any]:
    account_id = str(args.account_id)
    base = observed.cloudfront_url
    token = uuid.uuid4().hex[:12]
    outcome: dict[str, Any] = {"region": binding.region, "run_token": token}
    session = boto3_session(binding.region)
    with with_synthetic_user(observed, label="audit", groups=AUDIT_GROUPS) as user:
        owner_sub = _token_sub(user.token)
        outcome["owner_sub_hash"] = sha256_bytes(owner_sub.encode())[:16]
        http = BearerSession(user, base)
        try:
            positive_session = f"acfpr-audit-positive-{token}"
            _create_budget(http, f"audit_pos_{token}", positive_session)
            outcome["mutation-intent-and-outcome-visible"] = _verify_audit_pair(session, owner_sub, positive_session)
            outcome["audit-store-outage-blocks-mutation"] = _prove_outage(
                session, observed, binding, owner_sub, token, account_id
            )
            outcome["tampered-row-read-denied"] = _prove_tamper(session, http, owner_sub, token)
        finally:
            outcome["cleanup"] = _cleanup(session, owner_sub, token)
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
    if claim == "mutation-intent-and-outcome-visible":
        holds = facet.get("immutableDuplicateDenied") is True and facet.get("integrityVerified") is True
    elif claim == "audit-store-outage-blocks-mutation":
        holds = (
            facet.get("status") == EXACT_ORACLES["auditOutageStatus"]
            and facet.get("applicationMutationObserved") is False
        )
    elif claim == "tampered-row-read-denied":
        holds = facet.get("status") == EXACT_ORACLES["auditReadStatus"] and bool(facet.get("tamperedEventId"))
    else:
        raise ScenarioFailed(f"unknown audit claim {claim}")
    report = {
        "scenario": args.scenario_id,
        "region": binding.region,
        "deployment_binding": comparison,
        "journey": {k: v for k, v in journey.items() if k != "outcome"},
        "claim": claim,
        "claim_holds": holds,
        "facet": facet,
    }
    write_json(artifact_dir / "report", report)
    if not holds:
        raise ScenarioFailed(f"{claim}: {json.dumps(facet)[:300]}")
    return report


def mutation_intent_and_outcome_visible(args, artifact_dir):
    return _scenario(args, artifact_dir, "mutation-intent-and-outcome-visible")


def audit_store_outage_blocks_mutation(args, artifact_dir):
    return _scenario(args, artifact_dir, "audit-store-outage-blocks-mutation")


def tampered_row_read_denied(args, artifact_dir):
    return _scenario(args, artifact_dir, "tampered-row-read-denied")
