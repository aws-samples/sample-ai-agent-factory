"""Outcome classification: what may and may not count as evidence.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import pytest

from harness.outcome import (
    FORBIDDEN_PROOF_CODES,
    NON_AUTHORIZATION_CLASSES,
    ObservedOutcome,
    OutcomeClass,
    rejection_reason,
)

pytestmark = pytest.mark.adversarial


class FakeClientError(Exception):
    """Shaped like ``botocore.exceptions.ClientError`` without importing it."""

    def __init__(self, code, message="", status=None, request_id="req-1234"):
        super().__init__(message)
        self.response = {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {
                "HTTPStatusCode": status,
                "RequestId": request_id,
                "HTTPHeaders": {},
            },
        }


def outcome_for(code, status=None, message="", request_id="req-1234"):
    return ObservedOutcome.from_client_error(
        FakeClientError(code, message, status, request_id)
    )


def test_success_classifies_as_success():
    assert ObservedOutcome.from_success().outcome_class is OutcomeClass.SUCCESS


@pytest.mark.parametrize(
    "code",
    ["AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "Forbidden"],
)
def test_denial_codes_classify_as_authorization_denial(code):
    assert outcome_for(code, 403).outcome_class is OutcomeClass.AUTHORIZATION_DENIAL


@pytest.mark.parametrize(
    ("code", "status", "expected"),
    [
        ("ResourceNotFoundException", 404, OutcomeClass.NOT_FOUND),
        ("ValidationException", 400, OutcomeClass.VALIDATION),
        ("InternalServerError", 500, OutcomeClass.SERVER_ERROR),
        ("ServiceUnavailable", 503, OutcomeClass.SERVER_ERROR),
        ("RequestTimeout", None, OutcomeClass.TIMEOUT),
        ("EndpointConnectionError", None, OutcomeClass.NETWORK),
        ("ExpiredToken", 403, OutcomeClass.CREDENTIAL),
        ("UnrecognizedClientException", 403, OutcomeClass.CREDENTIAL),
        ("ThrottlingException", 429, OutcomeClass.RATE_LIMIT),
        ("ConflictException", 409, OutcomeClass.CONFLICT),
    ],
)
def test_non_authorization_failures_are_classified_apart(code, status, expected):
    assert outcome_for(code, status).outcome_class is expected


def test_expired_credentials_are_never_authorization_evidence():
    """A denial-looking 403 from bad credentials must not read as a control."""
    observed = outcome_for("InvalidClientTokenId", 403)
    assert observed.outcome_class is OutcomeClass.CREDENTIAL
    assert observed.outcome_class in NON_AUTHORIZATION_CLASSES


def test_five_hundred_beats_a_denial_code():
    """A 5xx carrying AccessDenied is still a server error."""
    assert outcome_for("AccessDeniedException", 500).outcome_class is (
        OutcomeClass.SERVER_ERROR
    )


def test_401_classifies_as_authentication_not_authorization():
    assert outcome_for("Unauthorized", 401).outcome_class is (
        OutcomeClass.AUTHENTICATION_DENIAL
    )


def test_bare_nonzero_exit_is_unspecified():
    observed = ObservedOutcome.from_exit(1, stderr="command failed")
    assert observed.outcome_class is OutcomeClass.UNSPECIFIED_FAILURE
    assert observed.exit_code == 1


def test_exit_with_parsed_error_code_can_classify():
    observed = ObservedOutcome.from_exit(
        255, stderr="An error occurred (AccessDenied)", error_code="AccessDenied"
    )
    assert observed.outcome_class is OutcomeClass.AUTHORIZATION_DENIAL


def test_unknown_exception_shape_degrades_to_unspecified():
    observed = ObservedOutcome.from_client_error(RuntimeError("no response attr"))
    assert observed.outcome_class is OutcomeClass.UNSPECIFIED_FAILURE


def test_guardrail_intervention_is_its_own_class():
    observed = ObservedOutcome.from_guardrail_trace(
        stop_reason="guardrail_intervened", request_id="req-9"
    )
    assert observed.outcome_class is OutcomeClass.GUARDRAIL_INTERVENTION
    assert observed.succeeded is True


def test_guardrail_pass_through_is_plain_success():
    observed = ObservedOutcome.from_guardrail_trace(stop_reason="end_turn")
    assert observed.outcome_class is OutcomeClass.SUCCESS


def test_request_id_is_captured_from_response_metadata():
    assert outcome_for("AccessDenied", 403, request_id="req-abc").request_id == "req-abc"


def test_http_403_without_a_known_code_is_still_a_denial_shape():
    observed = ObservedOutcome.from_http(403, error_code="SomethingNovel")
    assert observed.outcome_class is OutcomeClass.AUTHORIZATION_DENIAL


def test_every_rejected_class_has_a_stated_reason():
    for outcome_class in NON_AUTHORIZATION_CLASSES:
        assert rejection_reason(outcome_class)
        assert rejection_reason(outcome_class) != outcome_class.value


def test_forbidden_proof_codes_cover_the_named_families():
    for code in (
        "ResourceNotFoundException",
        "ValidationException",
        "InternalServerError",
        "RequestTimeout",
        "EndpointConnectionError",
        "ExpiredToken",
        "ConflictException",
    ):
        assert code in FORBIDDEN_PROOF_CODES


def test_authorization_codes_are_not_in_the_forbidden_set():
    for code in ("AccessDenied", "AccessDeniedException", "Forbidden"):
        assert code not in FORBIDDEN_PROOF_CODES


def test_to_dict_round_trips_the_classification():
    payload = outcome_for("AccessDenied", 403).to_dict()
    assert payload["outcomeClass"] == "AUTHORIZATION_DENIAL"
    assert payload["httpStatus"] == 403
