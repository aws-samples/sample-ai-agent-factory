"""The live-mode gate.

Three states, and only three:

``off``
    Live verification was not asked for. Live cases **skip**, loudly labelled.

``requested/required but unavailable``
    Live verification was asked for (or is required by the run) and the
    credentials or the account mapping are absent. This is an **error**. It is
    never downgraded to a skip, because "we asked for live evidence and
    produced none" is exactly the false-green this suite exists to catch.

``available``
    Manifest resolves, credentials are present, and an injected identity probe
    has confirmed each account. Live cases **run**.

The identity probe is injected rather than imported so that the harness and its
unit tests make no AWS calls and need no SDK.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Protocol

from .catalog import AdversarialCase
from .errors import LiveModeRequiredError, ManifestError
from .manifest import AccountManifest, ResolvedManifest, load_manifest

ENV_LIVE = "AGENTICAI_ADVERSARIAL_LIVE"
ENV_REQUIRE_LIVE = "AGENTICAI_ADVERSARIAL_REQUIRE_LIVE"
ENV_MANIFEST = "AGENTICAI_ADVERSARIAL_MANIFEST"
ENV_EVIDENCE_DIR = "AGENTICAI_ADVERSARIAL_EVIDENCE_DIR"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _flag(env: Mapping[str, str], name: str) -> bool:
    return (env.get(name) or "").strip().lower() in _TRUTHY


class IdentityProbe(Protocol):
    """Confirms that credentials for an account key resolve to its account id.

    Implementations call ``sts:GetCallerIdentity``. Returning the observed
    account id lets the gate detect a credential/account mismatch — pointing
    "platform" credentials at the workstream account is a setup error that
    would otherwise silently produce meaningless evidence.
    """

    def __call__(self, account_key: str, expected_account_id: str) -> str: ...


class GateAction(str, Enum):
    RUN = "run"
    SKIP = "skip"
    ERROR = "error"


@dataclass(frozen=True)
class GateDecision:
    action: GateAction
    reason: str

    @property
    def should_run(self) -> bool:
        return self.action is GateAction.RUN


@dataclass(frozen=True)
class LiveModeStatus:
    """The outcome of evaluating the live-mode gate."""

    requested: bool
    required: bool
    available: bool
    reasons: tuple[str, ...] = ()
    manifest_path: str | None = None
    resolved: ResolvedManifest | None = None
    identity_verified: bool = False
    evidence_dir: str | None = None
    checks: Mapping[str, str] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.requested or self.required

    def raise_if_unsatisfied(self) -> None:
        """Error when live mode was asked for but is not available."""
        if self.enabled and not self.available:
            raise LiveModeRequiredError(self.reasons)

    def require(self) -> ResolvedManifest:
        """Return the resolved manifest or raise.

        Used by live probes: reaching this point guarantees a complete account
        mapping and verified credentials.
        """
        self.raise_if_unsatisfied()
        if not self.available or self.resolved is None:
            raise LiveModeRequiredError(
                self.reasons or ("live mode is not enabled for this run",)
            )
        return self.resolved

    def describe(self) -> str:
        if self.available:
            return "live mode available"
        if not self.enabled:
            return (
                f"live mode not enabled (set {ENV_LIVE}=1 with "
                f"{ENV_MANIFEST}=<manifest.json> to run live cases)"
            )
        return "live mode unavailable: " + "; ".join(self.reasons)


def live_mode_status(
    env: Mapping[str, str] | None = None,
    *,
    manifest_loader: Callable[[str], AccountManifest] = load_manifest,
    identity_probe: IdentityProbe | None = None,
) -> LiveModeStatus:
    """Evaluate the gate against ``env``.

    Never raises for an unavailable live mode — it records the reasons. Call
    :meth:`LiveModeStatus.raise_if_unsatisfied` (or use the ``live_mode``
    fixture) to turn an unsatisfied request into the error it should be.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    requested = _flag(environ, ENV_LIVE)
    required = _flag(environ, ENV_REQUIRE_LIVE)
    reasons: list[str] = []
    checks: dict[str, str] = {}

    manifest_path = (environ.get(ENV_MANIFEST) or "").strip() or None
    resolved: ResolvedManifest | None = None
    identity_verified = False

    if manifest_path is None:
        reasons.append(
            f"account/role manifest not configured: set {ENV_MANIFEST} to a "
            "manifest JSON path"
        )
        checks["manifest"] = "unset"
    else:
        try:
            manifest = manifest_loader(manifest_path)
        except ManifestError as exc:
            reasons.append(f"manifest at {manifest_path} unusable: {exc}")
            checks["manifest"] = "invalid"
        else:
            checks["manifest"] = "loaded"
            resolved = manifest.resolve(environ)
            if resolved.missing:
                reasons.extend(resolved.missing)
                checks["accountMapping"] = "incomplete"
            else:
                checks["accountMapping"] = "complete"
                if identity_probe is None:
                    reasons.append(
                        "caller identity not verified: no identity probe was "
                        "supplied, so credentials cannot be bound to the "
                        "declared accounts"
                    )
                    checks["identity"] = "unverified"
                else:
                    mismatches: list[str] = []
                    for account_key, account_id in resolved.account_ids.items():
                        try:
                            observed = identity_probe(account_key, account_id)
                        except Exception as exc:  # noqa: BLE001 - reported, not raised
                            mismatches.append(
                                f"account '{account_key}' identity probe failed: "
                                f"{type(exc).__name__}"
                            )
                            continue
                        if observed != account_id:
                            mismatches.append(
                                f"account '{account_key}' credentials resolve to a "
                                "different account than the manifest declares"
                            )
                    if mismatches:
                        reasons.extend(mismatches)
                        checks["identity"] = "mismatch"
                    else:
                        identity_verified = True
                        checks["identity"] = "verified"

    available = (
        resolved is not None
        and resolved.complete
        and identity_verified
        and not reasons
    )

    return LiveModeStatus(
        requested=requested,
        required=required,
        available=available,
        reasons=tuple(reasons),
        manifest_path=manifest_path,
        resolved=resolved,
        identity_verified=identity_verified,
        evidence_dir=(environ.get(ENV_EVIDENCE_DIR) or "").strip() or None,
        checks=checks,
    )


def gate_for_case(case: AdversarialCase, status: LiveModeStatus) -> GateDecision:
    """Decide what to do with ``case`` under ``status``.

    A case that does not require live AWS runs regardless. A live case runs
    only when live mode is available, skips when live mode was not asked for,
    and errors when it was asked for and is unavailable.
    """
    if not case.live_required:
        return GateDecision(GateAction.RUN, "case does not require live AWS")
    if status.available:
        return GateDecision(GateAction.RUN, "live mode available")
    if status.enabled:
        return GateDecision(
            GateAction.ERROR,
            "live verification is required for "
            f"{case.case_id} but " + "; ".join(status.reasons),
        )
    return GateDecision(
        GateAction.SKIP,
        f"{case.case_id} requires live AWS; " + status.describe(),
    )


def require_live_mode(status: LiveModeStatus) -> ResolvedManifest:
    """Convenience wrapper mirroring :meth:`LiveModeStatus.require`."""
    return status.require()


__all__ = [
    "ENV_EVIDENCE_DIR",
    "ENV_LIVE",
    "ENV_MANIFEST",
    "ENV_REQUIRE_LIVE",
    "GateAction",
    "GateDecision",
    "IdentityProbe",
    "LiveModeStatus",
    "gate_for_case",
    "live_mode_status",
    "require_live_mode",
]
