"""Scheduled / event trigger API — Phase 3 Gap 3F.

Lets a tenant register cron / EventBridge / S3 / webhook triggers against the
production slot of one of their runtimes. This pass implements the trigger
REGISTRY (store + endpoints); the live EventBridge / Scheduler / webhook-Lambda
provisioning is described as the manifest CDK addition + a destroy_runtime
cleanup hook (Bug 124).

Endpoints (prefix /api/runtimes):
  POST   /{runtime_name}/triggers            create a trigger (owner-gated)
  GET    /{runtime_name}/triggers            list the runtime's triggers
  DELETE /{runtime_name}/triggers/{id}       delete a trigger the caller owns

Tenant isolation (Critic Finding 3, Bug 37/122/126): every endpoint depends on
``get_caller_sub``. Ownership is resolved through the production slot via a
local ``_resolve_owned_runtime()`` copied from
``evaluations._resolve_owned_runtime_id`` — slot-owner ``assert_owner`` +
version-owner ``assert_owner``, 404 on cross-tenant (existence-non-disclosure).
This makes the production-slot owner the trigger owner and closes Bug 122
(a tenant cannot create a trigger on another tenant's runtime_name -> 404).

Confused-deputy guard: ``target_runtime_arn`` is derived SERVER-SIDE from the
resolved owned version, NEVER taken from the request body — trusting a
body-supplied ARN would let a tenant point a trigger at another tenant's (or an
arbitrary) runtime.

SSRF (Critic Finding 2): an optional outbound ``webhook_out_url`` is validated
with the canonical ``gateway_deployer._validate_discovery_url`` guard (https +
DNS-resolve + IMDS/RFC1918/link-local/loopback denylist) before it is ever
persisted, so it can never be used for a server-side fetch against a private
target.

Secrets (lessons.md rule 5): the webhook HMAC signing key is created in Secrets
Manager with an owner-scoped name (mirror ``observability.store_credentials``:
``agentcore-trigger/{safe_owner}-{uuid}``, tagged owner_sub) and only the ARN
(``webhook_secret_ref``) is stored in DDB — never the raw secret in DDB or env.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.services.agent_versions_store import (
    get_slots_store,
    get_versions_store,
)
from app.services.auth import assert_owner, get_caller_sub
from app.services.rbac import require_scopes
from app.services.gateway_deployer import (
    _DiscoveryUrlBlocked,
    _DiscoveryUrlInvalid,
    _validate_discovery_url,
)
from app.services.trigger_store import (
    TRIGGER_TYPES,
    TYPE_CRON,
    TYPE_EVENTBRIDGE,
    TYPE_S3,
    TYPE_WEBHOOK,
    Trigger,
    get_trigger_store,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Input validation (mirror evaluations / hitl regex+length guards)
# ---------------------------------------------------------------------------

# runtime_name is the AgentCore-shaped friendly name (same regex as evaluations).
_RUNTIME_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
# Minted trigger ids are 32-char lowercase hex; allow a slightly looser charset.
_TRIGGER_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
# Strict 6-field EventBridge cron(...) form: minutes hours day-of-month month
# day-of-week year. We validate the wrapper + that each of the 6 fields is a
# safe charset, not the full cron grammar (AWS validates the semantics).
_CRON_RE = re.compile(
    r"^cron\(\s*"
    r"([0-9A-Za-z\*\?\-,/#LW]+)\s+"  # minutes
    r"([0-9A-Za-z\*\?\-,/#LW]+)\s+"  # hours
    r"([0-9A-Za-z\*\?\-,/#LW]+)\s+"  # day-of-month
    r"([0-9A-Za-z\*\?\-,/#LW]+)\s+"  # month
    r"([0-9A-Za-z\*\?\-,/#LW]+)\s+"  # day-of-week
    r"([0-9A-Za-z\*\?\-,/#LW]+)\s*"  # year
    r"\)$"
)

# Cap the serialized event pattern so a tenant can't store an oversized blob.
_MAX_PATTERN_BYTES = 4096


def _validate_runtime_name(name: str) -> str:
    if not name or len(name) > 64:
        raise HTTPException(status_code=400, detail="Invalid runtime_name")
    if not _RUNTIME_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid runtime_name format")
    return name


def _validate_trigger_id(trigger_id: str) -> str:
    if not trigger_id or len(trigger_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid trigger_id")
    if not _TRIGGER_ID_RE.match(trigger_id):
        raise HTTPException(status_code=400, detail="Invalid trigger_id format")
    return trigger_id


def _validate_cron(schedule: str) -> str:
    if not schedule or len(schedule) > 256:
        raise HTTPException(status_code=400, detail="Invalid cron schedule")
    if not _CRON_RE.match(schedule):
        raise HTTPException(
            status_code=400,
            detail="schedule must be a 6-field EventBridge cron(...) expression",
        )
    return schedule


def _validate_pattern(pattern: dict) -> dict:
    if not isinstance(pattern, dict) or not pattern:
        raise HTTPException(
            status_code=400, detail="pattern must be a non-empty JSON object"
        )
    try:
        serialized = json.dumps(pattern)
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail="pattern is not serializable") from e
    if len(serialized.encode("utf-8")) > _MAX_PATTERN_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"pattern exceeds {_MAX_PATTERN_BYTES} bytes",
        )
    return pattern


def _validate_webhook_out_url(url: str) -> str:
    """SSRF-guard an outbound webhook target (Critic Finding 2).

    Reuses the canonical ``gateway_deployer._validate_discovery_url`` (https +
    DNS-resolve + IMDS/RFC1918/link-local/loopback denylist). Both the
    structural-invalid and blocked-network cases map to 400.
    """
    try:
        return _validate_discovery_url(url)
    except (_DiscoveryUrlInvalid, _DiscoveryUrlBlocked) as e:
        raise HTTPException(
            status_code=400, detail=f"Invalid webhook_out_url: {e}"
        ) from e


def _region() -> str:
    return os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))


_OWNER_SUB_SAFE_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def _safe_owner_sub(owner_sub: str) -> str:
    return _OWNER_SUB_SAFE_RE.sub("-", owner_sub)[:64] or "anon"


def _store_webhook_secret(owner_sub: str) -> str:
    """Create an owner-scoped HMAC signing secret and return its ARN.

    Mirrors ``observability.store_credentials``: owner-scoped name under the
    ``agentcore-trigger/`` namespace, tagged with owner_sub. Only the ARN is
    persisted in DDB — never the raw secret (lessons.md rule 5).
    """
    safe_owner = _safe_owner_sub(owner_sub)
    secret_name = f"agentcore-trigger/{safe_owner}-{uuid.uuid4().hex[:12]}"
    created_at_iso = datetime.now(timezone.utc).isoformat()
    sm = boto3.client("secretsmanager", region_name=_region())
    try:
        resp = sm.create_secret(
            Name=secret_name,
            SecretString=secrets.token_hex(32),
            Description="Webhook HMAC signing key (agentcore-flows trigger)",
            Tags=[
                {"Key": "ManagedBy", "Value": "agentcore-flows"},
                {"Key": "Purpose", "Value": "trigger-webhook-hmac"},
                {"Key": "owner_sub", "Value": owner_sub},
                {"Key": "created_at", "Value": created_at_iso},
            ],
        )
    except ClientError as e:
        logger.exception("Failed to store webhook secret in Secrets Manager")
        raise HTTPException(
            status_code=500,
            detail=(
                "Could not store webhook secret: "
                f"{e.response.get('Error', {}).get('Message', str(e))}"
            ),
        ) from e
    return resp["ARN"]


router = APIRouter(prefix="/api/runtimes", tags=["triggers"])


# ---------------------------------------------------------------------------
# Ownership resolution (copied from evaluations._resolve_owned_runtime_id)
# ---------------------------------------------------------------------------


def _resolve_owned_runtime(runtime_name: str, caller_sub: str) -> str:
    """Return the production version's ``runtime_arn`` for the runtime owned by
    *caller_sub*, or 404 if either the runtime or the slot is missing / owned by
    someone else.

    This is the Bug-122 / Bug-126 gate: the write/list/delete owner is the
    production-slot owner, resolved before any trigger-table access, so a tenant
    cannot touch a trigger on another tenant's runtime_name.
    """
    slots = get_slots_store().get(runtime_name)
    if slots is None or not slots.production_version_id:
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(slots.owner_sub, caller_sub)
    version = get_versions_store().get(runtime_name, slots.production_version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(version.owner_sub, caller_sub)
    runtime_arn = getattr(version, "runtime_arn", None) or getattr(
        version, "agent_runtime_arn", None
    )
    if not runtime_arn:
        # Fall back to the canonical runtime_id if no ARN is recorded yet; the
        # invoker resolves the ARN from this id. Never trust a body-supplied arn.
        runtime_arn = getattr(version, "runtime_id", None)
    if not runtime_arn:
        raise HTTPException(status_code=404, detail="Not found")
    return runtime_arn


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateTriggerRequest(BaseModel):
    type: Literal["cron", "eventbridge", "s3", "webhook"]
    # cron(...) expression, required for type=cron.
    schedule: Optional[str] = Field(default=None, max_length=256)
    # event pattern JSON, required for type=eventbridge/s3.
    pattern: Optional[dict] = None
    # optional outbound POST target (SSRF-validated before persist).
    webhook_out_url: Optional[str] = Field(default=None, max_length=2048)
    # NOTE: any client-supplied target_runtime_arn is deliberately IGNORED — the
    # router derives it server-side from the resolved owned version.


class TriggerResponse(BaseModel):
    runtime_name: str
    trigger_id: str
    type: str
    status: str
    target_runtime_arn: str
    schedule: Optional[str] = None
    pattern: Optional[dict] = None
    webhook_secret_ref: Optional[str] = None
    webhook_out_url: Optional[str] = None
    created_at: int
    updated_at: int

    @classmethod
    def from_model(cls, t: Trigger) -> "TriggerResponse":
        return cls(
            runtime_name=t.runtime_name,
            trigger_id=t.trigger_id,
            type=t.type,
            status=t.status,
            target_runtime_arn=t.target_runtime_arn,
            schedule=t.schedule,
            pattern=t.pattern,
            webhook_secret_ref=t.webhook_secret_ref,
            webhook_out_url=t.webhook_out_url,
            created_at=t.created_at,
            updated_at=t.updated_at,
        )


class DeleteTriggerResponse(BaseModel):
    success: bool
    trigger_id: str
    message: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/{runtime_name}/triggers", response_model=TriggerResponse, dependencies=[Depends(require_scopes("trigger:write"))])
async def create_trigger(
    runtime_name: str,
    body: CreateTriggerRequest,
    caller_sub: str = Depends(get_caller_sub),
) -> TriggerResponse:
    """Register a trigger on the caller's runtime.

    Ownership is resolved through the production slot (404 cross-tenant /
    Bug-122). ``target_runtime_arn`` is derived server-side. type-specific
    inputs (cron schedule / event pattern) are validated; a webhook trigger gets
    an owner-scoped HMAC secret in Secrets Manager (only the ARN is persisted).
    """
    runtime_name = _validate_runtime_name(runtime_name)
    target_runtime_arn = _resolve_owned_runtime(runtime_name, caller_sub)

    if body.type not in TRIGGER_TYPES:
        raise HTTPException(status_code=400, detail="Invalid trigger type")

    schedule: Optional[str] = None
    pattern: Optional[dict] = None
    webhook_secret_ref: Optional[str] = None
    webhook_out_url: Optional[str] = None

    if body.type == TYPE_CRON:
        if not body.schedule:
            raise HTTPException(
                status_code=400, detail="schedule is required for cron triggers"
            )
        schedule = _validate_cron(body.schedule)
    elif body.type in (TYPE_EVENTBRIDGE, TYPE_S3):
        if body.pattern is None:
            raise HTTPException(
                status_code=400,
                detail="pattern is required for eventbridge/s3 triggers",
            )
        pattern = _validate_pattern(body.pattern)
    elif body.type == TYPE_WEBHOOK:
        # Inbound webhook is a platform-provided Lambda Function URL; we mint an
        # owner-scoped HMAC signing secret here. Only the ARN is stored.
        webhook_secret_ref = _store_webhook_secret(caller_sub)

    # Optional outbound POST target — SSRF-validated regardless of type.
    if body.webhook_out_url:
        webhook_out_url = _validate_webhook_out_url(body.webhook_out_url)

    trig = get_trigger_store().create_trigger(
        runtime_name=runtime_name,
        owner_sub=caller_sub,
        type=body.type,
        target_runtime_arn=target_runtime_arn,  # server-derived, never body
        schedule=schedule,
        pattern=pattern,
        webhook_secret_ref=webhook_secret_ref,
        webhook_out_url=webhook_out_url,
    )
    logger.info(
        "Created %s trigger %s on runtime %s (owner=%s)",
        trig.type,
        trig.trigger_id,
        runtime_name,
        caller_sub,
    )
    return TriggerResponse.from_model(trig)


@router.get("/{runtime_name}/triggers", response_model=list[TriggerResponse], dependencies=[Depends(require_scopes("trigger:read"))])
async def list_triggers(
    runtime_name: str,
    caller_sub: str = Depends(get_caller_sub),
) -> list[TriggerResponse]:
    """List the caller's triggers for a runtime, newest-first.

    Ownership is gated through the production slot first (404 cross-tenant), then
    the result is visibility-filtered to the caller (defense in depth against
    Bug-126 authz-drift).
    """
    runtime_name = _validate_runtime_name(runtime_name)
    _resolve_owned_runtime(runtime_name, caller_sub)  # 404 cross-tenant / Bug 122

    triggers = get_trigger_store().list_for_runtime(runtime_name)
    return [
        TriggerResponse.from_model(t)
        for t in triggers
        if t.owner_sub == caller_sub
    ]


@router.delete(
    "/{runtime_name}/triggers/{trigger_id}",
    response_model=DeleteTriggerResponse,
    dependencies=[Depends(require_scopes("trigger:write"))],
)
async def delete_trigger(
    runtime_name: str,
    trigger_id: str,
    caller_sub: str = Depends(get_caller_sub),
) -> DeleteTriggerResponse:
    """Delete a trigger the caller owns. Idempotent on already-gone rows.

    Tearing down the provisioned EventBridge rule / Scheduler schedule / Function
    URL + the webhook secret is the live integration step (described in the
    manifest); this pass deletes the registry row.
    """
    runtime_name = _validate_runtime_name(runtime_name)
    trigger_id = _validate_trigger_id(trigger_id)
    # Gate on runtime ownership first (404 cross-tenant / Bug 122).
    _resolve_owned_runtime(runtime_name, caller_sub)

    store = get_trigger_store()
    row = store.get(runtime_name, trigger_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(row.owner_sub, caller_sub)  # 404 on cross-tenant (Bug 126)

    store.delete(runtime_name, trigger_id)
    logger.info(
        "Deleted trigger %s on runtime %s (owner=%s)",
        trigger_id,
        runtime_name,
        caller_sub,
    )
    return DeleteTriggerResponse(
        success=True,
        trigger_id=trigger_id,
        message=f"Trigger {trigger_id} deleted.",
    )
