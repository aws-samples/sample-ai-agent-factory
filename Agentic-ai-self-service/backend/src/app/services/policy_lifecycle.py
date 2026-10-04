"""First-class lifecycle for the Cedar policies this platform manages on an AgentCore policy engine.

F-G05-001 / F-G09-003. Two findings, one root cause: a policy was only ever *named*, never *reconciled* or
*recorded*. A reused engine therefore kept a stale ACTIVE policy (a tool the canvas had just forbidden stayed
permitted), and a policy created for a deployment that reused an engine survived that deployment's delete.

This module gives a policy the same three properties every other managed child has:

* an exact desired definition -- ``cedar_digest`` over the whitespace-canonical Cedar text -- compared against the
  LIVE definition before any success is reported, and updated in place on the stable policy id when they differ;
* a manifest row (``policy_child_row``) with enough identity for a deterministic teardown -- engine id, policy id,
  region/account, name, provenance and the desired digest -- written BEFORE the mutation it describes;
* a confirmed deleter (``delete_policy_confirmed``) that proves absence, and a co-residency refusal that protects a
  policy another live deployment desires differently.

Write mechanics follow the provider's tested ones (cfn_provider/handler.py::_handle_policy_create_update):
``update_policy`` on the stable id, ``description={"optionalValue": ...}``, ``IGNORE_ALL_FINDINGS``, then a poll to
a terminal status. Anything unreadable or non-terminal fails closed: a policy whose definition is unknown is never
reused as if it were the desired one.
"""

from __future__ import annotations

import hashlib
import logging
import time

from app.services.deletion_confirmation import wait_until_absent
from app.services.resource_ownership import resource_is_missing

logger = logging.getLogger(__name__)

#: Statuses AgentCore reports once a create/update has finished (for better or worse).
TERMINAL_STATUSES = ("ACTIVE", "CREATE_FAILED", "FAILED", "UPDATE_FAILED")


class PolicyStateUnreadable(RuntimeError):
    """The live policy could not be read to a terminal state; reuse would be a guess."""


class PolicyCoResidencyConflict(RuntimeError):
    """Another live deployment desires this policy with a different definition."""


class PolicySetDrift(RuntimeError):
    """A platform-managed policy the desired set does not contain is ACTIVE on the engine and cannot be removed."""


def canonical_cedar(statement) -> str:
    """Cedar text with every whitespace run collapsed: the comparison unit for 'same definition'."""
    return " ".join(str(statement or "").split())


def cedar_digest(statement) -> str:
    return "sha256:" + hashlib.sha256(canonical_cedar(statement).encode()).hexdigest()


def live_statement(policy: dict | None) -> str:
    return str((((policy or {}).get("definition") or {}).get("cedar") or {}).get("statement") or "")


def policy_child_row(
    *,
    policy_id: str,
    engine_id: str,
    name: str,
    region: str,
    created_by_deployment: bool,
    statement: str,
    gateway_id: str | None = None,
    account: str | None = None,
) -> dict:
    """The manifest row for one managed policy. Canonical identity = engine + policy + account + region."""
    row = {
        "type": "policy",
        "id": policy_id,
        "policy_id": policy_id,  # alias: the ledger's exact engine + policy identity
        "engine_id": engine_id,
        "policy_engine_id": engine_id,
        "name": name,
        "region": region,
        "created_by_deployment": bool(created_by_deployment),
        "desired_definition_sha256": cedar_digest(statement),
    }
    if gateway_id:
        row["gateway_id"] = gateway_id
    if account:
        row["account"] = account
    return row


def read_policy_terminal(
    ctrl, engine_id: str, policy_id: str, *, attempts: int = 20, delay_seconds: float = 3.0
) -> dict:
    """The policy at a TERMINAL status, or PolicyStateUnreadable (read failure or still in flight)."""
    last = ""
    for attempt in range(max(1, attempts)):
        try:
            policy = ctrl.get_policy(policyEngineId=engine_id, policyId=policy_id) or {}
        except Exception as exc:  # noqa: BLE001
            raise PolicyStateUnreadable(
                f"policy {engine_id}/{policy_id} could not be read ({type(exc).__name__}); "
                "refusing to reuse a policy whose definition is unknown"
            ) from exc
        status = str(policy.get("status") or "").upper()
        if status in TERMINAL_STATUSES:
            return policy
        last = status
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    raise PolicyStateUnreadable(
        f"policy {engine_id}/{policy_id} is still {last or 'unknown'} after {attempts} reads; "
        "refusing to reuse a policy that has not reached a terminal state"
    )


