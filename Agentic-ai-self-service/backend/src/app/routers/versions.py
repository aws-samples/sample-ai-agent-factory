"""Agent versions API.

Phase 1 Gap 1A endpoints — list versions of a runtime, promote a version to
production, roll back to the previous production version.

Tenant isolation per Critic Finding 3: every read/write checks
``assert_owner`` against the caller's Cognito sub. Cross-tenant access
returns 404 (existence-non-disclosure).
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.models.deployment_models import TestRequest, TestResponse
from app.services.agent_versions_store import (
    AdmissionApprovalConflict,
    AgentVersion,
    RuntimeSlots,
    SlotWriteConflict,
    VersionFence,
    get_slots_store,
    get_versions_store,
    set_slot_pointers_atomically,
)
from app.services.auth import assert_owner, extract_cognito_groups, get_caller_sub
from app.services.delegated_identity import (
    DelegatedIdentityError,
    deployment_requires_obo,
    extract_user_bearer,
    workforce_token_expectations,
)
from app.services.gateway_deployer import gateway_aws_session, get_cognito_token
from app.services.harness_deployer import invoke_harness
from app.services.invocation_budget import (
    reserve_invocation_budget,
    settle_invocation_budget,
)
from app.services.invocation_identity import memory_invocation_identity
from app.services.promotion_evidence import (
    PromotionEvidenceError,
    require_promotion_evidence,
    validate_frozen_promotion_policy,
)
from app.services.rbac import require_scopes
from app.services.runtime_invocation import (
    invoke_verified_http_runtime,
    promote_pending_policy,
)
from app.services.runtime_target_context import (
    get_deployment_store,
    resolve_owned_runtime_slot_target,
)
from app.services.workspace_approval import (
    MalformedAdmissionError,
    admission_tier,
    pending_groups,
    promotion_blocked_message,
    required_approver_groups,
)

logger = logging.getLogger(__name__)


def _validate_runtime_name(runtime_name: str) -> str:
    if not runtime_name or len(runtime_name) > 64:
        raise HTTPException(status_code=400, detail="Invalid runtime_name")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", runtime_name):
        raise HTTPException(
            status_code=400,
            detail=("runtime_name must match [a-zA-Z][a-zA-Z0-9_]* (AgentCore naming rules)"),
        )
    return runtime_name


def _validate_version_id(version_id: str) -> str:
    # Our minted ids are 32-char lowercase hex. Allow a slightly looser charset
    # so future schemes (ULIDs in Crockford base32, etc) don't require a router
    # change.
    if not version_id or len(version_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid version_id")
    if not re.match(r"^[a-zA-Z0-9_-]+$", version_id):
        raise HTTPException(status_code=400, detail="Invalid version_id format")
    return version_id


router = APIRouter(prefix="/api/runtimes", tags=["versions"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class VersionResponse(BaseModel):
    runtime_name: str
    version_id: str
    created_at: str
    deployment_id: str
    agentcore_runtime_name: str
    runtime_id: str | None = None
    runtime_arn: str | None = None
    runtime_endpoint: str | None = None
    parent_version_id: str | None = None
    status: str
    description: str | None = None
    production_gate_required: bool | None = None
    promotion_policy: dict | None = None
    promotion_evidence: dict | None = None
    admission: dict | None = None
    admission_approvals: list[dict] | None = None

    @classmethod
    def from_model(cls, v: AgentVersion) -> VersionResponse:
        return cls(
            runtime_name=v.runtime_name,
            version_id=v.version_id,
            created_at=v.created_at,
            deployment_id=v.deployment_id,
            agentcore_runtime_name=v.agentcore_runtime_name,
            runtime_id=v.runtime_id,
            runtime_arn=v.runtime_arn,
            runtime_endpoint=v.runtime_endpoint,
            parent_version_id=v.parent_version_id,
            status=v.status,
            description=v.description,
            production_gate_required=v.production_gate_required,
            promotion_policy=v.promotion_policy,
            promotion_evidence=v.promotion_evidence,
            admission=v.admission,
            admission_approvals=v.admission_approvals,
        )


class SlotsResponse(BaseModel):
    runtime_name: str
    production_version_id: str | None = None
    staging_version_id: str | None = None
    previous_production_version_id: str | None = None
    last_promoted_at: str | None = None


class PromoteResponse(BaseModel):
    success: bool
    runtime_name: str
    promoted_version_id: str
    slot: str
    previous_version_id: str | None = None
    message: str


class PromoteRequest(BaseModel):
    slot: Literal["staging", "production"] = Field(default="production")


class ApproveRequest(BaseModel):
    """One approver group's approval of a version's workspace admission (plan §3.B).

    Explicit on purpose: a caller who belongs to several approver groups approves for ONE of them
    per call, so an approval record always names the group it was given under.
    """

    model_config = ConfigDict(extra="forbid")

    group: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")


class SlotHistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(max_length=10000)


class SlotInvokeRequest(BaseModel):
    """Only conversational input; target authority comes from the slot."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    input: str = Field(max_length=10000)
    session_id: str | None = Field(alias="sessionId", default=None, max_length=256)
    history: list[SlotHistoryMessage] | None = Field(default=None, max_length=50)


