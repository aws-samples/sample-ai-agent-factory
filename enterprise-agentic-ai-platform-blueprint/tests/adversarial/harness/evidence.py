"""Sanitized evidence records and the bundle writer.

An evidence record is the durable product of an adversarial case. It must be
able to answer, months later and without access to the accounts:

* which case ran, under which test id;
* against which code (``commitSha``), which account/role topology
  (``manifestSha``) and which control matrix (``catalogSha``);
* as which principal (by alias and role name — never an account number);
* what was expected, what was observed, and which evidence class the observed
  result falls into;
* the service ``requestId`` / trace id that ties the record to the provider's
  own logs;
* the corroborating audit record (CloudTrail, Gateway access log, guardrail
  trace, pipeline execution, ...);
* and, for a denial, which authorized positive twin passed in the same run.

Records are sanitized before validation, and validation rejects anything
incomplete. A record that cannot be written is a failed case, never a silent
omission.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .catalog import AdversarialCase, AuditSource, Expectation, catalog_sha
from .errors import EvidenceSchemaError
from .manifest import ResolvedManifest, ResolvedPrincipal
from .outcome import ObservedOutcome, OutcomeClass
from .provenance import (
    EVIDENCE_SCHEMA_VERSION,
    HARNESS_VERSION,
    commit_sha,
    is_commit_sha,
)
from .sanitize import alias_map, assert_sanitized, sanitize_value

_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")

#: Expectations whose records must carry a corroborating audit record.
AUDIT_REQUIRED_EXPECTATIONS: frozenset[Expectation] = frozenset(
    {
        Expectation.DENY,
        Expectation.AUTHENTICATION_DENIED,
        Expectation.RATE_LIMITED,
        Expectation.GUARDRAIL_BLOCKED,
        Expectation.NOT_EXPOSED,
        Expectation.ROLLED_BACK,
        Expectation.DETECTED,
    }
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class AuditEvidence:
    """One corroborating record from an audit source."""

    source: AuditSource
    locator: str
    matched_fields: Mapping[str, Any] = field(default_factory=dict)
    observed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "locator": self.locator,
            "matchedFields": dict(self.matched_fields),
            "observedAt": self.observed_at or utc_now(),
        }


@dataclass(frozen=True)
class EvidenceRecord:
    """A single sanitized, traceable case result."""

    test_id: str
    case_id: str
    domain: str
    expectation: str
    severity: str
    principal: Mapping[str, str]
    target: str
    region: str
    expected: Mapping[str, Any]
    observed: Mapping[str, Any]
    outcome_class: str
    verdict: str
    live_mode: bool
    manifest_sha: str
    catalog_sha: str
    commit_sha: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    audit_evidence: tuple[AuditEvidence, ...] = ()
    positive_twin_case_id: str | None = None
    positive_twin_test_id: str | None = None
    control_refs: tuple[str, ...] = ()
    started_at: str = ""
    finished_at: str = ""
    notes: str = ""
    schema_version: str = EVIDENCE_SCHEMA_VERSION
    harness_version: str = HARNESS_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "harnessVersion": self.harness_version,
            "testId": self.test_id,
            "caseId": self.case_id,
            "domain": self.domain,
            "expectation": self.expectation,
            "severity": self.severity,
            "commitSha": self.commit_sha,
            "manifestSha": self.manifest_sha,
            "catalogSha": self.catalog_sha,
            "principal": dict(self.principal),
            "target": self.target,
            "region": self.region,
            "expectedResult": dict(self.expected),
            "observedResult": dict(self.observed),
            "outcomeClass": self.outcome_class,
            "verdict": self.verdict,
            "liveMode": self.live_mode,
            "requestId": self.request_id,
            "traceId": self.trace_id,
            "auditEvidence": [item.to_dict() for item in self.audit_evidence],
            "positiveTwin": {
                "caseId": self.positive_twin_case_id,
                "testId": self.positive_twin_test_id,
            },
            "controlRefs": list(self.control_refs),
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "notes": self.notes,
        }

    # -- validation --------------------------------------------------------

    def problems(self) -> list[str]:
        """Every schema violation in this record."""
        issues: list[str] = []

        if not self.test_id.strip():
            issues.append("testId is required")
        if not self.case_id.strip():
            issues.append("caseId is required")
        if not self.domain.strip():
            issues.append("domain is required")
        if not self.target.strip():
            issues.append("target is required")
        if not self.region.strip():
            issues.append("region is required")

        if self.verdict not in ("pass", "fail"):
            issues.append("verdict must be 'pass' or 'fail'")

        try:
            Expectation(self.expectation)
        except ValueError:
            issues.append(f"expectation {self.expectation!r} is not a known expectation")

        try:
            outcome_class = OutcomeClass(self.outcome_class)
        except ValueError:
            issues.append(
                f"outcomeClass {self.outcome_class!r} is not a known outcome class"
            )
            outcome_class = None

        if not is_commit_sha(self.commit_sha):
            issues.append(
                "commitSha must be a 40-hex git SHA — set "
                "AGENTICAI_EVIDENCE_COMMIT_SHA if it cannot be read from the "
                "working tree"
            )
        if not _SHA256_RE.match(self.manifest_sha or ""):
            issues.append("manifestSha must be a 64-hex SHA-256 of the manifest")
        if not _SHA256_RE.match(self.catalog_sha or ""):
            issues.append("catalogSha must be a 64-hex SHA-256 of the case catalog")

        for key in ("principalRef", "accountAlias", "roleName"):
            if not (self.principal or {}).get(key):
                issues.append(f"principal.{key} is required")

        if not self.expected:
            issues.append("expectedResult is required")
        if not self.observed:
            issues.append("observedResult is required")

        if (
            self.live_mode
            and self.verdict == "pass"
            and not (self.request_id or self.trace_id)
        ):
            issues.append(
                "a passing live record must carry a requestId or traceId so it can be "
                "correlated with provider-side logs"
            )

        expectation_is_negative = self.expectation in {
            member.value for member in AUDIT_REQUIRED_EXPECTATIONS
        }
        if expectation_is_negative and self.verdict == "pass":
            if not self.audit_evidence:
                issues.append(
                    f"expectation {self.expectation} requires at least one "
                    "audit evidence entry"
                )
            if not self.positive_twin_case_id:
                issues.append(
                    f"expectation {self.expectation} requires a positive twin "
                    "case id"
                )
            if self.live_mode and not self.positive_twin_test_id:
                issues.append(
                    "a live negative record requires the test id of the "
                    "positive twin that passed in the same run"
                )

        if outcome_class is OutcomeClass.SUCCESS and self.expectation in {
            Expectation.DENY.value,
            Expectation.AUTHENTICATION_DENIED.value,
        }:
            if self.verdict != "fail":
                issues.append(
                    "a successful call cannot be recorded as a passing denial"
                )

        for index, item in enumerate(self.audit_evidence):
            if not item.locator.strip():
                issues.append(f"auditEvidence[{index}].locator is required")

        return issues

    def validate(self) -> None:
        issues = self.problems()
        if issues:
            raise EvidenceSchemaError(issues)

    def sanitized(self, aliases: Mapping[str, str] | None = None) -> "EvidenceRecord":
        """Return a copy with every field sanitized."""
        table = dict(aliases or {})
        return replace(
            self,
            principal=sanitize_value(dict(self.principal), table),
            target=sanitize_value(self.target, table),
            expected=sanitize_value(dict(self.expected), table),
            observed=sanitize_value(dict(self.observed), table),
            request_id=sanitize_value(self.request_id, table)
            if self.request_id
            else None,
            trace_id=sanitize_value(self.trace_id, table) if self.trace_id else None,
            audit_evidence=tuple(
                AuditEvidence(
                    source=item.source,
                    locator=sanitize_value(item.locator, table),
                    matched_fields=sanitize_value(dict(item.matched_fields), table),
                    observed_at=item.observed_at,
                )
                for item in self.audit_evidence
            ),
            notes=sanitize_value(self.notes, table),
        )


def build_record(
    *,
    case: AdversarialCase,
    test_id: str,
    principal: ResolvedPrincipal,
    outcome: ObservedOutcome,
    verdict: str,
    region: str,
    manifest_sha: str,
    live_mode: bool,
    audit_evidence: Sequence[AuditEvidence] = (),
    positive_twin_test_id: str | None = None,
    started_at: str = "",
    finished_at: str = "",
    notes: str = "",
    commit: str | None = None,
) -> EvidenceRecord:
    """Assemble an :class:`EvidenceRecord` from a case and its outcome."""
    return EvidenceRecord(
        test_id=test_id,
        case_id=case.case_id,
        domain=case.domain.value,
        expectation=case.expectation.value,
        severity=case.severity.value,
        principal=principal.to_dict(),
        target=case.target,
        region=region,
        expected={
            "result": case.expectation.value,
            "errorCodes": list(case.expected_error_codes),
            "httpStatus": list(case.expected_http_status),
            "messageSubstrings": list(case.required_message_substrings),
            "auditSources": [source.value for source in case.audit_sources],
        },
        observed=outcome.to_dict(),
        outcome_class=outcome.outcome_class.value,
        verdict=verdict,
        live_mode=live_mode,
        manifest_sha=manifest_sha,
        catalog_sha=catalog_sha(),
        commit_sha=commit if commit is not None else commit_sha(),
        request_id=outcome.request_id,
        trace_id=outcome.trace_id,
        audit_evidence=tuple(audit_evidence),
        positive_twin_case_id=case.positive_twin,
        positive_twin_test_id=positive_twin_test_id,
        control_refs=tuple(case.control_refs),
        started_at=started_at or utc_now(),
        finished_at=finished_at or utc_now(),
        notes=notes or case.notes,
    )


@dataclass
class EvidenceWriter:
    """Collects records in memory and writes a sanitized JSONL bundle."""

    output_dir: Path | None = None
    aliases: Mapping[str, str] = field(default_factory=dict)
    records: list[EvidenceRecord] = field(default_factory=list)

    @classmethod
    def for_run(
        cls,
        output_dir: str | Path | None,
        resolved: ResolvedManifest | None = None,
    ) -> "EvidenceWriter":
        aliases = alias_map(resolved.account_aliases()) if resolved else {}
        return cls(
            output_dir=Path(output_dir) if output_dir else None,
            aliases=aliases,
        )

    def record(self, record: EvidenceRecord) -> EvidenceRecord:
        """Sanitize, verify and retain a record.

        Raises :class:`SanitizationError` or :class:`EvidenceSchemaError`
        rather than writing something unusable.
        """
        clean = record.sanitized(self.aliases)
        payload = clean.to_dict()
        assert_sanitized(payload, path=f"$evidence[{record.case_id}]")
        clean.validate()
        self.records.append(clean)
        return clean

    # -- output ------------------------------------------------------------

    def to_jsonl(self) -> str:
        return "\n".join(
            json.dumps(record.to_dict(), sort_keys=True, ensure_ascii=True)
            for record in self.records
        )

    def summary(self) -> dict[str, Any]:
        by_domain: dict[str, dict[str, int]] = {}
        for record in self.records:
            bucket = by_domain.setdefault(record.domain, {"pass": 0, "fail": 0})
            bucket[record.verdict] = bucket.get(record.verdict, 0) + 1
        commits = sorted({record.commit_sha for record in self.records if record.commit_sha})
        manifests = sorted({record.manifest_sha for record in self.records})
        return {
            "schemaVersion": EVIDENCE_SCHEMA_VERSION,
            "harnessVersion": HARNESS_VERSION,
            "generatedAt": utc_now(),
            "recordCount": len(self.records),
            "commitShas": commits,
            "manifestShas": manifests,
            "catalogSha": catalog_sha(),
            "byDomain": by_domain,
            "failures": [
                record.case_id for record in self.records if record.verdict == "fail"
            ],
        }

    def flush(self) -> tuple[Path, Path] | None:
        """Write ``evidence.jsonl`` and ``summary.json``; no-op without a dir."""
        if self.output_dir is None or not self.records:
            return None
        self.output_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = self.output_dir / "evidence.jsonl"
        summary_path = self.output_dir / "summary.json"
        jsonl_path.write_text(self.to_jsonl() + "\n", encoding="utf-8")
        summary_path.write_text(
            json.dumps(self.summary(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return jsonl_path, summary_path


def validate_records(records: Iterable[EvidenceRecord]) -> list[str]:
    problems: list[str] = []
    for record in records:
        for issue in record.problems():
            problems.append(f"{record.case_id}: {issue}")
    return problems


__all__ = [
    "AUDIT_REQUIRED_EXPECTATIONS",
    "AuditEvidence",
    "EvidenceRecord",
    "EvidenceWriter",
    "build_record",
    "utc_now",
    "validate_records",
]
