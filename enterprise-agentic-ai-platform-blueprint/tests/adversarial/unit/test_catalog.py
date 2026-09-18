"""The case catalog: coverage, twin discipline, and proof-code hygiene.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from harness.catalog import (
    CATALOG,
    REQUIRED_DOMAINS,
    REQUIRED_RATE_LIMIT_TAGS,
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
from harness.errors import CatalogError
from harness.manifest import REQUIRED_ROLES, load_manifest
from harness.outcome import FORBIDDEN_PROOF_CODES

pytestmark = pytest.mark.adversarial

EXAMPLE_MANIFEST = (
    Path(__file__).resolve().parents[1] / "fixtures" / "manifest.example.json"
)


def base_case(**overrides) -> AdversarialCase:
    case = AdversarialCase(
        case_id="FIX-01-N",
        domain=Domain.SCP,
        title="fixture",
        expectation=Expectation.DENY,
        severity=Severity.CRITICAL,
        principal_ref="workstream.agent_runtime",
        target="bedrock:InvokeModel",
        rationale="fixture rationale",
        control_refs=("fixture control",),
        expected_error_codes=("AccessDeniedException",),
        expected_http_status=(403,),
        audit_sources=(AuditSource.CLOUDTRAIL,),
        positive_twin="FIX-01-P",
    )
    return replace(case, **overrides) if overrides else case


def base_twin(**overrides) -> AdversarialCase:
    case = AdversarialCase(
        case_id="FIX-01-P",
        domain=Domain.SCP,
        title="fixture twin",
        expectation=Expectation.ALLOW,
        severity=Severity.HIGH,
        principal_ref="workstream.agent_runtime",
        target="bedrock:InvokeModel",
        rationale="fixture rationale",
        control_refs=("fixture control",),
        audit_sources=(AuditSource.CLOUDTRAIL,),
    )
    return replace(case, **overrides) if overrides else case


# ---------------------------------------------------------------------------
# the shipped catalog
# ---------------------------------------------------------------------------


def test_shipped_catalog_is_valid():
    assert validate_catalog(CATALOG) == []
    assert_catalog_valid(CATALOG)


def test_every_required_domain_has_a_negative_case():
    for domain in REQUIRED_DOMAINS:
        negatives = [case for case in cases_for_domain(domain) if case.is_negative]
        assert negatives, f"domain {domain.value} has no negative case"


def test_every_domain_named_in_the_round_1c_scope_is_present():
    expected = {
        "scp",
        "iam-sts",
        "inference-gateway",
        "tool-gateway",
        "memory-isolation",
        "registry",
        "pipeline-bypass",
        "supply-chain-tamper",
        "rate-limiting",
        "failure-injection",
        "rollback",
        "teardown",
    }
    assert {domain.value for domain in Domain} == expected


def test_every_negative_case_has_an_allow_twin():
    index = {case.case_id: case for case in CATALOG}
    for case in CATALOG:
        if not case.is_negative:
            continue
        assert case.positive_twin, case.case_id
        twin = index[case.positive_twin]
        assert twin.expectation is Expectation.ALLOW


def test_no_denial_case_accepts_a_forbidden_proof_code():
    for case in CATALOG:
        if case.expectation is not Expectation.DENY:
            continue
        assert not (set(case.expected_error_codes) & FORBIDDEN_PROOF_CODES), case.case_id


def test_no_denial_case_accepts_a_throttle_status():
    for case in CATALOG:
        if case.expectation is Expectation.DENY:
            assert 429 not in case.expected_http_status, case.case_id


def test_rate_limiting_covers_rpm_tpm_cps_catch_all_and_fail_open():
    tags = {
        tag
        for case in cases_for_domain(Domain.RATE_LIMITING)
        if case.is_negative
        for tag in case.tags
    }
    assert REQUIRED_RATE_LIMIT_TAGS <= tags


def test_fail_open_case_expects_authorization_not_a_throttle():
    """Rate limiting fails open, so Policy must still deny."""
    case = case_by_id("RATE-05-N")
    assert case.expectation is Expectation.DENY
    assert 429 not in case.expected_http_status


def test_tool_gateway_covers_the_unexposed_tool_attack():
    tool_cases = cases_for_domain(Domain.TOOL_GATEWAY)
    expectations = {case.expectation for case in tool_cases}
    assert Expectation.NOT_EXPOSED in expectations, "tools/list filtering is untested"
    assert any(
        case.expectation is Expectation.DENY and "unexposed" in case.target
        for case in tool_cases
    ), "invoking an unexposed tool is untested"


REGISTRY_REQUIRED_EXPECTATIONS = {
    "REG-01-P": Expectation.ALLOW,
    "REG-02-P": Expectation.ALLOW,
    "REG-02-N": Expectation.DENY,
    "REG-03-N": Expectation.DENY,
    "REG-04-N": Expectation.DENY,
    "REG-05-N": Expectation.DENY,
    "REG-06-N": Expectation.DENY,
    "REG-07-P": Expectation.ALLOW,
    "REG-07-N": Expectation.DENY,
    "REG-08-N": Expectation.DENY,
    "REG-09-N": Expectation.DENY,
    "REG-10-N": Expectation.DENY,
    "REG-11-P": Expectation.ALLOW,
    "REG-11-N": Expectation.DENY,
    "REG-12-N": Expectation.DENY,
    "SUP-04-N": Expectation.DENY,
}


def assert_registry_contract(catalog):
    registry_cases = {
        case.case_id: case
        for case in catalog
        if case.domain is Domain.REGISTRY
    }
    assert REGISTRY_REQUIRED_EXPECTATIONS.keys() <= registry_cases.keys()
    for case_id, expectation in REGISTRY_REQUIRED_EXPECTATIONS.items():
        assert registry_cases[case_id].expectation is expectation

    assert registry_cases["REG-02-N"].principal_ref.startswith(
        "management-governance."
    ), "wrong-account test must originate outside the workstream account"
    assert registry_cases["REG-11-N"].positive_twin == "REG-11-P"
    assert "AgentRegistrationApi" in registry_cases["SUP-04-N"].target


def test_registry_is_a_first_class_boundary_with_required_attacks():
    assert_registry_contract(CATALOG)


def test_registry_expectation_and_twin_mutations_are_detected():
    registry_denial = case_by_id("REG-04-N")
    weakened_outcome = replace(registry_denial, expectation=Expectation.ALLOW)
    weakened_catalog = tuple(
        weakened_outcome if case.case_id == registry_denial.case_id else case
        for case in CATALOG
    )
    with pytest.raises(AssertionError):
        assert_registry_contract(weakened_catalog)

    without_twin = replace(registry_denial, positive_twin=None)
    mutated_catalog = tuple(
        without_twin if case.case_id == registry_denial.case_id else case
        for case in CATALOG
    )
    assert any(
        "requires a positive twin" in problem
        for problem in validate_catalog(mutated_catalog)
    )


def test_teardown_absence_cases_name_their_not_found_code():
    for case in cases_for_domain(Domain.TEARDOWN):
        if case.expectation is Expectation.ABSENT:
            assert case.expected_error_codes, case.case_id


def test_every_case_principal_resolves_against_the_example_manifest():
    manifest = load_manifest(EXAMPLE_MANIFEST)
    for case in CATALOG:
        role = manifest.role(case.principal_ref)
        assert role.role_name
        assert role.ref in REQUIRED_ROLES[role.account_key]


def test_every_negative_case_names_at_least_one_audit_source():
    for case in CATALOG:
        if case.is_negative:
            assert case.audit_sources, case.case_id


def test_every_case_cites_a_control():
    for case in CATALOG:
        assert case.control_refs, case.case_id
        assert case.rationale.strip(), case.case_id


def test_case_ids_are_unique():
    ids = [case.case_id for case in CATALOG]
    assert len(ids) == len(set(ids))


def test_catalog_sha_is_stable_and_sensitive_to_change():
    first = catalog_sha(CATALOG)
    assert first == catalog_sha(CATALOG)
    assert len(first) == 64
    mutated = CATALOG[:-1] + (replace(CATALOG[-1], title="changed"),)
    assert catalog_sha(mutated) != first


def test_case_by_id_rejects_an_unknown_id():
    with pytest.raises(CatalogError, match="unknown case id"):
        case_by_id("NOPE-99-N")


# ---------------------------------------------------------------------------
# the validator itself
# ---------------------------------------------------------------------------


def test_validator_rejects_a_denial_without_a_twin():
    problems = validate_catalog([base_case(positive_twin=None)])
    assert any("requires a positive twin" in problem for problem in problems)


def test_validator_rejects_a_twin_that_does_not_exist():
    problems = validate_catalog([base_case()])
    assert any("does not exist" in problem for problem in problems)


def test_validator_rejects_a_twin_that_is_not_an_allow_case():
    twin = base_twin(expectation=Expectation.DENY, expected_error_codes=("AccessDenied",))
    problems = validate_catalog([base_case(), twin])
    assert any("must have expectation ALLOW" in problem for problem in problems)


def test_validator_rejects_a_forbidden_proof_code():
    problems = validate_catalog(
        [base_case(expected_error_codes=("ResourceNotFoundException",)), base_twin()]
    )
    assert any("cannot be accepted as authorization evidence" in p for p in problems)


def test_validator_rejects_a_denial_with_no_expected_code():
    problems = validate_catalog([base_case(expected_error_codes=()), base_twin()])
    assert any("must name the exact code" in problem for problem in problems)


def test_validator_rejects_a_denial_with_no_expected_status():
    problems = validate_catalog([base_case(expected_http_status=()), base_twin()])
    assert any("expected HTTP status is required" in problem for problem in problems)


def test_validator_rejects_a_denial_with_no_audit_source():
    problems = validate_catalog([base_case(audit_sources=()), base_twin()])
    assert any("audit source is required" in problem for problem in problems)


def test_validator_rejects_a_rate_limit_case_expecting_a_denial_code():
    problems = validate_catalog(
        [
            base_case(
                expectation=Expectation.RATE_LIMITED,
                expected_error_codes=("AccessDeniedException",),
            ),
            base_twin(),
        ]
    )
    assert any("is not throttle evidence" in problem for problem in problems)


def test_validator_rejects_an_unknown_principal():
    problems = validate_catalog(
        [base_case(principal_ref="platform.no_such_role"), base_twin()]
    )
    assert any("not a required role" in problem for problem in problems)


def test_validator_rejects_an_unknown_account():
    problems = validate_catalog([base_case(principal_ref="shadow.admin"), base_twin()])
    assert any("unknown account" in problem for problem in problems)


def test_validator_rejects_an_allow_case_with_a_twin():
    problems = validate_catalog([base_twin(positive_twin="FIX-01-N")])
    assert any("must not declare a twin" in problem for problem in problems)


def test_validator_rejects_an_allow_case_with_expected_error_codes():
    problems = validate_catalog([base_twin(expected_error_codes=("AccessDenied",))])
    assert any("must not declare expected error codes" in problem for problem in problems)


def test_validator_rejects_duplicate_case_ids():
    problems = validate_catalog([base_twin(), base_twin()])
    assert any("duplicate case id" in problem for problem in problems)


def test_validator_reports_uncovered_domains():
    problems = validate_catalog([base_case(), base_twin()])
    uncovered = [problem for problem in problems if "has no negative case" in problem]
    assert len(uncovered) == len(REQUIRED_DOMAINS) - 1


def test_assert_catalog_valid_raises_with_every_problem():
    with pytest.raises(CatalogError) as excinfo:
        assert_catalog_valid([base_case(positive_twin=None, control_refs=())])
    assert len(excinfo.value.problems) >= 2
