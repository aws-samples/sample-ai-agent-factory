"""``iam.jit_approval`` — the three live just-in-time approval controls on the deployed stack.

One cached journey proves, against ``acf-production``, using an owner-tagged IAM role constrained by
the candidate's own permissions boundary:

* **leased-approval-applies-exact-policy** — a real JIT permission request, once approved through the
  product, leaves the request ``APPROVED`` and an inline ``JIT-<id>`` role policy byte-equal to the
  exact requested ``{Allow, actions, resources}`` document.
* **concurrent-rejection-cannot-win** — while the request is ``APPLYING`` (a short lease held
  directly on the row), a concurrent reject is refused 409 ``Request already APPLYING`` and the
  request still reaches its single terminal ``APPROVED`` state.
* **gateway-policy-mutation-request-denied** — a request for a Gateway/Policy control-plane action
  (``bedrock-agentcore:UpdateGateway``) is refused 400 with the exact detail and never persisted.

Cleanup removes the inline policy (only if it still equals the exact document), the permission row,
the role (only if its tags still prove this run owns it), and the synthetic user.
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from _durable import (
    EXACT_ORACLES,
    canonical_json,
    ddb_delete,
    ddb_get,
    sha256_bytes,
    table_names,
)
from _live import (
    REQUIRED_USER_GROUPS,
    STACK_NAME,
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

FAMILY = "jit-controls"
JIT_GROUPS = REQUIRED_USER_GROUPS | {"g-admins-super"}  # admin persona: create + approve/reject + pending
_ROLE_TAGS = ("ManagedBy", "agentcore-flows-tests")


def _token_sub(token: str) -> str:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    sub = str(json.loads(base64.urlsafe_b64decode(payload)).get("sub") or "")
    if not sub:
        raise ScenarioFailed("access token carries no sub claim")
    return sub


def _normalize(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


def _boundary_arn(session: Any) -> str:
    resources = session.client("cloudformation").list_stack_resources(StackName=STACK_NAME)["StackResourceSummaries"]
    for resource in resources:
        if str(resource.get("LogicalResourceId", "")).startswith("AgentCoreRoleBoundary"):
            arn = str(resource.get("PhysicalResourceId") or "")
            if not arn.startswith("arn:aws:iam::"):
                raise ScenarioFailed(f"boundary resource has no policy ARN: {arn}")
            return arn
    raise ScenarioFailed("the stack exposes no AgentCoreRoleBoundary policy")


def _create_role(session: Any, role_name: str, boundary_arn: str, run_id: str, commit: str) -> None:
    from botocore.exceptions import ClientError

    iam = session.client("iam")
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}
        ],
    }
    tags = [
        {"Key": "ManagedBy", "Value": "agentcore-flows-tests"},
        {"Key": "Purpose", "Value": "jit-durable-control-live-gate"},
        {"Key": "FixtureId", "Value": run_id},
        {"Key": "CandidateCommit", "Value": commit},
    ]
    try:
        iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=canonical_json(trust),
            Description=f"Temporary JIT fixture {run_id}",
            MaxSessionDuration=3600,
            PermissionsBoundary=boundary_arn,
            Tags=tags,
        )
    except ClientError:
        iam.get_role(RoleName=role_name)
    role = iam.get_role(RoleName=role_name).get("Role", {})
    observed_tags = {str(t["Key"]): str(t["Value"]) for t in iam.list_role_tags(RoleName=role_name).get("Tags", [])}
    if (
        observed_tags != {t["Key"]: t["Value"] for t in tags}
        or role.get("PermissionsBoundary", {}).get("PermissionsBoundaryArn") != boundary_arn
        or _normalize(role.get("AssumeRolePolicyDocument")) != trust
    ):
        raise ScenarioFailed("created/recovered JIT role differs from the exact owned fixture")


def _discover_org(session: Any, request_id: str) -> str:
    """Find the request row's partition key without assuming the backend's org convention.

    The permission table's org_id is a server-derived default that differs between the deployed
    candidate and the merged source (``default-org`` vs ``default``), so the scenario must read it
    rather than hardcode it. The control table is tiny; a bounded scan filtered by request_id is the
    reliable, deployment-agnostic way to recover the owning org_id.
    """
    client = session.client("dynamodb")
    kwargs: dict[str, Any] = {
        "TableName": table_names()["permission"],
        "FilterExpression": "request_id = :rid",
        "ExpressionAttributeValues": {":rid": {"S": request_id}},
        "ProjectionExpression": "org_id, request_id",
    }
    for _page in range(50):
        response = client.scan(**kwargs)
        for item in response.get("Items", []):
            org = item.get("org_id", {}).get("S")
            if org:
                return org
        nxt = response.get("LastEvaluatedKey")
        if not nxt:
            raise ScenarioFailed(f"permission request {request_id} not found to resolve its org_id")
        kwargs["ExclusiveStartKey"] = nxt
    raise ScenarioFailed(f"permission request {request_id} org_id scan did not converge")


def _create_request(
    http: Any, role_name: str, actions: list[str], resources: list[str], justification: str, token: str
) -> str:
    response = http.post(
        f"{http.base_url}/api/permissions/requests",
        json={"roleName": role_name, "actions": actions, "resources": resources, "justification": justification},
        headers={"X-Session-Id": f"acfpr-jit-create-{token}"[:128]},
        timeout=60,
    )
    if response.status_code != 200:
        raise ScenarioFailed(f"permission request returned {response.status_code}: {response.text[:300]}")
    request_id = response.json().get("request_id")
    if not isinstance(request_id, str) or not request_id.startswith("pr-"):
        raise ScenarioFailed(f"permission request returned an invalid id: {request_id!r}")
    return request_id


def _claim_lease(
    session: Any, org_id: str, owner_sub: str, request_id: str, role_name: str, actions: list[str], resources: list[str]
) -> int:
    from _durable import ddb_map

    now = int(time.time())
    expires = now + 5
    session.client("dynamodb").update_item(
        TableName=table_names()["permission"],
        Key=ddb_map({"org_id": org_id, "request_id": request_id}),
        UpdateExpression=(
            "SET #status = :applying, decided_by = :by, decision_reason = :reason, approval_token = :token, "
            "approval_started_at = :started, approval_expires_at = :expires"
        ),
        ConditionExpression=(
            "attribute_exists(request_id) AND #status = :pending AND requester_sub = :owner "
            "AND role_name = :role AND actions = :actions AND resources = :resources"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues=ddb_map(
            {
                ":applying": "APPLYING",
                ":pending": "PENDING",
                ":by": owner_sub,
                ":reason": "hold exact concurrent-rejection fixture lease",
                ":token": secrets.token_hex(16),
                ":started": datetime.fromtimestamp(now, UTC).isoformat(),
                ":expires": expires,
                ":owner": owner_sub,
                ":role": role_name,
                ":actions": actions,
                ":resources": resources,
            }
        ),
    )
    return expires


def _pending(http: Any) -> list[dict[str, Any]]:
    response = http.get(f"{http.base_url}/api/permissions/requests/pending", timeout=60)
    response.raise_for_status()
    body = response.json()
    return [item for item in body if isinstance(item, dict)] if isinstance(body, list) else []


def _approve_until_applied(
    http: Any, session: Any, org_id: str, request_id: str, role_name: str, expected_policy: dict[str, Any], token: str
) -> dict[str, Any]:
    permission = table_names()["permission"]
    deadline = time.monotonic() + 330
    while time.monotonic() < deadline:
        row = ddb_get(session, permission, {"org_id": org_id, "request_id": request_id})
        if row and row.get("status") == "APPROVED":
            policy = session.client("iam").get_role_policy(RoleName=role_name, PolicyName=f"JIT-{request_id}")
            if _normalize(policy.get("PolicyDocument")) != expected_policy:
                raise ScenarioFailed("approved JIT inline policy differs from the exact requested document")
            return row
        if row and row.get("status") == "APPLYING" and int(time.time()) <= int(row.get("approval_expires_at") or 0):
            time.sleep(2)
            continue
        response = http.post(
            f"{http.base_url}/api/permissions/requests/{request_id}/approve",
            json={"reason": "apply exact live-gate policy"},
            headers={"X-Session-Id": f"acfpr-jit-approve-{token}"[:128]},
            timeout=60,
        )
        if response.status_code in {200, 409}:
            time.sleep(2)
            continue
        raise ScenarioFailed(f"JIT approval returned {response.status_code}: {response.text[:300]}")
    raise ScenarioFailed("JIT approval did not reach APPROVED within 330s")


def _journey(args: argparse.Namespace, binding: RunBinding, observed: ObservedStack, artifacts: Path) -> dict[str, Any]:
    base = observed.cloudfront_url
    account_id = str(args.account_id)
    token = uuid.uuid4().hex[:12]
    role_name = f"AgentCoreDurableJit{token}"
    actions = ["s3:GetObject"]
    resources = [f"arn:aws:s3:::acfpr-durable-{token}/*"]
    expected_policy = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": actions, "Resource": resources}],
    }
    outcome: dict[str, Any] = {"region": binding.region, "run_token": token, "role_name": role_name}
    session = boto3_session(binding.region)
    request_id: str | None = None
    with with_synthetic_user(observed, label="jit", groups=JIT_GROUPS) as user:
        owner_sub = _token_sub(user.token)
        outcome["owner_sub_hash"] = sha256_bytes(owner_sub.encode())[:16]
        http = BearerSession(user, base)
        try:
            _create_role(session, role_name, _boundary_arn(session), token, binding.commit)
            request_id = _create_request(http, role_name, actions, resources, f"durable-control fixture {token}", token)
            outcome["request_id"] = request_id
            org_id = _discover_org(session, request_id)
            outcome["permission_org_id"] = org_id
            expires = _claim_lease(session, org_id, owner_sub, request_id, role_name, actions, resources)
            rejected = http.post(
                f"{base}/api/permissions/requests/{request_id}/reject",
                json={"reason": "concurrent rejection fixture"},
                headers={"X-Session-Id": f"acfpr-jit-reject-{token}"[:128]},
                timeout=60,
            )
            rbody = rejected.json() if rejected.content else {}
            if (
                rejected.status_code != EXACT_ORACLES["jitBusyStatus"]
                or rbody.get("detail") != EXACT_ORACLES["jitBusyDetail"]
            ):
                raise ScenarioFailed(
                    f"concurrent JIT rejection oracle differed: {rejected.status_code} {rejected.text[:200]}"
                )
            while int(time.time()) <= expires:
                time.sleep(1)
            row = _approve_until_applied(http, session, org_id, request_id, role_name, expected_policy, token)
            outcome["leased-approval-applies-exact-policy"] = {
                "requestId": request_id,
                "status": row["status"],
                "policyName": f"JIT-{request_id}",
                "policySha256": sha256_bytes(canonical_json(expected_policy).encode()),
            }
            outcome["concurrent-rejection-cannot-win"] = {
                "status": rejected.status_code,
                "detail": rbody["detail"],
                "terminalRequestStatus": row["status"],
            }
            forbidden_just = f"forbidden gateway mutation {token}"
            forbidden = http.post(
                f"{base}/api/permissions/requests",
                json={
                    "roleName": role_name,
                    "actions": ["bedrock-agentcore:UpdateGateway"],
                    "resources": [f"arn:aws:bedrock-agentcore:{binding.region}:{account_id}:gateway/fixture-{token}"],
                    "justification": forbidden_just,
                },
                headers={"X-Session-Id": f"acfpr-jit-forbidden-{token}"[:128]},
                timeout=60,
            )
            fbody = forbidden.json() if forbidden.content else {}
            persisted = any(item.get("justification") == forbidden_just for item in _pending(http))
            if (
                forbidden.status_code != EXACT_ORACLES["jitForbiddenStatus"]
                or fbody.get("detail") != EXACT_ORACLES["jitForbiddenDetail"]
                or persisted
            ):
                raise ScenarioFailed(
                    f"forbidden gateway mutation oracle differed: {forbidden.status_code} persisted={persisted}"
                )
            outcome["gateway-policy-mutation-request-denied"] = {
                "status": forbidden.status_code,
                "detail": fbody["detail"],
                "requestPersisted": persisted,
            }
        finally:
            outcome["cleanup"] = _cleanup(
                session,
                outcome.get("permission_org_id"),
                owner_sub,
                role_name,
                request_id,
                resources,
                run_id=token,
                commit=binding.commit,
            )
            http.close()
    write_json(artifacts / "journey-outcome.json", outcome)
    return outcome


def _cleanup(
    session: Any,
    org_id: str | None,
    owner_sub: str,
    role_name: str,
    request_id: str | None,
    resources: list[str],
    *,
    run_id: str,
    commit: str,
) -> dict[str, Any]:
    from botocore.exceptions import ClientError

    iam = session.client("iam")
    permission = table_names()["permission"]
    result: dict[str, Any] = {"role_deleted": False, "request_removed": False}
    if request_id:
        row = ddb_get(session, permission, {"org_id": org_id, "request_id": request_id})
        if row:
            policy_name = f"JIT-{request_id}"
            try:
                policy = iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)
            except ClientError:
                policy = None
            if policy is not None:
                expected = {
                    "Version": "2012-10-17",
                    "Statement": [{"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": resources}],
                }
                if _normalize(policy.get("PolicyDocument")) != expected:
                    raise ScenarioFailed("refusing to delete a changed JIT inline policy")
                iam.delete_role_policy(RoleName=role_name, PolicyName=policy_name)
            ddb_delete(
                session,
                permission,
                {"org_id": org_id, "request_id": request_id},
                condition="requester_sub = :owner AND role_name = :role",
                values={":owner": owner_sub, ":role": role_name},
            )
            result["request_removed"] = True
    try:
        tags = {str(t["Key"]): str(t["Value"]) for t in iam.list_role_tags(RoleName=role_name).get("Tags", [])}
    except ClientError as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") in {"NoSuchEntity", "404"}:
            return result
        raise
    if tags.get("FixtureId") != run_id or tags.get("CandidateCommit") != commit:
        raise ScenarioFailed("refusing to delete an unowned JIT role")
    inline = iam.list_role_policies(RoleName=role_name).get("PolicyNames", [])
    if inline:
        raise ScenarioFailed(f"JIT role still has inline policies: {inline}")
    iam.delete_role(RoleName=role_name)
    result["role_deleted"] = True
    return result


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
    if claim == "leased-approval-applies-exact-policy":
        holds = facet.get("status") == "APPROVED" and bool(facet.get("policySha256"))
    elif claim == "concurrent-rejection-cannot-win":
        holds = (
            facet.get("status") == EXACT_ORACLES["jitBusyStatus"] and facet.get("terminalRequestStatus") == "APPROVED"
        )
    elif claim == "gateway-policy-mutation-request-denied":
        holds = facet.get("status") == EXACT_ORACLES["jitForbiddenStatus"] and facet.get("requestPersisted") is False
    else:
        raise ScenarioFailed(f"unknown JIT claim {claim}")
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


def leased_approval_applies_exact_policy(args, artifact_dir):
    return _scenario(args, artifact_dir, "leased-approval-applies-exact-policy")


def concurrent_rejection_cannot_win(args, artifact_dir):
    return _scenario(args, artifact_dir, "concurrent-rejection-cannot-win")


def gateway_policy_mutation_request_denied(args, artifact_dir):
    return _scenario(args, artifact_dir, "gateway-policy-mutation-request-denied")
