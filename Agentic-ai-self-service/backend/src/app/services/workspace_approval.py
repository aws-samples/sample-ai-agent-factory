"""Risk-tier routing after admission (plan §3.B, implementation step 3, second half). Pure.

``workspace_admission.admit`` decides whether an agent may be deployed and which approver groups
its risk tier routes to. This module turns that decision into the deployment's route:

* **low** (no approver groups): nothing changes; the deployment lands where the caller asked.
* **medium / high** (one or more approver groups): the deployment can only land in the ``staging``
  slot, and the version cannot be promoted to ``production`` until every named group has approved
  it. Approval is recorded per group on the version row (``admission_approvals``) through
  ``POST /api/runtimes/{name}/versions/{id}/approve``; the version's owner can never approve their
  own version, whatever groups they hold.

This reuses the platform's one existing deployment-time human gate, staging -> production
promotion, instead of inventing a parallel queue: the thing an approver releases is exactly the
production pointer. The decision is read from the version ROW, not from the feature flag at promote
time, so turning the flag off later does not release a version that was admitted with an approval
requirement; versions deployed while the flag was off carry no admission and promote as before.

The helpers are pure functions over the two persisted documents (``admission`` as
``AdmissionDecision.model_dump(by_alias=True)``; ``admission_approvals`` as a list of
``{group, approverSub, approvedAt}``) so the route, the store condition and the tests share one
definition of "pending".
"""

from __future__ import annotations

import re
from typing import Any

from app.schemas.workspace_policy import GROUP_NAME_PATTERN

_GROUP_RE = re.compile(GROUP_NAME_PATTERN)

#: Version-row field names, shared by the store, the routes and the response model.
ADMISSION_FIELD = "admission"
APPROVALS_FIELD = "admission_approvals"

#: Document versions this build can read. A record carrying any other value was written by code this
#: build does not know, so its meaning cannot be relied on: it blocks rather than releases.
SUPPORTED_ADMISSION_VERSIONS = frozenset({1})
SUPPORTED_CLASSIFIER_VERSIONS = frozenset({1, 2})


class MalformedAdmissionError(ValueError):
    """A persisted admission or approval document is present but not well-formed.

    Fail closed: a requirement that cannot be read cannot be shown to be satisfied, so the version
    stays blocked and an administrator has to look, rather than a corrupt record releasing it.
    """


def _well_formed_group(value: Any) -> bool:
    return isinstance(value, str) and bool(_GROUP_RE.match(value))


def required_approver_groups(admission: dict[str, Any] | None) -> list[str]:
    """The approver groups an admission decision routes to, in policy order; ``[]`` when none.

    ``None`` (no admission: flag off at deploy time, or a legacy row) imposes nothing. A PRESENT
    admission must be a complete, well-formed ``AdmissionDecision`` whose ``outcome`` is ``admitted``
    (a denied decision is never persisted, so one on a row is corruption) and whose ``approvers`` are
    well-formed group names; anything else raises ``MalformedAdmissionError`` instead of being read as
    "no requirement".
    """
    if admission is None:
        return []
    if not isinstance(admission, dict):
        raise MalformedAdmissionError("admission is not an object")
    try:
        from app.services.workspace_admission import AdmissionDecision

        decision = AdmissionDecision.model_validate(admission)
    except Exception as exc:  # noqa: BLE001 - any validation failure is "cannot be read"
        raise MalformedAdmissionError(f"admission is not a well-formed decision ({type(exc).__name__})") from exc
    if decision.outcome != "admitted":
        raise MalformedAdmissionError(f"admission outcome is {decision.outcome!r}, not 'admitted'")
    # The version markers must be PRESENT (not defaulted) and ones this build knows how to read.
    if "admissionVersion" not in admission or "classifierVersion" not in admission:
        raise MalformedAdmissionError("admission does not name its admission/classifier versions")
    if decision.admission_version not in SUPPORTED_ADMISSION_VERSIONS:
        raise MalformedAdmissionError(f"unsupported admissionVersion {decision.admission_version!r}")
    if decision.classifier_version not in SUPPORTED_CLASSIFIER_VERSIONS:
        raise MalformedAdmissionError(f"unsupported classifierVersion {decision.classifier_version!r}")
    if not all(_well_formed_group(group) for group in decision.approvers):
        raise MalformedAdmissionError("admission.approvers is not a list of group names")
    return list(decision.approvers)


