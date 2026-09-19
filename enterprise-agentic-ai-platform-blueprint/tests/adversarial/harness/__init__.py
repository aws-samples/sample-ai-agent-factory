"""Adversarial verification harness for the three-account AgentCore platform.

This package is deliberately AWS-free: it imports no SDK, opens no sockets and
reads no credentials. All live interaction happens in probes registered under
``tests/adversarial/cases/probes``, which receive already-validated
configuration from the harness.

Layers:

``manifest``
    The account/role declaration for Management/Governance, Platform and
    Workstream, plus resolution from the environment and the manifest hash.
``livemode``
    The gate. Skips when live mode is off, errors when live mode was asked for
    but credentials or the account mapping are missing.
``outcome``
    Normalizes an observed result and classifies whether it may be used as
    control evidence at all.
``assertions``
    Assertions that accept only genuine control evidence, plus the positive-twin
    ledger.
``evidence``
    Sanitized, traceable evidence records and the bundle writer.
``catalog``
    The declarative adversarial case matrix.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

from .assertions import (
    TwinLedger,
    assert_absent,
    assert_allowed,
    assert_authentication_denied,
    assert_authorization_denied,
    assert_case,
    assert_detected,
    assert_guardrail_blocked,
    assert_not_exposed,
    assert_rate_limited,
    assert_rolled_back,
)
from .catalog import (
    CATALOG,
    AdversarialCase,
    AuditSource,
    Domain,
    Expectation,
    Severity,
    assert_catalog_valid,
    case_by_id,
    cases_for_domain,
    catalog_sha,
    validate_catalog,
)
from .errors import (
    AdversarialHarnessError,
    CatalogError,
    CredentialSourceError,
    EvidenceSchemaError,
    InvalidDenialProof,
    LiveModeRequiredError,
    LiveProbeNotImplemented,
    ManifestError,
    ManifestSchemaError,
    PositiveTwinMissing,
    SanitizationError,
)
from .evidence import AuditEvidence, EvidenceRecord, EvidenceWriter, build_record
from .livemode import (
    GateAction,
    GateDecision,
    LiveModeStatus,
    gate_for_case,
    live_mode_status,
)
from .manifest import (
    MANAGEMENT_ACCOUNT,
    PLATFORM_ACCOUNT,
    REQUIRED_ACCOUNTS,
    REQUIRED_ROLES,
    WORKSTREAM_ACCOUNT,
    AccountManifest,
    ResolvedManifest,
    load_manifest,
    manifest_sha,
)
from .outcome import ObservedOutcome, OutcomeClass
from .provenance import HARNESS_VERSION, Provenance, commit_sha

__all__ = [
    "AccountManifest",
    "AdversarialCase",
    "AdversarialHarnessError",
    "AuditEvidence",
    "AuditSource",
    "CATALOG",
    "CatalogError",
    "CredentialSourceError",
    "Domain",
    "EvidenceRecord",
    "EvidenceSchemaError",
    "EvidenceWriter",
    "Expectation",
    "GateAction",
    "GateDecision",
    "HARNESS_VERSION",
    "InvalidDenialProof",
    "LiveModeRequiredError",
    "LiveModeStatus",
    "LiveProbeNotImplemented",
    "MANAGEMENT_ACCOUNT",
    "ManifestError",
    "ManifestSchemaError",
    "ObservedOutcome",
    "OutcomeClass",
    "PLATFORM_ACCOUNT",
    "PositiveTwinMissing",
    "Provenance",
    "REQUIRED_ACCOUNTS",
    "REQUIRED_ROLES",
    "ResolvedManifest",
    "SanitizationError",
    "Severity",
    "TwinLedger",
    "WORKSTREAM_ACCOUNT",
    "assert_absent",
    "assert_allowed",
    "assert_authentication_denied",
    "assert_authorization_denied",
    "assert_case",
    "assert_catalog_valid",
    "assert_detected",
    "assert_guardrail_blocked",
    "assert_not_exposed",
    "assert_rate_limited",
    "assert_rolled_back",
    "build_record",
    "case_by_id",
    "cases_for_domain",
    "catalog_sha",
    "commit_sha",
    "gate_for_case",
    "live_mode_status",
    "load_manifest",
    "manifest_sha",
    "validate_catalog",
]
