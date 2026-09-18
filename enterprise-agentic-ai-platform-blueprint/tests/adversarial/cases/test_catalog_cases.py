"""The single live-case runner.

Every catalogued case is parametrized here, positives first so that a negative
case can find its twin in the ledger. The runner:

1. asks the gate what to do (run / skip / error);
2. finds the registered probe, and **fails** if a catalogued control has none
   — an unimplemented control check is an open gap, not a tolerated condition;
3. applies the assertion the case's expectation calls for, via ``assert_case``;
4. records a sanitized evidence record either way, then re-raises on failure.

Because assertions live in the harness and not here, no individual case can
weaken a rule.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import pytest

from harness import (
    CATALOG,
    AdversarialCase,
    EvidenceWriter,
    GateAction,
    LiveModeStatus,
    TwinLedger,
    assert_case,
    build_record,
    gate_for_case,
)
from harness.catalog import execution_order
from harness.evidence import utc_now

from cases.probes import ProbeContext, get_probe

pytestmark = [pytest.mark.adversarial, pytest.mark.adversarial_live]


@pytest.mark.parametrize(
    "case", execution_order(CATALOG), ids=lambda case: case.case_id
)
def test_adversarial_case(
    case: AdversarialCase,
    live_mode: LiveModeStatus,
    twin_ledger: TwinLedger,
    evidence_writer: EvidenceWriter,
    region: str,
    loaded_probes: tuple[str, ...],
    request: pytest.FixtureRequest,
) -> None:
    decision = gate_for_case(case, live_mode)
    if decision.action is GateAction.ERROR:
        pytest.fail(decision.reason, pytrace=False)
    if decision.action is GateAction.SKIP:
        pytest.skip(decision.reason)

    resolved = live_mode.require()
    probe = get_probe(case.case_id)
    if probe is None:
        pytest.fail(
            f"{case.case_id} is catalogued but has no live probe registered "
            f"(discovered probe modules: {list(loaded_probes) or 'none'}). "
            "Register one in tests/adversarial/cases/probes/probe_*.py. An "
            "unimplemented control check is an open gap and is reported as a "
            "failure, never a skip.",
            pytrace=False,
        )

    context = ProbeContext(case=case, resolved=resolved, region=region)
    started_at = utc_now()
    result = probe(context)
    finished_at = utc_now()

    principal = resolved.principal(case.principal_ref)
    extras = dict(result.extras)
    extras.setdefault("test_id", request.node.nodeid)

    verdict = "pass"
    twin_test_id = None
    failure: BaseException | None = None
    try:
        twin_test_id = assert_case(
            case, result.outcome, ledger=twin_ledger, extras=extras
        )
    except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
        verdict = "fail"
        failure = exc

    record = build_record(
        case=case,
        test_id=request.node.nodeid,
        principal=principal,
        outcome=result.outcome,
        verdict=verdict,
        region=region,
        manifest_sha=resolved.manifest_sha,
        live_mode=True,
        audit_evidence=result.audit_evidence,
        positive_twin_test_id=twin_test_id,
        started_at=started_at,
        finished_at=finished_at,
        notes=result.notes,
    )
    if verdict == "pass":
        evidence_writer.record(record)
    else:
        # A failing record is still retained, but it must never be blocked from
        # being written by its own incompleteness.
        try:
            evidence_writer.record(record)
        except Exception:  # noqa: BLE001 - the assertion failure is the signal
            pass

    if failure is not None:
        raise failure
