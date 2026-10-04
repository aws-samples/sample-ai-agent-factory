"""The live-probe registry: the single plug-in point for real AWS verification.

A probe performs exactly one catalogued case against a live deployment and
returns what it observed. It does **not** assert — the harness owns every
assertion, so no probe can weaken a rule by accident.

Adding a live case
------------------

1. Create ``probe_<domain>.py`` in this directory (the name must start with
   ``probe_`` to be discovered).
2. Register a function per case id::

       from harness import AuditEvidence, AuditSource, ObservedOutcome
       from cases.probes import ProbeContext, ProbeResult, probe

       @probe("SCP-01-N")
       def non_allowlisted_model_denied(ctx: ProbeContext) -> ProbeResult:
           session = ctx.session("workstream.agent_runtime")
           client = session.client("bedrock-runtime", region_name=ctx.region)
           try:
               client.converse(modelId=UNAPPROVED_MODEL, messages=[...])
           except Exception as exc:                      # botocore ClientError
               outcome = ObservedOutcome.from_client_error(exc)
           else:
               outcome = ObservedOutcome.from_success()
           return ProbeResult(
               outcome=outcome,
               audit_evidence=(
                   AuditEvidence(
                       source=AuditSource.CLOUDTRAIL,
                       locator="<event id>",
                       matched_fields={"errorCode": outcome.error_code},
                   ),
               ),
           )

3. Nothing else. ``test_catalog_cases.py`` picks the case up, applies the
   expectation's assertion, and records the sanitized evidence.

Rules a probe must respect:

* never assert, never skip, never swallow an exception — return what happened;
* never read a credential file: obtain credentials only through
  ``ctx.session(...)``, which is driven by the manifest;
* capture the service ``requestId`` (``ObservedOutcome.from_client_error`` does
  this for you) — a denial with no request id is rejected by the assertions;
* for a negative case, also collect the corroborating audit record.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from harness import AdversarialCase, AuditEvidence, ObservedOutcome, ResolvedManifest
from harness.errors import AdversarialHarnessError, CredentialSourceError
from harness.manifest import CredentialSourceType

_PROBE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class ProbeResult:
    """What a probe observed. No verdict, no assertion."""

    outcome: ObservedOutcome
    audit_evidence: tuple[AuditEvidence, ...] = ()
    extras: Mapping[str, Any] = field(default_factory=dict)
    notes: str = ""


@dataclass(frozen=True)
class ProbeContext:
    """Everything a probe is allowed to know.

    Credentials are reached only through :meth:`session`, which derives them
    from the manifest's declared credential source. There is no path here to an
    on-disk key export.
    """

    case: AdversarialCase
    resolved: ResolvedManifest
    region: str
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))

    def account_id(self, account_key: str) -> str:
        try:
            return self.resolved.account_ids[account_key]
        except KeyError as exc:
            raise AdversarialHarnessError(
                f"account {account_key!r} is not resolved; the live-mode gate "
                "should have prevented this probe from running"
            ) from exc

    def role_arn(self, principal_ref: str) -> str:
        role = self.resolved.manifest.role(principal_ref)
        partition = self.resolved.manifest.partition
        account_id = self.account_id(role.account_key)
        return f"arn:{partition}:iam::{account_id}:role/{role.role_name}"

    def external_id(self, account_key: str) -> str | None:
        source = self.resolved.manifest.account(account_key).credential_source
        if not source.external_id_env:
            return None
        return self.env.get(source.external_id_env)

    def session(self, principal_ref: str) -> Any:
        """Return a boto3 session acting as ``principal_ref``.

        Base credentials come from the account's declared credential source;
        the role itself is then assumed. Raises rather than falling back to
        ambient credentials, because a probe that silently ran as the wrong
        principal produces evidence that means nothing.
        """
        try:
            import boto3  # noqa: PLC0415 - probes are the only AWS-touching layer
        except ImportError as exc:  # pragma: no cover - live path only
            raise AdversarialHarnessError(
                "boto3 is required for live probes but is not installed"
            ) from exc

        role = self.resolved.manifest.role(principal_ref)
        account = self.resolved.manifest.account(role.account_key)
        source = account.credential_source

        if source.type is CredentialSourceType.ENV_PREFIX:
            prefix = source.value
            access_key = self.env.get(f"AWS_ACCESS_KEY_ID_{prefix}")
            secret_key = self.env.get(f"AWS_SECRET_ACCESS_KEY_{prefix}")
            if access_key and secret_key:
                base = boto3.session.Session(
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                    aws_session_token=self.env.get(f"AWS_SESSION_TOKEN_{prefix}"),
                    region_name=self.region,
                )
            else:
                profile = self.env.get(f"AWS_PROFILE_{prefix}")
                if not profile:
                    raise CredentialSourceError(
                        f"no credentials available for account "
                        f"{role.account_key!r} (set AWS_ACCESS_KEY_ID_{prefix} "
                        f"and AWS_SECRET_ACCESS_KEY_{prefix}, or "
                        f"AWS_PROFILE_{prefix})"
                    )
                base = boto3.session.Session(
                    profile_name=profile, region_name=self.region
                )
        elif source.type is CredentialSourceType.PROFILE:
            base = boto3.session.Session(
                profile_name=source.value, region_name=self.region
            )
        else:
            raise CredentialSourceError(
                f"credential source {source.type.value!r} for account "
                f"{role.account_key!r} must be resolved by the probe explicitly"
            )

        kwargs: dict[str, Any] = {
            "RoleArn": self.role_arn(principal_ref),
            "RoleSessionName": f"adversarial-{self.case.case_id}"[:64],
            "DurationSeconds": 900,
        }
        external_id = self.external_id(role.account_key)
        if external_id:
            kwargs["ExternalId"] = external_id
        credentials = base.client("sts").assume_role(**kwargs)["Credentials"]
        return boto3.session.Session(
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
            region_name=self.region,
        )


Probe = Callable[[ProbeContext], ProbeResult]

#: case id -> probe. Populated by ``@probe`` at module import.
PROBE_REGISTRY: dict[str, Probe] = {}


def probe(case_id: str) -> Callable[[Probe], Probe]:
    """Register a probe for a catalogued case id."""

    def decorator(func: Probe) -> Probe:
        if case_id in PROBE_REGISTRY:
            raise AdversarialHarnessError(
                f"a probe is already registered for case {case_id!r}"
            )
        PROBE_REGISTRY[case_id] = func
        return func

    return decorator


def get_probe(case_id: str) -> Probe | None:
    return PROBE_REGISTRY.get(case_id)


def registered_case_ids() -> tuple[str, ...]:
    return tuple(sorted(PROBE_REGISTRY))


def discover_probe_modules() -> tuple[str, ...]:
    """Module names in this package that look like probe modules."""
    return tuple(
        sorted(
            path.stem
            for path in _PROBE_DIR.glob("probe_*.py")
            if path.is_file() and not path.stem.endswith("_test")
        )
    )


def load_probe_modules() -> tuple[str, ...]:
    """Import every ``probe_*.py`` module so registrations take effect."""
    loaded: list[str] = []
    for module_name in discover_probe_modules():
        importlib.import_module(f"{__name__}.{module_name}")
        loaded.append(module_name)
    return tuple(loaded)


__all__ = [
    "PROBE_REGISTRY",
    "Probe",
    "ProbeContext",
    "ProbeResult",
    "discover_probe_modules",
    "get_probe",
    "load_probe_modules",
    "probe",
    "registered_case_ids",
]
