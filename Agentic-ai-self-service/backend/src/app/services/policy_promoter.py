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

That message has a SECOND cause, and this module is the one that used to be
unable to recover from it. AgentCore resolves the gateway named in a Cedar
statement as the CALLER, so a principal without
``bedrock-agentcore:InvokeGateway`` on the gateway ARN gets the same wording
permanently — no amount of promotion converges it, and the fail-closed engine
stays deny-all. Proven live on the customer-export path (same API, same account,
that action as the only variable). The grant is now on the deployment Lambda role
that runs this module, in ``infra/stacks/platform/lambdas.py``; if this text
turns up again and never clears, check the role before blaming consistency.

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

import boto3

from app.services.aws_errors import is_error
from app.services.aws_pagination import list_all
from app.services.gateway_mutation_lock import engine_is, gateway_mutation_lock
from app.services.gateway_update import preserving_gateway_update
from app.services.policy_lifecycle import (
    PolicyCoResidencyConflict,
    PolicyStateUnreadable,
    delete_policy_confirmed,
    policy_child_row,
    reconcile_policy_definition,
    refuse_incompatible_coresidency,
    refuse_shared_parent_engine,
)

logger = logging.getLogger(__name__)


def _ctrl(region: str):
    return boto3.client("bedrock-agentcore-control", region_name=region)


def _get_policies_from_response(resp: dict) -> list:
    """Extract the policies list from a list_policies response (SDK-key tolerant)."""
    return resp.get("policies", resp.get("items", resp.get("policySummaries", [])))


def _list_all_policies(ctrl, engine_id: str) -> list[dict]:
    """Read every policy page for one engine."""
    return list_all(
        ctrl,
        "list_policies",
        item_keys=("policies", "items", "policySummaries"),
        request={"policyEngineId": engine_id, "maxResults": 100},
    )


def _active_policy_count(ctrl, engine_id: str) -> int:
    try:
        pols = _list_all_policies(ctrl, engine_id)
        return sum(1 for p in pols if p.get("status") == "ACTIVE")
    except Exception:  # noqa: BLE001
        return 0


def _adopted_row_recorded(
    store, deployment_id, engine_id, policy_id, name, region, statement, account=None, children=None
) -> bool:
    """Write the adopted-policy child row durably before an existing policy is mutated.

    Without a store there is nothing to write into: the caller (the status/invoke touchpoint) has to pass one
    (``try_promote_to_enforce(..., store=...)``); until it does, the mutation proceeds and the gap is logged loudly,
    because a reused engine's stale ACTIVE permit is the larger hazard (F-G05-001). With a store, a failed write means
    no mutation.
    """
    if store is None or not deployment_id:
        logger.warning(
            "promote: mutating policy %s on engine %s without a manifest store; the adopted child row is NOT recorded",
            name,
            engine_id,
        )
        return True
    row = policy_child_row(
        policy_id=str(policy_id),
        engine_id=engine_id,
        name=name,
        region=region or "",
        created_by_deployment=False,
        statement=statement or "",
        account=account,
    )
    # Isolation before any write: the engine must not serve another live deployment (a legacy manifest names only
    # the engine), and no other live deployment may desire this policy -- bound to account + region -- differently.
    refuse_shared_parent_engine(store, deployment_id, engine_id, region, account, action="reconcile a policy")
    refuse_incompatible_coresidency(store, deployment_id, row)
    try:
        store.record_resource_strict(deployment_id, row)
    except Exception as exc:  # noqa: BLE001
        logger.error("promote: could not record adopted policy %s (%s); leaving it untouched", name, type(exc).__name__)
        return False
    refuse_incompatible_coresidency(store, deployment_id, row)  # re-check after our row is durable
    if children is not None:
        children.append(row)
    return True