class SlotInvokeResponse(TestResponse):
    model_config = ConfigDict(populate_by_name=True)

    runtime_name: str = Field(alias="runtimeName")
    slot: Literal["staging", "production"]
    version_id: str = Field(alias="versionId")
    deployment_id: str = Field(alias="deploymentId")
    runtime_id: str = Field(alias="runtimeId")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/{runtime_name}/versions",
    response_model=list[VersionResponse],
    dependencies=[Depends(require_scopes("agent:read"))],
)
async def list_versions(
    runtime_name: str,
    caller_sub: str = Depends(get_caller_sub),
) -> list[VersionResponse]:
    """List every version of *runtime_name* owned by the caller, newest first."""
    runtime_name = _validate_runtime_name(runtime_name)
    versions = get_versions_store().list_for_runtime(runtime_name)
    if not versions:
        # Nothing exists — return empty list (don't 404, the runtime name
        # may simply be new). For non-empty results, ownership is checked
        # below; if the runtime exists but the caller doesn't own it, the
        # filter yields an empty list which is indistinguishable from
        # "no versions" — that's intentional (existence-non-disclosure).
        return []
    return [VersionResponse.from_model(v) for v in versions if v.owner_sub == caller_sub]


@router.get("/{runtime_name}/slots", response_model=SlotsResponse, dependencies=[Depends(require_scopes("agent:read"))])
async def get_slots(
    runtime_name: str,
    caller_sub: str = Depends(get_caller_sub),
) -> SlotsResponse:
    """Return the production + staging slot pointers for a runtime."""
    runtime_name = _validate_runtime_name(runtime_name)
    slots = get_slots_store().get(runtime_name, consistent=True)
    if slots is None:
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(slots.owner_sub, caller_sub)
    return SlotsResponse(
        runtime_name=slots.runtime_name,
        production_version_id=slots.production_version_id,
        staging_version_id=slots.staging_version_id,
        previous_production_version_id=slots.previous_production_version_id,
        last_promoted_at=slots.last_promoted_at,
    )


