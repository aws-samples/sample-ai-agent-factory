"""Provisioned scheduled / event trigger API.

Lets a tenant create cron / EventBridge / S3 / webhook triggers against the
production slot of one of their AgentCore runtimes. Cron and event triggers
become deterministic EventBridge rules; webhooks use the static ``/hooks``
route with a per-trigger HMAC key. The DynamoDB row is the lifecycle authority:
it stays ``provisioning`` until AWS resources are attached and is retained as a
retry handle until cleanup is confirmed.

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
with the canonical ``gateway_deployer._validate_outbound_url`` guard (https +
DNS-resolve + IMDS/RFC1918/link-local/loopback denylist) before it is ever
persisted. Dispatch resolves it again and pins the approved IP for the TLS
connection, preventing DNS rebinding from reaching a private target.

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
from dataclasses import replace
from datetime import datetime, timezone
from typing import Literal

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.services.agent_versions_store import (
    get_slots_store,
    get_versions_store,
)
from app.services.auth import assert_owner, get_caller_sub
from app.services.gateway_deployer import (
    _DiscoveryUrlBlocked,
    _DiscoveryUrlInvalid,
    _validate_outbound_url,
)
from app.services.rbac import require_scopes
from app.services.runtime_target_context import (
    resolve_owned_deployment_runtime_target,
)
from app.services.trigger_runtime import (
    MAX_TRIGGER_EVENT_BYTES,
    TriggerCleanupRefused,
    TriggerDispatchError,
    TriggerProvisioningError,
    WebhookAuthenticationError,
    authenticate_webhook,
    cleanup_trigger_resources,
    enqueue_webhook_dispatch,
    provision_trigger,
    validate_trigger_target_runtime_arn,
    webhook_delivery_event,
)
from app.services.trigger_store import (
    STATUS_PROVISIONING,
    TRIGGER_TYPES,
    TYPE_CRON,
    TYPE_EVENTBRIDGE,
    TYPE_S3,
    TYPE_WEBHOOK,
    RuntimeClaim,
    Trigger,
    TriggerClaimConflict,
    TriggerDeleteBusy,
    TriggerSecretDeletionRefused,
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


def _validate_pattern(pattern: dict, *, trigger_type: str | None = None) -> dict:
    if not isinstance(pattern, dict) or not pattern:
        raise HTTPException(status_code=400, detail="pattern must be a non-empty JSON object")
    try:
        serialized = json.dumps(pattern)
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail="pattern is not serializable") from e
    if len(serialized.encode("utf-8")) > _MAX_PATTERN_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"pattern exceeds {_MAX_PATTERN_BYTES} bytes",
        )
    if trigger_type == TYPE_S3:
        sources = pattern.get("source")
        if not isinstance(sources, list) or sources != ["aws.s3"]:
            raise HTTPException(
                status_code=400,
                detail='S3 trigger patterns must set source to ["aws.s3"]',
            )
    return pattern


def _validate_webhook_out_url(url: str) -> str:
    """SSRF-guard an outbound webhook target (Critic Finding 2).

    Reuses the canonical ``gateway_deployer._validate_outbound_url`` (https +
    DNS-resolve + IMDS/RFC1918/link-local/loopback denylist). Both the
    structural-invalid and blocked-network cases map to 400.
    """
    try:
        return _validate_outbound_url(
            url,
            label="Trigger result callback URL",
        )
    except (_DiscoveryUrlInvalid, _DiscoveryUrlBlocked) as e:
        raise HTTPException(status_code=400, detail=f"Invalid webhook_out_url: {e}") from e


def _region() -> str:
    return os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))


_OWNER_SUB_SAFE_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def _safe_owner_sub(owner_sub: str) -> str:
    return _OWNER_SUB_SAFE_RE.sub("-", owner_sub)[:64] or "anon"


def _store_webhook_secret(
    owner_sub: str,
    *,
    secrets_client=None,
) -> tuple[str, str]:
    """Create an owner-scoped HMAC signing secret and return ARN + one-time value.

    Mirrors ``observability.store_credentials``: owner-scoped name under the
    ``agentcore-trigger/`` namespace, tagged with owner_sub. Only the ARN is
    persisted in DDB — never the raw secret (lessons.md rule 5).
    """
    safe_owner = _safe_owner_sub(owner_sub)
    secret_name = f"agentcore-trigger/{safe_owner}-{uuid.uuid4().hex[:12]}"
    created_at_iso = datetime.now(timezone.utc).isoformat()
    sm = secrets_client or boto3.client("secretsmanager", region_name=_region())
    secret_value = secrets.token_hex(32)
    try:
        resp = sm.create_secret(
            Name=secret_name,
            SecretString=secret_value,
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
            detail="Could not store webhook secret",
        ) from e
    return resp["ARN"], secret_value


router = APIRouter(prefix="/api/runtimes", tags=["triggers"])
webhook_router = APIRouter(prefix="/hooks", tags=["trigger-webhooks"])


# ---------------------------------------------------------------------------
# Ownership resolution (copied from evaluations._resolve_owned_runtime_id)
# ---------------------------------------------------------------------------


def _resolve_owned_runtime_claim(runtime_name: str, caller_sub: str) -> RuntimeClaim:
    """Return the production slot row and version row owned by *caller_sub*, as read, or 404.

    This is the Bug-122 / Bug-126 gate: the write/list/delete owner is the
    production-slot owner, resolved before any trigger-table access, so a tenant
    cannot touch a trigger on another tenant's runtime_name.

    F-81f — the two rows are returned, not just the ARN, and both reads are STRONGLY consistent.
    For a create, the rows are the write's conditions (``TriggerStore.create_trigger`` refuses to
    write unless they are unchanged), so what is pinned must be what is actually in the table: an
    eventually consistent read here would pin values the transaction cannot find and fail a
    legitimate create. Returning the ARN alone is how the original bug happened -- the check and
    the write were about different things.
    """
    slots = get_slots_store().get(runtime_name, consistent=True)
    if slots is None or not slots.production_version_id:
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(slots.owner_sub, caller_sub)
    version = get_versions_store().get(runtime_name, slots.production_version_id, consistent=True)
    if version is None:
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(version.owner_sub, caller_sub)
    runtime_arn = getattr(version, "runtime_arn", None) or getattr(version, "agent_runtime_arn", None)
    if not runtime_arn:
        # Fall back to the canonical runtime_id if no ARN is recorded yet; the
        # invoker resolves the ARN from this id. Never trust a body-supplied arn.
        runtime_arn = getattr(version, "runtime_id", None)
    if not runtime_arn:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        return RuntimeClaim(slot=slots, version=version, target_runtime_arn=runtime_arn)
    except ValueError:
        # The two rows passed both owner checks but do not form one claim (a row with no
        # deployment id or status, or one whose runtime name disagrees with its key). That is
        # corrupt metadata, not a tenant's runtime, and it authorizes nothing.
        logger.warning("Runtime %s has slot/version rows that do not form a claim", runtime_name)
        raise HTTPException(status_code=404, detail="Not found") from None


def _resolve_owned_runtime(runtime_name: str, caller_sub: str) -> str:
    """The ARN-only form of ``_resolve_owned_runtime_claim`` for readers and deletes."""
    return _resolve_owned_runtime_claim(runtime_name, caller_sub).target_runtime_arn


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateTriggerRequest(BaseModel):
    type: Literal["cron", "eventbridge", "s3", "webhook"]
    # cron(...) expression, required for type=cron.
    schedule: str | None = Field(default=None, max_length=256)
    # event pattern JSON, required for type=eventbridge/s3.
    pattern: dict | None = None
    # optional outbound POST target (SSRF-validated before persist).
    webhook_out_url: str | None = Field(default=None, max_length=2048)
    # NOTE: any client-supplied target_runtime_arn is deliberately IGNORED — the
    # router derives it server-side from the resolved owned version.


class TriggerResponse(BaseModel):
    runtime_name: str
    trigger_id: str
    type: str
    status: str
    target_runtime_arn: str
    schedule: str | None = None
    pattern: dict | None = None
    webhook_out_url: str | None = None
    webhook_path: str | None = None
    # Returned only by the create response. It is never stored in DynamoDB and
    # list responses therefore contain null.
    webhook_signing_secret: str | None = None
    last_error_code: str | None = None
    created_at: int
    updated_at: int

    @classmethod
    def from_model(
        cls,
        t: Trigger,
        *,
        webhook_signing_secret: str | None = None,
    ) -> TriggerResponse:
        return cls(
            runtime_name=t.runtime_name,
            trigger_id=t.trigger_id,
            type=t.type,
            status=t.status,
            target_runtime_arn=t.target_runtime_arn,
            schedule=t.schedule,
            pattern=t.pattern,
            webhook_out_url=t.webhook_out_url,
            webhook_path=t.webhook_path,
            webhook_signing_secret=webhook_signing_secret,
            last_error_code=t.last_error_code,
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


@router.post(
    "/{runtime_name}/triggers", response_model=TriggerResponse, dependencies=[Depends(require_scopes("trigger:write"))]
)
async def create_trigger(
    runtime_name: str,
    body: CreateTriggerRequest,
    response: Response,
    caller_sub: str = Depends(get_caller_sub),
) -> TriggerResponse:
    """Provision a trigger on the caller's runtime.

    Ownership is resolved through the production slot (404 cross-tenant /
    Bug-122). ``target_runtime_arn`` is derived server-side. type-specific
    inputs (cron schedule / event pattern) are validated; a webhook trigger gets
    an owner-scoped HMAC secret in Secrets Manager (only the ARN is persisted).
    The response is returned only after the row is ACTIVE and is marked
    ``no-store`` because it is the sole disclosure of the webhook secret.
    """
    runtime_name = _validate_runtime_name(runtime_name)
    claim = _resolve_owned_runtime_claim(runtime_name, caller_sub)

    if body.type not in TRIGGER_TYPES:
        raise HTTPException(status_code=400, detail="Invalid trigger type")
    schedule: str | None = None
    pattern: dict | None = None
    webhook_secret_ref: str | None = None
    webhook_out_url: str | None = None
    webhook_signing_secret: str | None = None
    webhook_secrets_client = None

    # Validate every request-only field before creating side effects. Previously
    # a webhook with an invalid outbound URL minted its HMAC secret first and
    # then returned 400, permanently leaking the unrecorded secret.
    if body.webhook_out_url:
        webhook_out_url = _validate_webhook_out_url(body.webhook_out_url)

    if body.type == TYPE_CRON:
        if not body.schedule:
            raise HTTPException(status_code=400, detail="schedule is required for cron triggers")
        schedule = _validate_cron(body.schedule)
    elif body.type in (TYPE_EVENTBRIDGE, TYPE_S3):
        if body.pattern is None:
            raise HTTPException(
                status_code=400,
                detail="pattern is required for eventbridge/s3 triggers",
            )
        pattern = _validate_pattern(body.pattern, trigger_type=body.type)
    elif body.type != TYPE_WEBHOOK:
        raise HTTPException(status_code=400, detail="Invalid trigger type")

    # Refuse an MCP-protocol runtime before any side effect. The trigger
    # dispatcher invokes an AgentCore HTTP agent with a prompt envelope; a
    # persisted MCP runtime speaks bearer-authenticated JSON-RPC over the MCP
    # data plane instead, so any trigger registered for it would create durable
    # infrastructure (EventBridge rule, webhook secret, provisioning row) that
    # can never deliver. The protocol is read from the owner-checked deployment
    # row that also authorized the claim above -- placed AFTER ownership so a
    # non-owner still gets 404 and never learns the protocol, and AFTER request
    # validation so a malformed request stays a 400.
    # Read the protocol from the owner-checked deployment row that also
    # authorized the claim above, and fail CLOSED: the row is written before the
    # AgentVersion and read with strong consistency, so an unreadable/missing row
    # is a real outage, not an eventual-consistency miss. Guessing HTTP would
    # provision durable trigger infrastructure (EventBridge rule, webhook secret,
    # provisioning row) for a runtime that may actually be MCP -- exactly the
    # broken configuration this guard exists to prevent -- so any lookup failure
    # propagates before a single side effect. Placed AFTER ownership so a
    # non-owner still gets 404 and never learns the protocol, and AFTER request
    # validation so a malformed request stays a 400.
    target = resolve_owned_deployment_runtime_target(
        claim.version.deployment_id,
        caller_sub,
    )
    if str(getattr(target, "protocol", "HTTP")).upper() == "MCP":
        raise HTTPException(
            status_code=409,
            detail=(
                "This runtime uses the MCP protocol and cannot back a trigger. "
                "Triggers invoke the HTTP agent envelope; exercise an MCP "
                "runtime through /api/test-mcp-runtime instead."
            ),
        )

    # Validate deployment eligibility after the request itself, but before any
    # Secrets Manager or EventBridge side effect. A malformed request remains a
    # 400 even if the runtime is temporarily ineligible.
    if claim.version.status != "succeeded":
        raise HTTPException(
            status_code=409,
            detail="Triggers require a succeeded production runtime deployment",
        )
    try:
        validate_trigger_target_runtime_arn(claim.target_runtime_arn)
    except TriggerProvisioningError:
        raise HTTPException(
            status_code=409,
            detail="Triggers require a canonical AgentCore production runtime ARN",
        ) from None

    if body.type == TYPE_WEBHOOK:
        # Inbound webhook uses the platform's static /hooks route. Mint the
        # per-trigger HMAC secret before the row transaction; only its ARN is
        # stored and the raw value is returned once.
        webhook_secrets_client = boto3.client(
            "secretsmanager",
            region_name=_region(),
        )
        webhook_secret_ref, webhook_signing_secret = _store_webhook_secret(
            caller_sub,
            secrets_client=webhook_secrets_client,
        )

    store = get_trigger_store()
    provisioning_token = secrets.token_hex(16)
    try:
        # F-81f — the row is written in one transaction conditioned on the exact slot and version
        # rows resolved above (the runtime name and the target come from the claim, not from here).
        # A teardown, promote or redeploy that landed since the read cancels the write instead of
        # leaving a trigger under a name whose slot is gone -- which the owner can then never
        # delete, because this very resolver answers 404 for it.
        trig = store.create_trigger(
            claim=claim,
            owner_sub=caller_sub,
            type=body.type,
            status=STATUS_PROVISIONING,
            schedule=schedule,
            pattern=pattern,
            webhook_secret_ref=webhook_secret_ref,
            webhook_out_url=webhook_out_url,
            provisioning_token=provisioning_token,
        )
    except Exception as exc:
        # Secrets Manager + DynamoDB cannot share a transaction. Compensate the
        # side effect before returning: otherwise every transient DDB failure
        # after CreateSecret leaks a credential with no row that can find it.
        if webhook_secret_ref and webhook_secrets_client is not None:
            try:
                # The ARN came directly from this request's successful
                # CreateSecret response, so provenance is already exact. Delete
                # directly rather than relying on an immediately-consistent
                # DescribeSecret round trip during compensation.
                webhook_secrets_client.delete_secret(
                    SecretId=webhook_secret_ref,
                    ForceDeleteWithoutRecovery=True,
                )
            except Exception:
                logger.exception(
                    "Trigger create compensation could not delete webhook secret for runtime %s",
                    runtime_name,
                )
        if isinstance(exc, TriggerClaimConflict):
            # Nothing was written; the runtime moved under the request. Not a server error and
            # not a 404 either: the caller's read WAS authorized, it is just no longer current.
            logger.warning("Trigger create for runtime %s lost a race with a teardown or promote", runtime_name)
            raise HTTPException(
                status_code=409,
                detail="The runtime changed while the trigger was being registered; re-check it and retry",
            ) from None
        logger.exception("Could not persist trigger for runtime %s", runtime_name)
        raise HTTPException(
            status_code=500,
            detail="Could not register trigger",
        ) from exc

    try:
        resources = provision_trigger(trig)
    except Exception as exc:
        # put_rule and put_targets are separate EventBridge calls. A failure at
        # either point is compensated from the deterministic rule name; the row
        # stays visible as ERROR if cleanup cannot be proved.
        try:
            cleanup_trigger_resources(trig, include_secret=False)
        except Exception:
            logger.exception(
                "Trigger provisioning compensation failed for %s/%s",
                runtime_name,
                trig.trigger_id,
            )
        store.fail_provisioning(
            runtime_name=runtime_name,
            trigger_id=trig.trigger_id,
            provisioning_token=provisioning_token,
            error_code=type(exc).__name__,
        )
        logger.error(
            "Could not provision trigger %s/%s (%s)",
            runtime_name,
            trig.trigger_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f"Trigger {trig.trigger_id} was recorded but its AWS resource "
                "could not be activated; delete it and retry"
            ),
        ) from None

    try:
        completed = store.complete_provisioning(
            runtime_name=runtime_name,
            trigger_id=trig.trigger_id,
            provisioning_token=provisioning_token,
            eventbridge_rule_arn=resources.eventbridge_rule_arn,
            webhook_path=resources.webhook_path,
        )
    except Exception as exc:
        # The AWS resource exists but its handle could not be published. Stop
        # it from firing before returning. The deterministic name also lets a
        # later delete retry cleanup even if this compensation is interrupted.
        provisioned = replace(
            trig,
            eventbridge_rule_arn=resources.eventbridge_rule_arn,
            webhook_path=resources.webhook_path,
        )
        try:
            cleanup_trigger_resources(provisioned, include_secret=False)
        except Exception:
            logger.exception(
                "Trigger activation compensation failed for %s/%s",
                runtime_name,
                trig.trigger_id,
            )
        try:
            store.fail_provisioning(
                runtime_name=runtime_name,
                trigger_id=trig.trigger_id,
                provisioning_token=provisioning_token,
                error_code=type(exc).__name__,
            )
        except Exception:
            logger.exception(
                "Could not record trigger activation failure for %s/%s",
                runtime_name,
                trig.trigger_id,
            )
        raise HTTPException(
            status_code=503,
            detail=(
                f"Trigger {trig.trigger_id} could not publish its activation; "
                "its resources were stopped and the operation can be retried"
            ),
        ) from None

    if completed is None:
        # A delete or teardown won the race after the AWS create. Remove what
        # this creator made, but leave secret ownership to the deletion path
        # while its row still exists.
        provisioned = replace(
            trig,
            eventbridge_rule_arn=resources.eventbridge_rule_arn,
            webhook_path=resources.webhook_path,
        )
        current = store.get(runtime_name, trig.trigger_id, consistent=True)
        try:
            cleanup_trigger_resources(
                provisioned,
                include_secret=current is None,
            )
        except Exception:
            logger.exception(
                "Lost-owner trigger compensation failed for %s/%s",
                runtime_name,
                trig.trigger_id,
            )
        raise HTTPException(
            status_code=409,
            detail="The trigger was deleted while it was being activated",
        )

    logger.info(
        "Activated %s trigger %s on runtime %s (owner=%s)",
        completed.type,
        completed.trigger_id,
        runtime_name,
        caller_sub,
    )
    # A webhook signing key exists only in this response. Explicitly prevent
    # browsers and intermediaries from retaining the otherwise successful POST.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return TriggerResponse.from_model(
        completed,
        webhook_signing_secret=webhook_signing_secret,
    )


@router.get(
    "/{runtime_name}/triggers",
    response_model=list[TriggerResponse],
    dependencies=[Depends(require_scopes("trigger:read"))],
)
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
    return [TriggerResponse.from_model(t) for t in triggers if t.owner_sub == caller_sub]


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

    The row is first moved to ``deleting`` so an in-flight creator cannot
    publish ``active`` after cleanup. Metadata is removed only after every
    provisioned resource and webhook secret is confirmed absent.
    """
    runtime_name = _validate_runtime_name(runtime_name)
    trigger_id = _validate_trigger_id(trigger_id)
    # Gate on runtime ownership first (404 cross-tenant / Bug 122).
    _resolve_owned_runtime(runtime_name, caller_sub)

    store = get_trigger_store()
    row = store.get(runtime_name, trigger_id, consistent=True)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(row.owner_sub, caller_sub)  # 404 on cross-tenant (Bug 126)

    delete_token = secrets.token_hex(16)
    try:
        claimed = store.claim_delete(
            runtime_name=runtime_name,
            trigger_id=trigger_id,
            owner_sub=caller_sub,
            delete_token=delete_token,
        )
    except TriggerDeleteBusy:
        raise HTTPException(
            status_code=409,
            detail="A trigger delivery is still in progress; retry deletion shortly",
        ) from None
    if claimed is None:
        raise HTTPException(status_code=404, detail="Not found")

    try:
        # The deleting status fences an in-flight provisioner before AWS
        # cleanup starts. Metadata remains until every resource is confirmed
        # absent, preserving the retry handle on any failure.
        cleanup_trigger_resources(claimed)
    except (TriggerSecretDeletionRefused, TriggerCleanupRefused) as exc:
        logger.error(
            "Refused unsafe resource cleanup for trigger %s/%s (%s)",
            runtime_name,
            trigger_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=409,
            detail="Trigger resources could not be safely deleted",
        ) from None
    except Exception as exc:
        logger.error(
            "Resource cleanup failed for trigger %s/%s (%s)",
            runtime_name,
            trigger_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail="Could not delete trigger resources",
        ) from exc

    if not store.delete_claimed(
        runtime_name=runtime_name,
        trigger_id=trigger_id,
        delete_token=delete_token,
    ):
        # A same-owner concurrent retry may have replaced the token and
        # completed deletion already. Treat an absent row as success; otherwise
        # preserve the newer claimant's authority.
        if store.get(runtime_name, trigger_id, consistent=True) is not None:
            raise HTTPException(
                status_code=409,
                detail="Trigger deletion is already in progress",
            )
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


