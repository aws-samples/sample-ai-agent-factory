"""Observed-outcome capture and classification.

The single most important job of this module is to distinguish *a control
denied me* from *something else went wrong*. A negative adversarial test is
only evidence of a control if the failure it observed is an authorization
decision. Anything else — a validation error, a missing resource, a 5xx, a
timeout, bad credentials, or a bare nonzero exit code — is a defect in the
test, not proof of a control.

Nothing here imports botocore. ``ObservedOutcome.from_client_error`` is
duck-typed on the ``.response`` mapping that ``botocore.exceptions.ClientError``
exposes, so the harness and its unit tests run with no AWS SDK installed.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class OutcomeClass(str, Enum):
    """How an observed outcome may be used as evidence."""

    SUCCESS = "SUCCESS"
    AUTHORIZATION_DENIAL = "AUTHORIZATION_DENIAL"
    AUTHENTICATION_DENIAL = "AUTHENTICATION_DENIAL"
    GUARDRAIL_INTERVENTION = "GUARDRAIL_INTERVENTION"
    RATE_LIMIT = "RATE_LIMIT"
    NOT_FOUND = "NOT_FOUND"
    VALIDATION = "VALIDATION"
    SERVER_ERROR = "SERVER_ERROR"
    TIMEOUT = "TIMEOUT"
    NETWORK = "NETWORK"
    CREDENTIAL = "CREDENTIAL"
    CONFLICT = "CONFLICT"
    UNSPECIFIED_FAILURE = "UNSPECIFIED_FAILURE"


#: Classes that may never be accepted as proof that an authorization control
#: fired. Each one has a plausible non-security cause.
NON_AUTHORIZATION_CLASSES: frozenset[OutcomeClass] = frozenset(
    {
        OutcomeClass.NOT_FOUND,
        OutcomeClass.VALIDATION,
        OutcomeClass.SERVER_ERROR,
        OutcomeClass.TIMEOUT,
        OutcomeClass.NETWORK,
        OutcomeClass.CREDENTIAL,
        OutcomeClass.CONFLICT,
        OutcomeClass.UNSPECIFIED_FAILURE,
    }
)

#: Human-readable reason each rejected class is not authorization evidence.
REJECTION_REASONS: Mapping[OutcomeClass, str] = {
    OutcomeClass.NOT_FOUND: (
        "a missing resource proves nothing about authorization — the call may "
        "never have reached a policy decision"
    ),
    OutcomeClass.VALIDATION: (
        "a validation error means the request was malformed, so no "
        "authorization decision was recorded"
    ),
    OutcomeClass.SERVER_ERROR: (
        "a 5xx is a service fault; it is indistinguishable from an outage and "
        "may mask an allow"
    ),
    OutcomeClass.TIMEOUT: (
        "a timeout leaves the outcome unknown; the request may have been "
        "authorized and succeeded server-side"
    ),
    OutcomeClass.NETWORK: (
        "a connection failure never reached the service, so no policy was "
        "evaluated"
    ),
    OutcomeClass.CREDENTIAL: (
        "invalid, missing or expired credentials fail before authorization; "
        "this is a harness misconfiguration, not a control"
    ),
    OutcomeClass.CONFLICT: (
        "a state conflict is unrelated to the authorization decision"
    ),
    OutcomeClass.UNSPECIFIED_FAILURE: (
        "a bare failure (for example a nonzero exit code with no service error "
        "code) carries no authorization semantics"
    ),
    OutcomeClass.AUTHENTICATION_DENIAL: (
        "an authentication failure (401) proves the caller was not identified, "
        "not that an authorization policy denied an identified caller"
    ),
}

_AUTHORIZATION_CODES: frozenset[str] = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "AccessDeniedFault",
        "AuthorizationError",
        "AuthorizationErrorException",
        "Forbidden",
        "NotAuthorized",
        "NotAuthorizedException",
        "PolicyEvaluationDenied",
        "ToolAccessDeniedException",
        "UnauthorizedException",
        "UnauthorizedOperation",
        "WithExplicitDeny",
    }
)

_RATE_LIMIT_CODES: frozenset[str] = frozenset(
    {
        "LimitExceededException",
        "ProvisionedThroughputExceededException",
        "RateLimitExceeded",
        "RequestLimitExceeded",
        "ServiceQuotaExceededException",
        "SlowDown",
        "Throttling",
        "ThrottlingException",
        "TooManyRequestsException",
    }
)

_NOT_FOUND_CODES: frozenset[str] = frozenset(
    {
        "EntityNotFound",
        "NoSuchBucket",
        "NoSuchEntity",
        "NoSuchEntityException",
        "NotFound",
        "NotFoundException",
        "ResourceNotFound",
        "ResourceNotFoundException",
        "StackNotFoundException",
    }
)

_VALIDATION_CODES: frozenset[str] = frozenset(
    {
        "InvalidInput",
        "InvalidInputException",
        "InvalidParameterCombination",
        "InvalidParameterException",
        "InvalidParameterValue",
        "InvalidRequestException",
        "MalformedPolicyDocument",
        "MalformedPolicyDocumentException",
        "MissingParameter",
        "SerializationException",
        "ValidationError",
        "ValidationException",
    }
)

_SERVER_CODES: frozenset[str] = frozenset(
    {
        "InternalError",
        "InternalFailure",
        "InternalServerError",
        "InternalServerException",
        "ServiceFailure",
        "ServiceUnavailable",
        "ServiceUnavailableException",
    }
)

_TIMEOUT_CODES: frozenset[str] = frozenset(
    {
        "ConnectTimeoutError",
        "ModelTimeoutException",
        "ReadTimeoutError",
        "RequestTimeout",
        "RequestTimeoutException",
        "TimeoutError",
    }
)

_NETWORK_CODES: frozenset[str] = frozenset(
    {
        "ConnectionClosedError",
        "ConnectionError",
        "DNSLookupError",
        "EndpointConnectionError",
        "SSLError",
    }
)

_CREDENTIAL_CODES: frozenset[str] = frozenset(
    {
        "CredentialsNotFound",
        "ExpiredToken",
        "ExpiredTokenException",
        "IncompleteSignature",
        "InvalidClientTokenId",
        "MissingAuthenticationToken",
        "MissingAuthenticationTokenException",
        "NoCredentialsError",
        "SignatureDoesNotMatch",
        "TokenRefreshRequired",
        "UnrecognizedClientException",
    }
)

_AUTHENTICATION_CODES: frozenset[str] = frozenset(
    {
        "AuthenticationFailed",
        "InvalidTokenException",
        "InvalidSignatureException",
        "JwtVerificationFailed",
        "Unauthorized",
        "UnauthorizedRequest",
    }
)

_CONFLICT_CODES: frozenset[str] = frozenset(
    {
        "ConcurrentModificationException",
        "ConflictException",
        "ResourceInUseException",
    }
)

#: Codes that must never appear in a case's expected-denial set.
FORBIDDEN_PROOF_CODES: frozenset[str] = (
    _NOT_FOUND_CODES
    | _VALIDATION_CODES
    | _SERVER_CODES
    | _TIMEOUT_CODES
    | _NETWORK_CODES
    | _CREDENTIAL_CODES
    | _CONFLICT_CODES
)


@dataclass(frozen=True)
class ObservedOutcome:
    """A single observed call result, normalized for classification.

    ``error_message`` is kept raw here; it is sanitized on the way into an
    evidence record, never mutated at capture time (so that assertions can
    match on required substrings such as "service control policy").
    """

    succeeded: bool
    error_code: str | None = None
    error_message: str | None = None
    http_status: int | None = None
    request_id: str | None = None
    trace_id: str | None = None
    exit_code: int | None = None
    raw_kind: str | None = None
    guardrail_intervened: bool = False
    payload: Mapping[str, Any] = field(default_factory=dict)

    # -- constructors ------------------------------------------------------

    @classmethod
    def from_success(
        cls,
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
        http_status: int | None = 200,
        payload: Mapping[str, Any] | None = None,
        guardrail_intervened: bool = False,
    ) -> "ObservedOutcome":
        return cls(
            succeeded=True,
            http_status=http_status,
            request_id=request_id,
            trace_id=trace_id,
            payload=dict(payload or {}),
            guardrail_intervened=guardrail_intervened,
            raw_kind="success",
        )

    @classmethod
    def from_client_error(cls, exc: Any) -> "ObservedOutcome":
        """Normalize a botocore ``ClientError``-shaped exception.

        Duck-typed on ``exc.response`` so no botocore import is needed. A
        non-conforming exception degrades to ``UNSPECIFIED_FAILURE`` rather
        than pretending to be a denial.
        """
        response = getattr(exc, "response", None)
        if not isinstance(response, Mapping):
            return cls.from_exception(exc)
        error = response.get("Error") or {}
        metadata = response.get("ResponseMetadata") or {}
        return cls(
            succeeded=False,
            error_code=(error.get("Code") or None),
            error_message=(error.get("Message") or None),
            http_status=metadata.get("HTTPStatusCode"),
            request_id=(
                metadata.get("RequestId")
                or response.get("RequestId")
                or metadata.get("RequestID")
            ),
            trace_id=(metadata.get("HTTPHeaders") or {}).get("x-amzn-trace-id"),
            raw_kind=type(exc).__name__,
        )

    @classmethod
    def from_exception(cls, exc: BaseException) -> "ObservedOutcome":
        """Normalize any other exception. Never classified as a denial."""
        return cls(
            succeeded=False,
            error_code=type(exc).__name__,
            error_message=str(exc) or None,
            raw_kind=type(exc).__name__,
        )

    @classmethod
    def from_http(
        cls,
        status: int,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> "ObservedOutcome":
        """Normalize a raw HTTP response (Gateway / MCP endpoints)."""
        return cls(
            succeeded=200 <= status < 300,
            error_code=error_code,
            error_message=error_message,
            http_status=status,
            request_id=request_id,
            trace_id=trace_id,
            payload=dict(payload or {}),
            raw_kind="http",
        )

    @classmethod
    def from_exit(
        cls,
        exit_code: int,
        *,
        stderr: str | None = None,
        error_code: str | None = None,
        request_id: str | None = None,
    ) -> "ObservedOutcome":
        """Normalize a CLI/subprocess result.

        A nonzero exit code on its own classifies as
        ``UNSPECIFIED_FAILURE``. To be usable as denial evidence the caller
        must supply the service ``error_code`` parsed out of ``stderr``.
        """
        return cls(
            succeeded=exit_code == 0,
            error_code=error_code,
            error_message=stderr,
            exit_code=exit_code,
            request_id=request_id,
            raw_kind="exit",
        )

    @classmethod
    def from_guardrail_trace(
        cls,
        *,
        stop_reason: str,
        request_id: str | None = None,
        trace_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> "ObservedOutcome":
        """A Bedrock response whose guardrail intervened.

        The HTTP call succeeded, so this is *not* an authorization denial; it
        is its own evidence class.
        """
        intervened = stop_reason == "guardrail_intervened"
        return cls(
            succeeded=True,
            http_status=200,
            request_id=request_id,
            trace_id=trace_id,
            guardrail_intervened=intervened,
            payload=dict(payload or {"stopReason": stop_reason}),
            raw_kind="guardrail",
        )

    # -- classification ----------------------------------------------------

    @property
    def outcome_class(self) -> OutcomeClass:
        return classify(self)

    def has_correlation_id(self) -> bool:
        return bool(self.request_id or self.trace_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "succeeded": self.succeeded,
            "outcomeClass": self.outcome_class.value,
            "errorCode": self.error_code,
            "errorMessage": self.error_message,
            "httpStatus": self.http_status,
            "requestId": self.request_id,
            "traceId": self.trace_id,
            "exitCode": self.exit_code,
            "rawKind": self.raw_kind,
            "guardrailIntervened": self.guardrail_intervened,
            "payload": dict(self.payload),
        }


def classify(outcome: ObservedOutcome) -> OutcomeClass:
    """Map an observed outcome onto an evidence class.

    Precedence is chosen so that ambiguity always degrades away from
    "authorization": transport and credential problems win over a denial-ish
    error code, and any 5xx is a server error even if the body claims
    ``AccessDenied``.
    """
    if outcome.succeeded:
        if outcome.guardrail_intervened:
            return OutcomeClass.GUARDRAIL_INTERVENTION
        return OutcomeClass.SUCCESS

    code = (outcome.error_code or "").strip()

    if code in _NETWORK_CODES:
        return OutcomeClass.NETWORK
    if code in _CREDENTIAL_CODES:
        return OutcomeClass.CREDENTIAL
    if code in _TIMEOUT_CODES:
        return OutcomeClass.TIMEOUT
    if code in _AUTHENTICATION_CODES or outcome.http_status == 401:
        return OutcomeClass.AUTHENTICATION_DENIAL
    if outcome.http_status is not None and outcome.http_status >= 500:
        return OutcomeClass.SERVER_ERROR
    if code in _SERVER_CODES:
        return OutcomeClass.SERVER_ERROR
    if code in _RATE_LIMIT_CODES or outcome.http_status == 429:
        return OutcomeClass.RATE_LIMIT
    if code in _AUTHORIZATION_CODES:
        return OutcomeClass.AUTHORIZATION_DENIAL
    if code in _NOT_FOUND_CODES or outcome.http_status == 404:
        return OutcomeClass.NOT_FOUND
    if code in _VALIDATION_CODES or outcome.http_status == 400:
        return OutcomeClass.VALIDATION
    if code in _CONFLICT_CODES or outcome.http_status == 409:
        return OutcomeClass.CONFLICT
    if outcome.http_status == 403:
        # 403 with an unrecognized code is still a forbidden response, but we
        # require the case to name the code, so surface it as a denial and let
        # the assertion layer reject the unexpected code.
        return OutcomeClass.AUTHORIZATION_DENIAL
    return OutcomeClass.UNSPECIFIED_FAILURE


def rejection_reason(outcome_class: OutcomeClass) -> str:
    return REJECTION_REASONS.get(
        outcome_class, f"{outcome_class.value} is not authorization evidence"
    )


__all__ = [
    "FORBIDDEN_PROOF_CODES",
    "NON_AUTHORIZATION_CLASSES",
    "REJECTION_REASONS",
    "ObservedOutcome",
    "OutcomeClass",
    "classify",
    "rejection_reason",
]
