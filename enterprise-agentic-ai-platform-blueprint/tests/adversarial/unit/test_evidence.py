"""Evidence records: sanitized, complete, and tied to code and topology.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from harness.catalog import AuditSource, case_by_id, catalog_sha
from harness.errors import EvidenceSchemaError, SanitizationError
from harness.evidence import (
    AuditEvidence,
    EvidenceRecord,
    EvidenceWriter,
    build_record,
    validate_records,
)
from harness.manifest import ResolvedPrincipal
from harness.outcome import ObservedOutcome
from harness.sanitize import alias_map

pytestmark = pytest.mark.adversarial

COMMIT = "0123456789abcdef0123456789abcdef01234567"
MANIFEST_SHA = "b" * 64

PRINCIPAL = ResolvedPrincipal(
    principal_ref="workstream.agent_runtime",
    account_alias="WORKSTREAM",
    role_name="AgenticAI-Workstream-AgentRuntime",
    privilege="least-privilege",
)


class FakeClientError(Exception):
    def __init__(self, code, message="", status=403, request_id="req-1"):
        super().__init__(message)
        self.response = {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {
                "HTTPStatusCode": status,
                "RequestId": request_id,
                "HTTPHeaders": {},
            },
        }


def denial(message="explicit deny in a service control policy"):
    return ObservedOutcome.from_client_error(
        FakeClientError("AccessDeniedException", message)
    )


def audit(locator="event-1"):
    return AuditEvidence(
        source=AuditSource.CLOUDTRAIL,
        locator=locator,
        matched_fields={"errorCode": "AccessDeniedException"},
        observed_at="2026-09-18T10:00:00+00:00",
    )


def negative_record(**overrides) -> EvidenceRecord:
    record = build_record(
        case=case_by_id("SCP-01-N"),
        test_id="tests/adversarial/cases/test_catalog_cases.py::SCP-01-N",
        principal=PRINCIPAL,
        outcome=denial(),
        verdict="pass",
        region="us-east-1",
        manifest_sha=MANIFEST_SHA,
        live_mode=True,
        audit_evidence=(audit(),),
        positive_twin_test_id="tests/adversarial/cases/test_catalog_cases.py::SCP-01-P",
        commit=COMMIT,
    )
    return replace(record, **overrides) if overrides else record


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------


def test_a_complete_negative_record_validates():
    negative_record().validate()


def test_record_carries_case_commit_and_manifest_provenance():
    payload = negative_record().to_dict()
    assert payload["caseId"] == "SCP-01-N"
    assert payload["commitSha"] == COMMIT
    assert payload["manifestSha"] == MANIFEST_SHA
    assert payload["catalogSha"] == catalog_sha()
    assert payload["harnessVersion"]


def test_record_carries_expected_and_observed_results():
    payload = negative_record().to_dict()
    assert payload["expectedResult"]["result"] == "DENY"
    assert payload["expectedResult"]["errorCodes"]
    assert payload["observedResult"]["errorCode"] == "AccessDeniedException"
    assert payload["outcomeClass"] == "AUTHORIZATION_DENIAL"


def test_record_carries_request_id_and_audit_evidence():
    payload = negative_record().to_dict()
    assert payload["requestId"] == "req-1"
    assert payload["auditEvidence"][0]["source"] == "cloudtrail"
    assert payload["auditEvidence"][0]["locator"] == "event-1"


def test_record_carries_the_positive_twin():
    payload = negative_record().to_dict()
    assert payload["positiveTwin"]["caseId"] == "SCP-01-P"
    assert payload["positiveTwin"]["testId"].endswith("SCP-01-P")


def test_record_principal_is_an_alias_never_an_account_id():
    payload = negative_record().to_dict()
    assert payload["principal"]["accountAlias"] == "WORKSTREAM"
    assert "accountId" not in payload["principal"]


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_missing_commit_sha_is_rejected():
    with pytest.raises(EvidenceSchemaError, match="commitSha"):
        negative_record(commit_sha=None).validate()


def test_non_sha_commit_is_rejected():
    with pytest.raises(EvidenceSchemaError, match="commitSha"):
        negative_record(commit_sha="HEAD").validate()


def test_missing_manifest_sha_is_rejected():
    with pytest.raises(EvidenceSchemaError, match="manifestSha"):
        negative_record(manifest_sha="").validate()


def test_missing_request_id_on_a_live_record_is_rejected():
    with pytest.raises(EvidenceSchemaError, match="requestId or traceId"):
        negative_record(request_id=None, trace_id=None).validate()


def test_missing_audit_evidence_on_a_passing_denial_is_rejected():
    with pytest.raises(EvidenceSchemaError, match="audit evidence"):
        negative_record(audit_evidence=()).validate()


def test_missing_positive_twin_on_a_passing_denial_is_rejected():
    with pytest.raises(EvidenceSchemaError, match="positive twin"):
        negative_record(positive_twin_case_id=None).validate()


def test_missing_twin_test_id_on_a_live_denial_is_rejected():
    with pytest.raises(EvidenceSchemaError, match="test id of the"):
        negative_record(positive_twin_test_id=None).validate()


def test_a_successful_call_cannot_be_a_passing_denial():
    record = build_record(
        case=case_by_id("SCP-01-N"),
        test_id="node::SCP-01-N",
        principal=PRINCIPAL,
        outcome=ObservedOutcome.from_success(request_id="req-1"),
        verdict="pass",
        region="us-east-1",
        manifest_sha=MANIFEST_SHA,
        live_mode=True,
        audit_evidence=(audit(),),
        positive_twin_test_id="node::SCP-01-P",
        commit=COMMIT,
    )
    with pytest.raises(EvidenceSchemaError, match="cannot be recorded as a passing"):
        record.validate()


def test_a_failing_record_is_not_required_to_carry_twin_or_audit():
    negative_record(verdict="fail", audit_evidence=(), positive_twin_test_id=None).validate()


def test_unknown_verdict_is_rejected():
    with pytest.raises(EvidenceSchemaError, match="verdict"):
        negative_record(verdict="probably").validate()


def test_missing_region_is_rejected():
    with pytest.raises(EvidenceSchemaError, match="region"):
        negative_record(region="").validate()


def test_empty_audit_locator_is_rejected():
    bad = AuditEvidence(source=AuditSource.CLOUDTRAIL, locator="  ")
    with pytest.raises(EvidenceSchemaError, match="locator"):
        negative_record(audit_evidence=(bad,)).validate()


def test_validate_records_aggregates_problems():
    problems = validate_records(
        [negative_record(commit_sha=None), negative_record(manifest_sha="")]
    )
    assert len(problems) == 2
    assert all(problem.startswith("SCP-01-N:") for problem in problems)


# ---------------------------------------------------------------------------
# writer
# ---------------------------------------------------------------------------


def test_writer_sanitizes_account_ids_out_of_a_record(tmp_path: Path):
    writer = EvidenceWriter(
        output_dir=tmp_path, aliases=alias_map({"333333333333": "WORKSTREAM"})
    )
    record = negative_record(
        observed={
            **negative_record().observed,
            "errorMessage": (
                "User: arn:aws:sts::333333333333:assumed-role/R/S is not "
                "authorized: explicit deny in a service control policy"
            ),
        }
    )
    stored = writer.record(record)
    assert "333333333333" not in json.dumps(stored.to_dict())
    assert "<account:WORKSTREAM>" in stored.observed["errorMessage"]


def test_writer_redacts_a_leaked_key_id(tmp_path: Path):
    """A key-shaped string in free text is redacted, not written through."""
    writer = EvidenceWriter(output_dir=tmp_path)
    record = negative_record(notes="AKIA" + "ABCDEFGHIJKLMNOP")
    stored = writer.record(record)
    assert "AKIA" not in stored.notes
    assert "<redacted>" in stored.notes


def test_writer_refuses_an_incomplete_record(tmp_path: Path):
    writer = EvidenceWriter(output_dir=tmp_path)
    with pytest.raises(EvidenceSchemaError):
        writer.record(negative_record(commit_sha=None))
    assert writer.records == []


def test_writer_flushes_jsonl_and_summary(tmp_path: Path):
    writer = EvidenceWriter(output_dir=tmp_path)
    writer.record(negative_record())
    paths = writer.flush()
    assert paths is not None
    jsonl_path, summary_path = paths
    lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["caseId"] == "SCP-01-N"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["recordCount"] == 1
    assert summary["commitShas"] == [COMMIT]
    assert summary["byDomain"]["scp"]["pass"] == 1
    assert summary["failures"] == []


def test_writer_summary_lists_failures(tmp_path: Path):
    writer = EvidenceWriter(output_dir=tmp_path)
    writer.record(negative_record(verdict="fail", audit_evidence=()))
    assert writer.summary()["failures"] == ["SCP-01-N"]


def test_writer_without_an_output_dir_keeps_records_in_memory():
    writer = EvidenceWriter()
    writer.record(negative_record())
    assert writer.flush() is None
    assert len(writer.records) == 1


def test_no_bundle_is_written_when_there_are_no_records(tmp_path: Path):
    writer = EvidenceWriter(output_dir=tmp_path / "bundle")
    assert writer.flush() is None
    assert not (tmp_path / "bundle").exists()


def test_final_sanitization_check_blocks_a_leak_that_slipped_past_rewriting():
    """Defence in depth: the writer verifies after sanitizing, not instead."""
    from harness.sanitize import assert_sanitized

    with pytest.raises(SanitizationError, match="email-address"):
        assert_sanitized({"notes": "builder@example.com"})
