"""Exception hierarchy for the adversarial verification harness.

Design rule: every failure mode that could otherwise be mistaken for a
passing test raises loudly. Nothing in this harness returns a soft
``False`` where a test could then be written to ignore it.

``InvalidDenialProof`` deliberately subclasses ``AssertionError`` so that a
negative (denial) test which received the *wrong kind* of failure reports as
a test failure rather than a harness error.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations


class AdversarialHarnessError(Exception):
    """Base class for all harness errors."""


# --- manifest -------------------------------------------------------------


class ManifestError(AdversarialHarnessError):
    """The account/role manifest could not be loaded or resolved."""


class ManifestSchemaError(ManifestError):
    """The manifest document violates the manifest schema."""

    def __init__(self, problems: "list[str] | tuple[str, ...]") -> None:
        self.problems = tuple(problems)
        joined = "\n  - ".join(self.problems)
        super().__init__(f"manifest schema violations:\n  - {joined}")


class CredentialSourceError(ManifestError):
    """A declared credential source is forbidden or malformed.

    Raised, in particular, when a manifest points at an on-disk credential
    file (for example a downloaded ``*.csv`` access-key export). The harness
    never opens such files.
    """


# --- live mode ------------------------------------------------------------


class LiveModeError(AdversarialHarnessError):
    """Base class for live-mode gate errors."""


class LiveModeRequiredError(LiveModeError):
    """Live verification was requested or required, but is not available.

    This is an error, never a skip: a run that asked for live evidence and
    silently produced none is the exact false-green this harness exists to
    prevent.
    """

    def __init__(self, reasons: "list[str] | tuple[str, ...]") -> None:
        self.reasons = tuple(reasons)
        joined = "\n  - ".join(self.reasons) if self.reasons else "(no reason recorded)"
        super().__init__(
            "live verification was requested/required but is unavailable:\n  - "
            + joined
        )


# --- evidence -------------------------------------------------------------


class EvidenceSchemaError(AdversarialHarnessError):
    """An evidence record is missing required fields or is malformed."""

    def __init__(self, problems: "list[str] | tuple[str, ...]") -> None:
        self.problems = tuple(problems)
        joined = "\n  - ".join(self.problems)
        super().__init__(f"evidence schema violations:\n  - {joined}")


class SanitizationError(AdversarialHarnessError):
    """A value carrying sensitive material was about to be recorded.

    The message names the *kind* and location of each finding and never the
    matched value itself.
    """

    def __init__(self, findings: "list[str] | tuple[str, ...]") -> None:
        self.findings = tuple(findings)
        joined = "\n  - ".join(self.findings)
        super().__init__(f"refusing to record unsanitized evidence:\n  - {joined}")


# --- assertions -----------------------------------------------------------


class InvalidDenialProof(AssertionError, AdversarialHarnessError):
    """The observed failure is not acceptable proof of a control decision."""


class PositiveTwinMissing(AssertionError, AdversarialHarnessError):
    """A negative case ran without its authorized positive twin passing."""


# --- catalog / cases ------------------------------------------------------


class CatalogError(AdversarialHarnessError):
    """The adversarial case catalog is internally inconsistent."""

    def __init__(self, problems: "list[str] | tuple[str, ...]") -> None:
        self.problems = tuple(problems)
        joined = "\n  - ".join(self.problems)
        super().__init__(f"catalog validation failed:\n  - {joined}")


class LiveProbeNotImplemented(AdversarialHarnessError):
    """A catalogued case has no registered live probe yet.

    In live mode this fails the case. It must never be converted into a skip
    or an ``xfail`` — an unimplemented control check is an open gap, not a
    tolerated condition.
    """


__all__ = [
    "AdversarialHarnessError",
    "CatalogError",
    "CredentialSourceError",
    "EvidenceSchemaError",
    "InvalidDenialProof",
    "LiveModeError",
    "LiveModeRequiredError",
    "LiveProbeNotImplemented",
    "ManifestError",
    "ManifestSchemaError",
    "PositiveTwinMissing",
    "SanitizationError",
]