@router.post(
    "/{runtime_name}/slots/{slot}/invoke",
    response_model=SlotInvokeResponse,
    response_model_by_alias=True,
    dependencies=[Depends(require_scopes("invoke"))],
)
async def invoke_runtime_slot(
    runtime_name: str,
    slot: Literal["staging", "production"],
    body: SlotInvokeRequest,
    raw_request: Request,
    caller_sub: str = Depends(get_caller_sub),
) -> SlotInvokeResponse:
    """Invoke the exact HTTP runtime version currently assigned to ``slot``."""

    runtime_name = _validate_runtime_name(runtime_name)
    target = resolve_owned_runtime_slot_target(
        runtime_name,
        slot,
        caller_sub,
        required_protocol="HTTP",
    )
    deployment_state = target.deployment_dict()
    invocation_request = TestRequest(
        input=body.input,
        sessionId=body.session_id,
        history=([message.model_dump(mode="json") for message in body.history] if body.history else None),
    )

    def _promote_policy(state: dict, region: str) -> bool:
        return promote_pending_policy(
            state,
            region,
            target_event=target.target_event(),
            state_store=get_deployment_store(),
        )

    user_bearer_token = None
    if deployment_requires_obo(deployment_state):
        try:
            expected_issuer, expected_client_id = workforce_token_expectations(target.region)
            user_bearer_token = extract_user_bearer(
                raw_request.headers,
                expected_subject=caller_sub,
                expected_issuer=expected_issuer,
                expected_client_id=expected_client_id,
            )
        except DelegatedIdentityError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    reservation = reserve_invocation_budget(
        owner_sub=caller_sub,
        runtime_name=runtime_name,
        resource_tags=deployment_state.get("resource_tags") or {},
    )
    dispatched = False

    def _mark_dispatched() -> None:
        nonlocal dispatched
        dispatched = True

    try:
        result = invoke_verified_http_runtime(
            invocation_request,
            caller_sub=caller_sub,
            deployment_state=deployment_state,
            runtime_id=target.runtime_id,
            runtime_arn=target.runtime_arn,
            region=target.region,
            target_session=target.session,
            promote_policy=_promote_policy,
            invoke_harness=invoke_harness,
            resolve_memory_identity=memory_invocation_identity,
            gateway_session=gateway_aws_session,
            get_gateway_token=get_cognito_token,
            user_bearer_token=user_bearer_token,
            on_dispatch=_mark_dispatched,
            evaluation_capture=(slot == "staging" and target.production_gate_required is True),
        )
    except Exception:
        settle_invocation_budget(reservation, dispatched=dispatched)
        raise
    settle_invocation_budget(reservation, dispatched=dispatched)
    return SlotInvokeResponse(
        **result.model_dump(mode="python"),
        runtime_name=runtime_name,
        slot=slot,
        version_id=target.version_id,
        deployment_id=target.deployment_id,
        runtime_id=target.runtime_id,
    )


@router.post(
    "/{runtime_name}/invoke",
    response_model=SlotInvokeResponse,
    response_model_by_alias=True,
    dependencies=[Depends(require_scopes("invoke"))],
)
async def invoke_runtime_client(
    runtime_name: str,
    body: SlotInvokeRequest,
    raw_request: Request,
    caller_sub: str = Depends(get_caller_sub),
) -> SlotInvokeResponse:
    """Stable client surface pinned to the owner-checked production slot."""

    return await invoke_runtime_slot(
        runtime_name,
        "production",
        body,
        raw_request,
        caller_sub,
    )


