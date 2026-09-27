"""Assertions that only accept genuine control evidence.

The rules enforced here are the point of the whole harness:

1. A denial assertion fails unless the observed outcome classifies as an
   authorization denial. Missing resources, validation errors, 5xx responses,
   timeouts, connection failures, bad credentials and bare nonzero exits are
   all rejected *by name*, with the reason stated.
2. A denial assertion fails unless the case named the exact error code(s) it
   accepts. "Something went wrong" is never proof.
3. A denial assertion fails unless the authorized positive twin passed in the
   same run. Without it, a wholly broken deployment would "prove" every
   control.
4. A successful call can never satisfy a denial assertion — which is what makes
   the suite fail when a control is removed.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .catalog import AdversarialCase, Expectation, RATE_LIMIT_PROOF_CODES
from .errors import InvalidDenialProof, PositiveTwinMissing
from .outcome import (
    FORBIDDEN_PROOF_CODES,
    ObservedOutcome,
    OutcomeClass,
    rejection_reason,
)


@dataclass
class TwinLedger:
    """Records which positive twins have passed in this run.

    A negative case consults the ledger before it is allowed to claim a
    control fired.
    """

    passed: dict[str, str] = field(default_factory=dict)

    def record_pass(self, case_id: str, test_id: str) -> None:
        self.passed[case_id] = test_id

    def twin_test_id(self, case_id: str | None) -> str | None:
        if not case_id:
            return None
        return self.passed.get(case_id)

    def require_twin(self, case: AdversarialCase) -> str:
        """Return the twin's test id, or raise.

        ``PositiveTwinMissing`` subclasses ``AssertionError`` so the negative
        case fails rather than erroring out of the suite.
        """
        if not case.positive_twin:
            raise PositiveTwinMissing(
                f"{case.case_id} asserts a control fired but declares no "
                "authorized positive twin"
            )
        test_id = self.passed.get(case.positive_twin)
        if not test_id:
            raise PositiveTwinMissing(
                f"{case.case_id} cannot be treated as evidence: its positive "
                f"twin {case.positive_twin} has not passed in this run, so the "
                "observed failure may simply mean nothing works"
            )
        return test_id


def _reject(message: str) -> None:
    raise InvalidDenialProof(message)


def _check_expected_codes(case_id: str, expected_codes: Sequence[str]) -> None:
    if not expected_codes:
        _reject(
            f"{case_id}: no expected error code was declared; a denial "
            "assertion must name the exact code it accepts"
        )
    forbidden = sorted(set(expected_codes) & FORBIDDEN_PROOF_CODES)
    if forbidden:
        _reject(
            f"{case_id}: {', '.join(forbidden)} cannot be accepted as "
            "authorization evidence"
        )


def _check_message(
    case_id: str, outcome: ObservedOutcome, substrings: Iterable[str]
) -> None:
    required = [item for item in substrings if item]
    if not required:
        return
    message = (outcome.error_message or "").lower()
    missing = [item for item in required if item.lower() not in message]
    if missing:
        _reject(
            f"{case_id}: denial message did not contain required evidence "
            f"{missing!r}; the failure may have come from a different control "
            "than the one under test"
        )


def _check_correlation(case_id: str, outcome: ObservedOutcome, required: bool) -> None:
    if required and not outcome.has_correlation_id():
        _reject(
            f"{case_id}: no requestId or trace id was captured, so the denial "
            "cannot be correlated with the provider's own audit record"
        )


def _check_status(
    case_id: str, outcome: ObservedOutcome, expected: Sequence[int]
) -> None:
    if not expected:
        return
    if outcome.http_status is None:
        _reject(
            f"{case_id}: no HTTP status was captured but the case expects one "
            f"of {list(expected)}"
        )
    elif outcome.http_status not in expected:
        _reject(
            f"{case_id}: HTTP {outcome.http_status} is not one of the expected "
            f"statuses {list(expected)}"
        )


def _reject_success(case_id: str, outcome: ObservedOutcome, what: str) -> None:
    if outcome.succeeded and not outcome.guardrail_intervened:
        _reject(
            f"{case_id}: the call SUCCEEDED where {what} was expected — the "
            "control is absent, disabled or misconfigured"
        )


def _reject_wrong_class(
    case_id: str, outcome: ObservedOutcome, wanted: OutcomeClass
) -> None:
    observed = outcome.outcome_class
    if observed is wanted:
        return
    _reject(
        f"{case_id}: observed {observed.value} "
        f"(code={outcome.error_code!r}, http={outcome.http_status}, "
        f"exit={outcome.exit_code}) is not {wanted.value} evidence: "
        + rejection_reason(observed)
    )


# ---------------------------------------------------------------------------
# public assertions
# ---------------------------------------------------------------------------


def assert_allowed(
    outcome: ObservedOutcome,
    *,
    case_id: str = "positive",
    require_correlation_id: bool = True,
) -> None:
    """The authorized positive twin must actually succeed."""
    if not outcome.succeeded:
        raise AssertionError(
            f"{case_id}: authorized call failed with "
            f"{outcome.outcome_class.value} (code={outcome.error_code!r}, "
            f"http={outcome.http_status}). The positive twin must pass before "
            "any denial in this domain can be treated as evidence."
        )
    if outcome.guardrail_intervened:
        raise AssertionError(
            f"{case_id}: a guardrail intervened on the authorized positive "
            "twin; the baseline is not clean"
        )
    _check_correlation(case_id, outcome, require_correlation_id)


def assert_authorization_denied(
    outcome: ObservedOutcome,
    *,
    expected_codes: Sequence[str],
    case_id: str = "negative",
    expected_http: Sequence[int] = (403,),
    required_message_substrings: Iterable[str] = (),
    require_correlation_id: bool = True,
) -> None:
    """Accept only an explicit authorization denial with a named code."""
    _check_expected_codes(case_id, expected_codes)
    _reject_success(case_id, outcome, "an authorization denial")
    _reject_wrong_class(case_id, outcome, OutcomeClass.AUTHORIZATION_DENIAL)
    if (outcome.error_code or "") not in set(expected_codes):
        _reject(
            f"{case_id}: error code {outcome.error_code!r} is not one of the "
            f"expected denial codes {list(expected_codes)}"
        )
    _check_status(case_id, outcome, expected_http)
    _check_message(case_id, outcome, required_message_substrings)
    _check_correlation(case_id, outcome, require_correlation_id)


def assert_authentication_denied(
    outcome: ObservedOutcome,
    *,
    expected_codes: Sequence[str],
    case_id: str = "negative",
    expected_http: Sequence[int] = (401,),
    require_correlation_id: bool = True,
) -> None:
    """Accept only an explicit authentication refusal (401)."""
    _check_expected_codes(case_id, expected_codes)
    _reject_success(case_id, outcome, "an authentication refusal")
    _reject_wrong_class(case_id, outcome, OutcomeClass.AUTHENTICATION_DENIAL)
    if (outcome.error_code or "") not in set(expected_codes):
        _reject(
            f"{case_id}: error code {outcome.error_code!r} is not one of the "
            f"expected authentication codes {list(expected_codes)}"
        )
    _check_status(case_id, outcome, expected_http)
    _check_correlation(case_id, outcome, require_correlation_id)


def assert_rate_limited(
    outcome: ObservedOutcome,
    *,
    expected_codes: Sequence[str],
    case_id: str = "negative",
    expected_http: Sequence[int] = (429,),
    require_correlation_id: bool = True,
) -> None:
    """Accept only a throttle. A 403 here would mean authorization, not rate."""
    if not expected_codes:
        _reject(f"{case_id}: rate-limit cases must name the throttle code")
    invalid = sorted(set(expected_codes) - RATE_LIMIT_PROOF_CODES)
    if invalid:
        _reject(
            f"{case_id}: {', '.join(invalid)} is not throttle evidence"
        )
    _reject_success(case_id, outcome, "a throttle")
    _reject_wrong_class(case_id, outcome, OutcomeClass.RATE_LIMIT)
    if (outcome.error_code or "") not in set(expected_codes):
        _reject(
            f"{case_id}: error code {outcome.error_code!r} is not one of the "
            f"expected throttle codes {list(expected_codes)}"
        )
    _check_status(case_id, outcome, expected_http)
    _check_correlation(case_id, outcome, require_correlation_id)


def assert_guardrail_blocked(
    outcome: ObservedOutcome,
    *,
    case_id: str = "negative",
    require_correlation_id: bool = True,
) -> None:
    """The guardrail must have intervened, evidenced by the trace."""
    if not outcome.guardrail_intervened:
        _reject(
            f"{case_id}: observed {outcome.outcome_class.value} with no "
            "guardrail intervention in the trace; a blocked completion must be "
            "proven by the guardrail trace, not inferred from a failure"
        )
    _check_correlation(case_id, outcome, require_correlation_id)


def assert_absent(
    outcome: ObservedOutcome,
    *,
    expected_codes: Sequence[str],
    case_id: str = "teardown",
    required_message_substrings: Iterable[str] = (),
) -> None:
    """Assert a resource no longer exists.

    This is the only assertion where ``NOT_FOUND`` is acceptable evidence, and
    only because non-existence is the claim. A successful describe means residue
    remains.
    """
    if not expected_codes:
        _reject(f"{case_id}: absence cases must name the not-found code")
    if outcome.succeeded:
        _reject(
            f"{case_id}: the resource still exists after teardown — residue "
            "remains"
        )
    observed = outcome.outcome_class
    if observed not in (OutcomeClass.NOT_FOUND, OutcomeClass.VALIDATION):
        _reject(
            f"{case_id}: observed {observed.value} rather than a not-found "
            "response, so absence was not established: " + rejection_reason(observed)
        )
    if (outcome.error_code or "") not in set(expected_codes):
        _reject(
            f"{case_id}: error code {outcome.error_code!r} is not one of the "
            f"expected not-found codes {list(expected_codes)}"
        )
    _check_message(case_id, outcome, required_message_substrings)


def assert_not_exposed(
    listed_tools: Iterable[str],
    forbidden_tools: Iterable[str],
    *,
    expected_tools: Iterable[str] = (),
    case_id: str = "negative",
) -> None:
    """Forbidden tools must be filtered out of ``tools/list`` entirely.

    ``expected_tools`` guards against the degenerate pass where the listing is
    empty (or the call silently failed) and therefore "contains no forbidden
    tool".
    """
    listed = list(listed_tools)
    listed_set = set(listed)
    expected = list(expected_tools)
    if not listed:
        _reject(
            f"{case_id}: tools/list returned nothing, so absence of a "
            "forbidden tool proves nothing"
        )
    missing_expected = [name for name in expected if name not in listed_set]
    if missing_expected:
        _reject(
            f"{case_id}: tools/list is missing the tools this principal should "
            f"see {missing_expected!r}; the listing is not a valid baseline"
        )
    leaked = sorted(set(forbidden_tools) & listed_set)
    if leaked:
        raise AssertionError(
            f"{case_id}: tools/list exposed forbidden tools {leaked!r}; "
            "unentitled tools must be filtered from discovery, not merely "
            "rejected on call"
        )


def assert_rolled_back(
    *,
    rollback_event_observed: bool,
    active_version: str,
    previous_version: str,
    active_manifest_sha: str,
    previous_manifest_sha: str,
    case_id: str = "negative",
) -> None:
    """A rollback must be observable *and* must actually revert what runs."""
    if not rollback_event_observed:
        _reject(
            f"{case_id}: no rollback event was observed; a failed canary that "
            "merely stops is not a rollback"
        )
    if active_version != previous_version:
        raise AssertionError(
            f"{case_id}: active runtime version {active_version!r} is not the "
            f"previous version {previous_version!r} — the rollback did not take "
            "effect"
        )
    if active_manifest_sha != previous_manifest_sha:
        raise AssertionError(
            f"{case_id}: active manifest SHA does not match the previous "
            "release's manifest SHA, so what is running was not reverted"
        )


def assert_detected(
    *,
    signal_observed: bool,
    signal_name: str,
    baseline_signal_observed: bool,
    case_id: str = "negative",
) -> None:
    """A detection control must fire on injection and be quiet at baseline."""
    if baseline_signal_observed:
        _reject(
            f"{case_id}: {signal_name} was already firing before injection, so "
            "its firing afterwards is not evidence"
        )
    if not signal_observed:
        raise AssertionError(
            f"{case_id}: {signal_name} did not fire after injection; a control "
            "that cannot be observed firing is indistinguishable from a "
            "missing control"
        )


# ---------------------------------------------------------------------------
# catalog-driven dispatch
# ---------------------------------------------------------------------------


def assert_case(
    case: AdversarialCase,
    outcome: ObservedOutcome,
    *,
    ledger: TwinLedger,
    extras: Mapping[str, object] | None = None,
    require_correlation_id: bool | None = None,
) -> str | None:
    """Apply the assertion the case's expectation calls for.

    Returns the twin's test id for negative cases (to be embedded in the
    evidence record), or ``None`` for positives.

    ``extras`` supplies the non-``ObservedOutcome`` inputs some expectations
    need (tool listings, rollback versions, detection signals).
    """
    payload: Mapping[str, object] = extras or {}
    correlation = (
        require_correlation_id if require_correlation_id is not None else case.live_required
    )

    if case.expectation is Expectation.ALLOW:
        assert_allowed(
            outcome, case_id=case.case_id, require_correlation_id=correlation
        )
        ledger.record_pass(case.case_id, str(payload.get("test_id", case.case_id)))
        return None

    twin_test_id = ledger.require_twin(case)

    if case.expectation is Expectation.DENY:
        assert_authorization_denied(
            outcome,
            expected_codes=case.expected_error_codes,
            case_id=case.case_id,
            expected_http=case.expected_http_status,
            required_message_substrings=case.required_message_substrings,
            require_correlation_id=correlation,
        )
    elif case.expectation is Expectation.AUTHENTICATION_DENIED:
        assert_authentication_denied(
            outcome,
            expected_codes=case.expected_error_codes,
            case_id=case.case_id,
            expected_http=case.expected_http_status or (401,),
            require_correlation_id=correlation,
        )
    elif case.expectation is Expectation.RATE_LIMITED:
        assert_rate_limited(
            outcome,
            expected_codes=case.expected_error_codes,
            case_id=case.case_id,
            expected_http=case.expected_http_status or (429,),
            require_correlation_id=correlation,
        )
    elif case.expectation is Expectation.GUARDRAIL_BLOCKED:
        assert_guardrail_blocked(
            outcome, case_id=case.case_id, require_correlation_id=correlation
        )
    elif case.expectation is Expectation.NOT_EXPOSED:
        assert_not_exposed(
            payload.get("listed_tools", []),  # type: ignore[arg-type]
            payload.get("forbidden_tools", []),  # type: ignore[arg-type]
            expected_tools=payload.get("expected_tools", []),  # type: ignore[arg-type]
            case_id=case.case_id,
        )
    elif case.expectation is Expectation.ABSENT:
        assert_absent(
            outcome,
            expected_codes=case.expected_error_codes,
            case_id=case.case_id,
            required_message_substrings=case.required_message_substrings,
        )
    elif case.expectation is Expectation.ROLLED_BACK:
        assert_rolled_back(
            rollback_event_observed=bool(payload.get("rollback_event_observed")),
            active_version=str(payload.get("active_version", "")),
            previous_version=str(payload.get("previous_version", "")),
            active_manifest_sha=str(payload.get("active_manifest_sha", "")),
            previous_manifest_sha=str(payload.get("previous_manifest_sha", "")),
            case_id=case.case_id,
        )
    elif case.expectation is Expectation.DETECTED:
        assert_detected(
            signal_observed=bool(payload.get("signal_observed")),
            signal_name=str(payload.get("signal_name", "detection signal")),
            baseline_signal_observed=bool(payload.get("baseline_signal_observed")),
            case_id=case.case_id,
        )
    else:  # pragma: no cover - exhaustive over Expectation
        raise InvalidDenialProof(
            f"{case.case_id}: no assertion is defined for expectation "
            f"{case.expectation.value}"
        )

    return twin_test_id


__all__ = [
    "TwinLedger",
    "assert_absent",
    "assert_allowed",
    "assert_authentication_denied",
    "assert_authorization_denied",
    "assert_case",
    "assert_detected",
    "assert_guardrail_blocked",
    "assert_not_exposed",
    "assert_rate_limited",
    "assert_rolled_back",
]