def approved_groups(approvals: list[dict[str, Any]] | None, *, owner_sub: str | None = None) -> set[str]:
    """The groups with a complete approval record (group, approver subject and timestamp). A record
    missing any of the three is malformed and raises, never silently counted or ignored. When the
    version's ``owner_sub`` is given, a record whose approver IS the owner is malformed too: the store
    never writes one, so its presence means the row was tampered with, and it must not count."""
    if approvals is None:
        return set()
    if not isinstance(approvals, list):
        raise MalformedAdmissionError("admission_approvals is not a list")
    groups: set[str] = set()
    for record in approvals:
        if not isinstance(record, dict):
            raise MalformedAdmissionError("approval record is not an object")
        group, approver, at = record.get("group"), record.get("approverSub"), record.get("approvedAt")
        complete = _well_formed_group(group) and isinstance(approver, str) and approver and isinstance(at, str) and at
        if not complete:
            raise MalformedAdmissionError("approval record is incomplete")
        if owner_sub is not None and approver == owner_sub:
            raise MalformedAdmissionError("approval record names the version's owner as approver")
        groups.add(group)
    return groups


def pending_groups(
    admission: dict[str, Any] | None,
    approvals: list[dict[str, Any]] | None,
    *,
    owner_sub: str | None = None,
) -> list[str]:
    """Approver groups that have not approved yet, in policy order."""
    done = approved_groups(approvals, owner_sub=owner_sub)
    return [group for group in required_approver_groups(admission) if group not in done]


def requires_approval(admission: dict[str, Any] | None) -> bool:
    return bool(required_approver_groups(admission))


def admission_tier(admission: dict[str, Any] | None) -> str | None:
    if not isinstance(admission, dict):
        return None
    tier = admission.get("tier")
    return str(tier) if isinstance(tier, str) else None


def route_deployment_slot(requested_slot: str | None, effective_slot: str, admission: dict[str, Any] | None) -> str:
    """The slot an admitted deployment may land in.

    Without an approval requirement the already-computed ``effective_slot`` stands. With one, an
    EXPLICIT request for ``production`` is a ``ProductionRequiresApprovalError`` (the caller asked
    for something this agent cannot have yet) and an unspecified slot resolves to ``staging``.
    """
    if not requires_approval(admission):
        return effective_slot
    if requested_slot == "production":
        raise ProductionRequiresApprovalError(admission)
    return "staging"


class ProductionRequiresApprovalError(ValueError):
    """A production slot was requested for a version whose tier requires approval first."""

    def __init__(self, admission: dict[str, Any] | None) -> None:
        self.tier = admission_tier(admission) or "unknown"
        self.groups = required_approver_groups(admission)
        super().__init__(production_requires_approval_message(self.tier, self.groups))


def production_requires_approval_message(tier: str, groups: list[str]) -> str:
    return (
        f"This agent is classified {tier} risk in its workspace, so it cannot be deployed straight to "
        f"production. Deploy to staging, obtain approval from {', '.join(groups)}, then promote the "
        "version. Nothing was created."
    )


def promotion_blocked_message(version_id: str, tier: str | None, pending: list[str]) -> str:
    return (
        f"Cannot promote version {version_id} to production: workspace admission classified it "
        f"{tier or 'unknown'} risk and approval from {', '.join(pending)} is still pending."
    )


def approval_record(group: str, approver_sub: str, approved_at: str) -> dict[str, str]:
    return {"group": group, "approverSub": approver_sub, "approvedAt": approved_at}
