"""Multi-region / multi-account deployment targets (Phase 7 — opt-in).

DISABLED BY DEFAULT. Unless an admin explicitly enables deployment targets
(a Settings row) AND registers a target, every deploy goes to the platform's
home account + region exactly as before — zero behavior change.

Two dimensions:
  * **region** — deploy to a different AWS region (already plumbed through
    services/deployment.py; this adds an admin allowlist + validation).
  * **account** — deploy to a DIFFERENT AWS account by assuming a cross-account
    ``deployment role`` that the target account's owner has created to trust the
    platform account. We sts:AssumeRole into it and hand step handlers a scoped
    boto3 Session.

Safety rails:
  * Feature gate: ``targets_enabled`` Settings flag (default false).
  * Region allowlist: only admin-approved regions are accepted.
  * Cross-account: the role must be assumable AND a dry-run GetCallerIdentity
    must confirm we landed in the expected account — else the deploy is refused.
  * Least privilege: the assumed role is the target owner's responsibility; we
    document the required trust policy + verb set (mirrors our step roles).

Config is stored in the tag-policy table (generic Settings store) to avoid a
new table:
  SK ``SETTING#deploy_targets_enabled`` → {"value": "true"|"false"}
  SK ``TARGET#region#<region>``          → {"region": ...}
  SK ``TARGET#account#<account_id>``     → {"account_id", "role_arn", "region"}
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import boto3

logger = logging.getLogger(__name__)

_ENABLED_SK = "SETTING#deploy_targets_enabled"
_REGION_PREFIX = "TARGET#region#"
_ACCOUNT_PREFIX = "TARGET#account#"

# The home region — deploys with no explicit target land here (unchanged path).
HOME_REGION_DEFAULT = "us-east-1"


def _region() -> str:
    return os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", HOME_REGION_DEFAULT))


def _settings_table():
    name = os.environ.get("TAG_POLICY_TABLE_NAME", "TagPolicy")
    return boto3.resource("dynamodb", region_name=_region()).Table(name)


# ---------------------------------------------------------------------------
# Feature gate + config
# ---------------------------------------------------------------------------


def targets_enabled() -> bool:
    """True only when an admin has explicitly enabled deployment targets."""
    if os.environ.get("DEPLOY_TARGETS_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        item = _settings_table().get_item(
            Key={"org_id": "default", "sk": _ENABLED_SK}
        ).get("Item")
        return bool(item and str(item.get("value", "")).lower() == "true")
    except Exception as e:  # noqa: BLE001
        logger.info("targets_enabled check failed (default false): %s", e)
        return False


def set_targets_enabled(enabled: bool) -> None:
    _settings_table().put_item(
        Item={"org_id": "default", "sk": _ENABLED_SK,
              "value": "true" if enabled else "false"}
    )


def add_region(region: str) -> None:
    _settings_table().put_item(
        Item={"org_id": "default", "sk": _REGION_PREFIX + region, "region": region}
    )


def list_regions() -> list[str]:
    from boto3.dynamodb.conditions import Key
    try:
        resp = _settings_table().query(
            KeyConditionExpression=Key("org_id").eq("default")
            & Key("sk").begins_with(_REGION_PREFIX)
        )
        return [i["region"] for i in resp.get("Items", [])]
    except Exception:  # noqa: BLE001
        return []


def add_account(account_id: str, role_arn: str, region: str) -> None:
    _settings_table().put_item(
        Item={"org_id": "default", "sk": _ACCOUNT_PREFIX + account_id,
              "account_id": account_id, "role_arn": role_arn, "region": region}
    )


def get_account(account_id: str) -> Optional[dict]:
    item = _settings_table().get_item(
        Key={"org_id": "default", "sk": _ACCOUNT_PREFIX + account_id}
    ).get("Item")
    return dict(item) if item else None


def list_accounts() -> list[dict]:
    from boto3.dynamodb.conditions import Key
    try:
        resp = _settings_table().query(
            KeyConditionExpression=Key("org_id").eq("default")
            & Key("sk").begins_with(_ACCOUNT_PREFIX)
        )
        return [dict(i) for i in resp.get("Items", [])]
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Target resolution → boto3 Session
# ---------------------------------------------------------------------------


class TargetError(ValueError):
    """Raised when a requested deploy target is invalid / disabled / unreachable."""


def resolve_region(requested: Optional[str]) -> str:
    """Return the region to deploy to, enforcing the allowlist when targeting.

    No requested region → home region (unchanged). A requested region is only
    honored when targets are enabled AND it's on the admin allowlist.
    """
    home = _region()
    if not requested or requested == home:
        return home
    if not targets_enabled():
        raise TargetError("Deployment targets are disabled; cannot target another region")
    if requested not in list_regions():
        raise TargetError(f"Region '{requested}' is not on the deployment allowlist")
    return requested


def session_for_target(
    account_id: Optional[str] = None, region: Optional[str] = None,
    *, require_gate: bool = True, role_arn: Optional[str] = None,
) -> boto3.Session:
    """Return a boto3 Session for the deploy target.

    * No account_id (or the home account) → the DEFAULT session (unchanged path).
    * A registered target account → assume its cross-account deployment role and
      return a scoped session, after a dry-run GetCallerIdentity confirms we
      landed in the expected account.

    ``require_gate`` (default True) re-checks the opt-in feature flag + region
    allowlist. The SFN STEP path passes ``require_gate=False`` because the
    deployment Lambda (handle_deploy) is the single authoritative gate — it
    validated targets_enabled() + the allowlist BEFORE starting the state
    machine, and the step Lambdas don't carry the Settings-table env to re-read
    the flag. ``role_arn`` may be supplied to skip the Settings lookup (used when
    the caller already knows the target's role, e.g. teardown from a manifest).

    Raises TargetError when targeting is disabled (gated path only), the account
    is unregistered, the role can't be assumed, or the landed account mismatches.
    """
    resolved_region = resolve_region(region) if require_gate else (
        region or os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", HOME_REGION_DEFAULT))
    )
    if not account_id:
        return boto3.Session(region_name=resolved_region)

    if require_gate and not targets_enabled():
        raise TargetError("Deployment targets are disabled; cannot target another account")

    if role_arn is None:
        target = get_account(account_id)
        if target is None:
            raise TargetError(f"Account '{account_id}' is not a registered deployment target")
        role_arn = target["role_arn"]
    sts = boto3.client("sts", region_name=resolved_region)
    try:
        creds = sts.assume_role(
            RoleArn=role_arn, RoleSessionName="agentcore-flows-deploy"
        )["Credentials"]
    except Exception as e:  # noqa: BLE001
        raise TargetError(f"Cannot assume deployment role in {account_id}: {str(e)[:160]}")

    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=resolved_region,
    )
    # Dry-run: confirm we actually landed in the expected account.
    landed = session.client("sts").get_caller_identity()["Account"]
    if landed != account_id:
        raise TargetError(
            f"Assumed role landed in account {landed}, expected {account_id}"
        )
    return session