@router.post(
    "/{runtime_name}/versions/{version_id}/promote",
    response_model=PromoteResponse,
    dependencies=[Depends(require_scopes("agent:write"))],
)
async def promote_version(
    runtime_name: str,
    version_id: str,
    body: PromoteRequest = PromoteRequest(),  # noqa: B008 — FastAPI optional-body idiom (validated per request)
    caller_sub: str = Depends(get_caller_sub),
) -> PromoteResponse:
    """Move *version_id* into the requested slot (default: production)."""
    runtime_name = _validate_runtime_name(runtime_name)
    version_id = _validate_version_id(version_id)

    versions_store = get_versions_store()
    # Strongly consistent: F-83 makes this row's (status, created_at) a CONDITION on the slot write,
    # so a stale read here pins values the transaction cannot find and fails a legitimate promote.
    target = versions_store.get(runtime_name, version_id, consistent=True)
    if target is None:
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(target.owner_sub, caller_sub)
    if target.status != "succeeded":
        # Refuse to point a slot at a version whose runtime never came up.
        # The frontend can still display these but they're not promotable.
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot promote version {version_id}: status is "
                f"'{target.status}'. Only succeeded deploys can be promoted."
            ),
        )

    promotion_authority = "deployment"
    promotion_evidence = None
    promotion_policy = None
    if body.slot == "production":
        # Workspace admission (plan §3.B): a medium/high version is promotable only once every
        # approver group its policy named has approved. Read from the ROW, so a flag flipped off
        # after admission releases nothing; versions admitted without a requirement pass through.
        # A present-but-malformed record stays blocked: a requirement that cannot be read cannot
        # be shown to be satisfied.
        try:
            pending = pending_groups(target.admission, target.admission_approvals, owner_sub=target.owner_sub)
        except MalformedAdmissionError as exc:
            logger.error("Version %s/%s has a malformed admission record (%s)", runtime_name, version_id, exc)
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot promote version {version_id} to production: its workspace admission record is "
                    "malformed, so its approval requirement cannot be verified. An administrator must review it."
                ),
            ) from None
        if pending:
            raise HTTPException(
                status_code=409,
                detail=promotion_blocked_message(version_id, admission_tier(target.admission), pending),
            )
    if body.slot == "production" and target.production_gate_required:
        try:
            promotion_policy = validate_frozen_promotion_policy(target.promotion_policy)
            promotion_evidence = require_promotion_evidence(
                target.promotion_evidence,
                runtime_name=runtime_name,
                version_id=version_id,
                deployment_id=target.deployment_id,
                runtime_id=target.runtime_id,
                policy=promotion_policy,
            )
        except PromotionEvidenceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        promotion_authority = "evaluation"

    slots_store = get_slots_store()
    # Consistent for the same reason as the version read: F-83 turns this read into the write's
    # compare-and-set condition, so reading a stale row pins values the transaction cannot match.
    existing = slots_store.get(runtime_name, consistent=True)
    if existing is not None:
        assert_owner(existing.owner_sub, caller_sub)
    base = existing if existing is not None else RuntimeSlots(runtime_name=runtime_name, owner_sub=caller_sub)

    previous = base.production_version_id if body.slot == "production" else base.staging_version_id

    # ``replace`` rather than in-place mutation: ``base`` IS ``existing``, which is the row the write
    # conditions on, so mutating it would make the condition assert the new values and the
    # compare-and-set would pass against any row.
    if body.slot == "production":
        updated = replace(
            base,
            previous_production_version_id=base.production_version_id,
            production_version_id=version_id,
            last_promoted_at=datetime.now(timezone.utc).isoformat(),
        )
    else:
        updated = replace(base, staging_version_id=version_id)

    try:
        set_slot_pointers_atomically(
            runtime_name,
            expected=existing,
            new=updated,
            require_version=VersionFence(
                version_id=version_id,
                owner_sub=target.owner_sub,
                status=target.status,
                created_at=target.created_at or None,
                slot=body.slot,
                deployment_id=target.deployment_id,
                runtime_id=target.runtime_id,
                runtime_arn=target.runtime_arn,
                production_gate_required=target.production_gate_required,
                promotion_authority=promotion_authority,
                promotion_evidence_digest=(
                    str(promotion_evidence["evidence_digest"]) if promotion_evidence is not None else None
                ),
                promotion_policy_digest=(
                    str(promotion_policy["policy_digest"]) if promotion_policy is not None else None
                ),
                minimum_promotion_evidence_expiry_epoch=(
                    int(promotion_evidence["expires_at_epoch"]) if promotion_evidence is not None else None
                ),
                # Workspace admission: the production pointer is conditioned on exactly the admission
                # and approvals this route validated above (or their absence), so a requirement
                # tightened or an approval replaced after the read cannot be promoted past.
                admission_fenced=body.slot == "production",
                admission=target.admission,
                admission_approvals=target.admission_approvals,
            ),
        )
    except SlotWriteConflict:
        # Someone else moved this runtime's slots, or the version stopped being the succeeded row
        # this promote was authorized against, between the reads above and the write. Nothing was
        # written. 409 rather than 500: the caller's own next read is the fix, and the detail says
        # nothing about the other writer.
        raise HTTPException(
            status_code=409,
            detail=(
                "This runtime's version slots changed while the promote was in flight, so nothing "
                "was changed. Re-read the slots and retry."
            ),
        ) from None

    logger.info(
        "Promoted %s/%s to %s slot (caller=%s, previous=%s)",
        runtime_name,
        version_id,
        body.slot,
        caller_sub,
        previous,
    )
    return PromoteResponse(
        success=True,
        runtime_name=runtime_name,
        promoted_version_id=version_id,
        slot=body.slot,
        previous_version_id=previous,
        message=f"Promoted version {version_id} to {body.slot}",
    )


