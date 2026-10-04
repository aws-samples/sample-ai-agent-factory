"""The probe registry and case-execution contract.

Verifies the plug-in point future live cases use, and the ordering the twin
ledger depends on — all without touching AWS.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cases.probes import (
    PROBE_REGISTRY,
    ProbeContext,
    ProbeResult,
    discover_probe_modules,
    get_probe,
    probe,
    registered_case_ids,
)
from harness import (
    CATALOG,
    AuditEvidence,
    AuditSource,
    ObservedOutcome,
    TwinLedger,
    assert_case,
    build_record,
)
from harness.catalog import Expectation, case_by_id, execution_order
from harness.errors import AdversarialHarnessError
from harness.evidence import EvidenceWriter
from harness.livemode import ENV_LIVE, ENV_MANIFEST, GateAction, gate_for_case, live_mode_status
from harness.manifest import load_manifest

pytestmark = pytest.mark.adversarial

EXAMPLE_MANIFEST = (
    Path(__file__).resolve().parents[1] / "fixtures" / "manifest.example.json"
)
COMMIT = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Never let a test's registrations leak into another test."""
    snapshot = dict(PROBE_REGISTRY)
    yield
    PROBE_REGISTRY.clear()
    PROBE_REGISTRY.update(snapshot)


def resolved_manifest():
    env = {
        "AGENTICAI_ACCOUNT_MANAGEMENT": "111111111111",
        "AGENTICAI_ACCOUNT_PLATFORM": "222222222222",
        "AGENTICAI_ACCOUNT_WORKSTREAM": "333333333333",
        "AWS_ACCESS_KEY_ID_MANAGEMENT": "x",
        "AWS_SECRET_ACCESS_KEY_MANAGEMENT": "x",
        "AWS_ACCESS_KEY_ID_PLATFORM": "x",
        "AWS_SECRET_ACCESS_KEY_PLATFORM": "x",
        "AWS_ACCESS_KEY_ID_WORKSTREAM": "x",
        "AWS_SECRET_ACCESS_KEY_WORKSTREAM": "x",
        "AGENTICAI_ADVERSARIAL_EXTERNAL_ID": "external-id-placeholder",
    }
    return load_manifest(EXAMPLE_MANIFEST).resolve(env)


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------


def test_positives_run_before_their_negatives():
    order = {case.case_id: index for index, case in enumerate(execution_order(CATALOG))}
    for case in CATALOG:
        if case.positive_twin:
            assert order[case.positive_twin] < order[case.case_id], case.case_id


def test_execution_order_covers_every_case_exactly_once():
    ordered = execution_order(CATALOG)
    assert len(ordered) == len(CATALOG)
    assert {case.case_id for case in ordered} == {case.case_id for case in CATALOG}


def test_execution_order_is_deterministic():
    assert [case.case_id for case in execution_order(CATALOG)] == [
        case.case_id for case in execution_order(CATALOG)
    ]


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_probe_registration_and_lookup():
    @probe("SCP-01-N")
    def _handler(ctx: ProbeContext) -> ProbeResult:  # pragma: no cover - not called
        return ProbeResult(outcome=ObservedOutcome.from_success())

    assert get_probe("SCP-01-N") is _handler
    assert "SCP-01-N" in registered_case_ids()


def test_duplicate_registration_is_refused():
    @probe("SCP-02-N")
    def _first(ctx: ProbeContext) -> ProbeResult:  # pragma: no cover
        return ProbeResult(outcome=ObservedOutcome.from_success())

    with pytest.raises(AdversarialHarnessError, match="already registered"):

        @probe("SCP-02-N")
        def _second(ctx: ProbeContext) -> ProbeResult:  # pragma: no cover
            return ProbeResult(outcome=ObservedOutcome.from_success())


def test_unregistered_case_has_no_probe():
    assert get_probe("SCP-06-N") is None


def test_no_probe_modules_are_shipped_yet():
    """The scaffold ships no probes: every live case is an open gap.

    This test is the honest statement of current status. When the first probe
    module lands, update it to assert the expected module names rather than
    deleting it.
    """
    assert discover_probe_modules() == ()


def test_probe_modules_are_discovered_by_filename(tmp_path: Path):
    import cases.probes as probes_pkg

    names = [path.stem for path in Path(probes_pkg.__file__).parent.glob("probe_*.py")]
    assert sorted(names) == list(discover_probe_modules())


# ---------------------------------------------------------------------------
# end-to-end wiring with a fake probe (still no AWS)
# ---------------------------------------------------------------------------


def fake_denial_outcome():
    class FakeClientError(Exception):
        def __init__(self):
            super().__init__("explicit deny in a service control policy")
            self.response = {
                "Error": {
                    "Code": "AccessDeniedException",
                    "Message": (
                        "User is not authorized: explicit deny in a service "
                        "control policy"
                    ),
                },
                "ResponseMetadata": {
                    "HTTPStatusCode": 403,
                    "RequestId": "req-42",
                    "HTTPHeaders": {},
                },
            }

    return ObservedOutcome.from_client_error(FakeClientError())


