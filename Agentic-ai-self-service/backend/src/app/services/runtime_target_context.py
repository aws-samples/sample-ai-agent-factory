"""Resolve the AWS target behind an owner-checked runtime version.

Runtime slots and version rows are control-plane records in the platform
account.  The runtime-specific resources they point at may live in another
region or account.  A friendly runtime name therefore cannot safely select an
ambient boto3 client: the immutable ``AgentVersion.deployment_id`` must lead
back to the deployment record that froze the target account, region, and role.

This module is shared by evaluation, trace, cost, and dashboard routes so those
surfaces cannot drift independently from the invoke/delete target semantics.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from app.services import step_clients
from app.services.agent_versions_store import (
    get_slots_store,
    get_versions_store,
)
from app.services.auth import assert_owner
from app.services.deployment_state_store import DeploymentStateStore

logger = logging.getLogger(__name__)

_deployment_store: DeploymentStateStore | None = None
_UNAVAILABLE_DETAIL = "Could not verify this resource right now. Try again shortly."
_RUNTIME_ARN_RE = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z0-9-]+)?):bedrock-agentcore:"
    r"(?P<region>[a-z0-9-]+):(?P<account_id>\d{12}):runtime/"
    r"(?P<runtime_id>[A-Za-z0-9_-]+)$"
)


def _home_region() -> str:
    return os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))


def _home_account_id() -> str | None:
    """Return the platform account from a trusted, server-set identifier.

    DeploymentLambda receives ``STATE_MACHINE_ARN`` from CDK.  Unlike a
    deployment row or request field, that ARN cannot be selected by the caller,
    so it can bind historical same-account records whose ``target_account_id``
    is intentionally empty.
    """

    for value in (
        os.environ.get("STATE_MACHINE_ARN", ""),
        os.environ.get("AWS_ACCOUNT_ID", ""),
    ):
        if value.isdigit() and len(value) == 12:
            return value
        parts = value.split(":")
        if len(parts) >= 5 and parts[4].isdigit() and len(parts[4]) == 12:
            return parts[4]
    return None


def get_deployment_store() -> DeploymentStateStore:
    """Return the platform-account deployment store.

    Deployment records always live in the platform region even when the
    resources recorded by them do not.
    """

    global _deployment_store
    if _deployment_store is None:
        _deployment_store = DeploymentStateStore(
            table_name=os.environ.get(
                "DEPLOYMENTS_TABLE_NAME",
                os.environ.get("DEPLOYMENT_TABLE_NAME", "AgentCoreDeployments"),
            ),
            region=_home_region(),
        )
    return _deployment_store


def _field(record: Any, name: str) -> Any:
    if isinstance(record, dict):
        return record.get(name)
    return getattr(record, name, None)


def _status_value(value: Any) -> str:
    """Normalize plain strings and ``str``-backed Enum values."""

    return str(getattr(value, "value", value) or "")


def _unavailable(reason: str, exc: Exception | None = None) -> HTTPException:
    if exc is None:
        logger.error("Runtime target verification failed: %s", reason)
    else:
        logger.warning(
            "Runtime target verification failed: %s (%s)",
            reason,
            type(exc).__name__,
        )
    return HTTPException(status_code=503, detail=_UNAVAILABLE_DETAIL)


@dataclass(frozen=True)
class OwnedRuntimeTarget:
    """The frozen target session and authority rows for one runtime version."""

    runtime_id: str
    version_id: str
    deployment_id: str
    region: str
    account_id: str | None
    role_arn: str | None
    session: Any
    runtime_arn: str = ""
    protocol: str = "HTTP"
    deployment: Any | None = None

    def client(self, service: str, **kwargs: Any):
        """Build a client from the resolved target session, never ambient AWS."""

        kwargs.setdefault("region_name", self.region)
        return self.session.client(service, **kwargs)

    def target_event(self) -> dict[str, str | None]:
        """Return the exact target context used to establish ``session``."""

        return {
            "target_account_id": self.account_id,
            "target_region": self.region,
            "target_role_arn": self.role_arn,
        }

    def deployment_dict(self) -> dict[str, Any]:
        """Normalize the exact deployment authority row for invocation logic."""

        if isinstance(self.deployment, dict):
            return dict(self.deployment)
        model_dump = getattr(self.deployment, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump(mode="json")
            if isinstance(dumped, dict):
                return dumped
        values = getattr(self.deployment, "__dict__", None)
        if isinstance(values, dict):
            return dict(values)
        raise _unavailable("deployment authority could not be normalized")


def _target_from_deployment(
    deployment: Any,
    caller_sub: str,
    *,
    expected_deployment_id: str,
    expected_version_id: str | None = None,
    expected_runtime_id: str | None = None,
    expected_runtime_arn: str | None = None,
    required_protocol: str | None = None,
) -> OwnedRuntimeTarget:
    """Validate one deployment authority row and build its frozen target session."""

    # A corrupt or maliciously redirected control-plane row must not borrow
    # another owner's target role.  Ownership is checked before protocol so a
    # cross-tenant caller cannot use the latter as an existence oracle.
    assert_owner(_field(deployment, "user_id"), caller_sub)

    if _status_value(_field(deployment, "status")) != "succeeded":
        raise _unavailable("deployment authority is not succeeded")
    if _field(deployment, "delete_status"):
        raise _unavailable("deployment authority is being or has been deleted")

    deployment_id = str(_field(deployment, "deployment_id") or "")
    version_id = str(_field(deployment, "version_id") or "")
    runtime_id = str(_field(deployment, "runtime_id") or "")
    runtime_arn = str(_field(deployment, "runtime_arn") or "")

    bindings = {
        "deployment": (deployment_id, expected_deployment_id),
    }
    if expected_version_id is not None:
        bindings["version"] = (version_id, expected_version_id)
    if expected_runtime_id is not None:
        bindings["runtime"] = (runtime_id, expected_runtime_id)
    if expected_runtime_arn is not None:
        bindings["runtime ARN"] = (runtime_arn, expected_runtime_arn)
    for label, (actual, expected) in bindings.items():
        if not actual or actual != expected:
            raise _unavailable(f"{label} binding does not match")

    if not runtime_id or not runtime_arn:
        raise _unavailable("deployment has no runtime authority")
    arn_match = _RUNTIME_ARN_RE.fullmatch(runtime_arn)
    if arn_match is None:
        raise _unavailable("runtime ARN is not canonical")
    if arn_match.group("runtime_id") != runtime_id:
        raise _unavailable("runtime ARN id binding does not match")

    protocol = str(_field(deployment, "runtime_protocol") or "HTTP").upper()
    if protocol not in {"HTTP", "MCP", "A2A"}:
        raise _unavailable("deployment protocol is not recognized")
    if required_protocol is not None and protocol != required_protocol.upper():
        raise HTTPException(
            status_code=409,
            detail=(
                f"This deployment uses the {protocol} runtime protocol. "
                f"The requested operation requires {required_protocol.upper()}."
            ),
        )

    account_id = str(_field(deployment, "target_account_id") or "").strip() or None
    role_arn = str(_field(deployment, "target_role_arn") or "").strip() or None
    if bool(account_id) != bool(role_arn):
        # Never reconstruct or re-resolve a cross-account role from mutable
        # registration state. Delete/invoke use the exact role frozen on the
        # deployment record; every protocol-specific route must do the same.
        raise _unavailable("target account and role binding is incomplete")
    if account_id and account_id != arn_match.group("account_id"):
        raise _unavailable("runtime ARN account binding does not match")
    if not account_id:
        home_account_id = _home_account_id()
        if home_account_id and home_account_id != arn_match.group("account_id"):
            raise _unavailable("runtime ARN home-account binding does not match")

    recorded_region = str(_field(deployment, "target_region") or "").strip()
    arn_region = arn_match.group("region")
    if recorded_region and recorded_region != arn_region:
        raise _unavailable("runtime ARN region binding does not match")
    if not recorded_region and account_id:
        # Cross-account records always freeze the region at admission. Deriving
        # it after the fact would turn a corrupt authority row into a usable
        # cross-account credential path.
        raise _unavailable("cross-account deployment has no frozen region")
    # Historical home-account rows did not persist target_region. Their exact
    # deployment/runtime ARN equality above is still authoritative, so derive
    # from that bound ARN rather than silently substituting the Lambda's ambient
    # home region.
    region = recorded_region or arn_region

    target_event = {
        "target_account_id": account_id,
        "target_region": region,
        "target_role_arn": role_arn,
    }
    try:
        session = step_clients.session_for_event(target_event)
    except Exception as exc:
        raise _unavailable("target session could not be established", exc) from exc

    return OwnedRuntimeTarget(
        runtime_id=runtime_id,
        version_id=version_id,
        deployment_id=deployment_id,
        region=region,
        account_id=target_event["target_account_id"],
        role_arn=target_event["target_role_arn"],
        session=session,
        runtime_arn=runtime_arn,
        protocol=protocol,
        deployment=deployment,
    )


def resolve_owned_deployment_runtime_target(
    deployment_id: str,
    caller_sub: str,
    *,
    required_protocol: str | None = None,
) -> OwnedRuntimeTarget:
    """Resolve one owner-checked deployment to its exact recorded runtime.

    Protocol-specific product surfaces use a deployment id rather than
    accepting a caller-supplied ARN, account, region, role, or protocol.  The
    deployment row is read consistently because it authorizes both the target
    session and whether invocation is still allowed.
    """

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", deployment_id or ""):
        raise HTTPException(status_code=400, detail="Invalid deployment id")
    try:
        deployment = get_deployment_store().get(deployment_id, consistent=True)
    except Exception as exc:
        raise _unavailable("deployment lookup failed", exc) from exc
    if deployment is None:
        raise HTTPException(status_code=404, detail="Not found")

    return _target_from_deployment(
        deployment,
        caller_sub,
        expected_deployment_id=deployment_id,
        required_protocol=required_protocol,
    )


def resolve_owned_runtime_slot_target(
    runtime_name: str,
    slot: str,
    caller_sub: str,
    *,
    required_protocol: str | None = None,
) -> OwnedRuntimeTarget:
    """Resolve one owner-checked slot to its exact immutable AWS target.

    The slot and version establish tenant ownership.  The version's immutable
    deployment id then selects the deployment record that froze target
    credentials.  The three records must agree on owner, version, runtime, and
    deployment identity before any target client is created.
    """

    if slot not in {"production", "staging"}:
        raise HTTPException(status_code=400, detail="Invalid runtime slot")

    try:
        # Every row in this chain authorizes a target session. Eventual reads
        # can briefly select the version that occupied the slot before a promote,
        # or continue authorizing a deployment after deletion started.
        slots = get_slots_store().get(runtime_name, consistent=True)
        if slots is None:
            raise HTTPException(status_code=404, detail="Not found")
        assert_owner(slots.owner_sub, caller_sub)

        version_id = slots.production_version_id if slot == "production" else slots.staging_version_id
        if not version_id:
            raise HTTPException(status_code=404, detail="Not found")
        version = get_versions_store().get(
            runtime_name,
            version_id,
            consistent=True,
        )
        if version is None:
            raise _unavailable(f"{slot} slot points to a missing version")
        assert_owner(version.owner_sub, caller_sub)
    except HTTPException:
        raise
    except Exception as exc:  # a failed authority read must not select ambient AWS
        raise _unavailable("slot or version lookup failed", exc) from exc

    if str(slots.runtime_name or "") != runtime_name:
        raise _unavailable("slot runtime binding does not match")
    if str(version.runtime_name or "") != runtime_name:
        raise _unavailable("version runtime binding does not match")
    if str(version.version_id or "") != str(version_id):
        raise _unavailable(f"{slot} slot version binding does not match")
    if _status_value(version.status) != "succeeded":
        raise _unavailable(f"{slot} version is not succeeded")
    if not version.deployment_id:
        raise _unavailable("version has no deployment authority")
    if not version.runtime_id or not version.runtime_arn:
        raise _unavailable("version has no runtime authority")

    try:
        deployment = get_deployment_store().get(
            version.deployment_id,
            consistent=True,
        )
    except Exception as exc:
        raise _unavailable("deployment lookup failed", exc) from exc
    if deployment is None:
        raise _unavailable("deployment authority is missing")

    return _target_from_deployment(
        deployment,
        caller_sub,
        expected_deployment_id=str(version.deployment_id),
        expected_version_id=str(version.version_id),
        expected_runtime_id=str(version.runtime_id),
        expected_runtime_arn=str(version.runtime_arn or ""),
        required_protocol=required_protocol,
    )


def resolve_owned_runtime_target(runtime_name: str, caller_sub: str) -> OwnedRuntimeTarget:
    """Resolve the owner-checked production slot for existing read surfaces."""

    return resolve_owned_runtime_slot_target(runtime_name, "production", caller_sub)
