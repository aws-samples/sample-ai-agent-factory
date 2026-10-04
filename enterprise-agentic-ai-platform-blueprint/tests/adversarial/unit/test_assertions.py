"""Assertion helpers: only genuine control evidence is accepted.

These tests are the "test the test" layer. Each one asserts that a *plausible
but invalid* proof is rejected, which is what stops the live suite from going
green against a broken or unprotected deployment.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import pytest

from harness.assertions import (
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
from harness.catalog import (
    AdversarialCase,
    AuditSource,
    Domain,
    Expectation,
    Severity,
    case_by_id,
)
from harness.errors import InvalidDenialProof, PositiveTwinMissing
from harness.outcome import ObservedOutcome

pytestmark = pytest.mark.adversarial


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


def denial(code="AccessDeniedException", message="", status=403, request_id="req-1"):
    return ObservedOutcome.from_client_error(
        FakeClientError(code, message, status, request_id)
    )


# ---------------------------------------------------------------------------
# positive twin
# ---------------------------------------------------------------------------


def test_allowed_accepts_a_successful_call():
    assert_allowed(ObservedOutcome.from_success(request_id="req-1"))


def test_allowed_rejects_a_failure():
    with pytest.raises(AssertionError, match="authorized call failed"):
        assert_allowed(denial())


def test_allowed_rejects_a_guardrail_intervention_on_the_baseline():
    outcome = ObservedOutcome.from_guardrail_trace(
        stop_reason="guardrail_intervened", request_id="req-1"
    )
    with pytest.raises(AssertionError, match="baseline is not clean"):
        assert_allowed(outcome)


def test_allowed_requires_a_correlation_id_by_default():
    with pytest.raises(InvalidDenialProof, match="requestId or trace id"):
        assert_allowed(ObservedOutcome.from_success(request_id=None))


# ---------------------------------------------------------------------------
# authorization denial
# ---------------------------------------------------------------------------


def test_authorization_denial_accepts_the_named_code():
    assert_authorization_denied(
        denial("AccessDeniedException", "explicit deny in a service control policy"),
        expected_codes=("AccessDeniedException",),
        required_message_substrings=("service control policy",),
    )


def test_authorization_denial_rejects_success():
    """Removing the control must fail the case, not pass it."""
    with pytest.raises(InvalidDenialProof, match="SUCCEEDED"):
        assert_authorization_denied(
            ObservedOutcome.from_success(request_id="req-1"),
            expected_codes=("AccessDeniedException",),
        )


@pytest.mark.parametrize(
    ("code", "status", "fragment"),
    [
        ("ResourceNotFoundException", 404, "missing resource"),
        ("ValidationException", 400, "malformed"),
        ("InternalServerError", 500, "service fault"),
        ("ServiceUnavailable", 503, "service fault"),
        ("RequestTimeout", None, "outcome unknown"),
        ("EndpointConnectionError", None, "never reached the service"),
        ("ExpiredToken", 403, "credentials"),
        ("ThrottlingException", 429, "not AUTHORIZATION_DENIAL"),
    ],
)
def test_authorization_denial_rejects_non_authorization_failures(code, status, fragment):
    with pytest.raises(InvalidDenialProof) as excinfo:
        assert_authorization_denied(
            denial(code, status=status), expected_codes=("AccessDeniedException",)
        )
    assert fragment in str(excinfo.value)


def test_authorization_denial_rejects_a_bare_nonzero_exit():
    with pytest.raises(InvalidDenialProof, match="UNSPECIFIED_FAILURE"):
        assert_authorization_denied(
            ObservedOutcome.from_exit(1, stderr="cdk deploy failed"),
            expected_codes=("AccessDenied",),
        )


def test_authorization_denial_requires_expected_codes():
    with pytest.raises(InvalidDenialProof, match="must name the exact code"):
        assert_authorization_denied(denial(), expected_codes=())


def test_authorization_denial_refuses_forbidden_expected_codes():
    with pytest.raises(InvalidDenialProof, match="cannot be accepted"):
        assert_authorization_denied(
            denial("ResourceNotFoundException", status=404),
            expected_codes=("ResourceNotFoundException",),
        )


def test_authorization_denial_rejects_an_unexpected_code():
    with pytest.raises(InvalidDenialProof, match="is not one of the expected"):
        assert_authorization_denied(
            denial("UnauthorizedOperation"), expected_codes=("AccessDeniedException",)
        )


def test_authorization_denial_rejects_an_unexpected_status():
    with pytest.raises(InvalidDenialProof, match="not one of the expected statuses"):
        assert_authorization_denied(
            denial("AccessDeniedException", status=400),
            expected_codes=("AccessDeniedException",),
            expected_http=(403,),
        )


def test_authorization_denial_requires_the_message_evidence():
    with pytest.raises(InvalidDenialProof, match="required evidence"):
        assert_authorization_denied(
            denial("AccessDeniedException", "not authorized to perform"),
            expected_codes=("AccessDeniedException",),
            required_message_substrings=("service control policy",),
        )


def test_authorization_denial_requires_a_request_id():
    with pytest.raises(InvalidDenialProof, match="requestId or trace id"):
        assert_authorization_denied(
            denial(request_id=None), expected_codes=("AccessDeniedException",)
        )


# ---------------------------------------------------------------------------
# authentication, rate limit, guardrail
# ---------------------------------------------------------------------------


def test_authentication_denial_accepts_401():
    assert_authentication_denied(
        denial("Unauthorized", status=401), expected_codes=("Unauthorized",)
    )


def test_authentication_denial_rejects_403():
    with pytest.raises(InvalidDenialProof, match="not AUTHENTICATION_DENIAL"):
        assert_authentication_denied(
            denial("AccessDeniedException", status=403),
            expected_codes=("Unauthorized",),
        )


def test_rate_limit_accepts_a_throttle():
    assert_rate_limited(
        denial("ThrottlingException", status=429),
        expected_codes=("ThrottlingException",),
    )


def test_rate_limit_rejects_an_authorization_denial():
    """A 403 means the request never got as far as the limiter."""
    with pytest.raises(InvalidDenialProof, match="not RATE_LIMIT"):
        assert_rate_limited(
            denial("AccessDeniedException", status=403),
            expected_codes=("ThrottlingException",),
        )


def test_rate_limit_rejects_non_throttle_expected_codes():
    with pytest.raises(InvalidDenialProof, match="not throttle evidence"):
        assert_rate_limited(
            denial("ThrottlingException", status=429),
            expected_codes=("AccessDeniedException",),
        )


def test_rate_limit_rejects_a_5xx():
    with pytest.raises(InvalidDenialProof, match="service fault"):
        assert_rate_limited(
            denial("InternalServerError", status=503),
            expected_codes=("ThrottlingException",),
        )


def test_guardrail_blocked_requires_an_intervention_in_the_trace():
    assert_guardrail_blocked(
        ObservedOutcome.from_guardrail_trace(
            stop_reason="guardrail_intervened", request_id="req-1"
        )
    )


def test_guardrail_blocked_rejects_a_plain_error():
    with pytest.raises(InvalidDenialProof, match="no guardrail intervention"):
        assert_guardrail_blocked(denial("InternalServerError", status=500))


def test_guardrail_blocked_rejects_an_unblocked_completion():
    with pytest.raises(InvalidDenialProof, match="no guardrail intervention"):
        assert_guardrail_blocked(
            ObservedOutcome.from_guardrail_trace(
                stop_reason="end_turn", request_id="req-1"
            )
        )


# ---------------------------------------------------------------------------
# absence (teardown)
# ---------------------------------------------------------------------------


def test_absent_accepts_not_found():
    assert_absent(
        denial("ResourceNotFoundException", status=404),
        expected_codes=("ResourceNotFoundException",),
    )


def test_absent_rejects_a_resource_that_still_exists():
    with pytest.raises(InvalidDenialProof, match="still exists"):
        assert_absent(
            ObservedOutcome.from_success(request_id="req-1"),
            expected_codes=("ResourceNotFoundException",),
        )


def test_absent_rejects_an_access_denied():
    """Denied-describe hides residue; it does not prove deletion."""
    with pytest.raises(InvalidDenialProof, match="rather than a not-found"):
        assert_absent(
            denial("AccessDeniedException", status=403),
            expected_codes=("ResourceNotFoundException",),
        )


def test_absent_enforces_the_message_substring():
    with pytest.raises(InvalidDenialProof, match="required evidence"):
        assert_absent(
            denial("ValidationError", "Parameter StackName is invalid", status=400),
            expected_codes=("ValidationError",),
            required_message_substrings=("does not exist",),
        )


# ---------------------------------------------------------------------------
# tool exposure
# ---------------------------------------------------------------------------


def test_not_exposed_passes_when_the_forbidden_tool_is_filtered():
    assert_not_exposed(
        ["tool.allowed.a", "tool.allowed.b"],
        ["tool.forbidden.x"],
        expected_tools=["tool.allowed.a"],
    )


def test_not_exposed_fails_when_the_forbidden_tool_is_listed():
    with pytest.raises(AssertionError, match="exposed forbidden tools"):
        assert_not_exposed(
            ["tool.allowed.a", "tool.forbidden.x"],
            ["tool.forbidden.x"],
            expected_tools=["tool.allowed.a"],
        )


def test_not_exposed_rejects_an_empty_listing():
    """An empty tools/list would trivially 'contain no forbidden tool'."""
    with pytest.raises(InvalidDenialProof, match="returned nothing"):
        assert_not_exposed([], ["tool.forbidden.x"])


def test_not_exposed_rejects_a_listing_missing_its_own_baseline():
    with pytest.raises(InvalidDenialProof, match="missing the tools"):
        assert_not_exposed(
            ["tool.other"], ["tool.forbidden.x"], expected_tools=["tool.allowed.a"]
        )


# ---------------------------------------------------------------------------
# rollback and detection
# ---------------------------------------------------------------------------


def test_rolled_back_requires_event_and_reverted_state():
    assert_rolled_back(
        rollback_event_observed=True,
        active_version="7",
        previous_version="7",
        active_manifest_sha="a" * 64,
        previous_manifest_sha="a" * 64,
    )


def test_rolled_back_rejects_a_missing_rollback_event():
    with pytest.raises(InvalidDenialProof, match="no rollback event"):
        assert_rolled_back(
            rollback_event_observed=False,
            active_version="7",
            previous_version="7",
            active_manifest_sha="a" * 64,
            previous_manifest_sha="a" * 64,
        )


def test_rolled_back_rejects_an_unchanged_manifest():
    with pytest.raises(AssertionError, match="was not reverted"):
        assert_rolled_back(
            rollback_event_observed=True,
            active_version="7",
            previous_version="7",
            active_manifest_sha="b" * 64,
            previous_manifest_sha="a" * 64,
        )


def test_detected_requires_a_quiet_baseline():
    with pytest.raises(InvalidDenialProof, match="already firing"):
        assert_detected(
            signal_observed=True,
            signal_name="ErrorRateAlarm",
            baseline_signal_observed=True,
        )


def test_detected_fails_when_the_signal_never_fires():
    with pytest.raises(AssertionError, match="did not fire"):
        assert_detected(
            signal_observed=False,
            signal_name="ErrorRateAlarm",
            baseline_signal_observed=False,
        )


# ---------------------------------------------------------------------------
# twin ledger
# ---------------------------------------------------------------------------


def test_negative_case_without_a_passing_twin_is_rejected():
    ledger = TwinLedger()
    case = case_by_id("SCP-01-N")
    with pytest.raises(PositiveTwinMissing, match="has not passed in this run"):
        assert_case(case, denial(), ledger=ledger)


def test_negative_case_accepts_evidence_once_its_twin_passed():
    ledger = TwinLedger()
    positive = case_by_id("SCP-01-P")
    assert_case(
        positive,
        ObservedOutcome.from_success(request_id="req-p"),
        ledger=ledger,
        extras={"test_id": "nodeid::SCP-01-P"},
    )
    negative = case_by_id("SCP-01-N")
    twin_test_id = assert_case(
        negative,
        denial("AccessDeniedException", "explicit deny in a service control policy"),
        ledger=ledger,
    )
    assert twin_test_id == "nodeid::SCP-01-P"


def test_case_without_a_declared_twin_is_rejected():
    ledger = TwinLedger()
    orphan = AdversarialCase(
        case_id="ORPHAN-N",
        domain=Domain.SCP,
        title="denial with no twin",
        expectation=Expectation.DENY,
        severity=Severity.CRITICAL,
        principal_ref="workstream.agent_runtime",
        target="bedrock:InvokeModel",
        rationale="fixture",
        control_refs=("fixture",),
        expected_error_codes=("AccessDeniedException",),
        expected_http_status=(403,),
        audit_sources=(AuditSource.CLOUDTRAIL,),
    )
    with pytest.raises(PositiveTwinMissing, match="declares no"):
        assert_case(orphan, denial(), ledger=ledger)


def test_assert_case_dispatches_rate_limit_expectation():
    ledger = TwinLedger()
    assert_case(
        case_by_id("RATE-01-P"),
        ObservedOutcome.from_success(request_id="req-p"),
        ledger=ledger,
        extras={"test_id": "nodeid::RATE-01-P"},
    )
    assert_case(
        case_by_id("RATE-01-N"),
        denial("ThrottlingException", status=429),
        ledger=ledger,
    )


def test_assert_case_dispatches_not_exposed_expectation():
    ledger = TwinLedger()
    assert_case(
        case_by_id("TG-01-P"),
        ObservedOutcome.from_success(request_id="req-p"),
        ledger=ledger,
        extras={"test_id": "nodeid::TG-01-P"},
    )
    assert_case(
        case_by_id("TG-02-N"),
        ObservedOutcome.from_success(request_id="req-n"),
        ledger=ledger,
        extras={
            "listed_tools": ["tool.allowed"],
            "forbidden_tools": ["tool.unexposed"],
            "expected_tools": ["tool.allowed"],
        },
    )