def reconcile_policy_definition(
    ctrl,
    engine_id: str,
    policy_id: str,
    desired_statement: str,
    description: str,
    *,
    attempts: int = 20,
    delay_seconds: float = 3.0,
) -> dict:
    """Make the LIVE definition of an existing policy the desired one, in place, and prove it landed.

    Returns ``{"updated": bool, "status": <terminal status>, "digest": <desired digest>}``. Fails closed when the live
    state is unreadable/non-terminal or when the update did not land the desired text.
    """
    live = read_policy_terminal(ctrl, engine_id, policy_id, attempts=attempts, delay_seconds=delay_seconds)
    digest = cedar_digest(desired_statement)
    if canonical_cedar(live_statement(live)) == canonical_cedar(desired_statement):
        return {"updated": False, "status": str(live.get("status") or ""), "digest": digest}
    ctrl.update_policy(
        policyEngineId=engine_id,
        policyId=policy_id,
        description={"optionalValue": description or "Reconciled to the desired policy"},
        definition={"cedar": {"statement": desired_statement}},
        validationMode="IGNORE_ALL_FINDINGS",
    )
    after = read_policy_terminal(ctrl, engine_id, policy_id, attempts=attempts, delay_seconds=delay_seconds)
    if canonical_cedar(live_statement(after)) != canonical_cedar(desired_statement):
        raise PolicyStateUnreadable(
            f"policy {engine_id}/{policy_id}: the update was accepted but the live definition is not the desired one"
        )
    return {"updated": True, "status": str(after.get("status") or ""), "digest": digest}


def other_deployments_desired_digests(store, deployment_id: str, row: dict) -> set[str]:
    """Desired digests other LIVE deployments recorded for the same policy identity (empty when none / unknown)."""
    rows = _rows_or_none(store, deployment_id, row)
    if rows is None:  # a store without the accessor's contract cannot name a conflict
        return set()
    return {str(r.get("desired_definition_sha256")) for r in rows if r.get("desired_definition_sha256")}


def refuse_incompatible_coresidency(store, deployment_id: str, row: dict) -> None:
    """A policy another live deployment desires with a DIFFERENT definition is never overwritten."""
    mine = row.get("desired_definition_sha256")
    foreign = sorted(d for d in other_deployments_desired_digests(store, deployment_id, row) if d != mine)
    if foreign:
        raise PolicyCoResidencyConflict(
            f"policy {row.get('id')} on engine {row.get('engine_id')} is desired by another live deployment with a "
            f"different definition ({', '.join(d[:19] for d in foreign)}); refusing to overwrite a co-resident "
            "deployment's policy -- give this agent a dedicated gateway/engine"
        )


def engine_reference_row(engine_id: str, region: str | None, account: str | None) -> dict:
    row = {"type": "policy_engine", "id": engine_id}
    if region:
        row["region"] = region
    if account:
        row["account"] = account
    return row


def refuse_shared_parent_engine(
    store, deployment_id: str, engine_id: str, region: str | None, account: str | None, *, action: str
) -> None:
    """A policy on an engine ANOTHER live deployment references is never mutated: a pre-fix manifest names only the
    engine, and that deployment is authorized by whatever the engine's policies say today. The lookup is bound to the
    target account and region (ids are not globally unique). An unreadable table fails closed."""
    try:
        shared = store.has_other_live_resource_reference(
            deployment_id,
            engine_reference_row(engine_id, region, account),
            target_account_id=account,
            target_region=region,
        )
    except Exception as exc:  # noqa: BLE001
        raise PolicyCoResidencyConflict(
            f"cannot {action} on engine {engine_id}: the deployment table could not prove that no other live "
            f"deployment references the engine ({type(exc).__name__})"
        ) from exc
    if shared is True:
        raise PolicyCoResidencyConflict(
            f"cannot {action} on engine {engine_id}: another live deployment still references this engine and is "
            "authorized by its current policies -- give this agent a dedicated gateway/engine"
        )


def is_managed_policy_name(name: str, engine_name: str) -> bool:
    """Whether ``name`` is in this engine's platform namespace: ``<prefix of engine_name>_<base>`` (policy_step's
    Bug-137 naming), for any non-empty prefix of the engine name."""
    if not name or not engine_name:
        return False
    return any(name.startswith(engine_name[:k] + "_") for k in range(1, len(engine_name) + 1))