@router.post(
    "/{runtime_name}/versions/{version_id}/approve",
    response_model=VersionResponse,
    dependencies=[Depends(require_scopes("hitl:write"))],
)
async def approve_version(
    runtime_name: str,
    version_id: str,
    body: ApproveRequest,
    request: Request,
    caller_sub: str = Depends(get_caller_sub),
) -> VersionResponse:
    """Record one approver group's approval of a version's workspace admission (plan §3.B).

    Who may call this: a member of ``body.group`` (from the caller's ``cognito:groups`` claim) where
    that group is one the version's admission routed to, holding the existing HITL approval scope
    ``hitl:write``. The version's OWNER is refused whatever groups they hold (403): an approval is a
    second person's decision. The owner check runs before the membership check so a self-approval
    is always named as such.

    Existence non-disclosure: a caller who is neither the owner nor a member of ANY group the version
    routes to gets the same 404 as a missing version, so a ``hitl:write`` holder cannot enumerate
    other tenants' versions or learn their approval routing. Only a member of some required group
    sees the 409s that explain which group is wrong or what is still pending.

    Idempotent per group: a repeated approval returns the current row unchanged.
    """
    from app.services.resource_ownership import owner_sub_hash

    runtime_name = _validate_runtime_name(runtime_name)
    version_id = _validate_version_id(version_id)
    versions_store = get_versions_store()
    target = versions_store.get(runtime_name, version_id, consistent=True)
    if target is None:
        raise HTTPException(status_code=404, detail="Not found")
    caller_groups = set(extract_cognito_groups(request))
    # Parse the admission record tolerantly FIRST so that a malformed record is not an existence
    # oracle: an unrelated caller must get the same 404 whether the record is sound or broken.
    try:
        required = required_approver_groups(target.admission)
        malformed = False
    except MalformedAdmissionError as exc:
        logger.error("Version %s/%s has a malformed admission record (%s)", runtime_name, version_id, exc)
        required, malformed = [], True
    is_owner = caller_sub == target.owner_sub
    # Only an owner, or a member of a group the record names, learns anything beyond 404. For a
    # malformed record nobody but the owner is a named approver, so the owner is told to call an admin.
    if not is_owner and not (caller_groups & set(required)):
        logger.warning(
            "Admission approval denied: caller %s is not an approver of version %s/%s",
            owner_sub_hash(caller_sub),
            runtime_name,
            version_id,
        )
        raise HTTPException(status_code=404, detail="Not found")
    if malformed:
        raise HTTPException(
            status_code=409,
            detail="This version's workspace admission record is malformed; an administrator must review it.",
        )
    if is_owner:
        if not required:
            raise HTTPException(
                status_code=409, detail=f"Version {version_id} does not require workspace admission approval."
            )
        logger.warning(
            "Admission approval denied: owner %s tried to approve their own version %s/%s",
            owner_sub_hash(caller_sub),
            runtime_name,
            version_id,
        )
        raise HTTPException(
            status_code=403,
            detail="A version's owner cannot approve their own version; approval is a second person's decision.",
        )
    if body.group not in required:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{body.group!r} is not an approver group for version {version_id}; "
                f"its admission routes to {', '.join(required)}."
            ),
        )
    if body.group not in caller_groups:
        logger.warning(
            "Admission approval denied: caller %s is not a member of %s (version %s/%s)",
            owner_sub_hash(caller_sub),
            body.group,
            runtime_name,
            version_id,
        )
        raise HTTPException(status_code=403, detail=f"You are not a member of the approver group {body.group!r}.")

    try:
        updated = versions_store.record_admission_approval(
            runtime_name,
            version_id,
            expected_owner_sub=target.owner_sub,
            expected_deployment_id=target.deployment_id,
            expected_admission=target.admission,
            group=body.group,
            approver_sub=caller_sub,
            approved_at=datetime.now(timezone.utc).isoformat(),
        )
    except AdmissionApprovalConflict:
        raise HTTPException(
            status_code=409,
            detail="This version changed while the approval was in flight, so nothing was recorded. Re-read and retry.",
        ) from None
    logger.info(
        "Admission approval recorded: version %s/%s group %s by %s (pending: %s)",
        runtime_name,
        version_id,
        body.group,
        owner_sub_hash(caller_sub),
        pending_groups(updated.admission, updated.admission_approvals, owner_sub=updated.owner_sub),
    )
    return VersionResponse.from_model(updated)