@webhook_router.post("/{runtime_name}/{trigger_id}")
async def receive_webhook(
    runtime_name: str,
    trigger_id: str,
    request: Request,
) -> JSONResponse:
    """Authenticate a public webhook and enqueue its AgentCore invocation."""

    runtime_name = _validate_runtime_name(runtime_name)
    trigger_id = _validate_trigger_id(trigger_id)
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
            if declared_length < 0:
                raise ValueError
            if declared_length > MAX_TRIGGER_EVENT_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="Webhook payload exceeds the size limit",
                )
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid Content-Length header",
            ) from None
    body = await request.body()
    # Content-Length is optional and cannot be trusted to match the bytes
    # delivered by every proxy/client. Enforce the limit against the body too.
    if len(body) > MAX_TRIGGER_EVENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Webhook payload exceeds the size limit",
        )
    trigger = get_trigger_store().get(
        runtime_name,
        trigger_id,
        consistent=True,
    )
    if trigger is None:
        # Missing, inactive, and bad-signature requests share one response so
        # the public endpoint is not a trigger-existence oracle.
        raise HTTPException(status_code=401, detail="Webhook authentication failed")

    try:
        delivery_id = authenticate_webhook(
            trigger,
            timestamp=request.headers.get("x-agentcore-timestamp"),
            delivery_id=request.headers.get("x-agentcore-delivery-id"),
            signature=request.headers.get("x-agentcore-signature"),
            body=body,
        )
    except WebhookAuthenticationError:
        raise HTTPException(
            status_code=401,
            detail="Webhook authentication failed",
        ) from None

    delivery_event = webhook_delivery_event(
        body=body,
        delivery_id=delivery_id,
        content_type=request.headers.get("content-type", ""),
    )
    try:
        enqueue_webhook_dispatch(
            trigger=trigger,
            delivery_event=delivery_event,
        )
    except TriggerDispatchError:
        raise HTTPException(
            status_code=503,
            detail="Webhook delivery could not be queued",
        ) from None
    return JSONResponse(
        status_code=202,
        content={
            "accepted": True,
            "trigger_id": trigger_id,
            "delivery_id": delivery_id,
        },
    )