def _ensure_policies_active(
    ctrl,
    engine_id: str,
    policies: list,
    *,
    store=None,
    deployment_id: str | None = None,
    region: str | None = None,
    account: str | None = None,
    children: list[dict] | None = None,
) -> int:
    """Make sure the intended policies exist, hold the DESIRED definition, and are ACTIVE on the engine.

    Recreates any that are missing or CREATE_FAILED (now that the gateway has converged the recreate should
    validate). An ACTIVE policy whose live Cedar differs from the desired statement is reconciled in place
    (F-G05-001) -- ACTIVE alone is not success. Returns the count of ACTIVE policies holding the desired definition.

    Lazy creation happens AFTER deployment finalization, so a created policy is recorded durably
    (``record_resource_strict``) before it counts; when the row cannot be written the policy is compensate-deleted
    and not counted (F-G09-003). Without a store nothing is recorded and nothing new is created.
    """
    # Index existing by name.
    existing = {}
    try:
        for p in _list_all_policies(ctrl, engine_id):
            existing[p.get("name")] = p
    except Exception as e:  # noqa: BLE001
        logger.warning("promote: list_policies failed: %s", str(e)[:120])

    active = 0
    for pol in policies or []:
        name = pol.get("name")
        stmt = pol.get("statement", "")
        cur = existing.get(name)
        # A reconcile-class payload (see try_promote_to_enforce) carries no
        # statement because the policy already exists — recover it in place
        # using its own live definition. A brand-new policy still needs a
        # statement to create.
        if not name or not stmt:
            # No exact statement = no intent: live text is never re-driven as if it were desired.
            continue
        status = (cur or {}).get("status", "")
        _desc = pol.get("description") or "Auto-permit for allowed gateway tools (ENFORCE)."
        if status == "ACTIVE":
            if stmt:
                pid = cur.get("policyId") or cur.get("id")
                if not _adopted_row_recorded(
                    store, deployment_id, engine_id, pid, name, region, stmt, account, children
                ):
                    continue  # no durable row, no mutation
                try:
                    outcome = reconcile_policy_definition(ctrl, engine_id, pid, stmt, _desc)
                except PolicyStateUnreadable as exc:
                    logger.warning("promote: policy %s is ACTIVE but could not be reconciled: %s", name, str(exc)[:160])
                    continue
                if outcome["updated"]:
                    logger.warning(
                        "promote: policy %s reconciled to the desired Cedar (%s)", name, outcome["digest"][:19]
                    )
                if outcome["status"] != "ACTIVE":
                    logger.warning("promote: policy %s is %s after reconciliation", name, outcome["status"])
                    continue
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
        # Reconcile-in-place with no supplied statement: re-drive the policy's
        # OWN live definition (fetched via get_policy) so a regressed
        # UPDATE_FAILED policy re-validates without needing the original Cedar.
        _defn = {"cedar": {"statement": stmt}}
        try:
            if cur:
                _pid = cur.get("policyId") or cur.get("id")
                if not _adopted_row_recorded(
                    store, deployment_id, engine_id, _pid, name, region, stmt or "", account, children
                ):
                    continue  # no durable row, no mutation (the no-statement reconcile class included)
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
                    policyEngineId=engine_id,
                    policyId=pid,
                    description={"optionalValue": _desc},
                    definition=_defn,
                    validationMode="IGNORE_ALL_FINDINGS",
                )
            else:
                # Creation isolation: the engine the promoter re-drives always pre-exists; a NEW permit on it must not
                # expand another live deployment's authorization (account-bound, unreadable table = refusal).
                refuse_shared_parent_engine(
                    store, deployment_id or "", engine_id, region, account, action="create a policy"
                )
                if store is None or not deployment_id:
                    # Post-finalization creation without a manifest to record into would be an untracked leak.
                    logger.warning(
                        "promote: policy %s missing on engine %s but no manifest store given; not creating",
                        name,
                        engine_id,
                    )
                    continue
                cp = ctrl.create_policy(
                    policyEngineId=engine_id,
                    name=name,
                    # create_policy requires a NON-EMPTY description (min length 1).
                    description=_desc,
                    definition=_defn,
                    # IGNORE_ALL_FINDINGS: skip the gateway-calling validation that
                    # fails "Insufficient permissions to call gateway" on fresh
                    # gateways (proven live). Enforcement preserved via default-deny.
                    validationMode="IGNORE_ALL_FINDINGS",
                )
                pid = cp.get("policyId")
                row = policy_child_row(
                    policy_id=pid,
                    engine_id=engine_id,
                    name=name,
                    region=region or "",
                    created_by_deployment=True,
                    statement=stmt,
                    account=account,
                )
                try:
                    store.record_resource_strict(deployment_id, row)
                    if children is not None:
                        children.append(row)
                except Exception as exc:  # noqa: BLE001
                    # F-G09-003: a created policy nobody can find later is a permanent leak. Compensate now.
                    logger.error(
                        "promote: could not record policy %s (%s); compensate-deleting it", name, type(exc).__name__
                    )
                    try:
                        delete_policy_confirmed(ctrl, engine_id, str(pid))
                    except Exception:  # noqa: BLE001
                        logger.exception("promote: compensating delete of policy %s failed", name)
                    continue
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
            # "already exists" fallback kept: the concurrent-create form was seen
            # live as a message, not always a ConflictException code.
            if is_error(e, "ConflictException") or "already exists" in str(e):
                logger.info("promote: %s owned by a concurrent run (%s) — leaving it", name, str(e)[:80])
            else:
                logger.info("promote: recreate of %s not yet valid: %s", name, str(e)[:120])

    return active


