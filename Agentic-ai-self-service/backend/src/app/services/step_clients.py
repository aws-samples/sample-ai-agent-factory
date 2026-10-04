"""Per-deploy boto3 client factory — the cross-account seam (Phase 7 wiring).

Step handlers historically built boto3 clients ad-hoc
(``boto3.client("x", region_name=region)``), which hard-wires every deploy to
the platform's home account + region. This factory is the single place that
decides WHICH account/region a step's clients target, so cross-account /
multi-region deploy works without bespoke logic at 43 call sites.

Contract (backward-compatible by construction):
  * No target on the SFN event  → the DEFAULT boto3 session in the deploy's
    region — byte-for-byte the previous behavior (home account, home region).
  * ``target_account_id`` / ``target_region`` present → an assumed-role session
    into the registered target account via
    ``deploy_target.session_for_target`` (which enforces the opt-in gate, the
    region allowlist, and the landed-account dry-run check).

The SFN event carries ``target_account_id`` / ``target_region`` (added by
deployment_handler.handle_deploy). Absent/empty → home. Handlers call
``client(event, "s3")`` / ``resource(event, "dynamodb")`` instead of boto3
directly; ``session_for_event(event)`` returns the raw Session when a handler
needs to build several clients or derive the caller account.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3

logger = logging.getLogger(__name__)


def _home_region(event: dict | None = None) -> str:
    if event:
        r = event.get("target_region") or event.get("region")
        if r:
            return r
    return os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))


def session_for_event(event: dict | None) -> boto3.Session:
    """Return the boto3 Session a step should use for its target.

    Default session (home account) when the event carries no target account;
    an assumed-role cross-account session otherwise. Never raises for the
    home-account path; a cross-account failure raises deploy_target.TargetError
    (the deploy should fail loudly rather than silently land in the wrong place).
    """
    event = event or {}
    account_id = event.get("target_account_id")
    region = event.get("target_region")
    if not account_id:
        # Home account, unchanged path.
        return boto3.Session(region_name=_home_region(event))
    # Cross-account: assume the target role. require_gate=False because
    # handle_deploy (the deployment Lambda) is the SINGLE authoritative gate —
    # it validated targets_enabled() + the allowlist before starting the SFN,
    # and the step Lambdas don't carry the Settings-table env to re-read it. The
    # role ARN is threaded on the SFN event (resolved at deploy time) so the step
    # never needs a Settings lookup; the landed-account dry-run check still runs.
    from app.services.deploy_target import session_for_target

    return session_for_target(
        account_id=account_id,
        region=region,
        role_arn=event.get("target_role_arn"),
        require_gate=False,
    )


def client(event: dict | None, service: str, **kwargs: Any):
    """boto3 client for *service* targeting the deploy's account/region.

    Drop-in for ``boto3.client(service, region_name=region)``: pass the SFN
    ``event`` and the service name. Extra kwargs (e.g. ``config=``) pass through.

    Home account with an explicit ``region_name``: the default session, directly. Resolving the home region through
    ``session_for_event`` costs a settings-table read the caller did not ask for, and in the manifest deleter a
    NotFound from THAT read was classified as "the resource is already gone" (measured on a policy row whose policy
    still existed). Cross-account events still assume the target role.
    """
    event = event or {}
    if not event.get("target_account_id") and kwargs.get("region_name"):
        return boto3.client(service, **kwargs)
    return session_for_event(event).client(service, **kwargs)


def resource(event: dict | None, service: str, **kwargs: Any):
    """boto3 resource for *service* targeting the deploy's account/region."""
    return session_for_event(event).resource(service, **kwargs)


def account_id_for_event(event: dict | None) -> str:
    """The account id the deploy targets (the TARGET account, cross-account-aware).

    Handlers that build ARNs must use THIS (not a home-account
    sts:GetCallerIdentity) so ARNs point at the target account.
    """
    return session_for_event(event).client("sts").get_caller_identity()["Account"]


def artifacts_bucket_for_event(
    event: dict | None,
    *,
    platform_bucket: str | None = None,
) -> str:
    """Return the exact code/staging bucket for this deployment target.

    A cross-account or same-account/non-home-region event must carry the bucket
    that the deployment handler already ownership/region-validated.
    Reconstructing a name inside a step bypasses that registration boundary and
    can target a missing or foreign bucket. Home-region deploys retain the
    platform bucket environment value.
    """
    event = event or {}
    account_id = event.get("target_account_id")
    target_region = event.get("target_region")
    platform_region = os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))
    if not account_id:
        if target_region and str(target_region) != platform_region:
            artifact_bucket = event.get("target_artifact_bucket")
            if not artifact_bucket:
                raise ValueError("Same-account regional deployment is missing its validated target_artifact_bucket")
            return str(artifact_bucket)
        return platform_bucket if platform_bucket is not None else os.environ.get("ARTIFACTS_BUCKET_NAME", "")
    from app.services.deploy_target import target_artifact_bucket_name

    artifact_bucket = event.get("target_artifact_bucket")
    if not artifact_bucket:
        raise ValueError("Cross-account deployment is missing its validated target_artifact_bucket")
    return target_artifact_bucket_name(
        str(account_id),
        str(event.get("target_region") or _home_region(event)),
        artifact_bucket=str(artifact_bucket),
    )