def _rows_or_none(store, deployment_id: str, row: dict, **kw) -> list[dict] | None:
    """Rows from the store's accessor, or None when the store cannot enumerate them (a caller must then fail closed)."""
    accessor = getattr(store, "resource_rows", None)
    if not callable(accessor):
        return None
    rows = accessor(deployment_id, row, **kw)
    return rows if isinstance(rows, list) else None


def reconcile_managed_policy_set(
    ctrl,
    store,
    deployment_id: str,
    *,
    engine_id: str,
    engine_name: str,
    desired_names: set[str],
    region: str,
    list_policies,
    account: str | None = None,
) -> dict:
    """On a REUSED engine, no platform-managed policy outside the desired set may stay ACTIVE.

    For every live policy the desired set does not name:

    * outside the engine's namespace -> never touched, reported as foreign;
    * inside the namespace -> removed (confirmed) ONLY when its platform provenance is proven by a manifest row of
      some deployment (live or tombstoned -- the name pattern alone proves nothing: ``P_customer`` matches the prefix
      rule for ``PolicyEngine``), no OTHER live deployment records it, and no other live deployment references the
      parent engine (a pre-fix manifest names only the engine, and that deployment may still be served by the extra);
    * otherwise the deploy fails closed BEFORE the gateway is attached: ENFORCE success is never reported with an
      unexpected ACTIVE managed policy.
    """
    live = list_policies()
    removed: list[str] = []
    foreign: list[str] = []
    engine_row = engine_reference_row(engine_id, region, account)
    for pol in live:
        name = str(pol.get("name") or "")
        pid = pol.get("policyId") or pol.get("id")
        if not name or not pid or name in desired_names:
            continue
        if not is_managed_policy_name(name, engine_name):
            foreign.append(name)
            continue
        row = {"type": "policy", "id": pid, "engine_id": engine_id, "policy_engine_id": engine_id, "region": region}
        if account:
            row["account"] = account
        why = None
        try:
            engine_shared = store.has_other_live_resource_reference(
                deployment_id, engine_row, target_account_id=account, target_region=region
            )
        except Exception as exc:  # noqa: BLE001
            why = f"the deployment table could not be read ({type(exc).__name__})"
            engine_shared = None
        if engine_shared is True:
            why = f"another live deployment still references engine {engine_id} and may be served by it"
        elif why is None:
            provenance = _rows_or_none(store, deployment_id, row, include_self=True, include_deleted=True)
            if provenance is None:
                why = "its platform provenance cannot be enumerated"
            elif not provenance:
                why = "no deployment ever recorded it (the name pattern alone is not ownership)"
            elif not any(r.get("created_by_deployment") is True for r in provenance):
                why = "only adopted (created_by_deployment=false) rows record it; adoption is not creation authority"
            elif any(
                r.get("_deployment_id") != deployment_id and r.get("_delete_status") not in ("deleted", "deleting")
                for r in provenance
            ):
                why = "another live deployment records it"
        if why:
            raise PolicySetDrift(
                f"managed policy {name} ({pid}) is ACTIVE on reused engine {engine_id} but is not in this "
                f"deployment's desired set, and cannot be removed safely: {why}. Refusing to report ENFORCE with "
                "stale authorization -- give this agent a dedicated gateway/engine or remove the policy deliberately"
            )
        logger.warning("Removing stale managed policy %s (%s) from reused engine %s", name, pid, engine_id)
        delete_policy_confirmed(ctrl, engine_id, str(pid))
        removed.append(name)
    return {"removed": removed, "foreign": foreign}


def delete_policy_confirmed(
    ctrl,
    engine_id: str,
    policy_id: str,
    *,
    deadline_monotonic: float | None = None,
    confirmation_attempts: int = 30,
    delay_seconds: float = 2.0,
) -> None:
    """Delete one policy by exact engine + policy id and prove it is absent (not-found on delete = already gone)."""
    try:
        ctrl.delete_policy(policyEngineId=engine_id, policyId=policy_id)
    except Exception as exc:  # noqa: BLE001
        if resource_is_missing(exc):
            return
        raise
    wait_until_absent(
        resource_label=f"policy {engine_id}/{policy_id}",
        read=lambda: ctrl.get_policy(policyEngineId=engine_id, policyId=policy_id),
        max_attempts=confirmation_attempts,
        delay_seconds=delay_seconds,
        deadline_monotonic=deadline_monotonic,
    )