@router.post(
    "/{runtime_name}/rollback", response_model=PromoteResponse, dependencies=[Depends(require_scopes("agent:write"))]
)
async def rollback_runtime(
    runtime_name: str,
    caller_sub: str = Depends(get_caller_sub),
) -> PromoteResponse:
    """Roll the production slot back to the previous version.

    Implementation: swap ``production_version_id`` ↔
    ``previous_production_version_id``. Subsequent rollbacks therefore
    oscillate between the two most recent productions — explicit
    ``promote()`` calls are required for further history navigation.
    """
    runtime_name = _validate_runtime_name(runtime_name)
    slots_store = get_slots_store()
    # Consistent: this row is both the authorization input and the write's condition (F-83).
    slots = slots_store.get(runtime_name, consistent=True)
    if slots is None:
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(slots.owner_sub, caller_sub)

    if not slots.previous_production_version_id:
        raise HTTPException(
            status_code=409,
            detail=("No previous production version to roll back to — this is the first deploy of this runtime."),
        )

    target_version = slots.previous_production_version_id
    # Confirm the target is a real, succeeded version in the table. Consistent, because its
    # (status, created_at, deployment_id) become conditions on the write below.
    target = get_versions_store().get(runtime_name, target_version, consistent=True)
    if target is None or target.status != "succeeded":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Previous production version {target_version} is missing or not in succeeded state; cannot roll back."
            ),
        )

    rolled_from = slots.production_version_id
    updated = replace(
        slots,
        previous_production_version_id=slots.production_version_id,
        production_version_id=target_version,
        last_promoted_at=datetime.now(timezone.utc).isoformat(),
    )
    try:
        set_slot_pointers_atomically(
            runtime_name,
            expected=slots,
            new=updated,
            require_version=VersionFence(
                version_id=target_version,
                owner_sub=target.owner_sub,
                status=target.status,
                created_at=target.created_at or None,
                slot="production",
                deployment_id=target.deployment_id,
                runtime_id=target.runtime_id,
                runtime_arn=target.runtime_arn,
                production_gate_required=target.production_gate_required,
                promotion_authority="rollback",
            ),
        )
    except SlotWriteConflict:
        # A rollback that lost the race must not be retried silently: the row it would roll back
        # FROM is no longer the row it read, so "previous production" may now mean a different
        # version. Hand it back to the caller to re-read.
        raise HTTPException(
            status_code=409,
            detail=(
                "This runtime's version slots changed while the rollback was in flight, so nothing "
                "was changed. Re-read the slots and retry."
            ),
        ) from None

    logger.info(
        "Rolled back %s production: %s -> %s (caller=%s)",
        runtime_name,
        rolled_from,
        target_version,
        caller_sub,
    )
    return PromoteResponse(
        success=True,
        runtime_name=runtime_name,
        promoted_version_id=target_version,
        slot="production",
        previous_version_id=rolled_from,
        message=f"Rolled back production to version {target_version}",
    )
