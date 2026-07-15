"""Lazy promotion of a Cedar policy engine from LOG_ONLY to ENFORCE (Bug 178).

Why this exists
---------------
When a gateway+policy(ENFORCE) flow deploys, AgentCore's gateway-side policy
authorization plane is NOT immediately consistent: for ~3-5 minutes after the
gateway's tools sync, ``create_policy`` against the (otherwise ACTIVE) engine
ends ``CREATE_FAILED: Insufficient permissions to call gateway`` — the IDENTICAL
statement validates ACTIVE once the gateway settles (proven live). This matches
the AWS policy workshop, where policy attachment is a SEPARATE lifecycle step
from gateway creation, not a single-shot deploy.

Blocking the deploy pipeline for 5 minutes per policy flow is poor UX, so the
policy step attaches the engine in LOG_ONLY immediately (tools work, policies are
still evaluated + logged) and records an ``enforce_pending`` payload on the
deployment record. This module PROMOTES the engine to ENFORCE the first time the
agent is used (test/invoke or status poll), minutes later, when the gateway has
converged.

``try_promote_to_enforce`` is:
  - IDEMPOTENT: if already ENFORCE (or nothing pending) it no-ops.
  - SAFE: it only flips to ENFORCE once at least one intended policy reaches
    ACTIVE on the engine (never ships an empty deny-all engine).
  - BEST-EFFORT: any failure leaves the engine in LOG_ONLY (tools keep working)
    and returns a status so the caller can clear/keep the pending flag.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import boto3

logger = logging.getLogger(__name__)


def _ctrl(region: str):
    return boto3.client("bedrock-agentcore-control", region_name=region)


def _active_policy_count(ctrl, engine_id: str) -> int:
    try:
        lp = ctrl.list_policies(policyEngineId=engine_id, maxResults=100)
        pols = lp.get("policies", lp.get("items", []))
        return sum(1 for p in pols if p.get("status") == "ACTIVE")
    except Exception:  # noqa: BLE001
        return 0


def _ensure_policies_active(ctrl, engine_id: str, policies: list) -> int:
    """Make sure the intended policies exist + are ACTIVE on the engine.

    Recreates any that are missing or CREATE_FAILED (now that the gateway has
    converged the recreate should validate). Returns the count of ACTIVE policies.
    """
    # Index existing by name.
    existing = {}
    try:
        lp = ctrl.list_policies(policyEngineId=engine_id, maxResults=100)
        for p in lp.get("policies", lp.get("items", [])):
            existing[p.get("name")] = p
    except Exception as e:  # noqa: BLE001
        logger.warning("promote: list_policies failed: %s", str(e)[:120])

    active = 0
    for pol in policies or []:
        name = pol.get("name")
        stmt = pol.get("statement", "")
        if not name or not stmt:
            continue
        cur = existing.get(name)
        status = (cur or {}).get("status", "")
        if status == "ACTIVE":
            active += 1
            continue
        # RACE GUARD (root cause of the never-converging permit): this promoter
        # runs on EVERY status poll, and the poller (UI / test harness) hits the
        # status endpoint every ~20s. Lambda scales out, so 2+ promoter runs
        # overlap. The old code unconditionally did delete->wait->create on the
        # SAME account-global name, so overlapping runs clobbered each other:
        # run A creates a fresh policy, run B (seeing it mid-create) deletes it
        # and starts its own, forever — the exact CREATING/CREATE_FAILED/DELETING
        # churn observed live for 40+ min on a gateway that a SINGLE un-raced
        # create converges to ACTIVE instantly. Fix: never touch an in-flight
        # (CREATING/DELETING) policy — another concurrent run owns it; just report
        # not-yet-active and let it finish. Only recreate a genuinely terminal
        # CREATE_FAILED/FAILED one.
        if status in ("CREATING", "DELETING", "UPDATING"):
            logger.info("promote: policy %s is %s (another run owns it) — skipping", name, status)
            continue
        _desc = pol.get("description") or "Auto-permit for allowed gateway tools (ENFORCE)."
        _defn = {"cedar": {"statement": stmt}}
        try:
            if cur:
                # RECOVER IN PLACE (the elegant race-free fix): a CREATE_FAILED
                # policy already occupies this account-global name. The old code
                # deleted it and recreated — but delete_policy is ASYNC, opening a
                # name-free window in which a CONCURRENT status-poll promoter run
                # (Lambda scales out; clients poll ~every 20s) creates its own,
                # then the two clobber each other forever (observed live: 40+ min
                # of CREATING/CREATE_FAILED/DELETING churn on a gateway that a
                # single un-raced call converges instantly). update_policy mutates
                # the SAME stable policyId with no deletion — no name-free window,
                # so overlapping runs are idempotent (both update the same id).
                # This re-validates against the now-converged gateway → ACTIVE.
                pid = cur.get("policyId") or cur.get("id")
                # NOTE: update_policy's `description` is a STRUCTURE
                # {"optionalValue": str} — NOT a bare string like create_policy's.
                # Passing a str raises ParamValidationError (caught below), which
                # silently left the policy CREATE_FAILED forever. Proven live: with
                # the correct shape the stuck policy flips ACTIVE on the first poll.
                ctrl.update_policy(
                    policyEngineId=engine_id, policyId=pid,
                    description={"optionalValue": _desc}, definition=_defn,
                    validationMode="IGNORE_ALL_FINDINGS",
                )
            else:
                cp = ctrl.create_policy(
                    policyEngineId=engine_id, name=name,
                    # create_policy requires a NON-EMPTY description (min length 1).
                    description=_desc, definition=_defn,
                    # IGNORE_ALL_FINDINGS: skip the gateway-calling validation that
                    # fails "Insufficient permissions to call gateway" on fresh
                    # gateways (proven live). Enforcement preserved via default-deny.
                    validationMode="IGNORE_ALL_FINDINGS",
                )
                pid = cp.get("policyId")
            # Poll THIS policy's own status to terminal — do NOT rely on a fresh
            # list_policies(), which is eventually-consistent and returns 0 right
            # after a create (the bug that made promotion always report "not
            # active yet"). Count ACTIVE from the per-policy get_policy result.
            final = "CREATING"
            for _ in range(10):
                d = ctrl.get_policy(policyEngineId=engine_id, policyId=pid)
                final = d.get("status", "")
                if final in ("ACTIVE", "CREATE_FAILED", "FAILED", "UPDATE_FAILED"):
                    break
                time.sleep(3)
            if final == "ACTIVE":
                active += 1
            else:
                logger.info("promote: policy %s still %s (gateway converging)", name, final)
        except Exception as e:  # noqa: BLE001
            # ConflictException is BENIGN and self-healing — a concurrent promoter
            # run (Lambda scale-out on overlapping status polls) is already acting
            # on this policy. Two forms seen live: "already exists" (concurrent
            # create) and "Concurrent modification ... / cannot be updated while it
            # is in UPDATING status" (concurrent update). In BOTH cases another run
            # owns the transition — do NOT delete or retry destructively here; the
            # next poll sees it CREATING/UPDATING/ACTIVE and converges.
            if "ConflictException" in type(e).__name__ or "already exists" in str(e):
                logger.info("promote: %s owned by a concurrent run (%s) — leaving it",
                            name, str(e)[:80])
            else:
                logger.info("promote: recreate of %s not yet valid: %s", name, str(e)[:120])

    return active


def try_promote_to_enforce(deployment_state: dict, region: str) -> Optional[dict]:
    """Promote a pending LOG_ONLY engine to ENFORCE if the gateway has converged.

    Returns a dict describing the outcome, or None when there is nothing to do
    (no pending promotion). Outcome dict: ``{"promoted": bool, "mode": str,
    "reason": str}``. Idempotent + best-effort — never raises.
    """
    pr = (deployment_state or {}).get("policy_result") or {}
    pending = pr.get("enforce_pending")
    # Nothing pending → no-op. Note: mode == ENFORCE alone is NOT a no-op
    # anymore — the fail-closed path (P-PLAT-027) attaches in ENFORCE with the
    # permit policies still pending (default-deny bricks the tool plane), and
    # this promoter is what creates them to restore the permitted tools.
    if not pending:
        return None
    already_enforcing = pr.get("mode") == "ENFORCE"
    if already_enforcing and not pr.get("enforce_validation_pending"):
        return None

    engine_id = pending.get("engine_id")
    gateway_id = pending.get("gateway_id")
    if not engine_id or not gateway_id:
        return {"promoted": False, "mode": pr.get("mode"), "reason": "missing engine/gateway id"}

    ctrl = _ctrl(region)
    try:
        # 1. Ensure the intended policies are ACTIVE (recreate if the gateway has
        #    only now converged). If none are ACTIVE, stay LOG_ONLY (never deny-all).
        active = _ensure_policies_active(ctrl, engine_id, pending.get("policies") or [])
        if active == 0:
            return {"promoted": False, "mode": pr.get("mode") or "LOG_ONLY",
                    "reason": "policies not ACTIVE yet (gateway still converging)"}

        # Fail-closed path (P-PLAT-027): the gateway is ALREADY in ENFORCE; the
        # pending policies just became ACTIVE, which un-bricks the permitted
        # tools. No gateway update needed.
        if already_enforcing:
            logger.info("promote: engine %s policies now ACTIVE under ENFORCE", engine_id)
            return {"promoted": True, "mode": "ENFORCE",
                    "reason": f"{active} ACTIVE policy(ies); fail-closed ENFORCE now serving permitted tools"}

        # 2. Flip the gateway's engine config to ENFORCE, preserving other fields.
        gw = ctrl.get_gateway(gatewayIdentifier=gateway_id)
        # Prefer the gateway's OWN attached engine arn (authoritative), then the
        # recorded engine_arn — but only if it's a real arn (guard against a stale
        # placeholder that would fail UpdateGateway validation).
        gw_arn = (gw.get("policyEngineConfiguration") or {}).get("arn")
        rec_arn = pr.get("engine_arn")
        engine_arn = gw_arn or (rec_arn if str(rec_arn).startswith("arn:") else None)
        if not engine_arn:
            return {"promoted": False, "mode": pr.get("mode"),
                    "reason": "no valid engine arn to attach"}
        update = {
            "gatewayIdentifier": gateway_id,
            "name": gw.get("name", ""),
            "roleArn": gw.get("roleArn", ""),
            "protocolType": gw.get("protocolType", "MCP"),
            "policyEngineConfiguration": {"arn": engine_arn, "mode": "ENFORCE"},
        }
        for opt in ("description", "authorizerType", "authorizerConfiguration",
                    "protocolConfiguration", "kmsKeyArn"):
            if gw.get(opt):
                update[opt] = gw[opt]
        ctrl.update_gateway(**update)
        logger.info("promote: flipped gateway %s engine %s to ENFORCE", gateway_id, engine_id)
        return {"promoted": True, "mode": "ENFORCE",
                "reason": f"{active} ACTIVE policy(ies); gateway converged"}
    except Exception as e:  # noqa: BLE001
        logger.warning("promote: could not flip to ENFORCE (will retry next call): %s", str(e)[:200])
        return {"promoted": False, "mode": pr.get("mode"),
                "reason": f"transient: {str(e)[:120]}"}