def try_promote_to_enforce(
    deployment_state: dict,
    region: str,
    *,
    control_client=None,
    store=None,
) -> dict | None:
    """Promote a pending LOG_ONLY engine to ENFORCE if the gateway has converged.

    Returns a dict describing the outcome, or None when there is nothing to do
    (no pending promotion). Outcome dict: ``{"promoted": bool, "mode": str,
    "reason": str}``. Idempotent + best-effort — never raises.
    """
    pr = (deployment_state or {}).get("policy_result") or {}
    pending = pr.get("enforce_pending")
    already_enforcing = pr.get("mode") == "ENFORCE"

    # RECONCILE class: mode is already ENFORCE and enforce_pending was cleared
    # (policy once reached ACTIVE), but AgentCore's gateway-authz plane can
    # REGRESS a policy back to UPDATE_FAILED afterward. With the pending payload
    # gone, no touchpoint would ever re-drive it and the tool plane silently
    # goes deny-all forever (found live in production-readiness testing: a
    # policy stuck UPDATE_FAILED >2h that flipped ACTIVE instantly on a single
    # update_policy re-drive). If any policy on the engine is not ACTIVE,
    # reconstruct a minimal pending payload from the live engine so the
    # re-drive below runs. When everything is ACTIVE this is a cheap no-op.
    if not pending:
        if not already_enforcing:
            return None
        engine_id = pr.get("engine_id")
        if not engine_id:
            return None
        try:
            _ctrl_r = control_client or _ctrl(region)
            _pols = _list_all_policies(_ctrl_r, engine_id)
            _unhealthy = [p for p in _pols if p.get("status") not in ("ACTIVE", "CREATING", "UPDATING", "DELETING")]
            if not _unhealthy:
                return None  # all healthy — nothing to reconcile
            # Recovery reconciles to the EXACT persisted intent, never to the drifted live text. A legacy record
            # without intent cannot be recovered safely: redeploy instead of revalidating unknown Cedar.
            intent = {
                str(spec.get("name")): spec
                for spec in (pr.get("desired_policies") or pr.get("managed_policies") or [])
                if isinstance(spec, dict) and spec.get("name") and (spec.get("statement") or "").strip()
            }
            if not intent:
                logger.error(
                    "policy reconcile: engine %s has %d non-ACTIVE policy(ies) but the record carries no exact policy "
                    "intent; refusing to revalidate live text — redeploy the agent",
                    engine_id,
                    len(_unhealthy),
                )
                return {
                    "promoted": False,
                    "mode": pr.get("mode"),
                    "reason": "legacy record without exact policy intent (desired_policies); redeploy required",
                }
            missing_intent = [p.get("name") for p in _unhealthy if str(p.get("name")) not in intent]
            if missing_intent:
                return {
                    "promoted": False,
                    "mode": pr.get("mode"),
                    "reason": f"no exact intent for non-ACTIVE policy(ies) {missing_intent}; redeploy required",
                }
            logger.warning(
                "policy reconcile: engine %s has %d non-ACTIVE policy(ies) under ENFORCE — re-driving",
                engine_id,
                len(_unhealthy),
            )
            # Reconstruct just enough pending payload for the re-drive path. The
            # policies already exist (RECOVER-IN-PLACE update_policy uses their
            # live definition), so statements can be empty here.
            pending = {
                "engine_id": engine_id,
                "gateway_id": pr.get("gateway_id") or (pr.get("engine_arn") or ""),
                "policies": [
                    {
                        "name": str(p.get("name")),
                        "statement": intent[str(p.get("name"))].get("statement", ""),
                        "description": intent[str(p.get("name"))].get("description", ""),
                    }
                    for p in _unhealthy
                ],
                "_reconcile": True,
            }
        except Exception:  # noqa: BLE001
            logger.debug("policy reconcile: list_policies failed for %s", pr.get("engine_id"), exc_info=True)
            return None
    elif already_enforcing and not pr.get("enforce_validation_pending"):
        return None

    engine_id = pending.get("engine_id")
    gateway_id = pending.get("gateway_id")
    if not engine_id or not gateway_id:
        return {"promoted": False, "mode": pr.get("mode"), "reason": "missing engine/gateway id"}

    ctrl = control_client or _ctrl(region)
    children: list[dict] = []  # receipts of policies this call created/adopted; the caller persists them
    try:
        # 1. Ensure the intended policies are ACTIVE (recreate if the gateway has
        #    only now converged). If none are ACTIVE, stay LOG_ONLY (never deny-all).
        active = _ensure_policies_active(
            ctrl,
            engine_id,
            pending.get("policies") or [],
            store=store,
            deployment_id=(deployment_state or {}).get("deployment_id"),
            region=region,
            account=(deployment_state or {}).get("target_account_id"),
            children=children,
        )
        if active == 0:
            return {
                "promoted": False,
                "mode": pr.get("mode") or "LOG_ONLY",
                "reason": "policies not ACTIVE yet (gateway still converging)",
                "policy_children": children,  # a created, still-converging policy is already a receipt
            }

        # Fail-closed path (P-PLAT-027): the gateway is ALREADY in ENFORCE; the
        # pending policies just became ACTIVE, which un-bricks the permitted
        # tools. No gateway update needed.
        if already_enforcing:
            logger.info("promote: engine %s policies now ACTIVE under ENFORCE", engine_id)
            return {
                "promoted": True,
                "mode": "ENFORCE",
                "reason": f"{active} ACTIVE policy(ies); fail-closed ENFORCE now serving permitted tools",
                "policy_children": children,
            }

        # 2. Flip the gateway's engine config to ENFORCE, preserving other fields.
        # F-66e: the flip re-sends the authorizer it reads, so it reads, writes and
        # waits for READY under the gateway's write lock.
        with gateway_mutation_lock(ctrl, region, gateway_id) as gw_lock:
            gw = gw_lock.read()
            # Prefer the gateway's OWN attached engine arn (authoritative), then the
            # recorded engine_arn — but only if it's a real arn (guard against a stale
            # placeholder that would fail UpdateGateway validation).
            gw_arn = (gw.get("policyEngineConfiguration") or {}).get("arn")
            rec_arn = pr.get("engine_arn")
            engine_arn = gw_arn or (rec_arn if str(rec_arn).startswith("arn:") else None)
            if not engine_arn:
                return {"promoted": False, "mode": pr.get("mode"), "reason": "no valid engine arn to attach"}
            # A full replace: everything else the gateway holds is re-sent (F-62).
            gw_lock.update(
                preserving_gateway_update(
                    gw,
                    gateway_id,
                    overrides={"policyEngineConfiguration": {"arn": engine_arn, "mode": "ENFORCE"}},
                ),
                engine_is(engine_arn, "ENFORCE"),
            )
        logger.info("promote: flipped gateway %s engine %s to ENFORCE", gateway_id, engine_id)
        return {
            "promoted": True,
            "mode": "ENFORCE",
            "reason": f"{active} ACTIVE policy(ies); gateway converged",
            "policy_children": children,
        }
    except PolicyCoResidencyConflict as e:
        logger.error("promote: refused (co-residency): %s", str(e)[:200])
        return {
            "promoted": False,
            "mode": pr.get("mode"),
            "reason": f"co-residency refusal: {str(e)[:160]}",
            "policy_children": children,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("promote: could not flip to ENFORCE (will retry next call): %s", str(e)[:200])
        return {
            "promoted": False,
            "mode": pr.get("mode"),
            "reason": f"transient: {str(e)[:120]}",
            "policy_children": children,
        }
