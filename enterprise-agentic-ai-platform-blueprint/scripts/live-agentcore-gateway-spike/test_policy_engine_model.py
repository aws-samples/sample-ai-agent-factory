"""Unit tests for the AWS-free PolicyEngine spike model.

These tests import no AWS SDK and make no network calls, so they run anywhere.
They pin the security-critical behaviour: the Cedar statements that are
generated, the four-user subject/group decision truth table those statements
imply, the delimiter-aware ``cognito:groups`` membership candidate and its
prefix/suffix-collision safety (property/fuzz), resource-name ownership, error
sanitisation, and the secret-safety scanner.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import json
import random
import string

import pytest

import policy_engine_model as model

PREFIX = "aiaf-pe-test"
GATEWAY_ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:gateway/aiaf-pe-test-pe-gw-abc123"
SUB_ALPHA = "11111111-2222-4333-8444-555555555555"
SUB_BETA = "66666666-7777-4888-8999-aaaaaaaaaaaa"
SUB_GAMMA = "22222222-3333-4444-8555-666666666666"
SUB_DELTA = "33333333-4444-4555-8666-777777777777"
SUBJECTS = {
    model.ALPHA: SUB_ALPHA,
    model.BETA: SUB_BETA,
    model.GAMMA: SUB_GAMMA,
    model.DELTA: SUB_DELTA,
}


@pytest.fixture()
def names() -> model.SpikeNames:
    return model.SpikeNames(prefix=PREFIX)


@pytest.fixture()
def policies(names: model.SpikeNames) -> tuple[model.ToolPolicy, ...]:
    return model.build_policy_set(names=names, subjects=SUBJECTS)


# --------------------------------------------------------------------------
# Naming and ownership
# --------------------------------------------------------------------------


def test_every_generated_name_is_prefix_owned(names: model.SpikeNames) -> None:
    for name in names.all_names:
        assert names.owns(name), name


def test_ownership_rejects_foreign_and_empty_names(names: model.SpikeNames) -> None:
    for candidate in ("", "prod-gateway", "aiaf", "other-aiaf-pe-test", None):
        assert not names.owns(candidate)  # type: ignore[arg-type]


def test_policy_names_use_the_underscore_charset(names: model.SpikeNames) -> None:
    assert "-" not in names.engine_name
    assert "-" not in names.policy_name("subject_only_alpha")
    assert model.POLICY_NAME_PATTERN.fullmatch(names.engine_name)
    assert names.underscore_prefix == "aiaf_pe_test"


def test_gateway_and_target_names_match_service_patterns(names: model.SpikeNames) -> None:
    assert model.GATEWAY_NAME_PATTERN.fullmatch(names.gateway_name)
    assert model.TARGET_NAME_PATTERN.fullmatch(names.target_name)


def test_group_layout_has_allowed_plus_two_collisions(names: model.SpikeNames) -> None:
    layout = model.group_layout(names)
    assert set(layout) == {"allowed", "suffix_collision", "inner_collision"}
    assert layout["suffix_collision"] == layout["allowed"] + "x"
    assert layout["inner_collision"] == layout["allowed"] + "zz"
    for group in layout.values():
        assert model.GROUP_NAME_PATTERN.fullmatch(group)
        # Both decoys must remain prefix-owned so cleanup can delete them.
        assert names.owns(group)


def test_allocation_tags_are_the_required_five(names: model.SpikeNames) -> None:
    assert set(names.allocation_tags()) == {
        "application-id",
        "agent-id",
        "tenant-id",
        "cost-centre",
        "environment",
    }
    assert names.allocation_tags()["environment"] == "nonprod"


@pytest.mark.parametrize("prefix", ["", "A-bad", "x", "has_underscore", "toolong" * 10, "-lead"])
def test_invalid_prefixes_are_refused(prefix: str) -> None:
    with pytest.raises(model.CedarPolicyError):
        model.SpikeNames(prefix=prefix)


def test_prefix_that_overflows_the_policy_name_budget_is_refused() -> None:
    # A long prefix pushes a per-label policy name past the 48-character
    # CreatePolicy limit, so construction must fail closed.
    with pytest.raises(model.CedarPolicyError):
        model.SpikeNames(prefix="a" + "b" * 38)


# --------------------------------------------------------------------------
# Cedar generation
# --------------------------------------------------------------------------


def test_action_literal_uses_the_exact_triple_underscore_format() -> None:
    assert model.qualified_action("tgt", "echo") == "tgt___echo"
    assert model.action_literal("tgt", "echo") == 'AgentCore::Action::"tgt___echo"'


def test_resource_literal_requires_a_concrete_gateway_arn() -> None:
    assert model.gateway_literal(GATEWAY_ARN).startswith('AgentCore::Gateway::"arn:aws:')
    for bad in (
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:gateway/*",
        "*",
        "arn:aws:bedrock-agentcore:us-west-2:12345:gateway/x",
    ):
        with pytest.raises(model.CedarPolicyError):
            model.gateway_literal(bad)


def test_subject_literal_rejects_non_uuid_and_injection_attempts() -> None:
    assert model.subject_literal(SUB_ALPHA).endswith(f'"{SUB_ALPHA}"')
    for bad in ('" || true || "', "alpha", "", SUB_ALPHA + "x"):
        with pytest.raises(model.CedarPolicyError):
            model.subject_literal(bad)


def test_rendered_statements_name_exact_action_and_resource(
    policies: tuple[model.ToolPolicy, ...], names: model.SpikeNames
) -> None:
    rendered = model.render_policies(
        policies, target_name=names.target_name, gateway_arn=GATEWAY_ARN
    )
    assert len(rendered) == len(policies)
    for name, statement in rendered.items():
        assert model.gateway_literal(GATEWAY_ARN) in statement
        assert f'AgentCore::Action::"{names.target_name}___' in statement
        assert statement.endswith(";")
        assert names.owns(name)


def test_every_generated_statement_passes_the_narrow_pattern_guard(
    policies: tuple[model.ToolPolicy, ...], names: model.SpikeNames
) -> None:
    for statement in model.render_policies(
        policies, target_name=names.target_name, gateway_arn=GATEWAY_ARN
    ).values():
        model.assert_no_pattern_matching(statement)
        if model.GROUP_CLAIM_NAME not in statement:
            for operator in model.FORBIDDEN_CEDAR_OPERATORS:
                assert operator not in statement


@pytest.mark.parametrize(
    "statement",
    [
        'permit(principal, action, resource) when { principal.getTag("x") == "y" };',
        'permit(principal, action, resource) when { principal.hasTag("g") };',
        'permit(principal, action, resource) when { context.input.s like "*admin*" };',
        "permit(principal, action, resource) when { context.input.l.contains(1) };",
    ],
)
def test_pattern_matching_guard_rejects_unsafe_statements(statement: str) -> None:
    with pytest.raises(model.CedarPolicyError):
        model.assert_no_pattern_matching(statement)


def test_pattern_guard_does_not_false_positive_on_similar_words() -> None:
    model.assert_no_pattern_matching(
        'permit(principal == AgentCore::OAuthUser::"x") when { context.input has auditLikeness };'
    )


def test_a_permit_without_an_exact_principal_is_refused() -> None:
    with pytest.raises(model.CedarPolicyError):
        model.ToolPolicy(
            name="p_open",
            effect=model.Effect.PERMIT,
            tool=model.TOOL_SUBJECT_ONLY,
            scope=model.PrincipalScope.ANY,
        )


def test_scope_and_principal_fields_must_agree() -> None:
    with pytest.raises(model.CedarPolicyError):
        model.ToolPolicy(
            name="p_subj_no_sub",
            effect=model.Effect.PERMIT,
            tool=model.TOOL_SUBJECT_ONLY,
            scope=model.PrincipalScope.SUBJECT,
        )
    with pytest.raises(model.CedarPolicyError):
        model.ToolPolicy(
            name="p_group_with_sub",
            effect=model.Effect.PERMIT,
            tool=model.TOOL_GROUP_ONLY,
            scope=model.PrincipalScope.GROUP,
            group="aiaf_pe_test_tools",
            subject=SUB_ALPHA,
        )


def test_a_policy_cannot_mix_when_and_unless() -> None:
    with pytest.raises(model.CedarPolicyError):
        model.ToolPolicy(
            name="p_mixed",
            effect=model.Effect.FORBID,
            tool=model.TOOL_COMBINED_ALL,
            scope=model.PrincipalScope.ANY,
            when_all=(model.InputCondition(model.ConditionKind.HAS, "a"),),
            unless_all=(model.InputCondition(model.ConditionKind.HAS, "b"),),
        )


def test_input_condition_validation() -> None:
    with pytest.raises(model.CedarPolicyError):
        model.InputCondition(model.ConditionKind.EQUALS, "mode")  # missing value
    with pytest.raises(model.CedarPolicyError):
        model.InputCondition(model.ConditionKind.HAS, "mode", "unexpected")
    with pytest.raises(model.CedarPolicyError):
        model.InputCondition(model.ConditionKind.EQUALS, "mode", 'x" || "y')
    with pytest.raises(model.CedarPolicyError):
        model.InputCondition(model.ConditionKind.HAS, "Bad-Field")


def test_equals_condition_pairs_presence_with_comparison() -> None:
    condition = model.InputCondition(model.ConditionKind.EQUALS, "mode", "readonly")
    assert condition.render() == (
        'context.input has mode && context.input.mode == "readonly"'
    )


def test_duplicate_policy_names_are_refused(names: model.SpikeNames) -> None:
    policy = model.ToolPolicy(
        name=names.policy_name("dup"),
        effect=model.Effect.PERMIT,
        tool=model.TOOL_SUBJECT_ONLY,
        scope=model.PrincipalScope.SUBJECT,
        subject=SUB_ALPHA,
    )
    with pytest.raises(model.CedarPolicyError):
        model.render_policies(
            [policy, policy], target_name=names.target_name, gateway_arn=GATEWAY_ARN
        )


# --------------------------------------------------------------------------
# Group claim compatibility candidate
# --------------------------------------------------------------------------


def test_group_statement_uses_the_quoted_element_candidate(
    names: model.SpikeNames,
) -> None:
    statement = model.group_scoped_statement(
        group=model.group_layout(names)["allowed"],
        tool=model.TOOL_GROUP_ONLY,
        target_name=names.target_name,
        gateway_arn=GATEWAY_ARN,
    )
    allowed = model.group_layout(names)["allowed"]
    assert f'principal.hasTag("{model.GROUP_CLAIM_NAME}")' in statement
    assert f'principal.getTag("{model.GROUP_CLAIM_NAME}")' in statement
    assert f'*\\"{allowed}\\"*' in statement
    assert f"*{allowed}*" not in statement
    model.assert_no_pattern_matching(statement)


def test_membership_candidate_never_emits_a_bare_group_wildcard(
    names: model.SpikeNames,
) -> None:
    allowed = model.group_layout(names)["allowed"]
    fragment = model.group_membership_candidate(allowed)
    assert f'"*\\"{allowed}\\"*"' in fragment
    assert f'"*{allowed}*"' not in fragment


def test_pattern_guard_rejects_an_ad_hoc_group_wildcard() -> None:
    with pytest.raises(model.CedarPolicyError):
        model.assert_no_pattern_matching(
            'when { principal.hasTag("cognito:groups") && '
            'principal.getTag("cognito:groups") like "*admin*" };'
        )


def test_live_policy_set_contains_group_and_subject_group_candidates(
    policies: tuple[model.ToolPolicy, ...], names: model.SpikeNames
) -> None:
    rendered = model.render_policies(
        policies, target_name=names.target_name, gateway_arn=GATEWAY_ARN
    )
    group_statements = [
        statement
        for statement in rendered.values()
        if model.GROUP_CLAIM_NAME in statement
    ]
    assert len(group_statements) == 4
    assert any(f'principal == {model.subject_literal(SUB_ALPHA)}' in s for s in group_statements)
    assert all(model.assert_no_pattern_matching(statement) for statement in group_statements)


# --------------------------------------------------------------------------
# Quoted-element collision safety: property / fuzz
# --------------------------------------------------------------------------


def test_quoted_element_matches_exact_group_only(names: model.SpikeNames) -> None:
    allowed = model.group_layout(names)["allowed"]
    assert model.quoted_element_matches(model.serialize_groups_claim([allowed]), allowed)
    # A member alongside others still matches.
    assert model.quoted_element_matches(
        model.serialize_groups_claim(["other_group", allowed, "third_group"]), allowed
    )


def test_quoted_element_rejects_prefix_and_suffix_collisions(
    names: model.SpikeNames,
) -> None:
    layout = model.group_layout(names)
    allowed = layout["allowed"]
    # Live decoys are suffix decorations (prefix-owned); a prefix decoration is
    # proven at the model layer directly.
    for colliding in (layout["suffix_collision"], layout["inner_collision"], "x" + allowed):
        tag = model.serialize_groups_claim([colliding])
        assert not model.quoted_element_matches(tag, allowed)


def test_quoted_element_collision_fuzz(names: model.SpikeNames) -> None:
    """Property/fuzz: no random prefix/suffix decoration of the allowed name can
    satisfy the quoted-element check unless it is the exact name."""
    allowed = model.group_layout(names)["allowed"]
    alphabet = string.ascii_letters + string.digits + "_-"
    rng = random.Random(1337)
    for _ in range(2000):
        prefix = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 4)))
        suffix = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 4)))
        candidate = f"{prefix}{allowed}{suffix}"
        if not model.GROUP_NAME_PATTERN.fullmatch(candidate):
            continue
        tag = model.serialize_groups_claim([candidate])
        expected = candidate == allowed
        assert model.quoted_element_matches(tag, allowed) is expected, candidate


def test_quoted_element_fuzz_with_multiple_groups(names: model.SpikeNames) -> None:
    """Membership holds iff the exact allowed name is one of the array elements,
    regardless of how many colliding decoys share the array."""
    allowed = model.group_layout(names)["allowed"]
    rng = random.Random(4242)
    decoys = [allowed + "x", "x" + allowed, "z" + allowed + "z", allowed + "_1"]
    for _ in range(1000):
        members = rng.sample(decoys, rng.randint(1, len(decoys)))
        include_allowed = rng.random() < 0.5
        if include_allowed:
            members.append(allowed)
        rng.shuffle(members)
        members = [g for g in members if model.GROUP_NAME_PATTERN.fullmatch(g)]
        tag = model.serialize_groups_claim(members)
        assert model.quoted_element_matches(tag, allowed) is (allowed in members)


# --------------------------------------------------------------------------
# Truth table
# --------------------------------------------------------------------------

EXPECTED_DECISIONS = {
    "alpha-subject-tool-readonly-audit": model.Decision.ALLOW,
    "alpha-group-tool-readonly-audit": model.Decision.ALLOW,
    "alpha-any-tool-readonly-audit": model.Decision.ALLOW,
    "alpha-all-tool-readonly-audit": model.Decision.ALLOW,
    "alpha-denied-tool-readonly-audit": model.Decision.DENY,
    "beta-subject-tool-readonly-audit": model.Decision.DENY,
    "beta-group-tool-readonly-audit": model.Decision.ALLOW,
    "beta-any-tool-readonly-audit": model.Decision.ALLOW,
    "beta-all-tool-readonly-audit": model.Decision.DENY,
    "beta-denied-tool-readonly-audit": model.Decision.DENY,
    "gamma-subject-tool-readonly-audit": model.Decision.ALLOW,
    "gamma-group-tool-readonly-audit": model.Decision.DENY,
    "gamma-any-tool-readonly-audit": model.Decision.ALLOW,
    "gamma-all-tool-readonly-audit": model.Decision.DENY,
    "gamma-denied-tool-readonly-audit": model.Decision.DENY,
    "delta-subject-tool-readonly-audit": model.Decision.DENY,
    "delta-group-tool-readonly-audit": model.Decision.DENY,
    "delta-any-tool-readonly-audit": model.Decision.DENY,
    "delta-all-tool-readonly-audit": model.Decision.DENY,
    "delta-denied-tool-readonly-audit": model.Decision.DENY,
}


def test_truth_table_matches_pinned_subject_group_expectations(
    policies: tuple[model.ToolPolicy, ...], names: model.SpikeNames
) -> None:
    table = model.build_truth_table(policies, names=names, subjects=SUBJECTS)
    assert {case.case_id: case.expected for case in table} == EXPECTED_DECISIONS


def test_truth_table_contains_both_outcomes_and_every_semantics(
    policies: tuple[model.ToolPolicy, ...], names: model.SpikeNames
) -> None:
    table = model.build_truth_table(policies, names=names, subjects=SUBJECTS)
    assert any(case.expected is model.Decision.ALLOW for case in table)
    assert any(case.expected is model.Decision.DENY for case in table)
    semantics = {case.semantics for case in table}
    assert semantics == {
        "subject-only",
        "group-only",
        "ANY-disjunction",
        "ALL-conjunction",
        "default-deny",
    }


def test_four_users_span_the_subject_group_matrix(
    policies: tuple[model.ToolPolicy, ...], names: model.SpikeNames
) -> None:
    profiles = model.user_profiles(names, SUBJECTS)
    allowed = model.group_layout(names)["allowed"]
    # alpha: allowed subject + allowed group
    assert profiles[model.ALPHA].has_allowed_subject and allowed in profiles[model.ALPHA].groups
    # beta: allowed group only
    assert (not profiles[model.BETA].has_allowed_subject) and allowed in profiles[model.BETA].groups
    # gamma: allowed subject only + collision group
    assert profiles[model.GAMMA].has_allowed_subject and allowed not in profiles[model.GAMMA].groups
    # delta: neither + collision groups
    assert (not profiles[model.DELTA].has_allowed_subject) and allowed not in profiles[model.DELTA].groups


def test_group_only_user_is_denied_subject_tool_and_allowed_group_tool(
    policies: tuple[model.ToolPolicy, ...], names: model.SpikeNames
) -> None:
    args = model.probe_arguments(mode=model.READONLY_MODE, audit=True)
    beta_groups = model.user_profiles(names, SUBJECTS)[model.BETA].groups
    assert (
        model.evaluate(policies, subject=SUB_BETA, groups=beta_groups,
                       tool=model.TOOL_SUBJECT_ONLY, arguments=args)
        is model.Decision.DENY
    )
    assert (
        model.evaluate(policies, subject=SUB_BETA, groups=beta_groups,
                       tool=model.TOOL_GROUP_ONLY, arguments=args)
        is model.Decision.ALLOW
    )


def test_collision_group_user_is_denied_the_group_tool(
    policies: tuple[model.ToolPolicy, ...], names: model.SpikeNames
) -> None:
    # gamma and delta hold only prefix/suffix-collision groups, so the
    # delimiter-aware membership must NOT admit them to the group tool.
    args = model.probe_arguments(mode=model.READONLY_MODE, audit=True)
    for label, sub in ((model.GAMMA, SUB_GAMMA), (model.DELTA, SUB_DELTA)):
        groups = model.user_profiles(names, SUBJECTS)[label].groups
        assert (
            model.evaluate(policies, subject=sub, groups=groups,
                           tool=model.TOOL_GROUP_ONLY, arguments=args)
            is model.Decision.DENY
        ), label


def test_any_semantics_is_a_disjunction_of_subject_and_group(
    policies: tuple[model.ToolPolicy, ...], names: model.SpikeNames
) -> None:
    args = model.probe_arguments(mode=model.READONLY_MODE, audit=True)
    profiles = model.user_profiles(names, SUBJECTS)
    # gamma allowed by subject disjunct; beta allowed by group disjunct.
    assert (
        model.evaluate(policies, subject=SUB_GAMMA, groups=profiles[model.GAMMA].groups,
                       tool=model.TOOL_COMBINED_ANY, arguments=args)
        is model.Decision.ALLOW
    )
    assert (
        model.evaluate(policies, subject=SUB_BETA, groups=profiles[model.BETA].groups,
                       tool=model.TOOL_COMBINED_ANY, arguments=args)
        is model.Decision.ALLOW
    )


def test_all_semantics_requires_subject_and_group(
    policies: tuple[model.ToolPolicy, ...], names: model.SpikeNames
) -> None:
    args = model.probe_arguments(mode=model.READONLY_MODE, audit=True)
    profiles = model.user_profiles(names, SUBJECTS)
    assert (
        model.evaluate(
            policies,
            subject=SUB_ALPHA,
            groups=profiles[model.ALPHA].groups,
            tool=model.TOOL_COMBINED_ALL,
            arguments=args,
        )
        is model.Decision.ALLOW
    )
    # beta has the group but no allowed subject; gamma has the subject but only
    # a collision group. Each independently proves one missing conjunct denies.
    for label, subject in ((model.BETA, SUB_BETA), (model.GAMMA, SUB_GAMMA)):
        assert (
            model.evaluate(
                policies,
                subject=subject,
                groups=profiles[label].groups,
                tool=model.TOOL_COMBINED_ALL,
                arguments=args,
            )
            is model.Decision.DENY
        )


def test_default_is_deny_and_exact_subject_permit_allows(
    policies: tuple[model.ToolPolicy, ...], names: model.SpikeNames
) -> None:
    groups = model.user_profiles(names, SUBJECTS)[model.ALPHA].groups
    assert (
        model.evaluate(policies, subject=SUB_ALPHA, groups=groups,
                       tool=model.TOOL_UNPERMITTED,
                       arguments=model.probe_arguments(mode=model.READONLY_MODE, audit=True))
        is model.Decision.DENY
    )
    assert (
        model.evaluate(policies, subject=SUB_ALPHA, groups=groups,
                       tool=model.TOOL_SUBJECT_ONLY, arguments={})
        is model.Decision.ALLOW  # subject-only permit is unconditional
    )


def test_expected_listing_filters_by_entitlement(
    policies: tuple[model.ToolPolicy, ...], names: model.SpikeNames
) -> None:
    listing = model.expected_listing(
        policies, names=names, subjects=SUBJECTS, target_name=names.target_name
    )
    q = lambda tool: model.qualified_action(names.target_name, tool)
    # alpha sees subject/group/any/all; delta sees nothing; the unpermitted tool
    # is never listed for anyone.
    assert q(model.TOOL_SUBJECT_ONLY) in listing[model.ALPHA]
    assert q(model.TOOL_GROUP_ONLY) in listing[model.BETA]
    assert listing[model.DELTA] == ()
    for label in model.SUBJECT_LABELS:
        assert q(model.TOOL_UNPERMITTED) not in listing[label]


def test_subject_and_group_permit_lists_the_all_tool(
    policies: tuple[model.ToolPolicy, ...], names: model.SpikeNames
) -> None:
    groups = model.user_profiles(names, SUBJECTS)[model.ALPHA].groups
    assert model.tool_is_listable(
        policies, subject=SUB_ALPHA, groups=groups, tool=model.TOOL_COMBINED_ALL
    )


def test_unconditional_forbid_hides_a_tool() -> None:
    policies = (
        model.ToolPolicy(
            name="p_permit", effect=model.Effect.PERMIT, tool=model.TOOL_SUBJECT_ONLY,
            scope=model.PrincipalScope.SUBJECT, subject=SUB_ALPHA,
        ),
        model.ToolPolicy(
            name="p_forbid", effect=model.Effect.FORBID, tool=model.TOOL_SUBJECT_ONLY,
            scope=model.PrincipalScope.ANY,
        ),
    )
    assert not model.tool_is_listable(
        policies, subject=SUB_ALPHA, groups=(), tool=model.TOOL_SUBJECT_ONLY
    )


def test_build_policy_set_requires_all_four_subjects(names: model.SpikeNames) -> None:
    with pytest.raises(model.CedarPolicyError):
        model.build_policy_set(names=names, subjects={model.ALPHA: SUB_ALPHA})


# --------------------------------------------------------------------------
# Secret safety
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"accessToken": "abc"},
        {"nested": {"clientSecret": "abc"}},
        {"password": "abc"},
        {"Authorization": "x"},
        {"cookie": "x"},
        {"innocuous": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhIn0.sig"},
        {"header": "Bearer abcdefghijkl"},
        {"key": "AKIAIOSFODNN7EXAMPLE"},
        {"list": [{"sessionToken": "x"}]},
    ],
)
def test_secret_scanner_refuses_credential_shapes(payload: dict) -> None:
    with pytest.raises(model.SecretLeakError):
        model.assert_no_secret_values(payload)


def test_secret_scanner_allows_ordinary_evidence() -> None:
    payload = {
        "gatewayId": "aiaf-pe-test-pe-gw-abc123",
        "httpStatus": 403,
        "statuses": {"missing_header": 401},
        "fingerprint": model.fingerprint("anything"),
        "tools": ["tgt___echo"],
    }
    assert model.assert_no_secret_values(payload) is payload


def test_redact_headers_keeps_names_and_drops_values() -> None:
    redacted = model.redact_headers(
        {"Authorization": "Bearer abc", "Content-Type": "application/json"}
    )
    assert redacted["Authorization"] == "[redacted]"
    assert redacted["Content-Type"] == "application/json"


def test_fingerprint_is_stable_and_non_reversible() -> None:
    assert model.fingerprint("abc") == model.fingerprint("abc")
    assert model.fingerprint("abc") != model.fingerprint("abd")
    assert len(model.fingerprint("abc")) == 32
    assert "abc" not in model.fingerprint("abc")


# --------------------------------------------------------------------------
# Error sanitisation (B11)
# --------------------------------------------------------------------------


def test_sanitize_error_drops_jwt_and_collapses_arns() -> None:
    message = (
        "AccessDenied on arn:aws:bedrock-agentcore:us-west-2:123456789012:"
        "policy-engine/aiaf_pe_test_engine-abcdefghij using token "
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhIn0.signaturepart"
    )
    cleaned = model.sanitize_error(message, prefix="deploy")
    assert "eyJ" not in cleaned
    assert "aiaf_pe_test_engine-abcdefghij" not in cleaned
    assert "arn:aws:bedrock-agentcore:[redacted]" in cleaned
    # And the cleaned text itself passes the secret scanner.
    model.assert_no_secret_values(cleaned)


def test_sanitize_error_caps_length_and_stubs_residual_secrets() -> None:
    long_message = "AKIAIOSFODNN7EXAMPLE " + "x" * 500
    cleaned = model.sanitize_error(long_message, prefix="cleanup")
    assert "AKIA" not in cleaned
    assert len(cleaned) <= 300


def test_transient_passwords_are_unique_and_meet_the_policy() -> None:
    generated = {model.generate_transient_password() for _ in range(20)}
    assert len(generated) == 20
    for password in generated:
        assert len(password) == 32
        assert any(character.isupper() for character in password)
        assert any(character.islower() for character in password)
        assert any(character.isdigit() for character in password)
        assert any(character in "!@#$%^&*()-_=+" for character in password)


def test_short_password_request_is_refused() -> None:
    with pytest.raises(model.ModelError):
        model.generate_transient_password(8)


# --------------------------------------------------------------------------
# Token forgery helpers
# --------------------------------------------------------------------------


def _fake_token(subject: str) -> str:
    import base64

    def segment(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{segment({'alg': 'RS256'})}.{segment({'sub': subject})}.originalsignature"


def test_tampering_keeps_the_original_signature_segment() -> None:
    forged = model.tamper_token_subject(_fake_token(SUB_ALPHA), SUB_BETA)
    assert forged.split(".")[2] == "originalsignature"
    assert forged.split(".")[1] != _fake_token(SUB_ALPHA).split(".")[1]


def test_tampering_rejects_malformed_input_and_bad_subjects() -> None:
    with pytest.raises(model.ModelError):
        model.tamper_token_subject("not-a-jws", SUB_BETA)
    with pytest.raises(model.CedarPolicyError):
        model.tamper_token_subject(_fake_token(SUB_ALPHA), "not-a-uuid")


def test_unsigned_token_has_alg_none_and_an_empty_signature() -> None:
    token = model.unsigned_token(
        subject=SUB_ALPHA, issuer="https://issuer", client_id="client", expires_at=1
    )
    header, _, signature = token.split(".")
    assert signature == ""
    assert b'"alg":"none"' in model._b64url_decode(header)


# --------------------------------------------------------------------------
# MCP response classification
# --------------------------------------------------------------------------


def _tool_response(text: str, *, is_error: bool) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": text}], "isError": is_error},
    }


def test_allow_requires_the_deterministic_marker() -> None:
    outcome = model.classify_tool_result(
        _tool_response(json.dumps({"marker": model.ECHO_MARKER}), is_error=False)
    )
    assert outcome.decision is model.Decision.ALLOW
    with pytest.raises(model.ToolResultError):
        model.classify_tool_result(_tool_response("something else", is_error=False))


def test_policy_denial_is_recognised_without_storing_the_text() -> None:
    text = (
        "AuthorizeActionException - Tool Execution Denied: Tool call not allowed "
        "due to policy enforcement [No policy applies to the request (denied by default).]"
    )
    outcome = model.classify_tool_result(_tool_response(text, is_error=True))
    assert outcome.decision is model.Decision.DENY
    assert text not in outcome.reason
    assert outcome.body_fingerprint == model.fingerprint(text)


def test_a_non_policy_tool_error_is_not_treated_as_a_denial() -> None:
    with pytest.raises(model.ToolResultError):
        model.classify_tool_result(
            _tool_response("Runtime.HandlerNotFound", is_error=True)
        )


def test_jsonrpc_error_objects_are_classified_conservatively() -> None:
    denial = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "policy enforcement"}}
    assert model.classify_tool_result(denial).decision is model.Decision.DENY
    with pytest.raises(model.ToolResultError):
        model.classify_tool_result(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "Method not found"}}
        )
    with pytest.raises(model.ToolResultError):
        model.classify_tool_result({"jsonrpc": "2.0", "id": 1})


def test_extract_tool_names_sorts_and_validates() -> None:
    payload = {"result": {"tools": [{"name": "t___b"}, {"name": "t___a"}, {"name": "t___a"}]}}
    assert model.extract_tool_names(payload) == ("t___a", "t___b")
    with pytest.raises(model.ToolResultError):
        model.extract_tool_names({"result": {}})
    with pytest.raises(model.ToolResultError):
        model.extract_tool_names({})


# --------------------------------------------------------------------------
# Tool schema and region gate
# --------------------------------------------------------------------------


def test_echo_tool_schema_declares_the_condition_fields() -> None:
    schema = model.echo_tool_schema(model.TOOL_SUBJECT_ONLY)
    assert schema["name"] == model.TOOL_SUBJECT_ONLY
    properties = schema["inputSchema"]["properties"]
    assert model.MODE_FIELD in properties
    assert model.AUDIT_FIELD in properties


def test_region_gate_matches_the_documented_list() -> None:
    assert model.region_is_supported("us-west-2")
    assert model.region_is_supported("eu-west-1")
    assert not model.region_is_supported("me-south-1")
    assert model.POLICY_EMEA_REGIONS <= model.POLICY_SUPPORTED_REGIONS


def test_list_policies_operation_constants_are_the_summary_listing() -> None:
    # B1: the model pins ListPolicySummaries (summary listing), not ListPolicies.
    assert model.LIST_POLICIES_OPERATION == "ListPolicySummaries"
    assert model.LIST_POLICIES_METHOD == "list_policy_summaries"
    assert "policyId" in model.POLICY_SUMMARY_MEMBERS
    assert "status" in model.POLICY_SUMMARY_MEMBERS


def test_group_tampering_preserves_signature_and_rewrites_claim() -> None:
    token = _fake_token(SUB_DELTA)
    forged = model.tamper_token_groups(token, ["aiaf_pe_test_tools"])
    assert forged.split(".")[2] == token.split(".")[2]
    claims = json.loads(model._b64url_decode(forged.split(".")[1]))
    assert claims[model.GROUP_CLAIM_NAME] == ["aiaf_pe_test_tools"]


def test_group_tampering_rejects_unsafe_group_names() -> None:
    with pytest.raises(model.CedarPolicyError):
        model.tamper_token_groups(_fake_token(SUB_DELTA), ['bad"group'])