def test_a_fake_probe_pair_produces_a_valid_evidence_record(tmp_path: Path):
    """Exercises the whole path: probe -> assertion -> sanitized evidence."""
    resolved = resolved_manifest()
    ledger = TwinLedger()
    writer = EvidenceWriter.for_run(tmp_path, resolved)

    positive = case_by_id("SCP-01-P")
    positive_outcome = ObservedOutcome.from_success(request_id="req-41")
    assert_case(
        positive, positive_outcome, ledger=ledger, extras={"test_id": "node::SCP-01-P"}
    )
    writer.record(
        build_record(
            case=positive,
            test_id="node::SCP-01-P",
            principal=resolved.principal(positive.principal_ref),
            outcome=positive_outcome,
            verdict="pass",
            region="us-east-1",
            manifest_sha=resolved.manifest_sha,
            live_mode=True,
            audit_evidence=(
                AuditEvidence(source=AuditSource.CLOUDTRAIL, locator="event-41"),
            ),
            commit=COMMIT,
        )
    )

    negative = case_by_id("SCP-01-N")
    negative_outcome = fake_denial_outcome()
    twin_test_id = assert_case(negative, negative_outcome, ledger=ledger)
    record = writer.record(
        build_record(
            case=negative,
            test_id="node::SCP-01-N",
            principal=resolved.principal(negative.principal_ref),
            outcome=negative_outcome,
            verdict="pass",
            region="us-east-1",
            manifest_sha=resolved.manifest_sha,
            live_mode=True,
            audit_evidence=(
                AuditEvidence(
                    source=AuditSource.CLOUDTRAIL,
                    locator="event-42",
                    matched_fields={"errorCode": "AccessDeniedException"},
                ),
            ),
            positive_twin_test_id=twin_test_id,
            commit=COMMIT,
        )
    )

    assert record.outcome_class == "AUTHORIZATION_DENIAL"
    assert record.positive_twin_test_id == "node::SCP-01-P"
    paths = writer.flush()
    assert paths is not None
    assert "333333333333" not in paths[0].read_text(encoding="utf-8")


def test_a_control_removal_fails_the_case_end_to_end():
    """If the SCP is detached, the attack succeeds — and the case must fail."""
    ledger = TwinLedger()
    positive = case_by_id("SCP-01-P")
    assert_case(
        positive,
        ObservedOutcome.from_success(request_id="req-41"),
        ledger=ledger,
        extras={"test_id": "node::SCP-01-P"},
    )
    with pytest.raises(AssertionError, match="SUCCEEDED"):
        assert_case(
            case_by_id("SCP-01-N"),
            ObservedOutcome.from_success(request_id="req-42"),
            ledger=ledger,
        )


def test_gate_skips_every_live_case_without_live_mode():
    status = live_mode_status({}, identity_probe=lambda key, expected: expected)
    for case in CATALOG:
        assert gate_for_case(case, status).action is GateAction.SKIP


def test_gate_errors_for_every_live_case_when_live_mode_is_broken():
    status = live_mode_status(
        {ENV_LIVE: "1", ENV_MANIFEST: str(EXAMPLE_MANIFEST)},
        identity_probe=lambda key, expected: expected,
    )
    assert not status.available
    for case in CATALOG:
        assert gate_for_case(case, status).action is GateAction.ERROR


def test_probe_context_exposes_role_arns_without_leaking_into_evidence():
    resolved = resolved_manifest()
    context = ProbeContext(
        case=case_by_id("SCP-01-N"), resolved=resolved, region="us-east-1"
    )
    arn = context.role_arn("workstream.agent_runtime")
    assert arn.endswith(":role/AgenticAI-Workstream-AgentRuntime")
    assert "333333333333" in arn  # probes may see it; evidence never records it
    principal = resolved.principal("workstream.agent_runtime")
    assert "333333333333" not in str(principal.to_dict())


def test_probe_context_reports_an_unresolved_account():
    resolved = load_manifest(EXAMPLE_MANIFEST).resolve({})
    context = ProbeContext(
        case=case_by_id("SCP-01-N"), resolved=resolved, region="us-east-1"
    )
    with pytest.raises(AdversarialHarnessError, match="not resolved"):
        context.account_id("workstream")


def test_probe_result_defaults_are_inert():
    result = ProbeResult(outcome=ObservedOutcome.from_success())
    assert result.audit_evidence == ()
    assert result.extras == {}
    assert result.notes == ""


def test_every_catalogued_negative_expectation_has_a_dispatch_path():
    """assert_case must handle every expectation the catalog uses."""
    used = {case.expectation for case in CATALOG}
    handled = {
        Expectation.ALLOW,
        Expectation.DENY,
        Expectation.AUTHENTICATION_DENIED,
        Expectation.RATE_LIMITED,
        Expectation.GUARDRAIL_BLOCKED,
        Expectation.NOT_EXPOSED,
        Expectation.ABSENT,
        Expectation.ROLLED_BACK,
        Expectation.DETECTED,
    }
    assert used <= handled
