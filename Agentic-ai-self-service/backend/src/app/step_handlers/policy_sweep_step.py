"""Scheduled Cedar-ENFORCE promotion sweep (EventBridge-driven).

Loom-study Phase-0 item 0.6. A Cedar ENFORCE gateway attaches FAIL-CLOSED with its
permit policy pending, because the gateway's authorization plane takes 20-59+ min
(AWS-side) to converge before the permit can validate. The lazy promoter
(`deployment_handler._maybe_promote_policy`) correctly re-attempts on every USER
touchpoint (invoke / status GET) — but if a deployed ENFORCE agent sits IDLE with
no touchpoints after the gateway converges, its permit never flips ACTIVE and the
tool plane stays deny-all indefinitely (observed live in P-PLAT-027: 66 min with
no converging touchpoint).

This handler is the missing SELF-DRIVE: an EventBridge schedule invokes it every
few minutes; it scans for deployments with a pending ENFORCE promotion and drives
`_maybe_promote_policy` for each — no user interaction required. Idempotent and
best-effort: a deployment already promoted has `enforce_pending` cleared and is
skipped by the scan filter; failures are logged and retried next tick.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def handler(event: dict, context: object = None) -> dict:  # noqa: ARG001
    """EventBridge entrypoint: promote all deployments with a pending ENFORCE.

    Returns a small summary dict for CloudWatch (swept/promoted/failed counts).
    """
    region = os.environ.get("APP_AWS_REGION") or os.environ.get("AWS_REGION", "us-east-1")

    # Import here (not at module top) so the module is import-safe without AWS.
    from app.deployment_handler import _get_state_store, _maybe_promote_policy

    store = _get_state_store()
    try:
        pending = store.scan_pending_enforce()
    except Exception:  # noqa: BLE001
        logger.exception("policy-sweep: scan_pending_enforce failed")
        return {"swept": 0, "promoted": 0, "failed": 0, "error": "scan_failed"}

    swept = len(pending)
    promoted = 0
    failed = 0
    for dep in pending:
        # dep is a DeploymentState model; _maybe_promote_policy expects a dict and
        # mutates+persists policy_result in place (same contract as the touchpoints).
        state = dep.model_dump(mode="json") if hasattr(dep, "model_dump") else dict(dep)
        try:
            if _maybe_promote_policy(state, region):
                promoted += 1
        except Exception:  # noqa: BLE001
            failed += 1
            logger.warning(
                "policy-sweep: promote failed for %s (retry next tick)",
                state.get("deployment_id"),
            )

    logger.info("policy-sweep: swept=%d promoted=%d failed=%d", swept, promoted, failed)
    return {"swept": swept, "promoted": promoted, "failed": failed}
