#!/usr/bin/env python3
"""Offline unit tests for :mod:`identity_m2m_model`.

Pure, AWS-free assertions on the security-critical model: validation, run-marker
token derivation, provider status classification, resource naming + ownership,
provider-config assembly, state provenance, and the recursive secret scanner.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import pytest

import identity_m2m_model as model

SOURCE_REVISION = "a" * 40


# --------------------------------------------------------------------------
# Service-model pins
# --------------------------------------------------------------------------


def test_pins_match_expected_contract():
    assert model.REQUIRED_BOTO3_VERSION == "1.43.98"
    assert model.REQUIRED_BOTOCORE_VERSION == "1.43.98"
    assert model.CREDENTIAL_PROVIDER_VENDOR == "CognitoOauth2"
    assert model.PROVIDER_CONFIG_MEMBER == "includedOauth2ProviderConfig"
    assert model.OAUTH2_FLOW_M2M == "M2M"


def test_provider_status_enum_is_exact():
    assert model.PROVIDER_STATUSES == frozenset(
        {
            "CREATING",
            "CREATE_FAILED",
            "UPDATING",
            "UPDATE_FAILED",
            "READY",
            "DELETING",
            "DELETE_FAILED",
        }
    )
    assert model.PROVIDER_TERMINAL_FAILURES == frozenset(
        {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}
    )


def test_required_members_present_for_all_official_operations():
    assert model.CONTROL_OPERATIONS["CreateOauth2CredentialProvider"] == (
        "name",
        "credentialProviderVendor",
        "oauth2ProviderConfigInput",
    )
    assert model.DATA_OPERATIONS["GetWorkloadAccessToken"] == ("workloadName",)
    assert model.CONTROL_OPERATIONS["ListTagsForResource"] == ("resourceArn",)
    assert model.DATA_OPERATIONS["GetResourceOauth2Token"] == (
        "workloadIdentityToken",
        "resourceCredentialProviderName",
        "scopes",
        "oauth2Flow",
    )


# --------------------------------------------------------------------------
# Provider status classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        ("READY", "ready"),
        ("CREATING", "pending"),
        ("UPDATING", "pending"),
        ("DELETING", "pending"),
        ("CREATE_FAILED", "terminal"),
        ("UPDATE_FAILED", "terminal"),
        ("DELETE_FAILED", "terminal"),
    ],
)
def test_classify_provider_status(status, expected):
    assert model.classify_provider_status(status) == expected


def test_classify_provider_status_rejects_unknown():
    with pytest.raises(model.StatusError):
        model.classify_provider_status("SOMETHING_ELSE")


def test_assert_not_terminal_provider():
    model.assert_not_terminal_provider("CREATING")  # no raise
    with pytest.raises(model.StatusError):
        model.assert_not_terminal_provider("CREATE_FAILED")
    with pytest.raises(model.StatusError):
        model.assert_not_terminal_provider("MYSTERY")


# --------------------------------------------------------------------------
# Run markers + tokens
# --------------------------------------------------------------------------


def test_new_run_marker_is_valid_hex():
    marker = model.new_run_marker()
    assert model.RUN_MARKER_PATTERN.fullmatch(marker)
    assert model.validate_run_marker(marker) == marker


def test_validate_run_marker_rejects_bad():
    for bad in ("", "xyz", "0" * 31, "0" * 33, "G" * 32):
        with pytest.raises(model.ValidationError):
            model.validate_run_marker(bad)


def test_client_token_deterministic_and_long_enough():
    marker = "a" * 32
    t1 = model.client_token(marker, "CreateWorkloadIdentity")
    t2 = model.client_token(marker, "CreateWorkloadIdentity")
    assert t1 == t2
    assert len(t1) >= model.MIN_CLIENT_TOKEN_LENGTH
    other = model.client_token(marker, "DeleteWorkloadIdentity")
    assert other != t1


def test_client_token_rejects_bad_operation():
    with pytest.raises(model.ValidationError):
        model.client_token("a" * 32, "not an identifier")


def test_marker_suffix_is_short_stable_and_hides_marker():
    marker = "b" * 32
    suffix = model.marker_suffix(marker)
    assert len(suffix) == 12
    assert marker not in suffix
    assert model.marker_suffix(marker) == suffix


# --------------------------------------------------------------------------
# Naming + ownership
# --------------------------------------------------------------------------


def _names(marker: str = "c" * 32) -> model.SpikeNames:
    return model.SpikeNames(prefix="aiaf-idm2m", run_marker=marker)


def test_names_are_valid_and_distinct():
    names = _names()
    assert model.RESOURCE_NAME_PATTERN.fullmatch(names.workload_name)
    assert model.RESOURCE_NAME_PATTERN.fullmatch(names.provider_name)
    assert names.workload_name != names.provider_name
    assert "-" not in names.workload_name  # hyphens are invalid in the charset


def test_names_embed_run_marker_fingerprint():
    a = _names("c" * 32)
    b = _names("d" * 32)
    assert a.workload_name != b.workload_name
    assert a.provider_name != b.provider_name


def test_owns_helpers_are_exact():
    names = _names()
    assert names.owns_workload(names.workload_name)
    assert not names.owns_workload(names.provider_name)
    assert not names.owns_workload(names.workload_name + "x")
    assert names.owns_provider(names.provider_name)
    assert not names.owns_provider("aiaf-idm2m_cp_deadbeef")


def test_allocation_tags_carry_five_required_keys():
    tags = _names().allocation_tags()
    assert set(tags) == {
        "application-id",
        "agent-id",
        "tenant-id",
        "cost-centre",
        "environment",
    }
    assert tags["environment"] == "nonprod"


def test_names_reject_bad_prefix():
    with pytest.raises(model.ValidationError):
        model.SpikeNames(prefix="Bad Prefix", run_marker="c" * 32)


def test_assert_workload_owned_accepts_matching_arn_and_tags():
    names = _names()
    arn = (
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:"
        f"workload-identity-directory/default/workload-identity/{names.workload_name}"
    )
    model.assert_workload_owned(
        {"name": names.workload_name, "workloadIdentityArn": arn},
        tags=names.allocation_tags(),
        names=names,
        account_id="123456789012",
        region="us-west-2",
    )


def test_assert_workload_owned_rejects_wrong_name_scope_or_tags():
    names = _names()
    with pytest.raises(model.OwnershipError):
        model.assert_workload_owned(
            {"name": "someone-else"},
            tags=names.allocation_tags(),
            names=names,
            account_id="123456789012",
            region="us-west-2",
        )
    bad_arn = (
        "arn:aws:bedrock-agentcore:us-east-1:999999999999:"
        f"workload-identity-directory/default/workload-identity/{names.workload_name}"
    )
    with pytest.raises(model.OwnershipError):
        model.assert_workload_owned(
            {"name": names.workload_name, "workloadIdentityArn": bad_arn},
            tags=names.allocation_tags(),
            names=names,
            account_id="123456789012",
            region="us-west-2",
        )
    arn = (
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:"
        f"workload-identity-directory/default/workload-identity/{names.workload_name}"
    )
    with pytest.raises(model.OwnershipError):
        model.assert_workload_owned(
            {"name": names.workload_name, "workloadIdentityArn": arn},
            tags={**names.allocation_tags(), "environment": "prod"},
            names=names,
            account_id="123456789012",
            region="us-west-2",
        )


def test_assert_provider_owned_checks_vendor_scope_and_tags():
    names = _names()
    arn = (
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:"
        f"token-vault/default/oauth2credentialprovider/{names.provider_name}"
    )
    record = {
        "name": names.provider_name,
        "credentialProviderVendor": "CognitoOauth2",
        "credentialProviderArn": arn,
    }
    model.assert_provider_owned(
        record,
        tags=names.allocation_tags(),
        names=names,
        account_id="123456789012",
        region="us-west-2",
    )
    with pytest.raises(model.OwnershipError):
        model.assert_provider_owned(
            {**record, "credentialProviderVendor": "GoogleOauth2"},
            tags=names.allocation_tags(),
            names=names,
            account_id="123456789012",
            region="us-west-2",
        )


def test_is_owned_predicates_return_bool():
    names = _names()
    workload_arn = (
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:"
        f"workload-identity-directory/default/workload-identity/{names.workload_name}"
    )
    assert model.is_workload_owned(
        {"name": names.workload_name, "workloadIdentityArn": workload_arn},
        tags=names.allocation_tags(),
        names=names,
        account_id="123456789012",
        region="us-west-2",
    )
    assert not model.is_provider_owned(
        {"name": "nope"},
        tags=names.allocation_tags(),
        names=names,
        account_id="123456789012",
        region="us-west-2",
    )


# --------------------------------------------------------------------------
# Endpoint and route binding
# --------------------------------------------------------------------------


def test_cognito_endpoint_bundle_is_bound_to_pool_and_region():
    pool = "us-west-2_ExamplePool"
    host = "example.auth.us-west-2.amazoncognito.com"
    assert model.validate_cognito_endpoint_bundle(
        region="us-west-2",
        user_pool_id=pool,
        issuer=f"https://cognito-idp.us-west-2.amazonaws.com/{pool}",
        authorization_endpoint=f"https://{host}/oauth2/authorize",
        token_endpoint=f"https://{host}/oauth2/token",
    )[0].endswith(pool)
    with pytest.raises(model.ValidationError):
        model.validate_cognito_endpoint_bundle(
            region="us-west-2",
            user_pool_id=pool,
            issuer=f"https://cognito-idp.us-east-1.amazonaws.com/{pool}",
            authorization_endpoint=f"https://{host}/oauth2/authorize",
            token_endpoint=f"https://{host}/oauth2/token",
        )


def test_gateway_url_refuses_non_agentcore_token_destination():
    valid = "https://example.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"
    assert model.validate_gateway_url(valid, region="us-west-2") == valid
    with pytest.raises(model.ValidationError):
        model.validate_gateway_url("https://example.invalid/mcp", region="us-west-2")


def test_inference_base_url_strips_mcp_and_does_not_nest():
    # Regression: the OpenAI-compatible API is a SIBLING of /mcp, not nested
    # under it. Concatenating /inference/v1 onto the /mcp URL yields
    # .../mcp/inference/v1 which the Gateway rejects with HTTP 400.
    gw = "https://example.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"
    assert (
        model.inference_base_url(gw)
        == "https://example.gateway.bedrock-agentcore.us-west-2.amazonaws.com/inference/v1"
    )
    # Trailing slash tolerated.
    assert model.inference_base_url(gw + "/") == model.inference_base_url(gw)
    # Must never contain the erroneous /mcp/inference nesting.
    assert "/mcp/inference" not in model.inference_base_url(gw)
    with pytest.raises(model.ValidationError):
        model.inference_base_url(
            "https://example.gateway.bedrock-agentcore.us-west-2.amazonaws.com/other"
        )


def test_target_qualified_model_and_source_revision_validation():
    assert model.validate_target_qualified_model_id(
        "target-name/openai.gpt-oss-120b"
    ) == "target-name/openai.gpt-oss-120b"
    assert model.validate_source_revision(SOURCE_REVISION) == SOURCE_REVISION
    with pytest.raises(model.ValidationError):
        model.validate_target_qualified_model_id("openai.gpt-oss-120b")


# --------------------------------------------------------------------------
# Provider-config assembly
# --------------------------------------------------------------------------


def test_build_included_provider_config_shape():
    cfg = model.build_included_provider_config(
        client_id="abc123",
        client_secret="s3cr3t-value",
        issuer="https://issuer.example.com",
        authorization_endpoint="https://issuer.example.com/oauth2/authorize",
        token_endpoint="https://issuer.example.com/oauth2/token",
    )
    inner = cfg[model.PROVIDER_CONFIG_MEMBER]
    assert inner["clientId"] == "abc123"
    assert inner["clientSecret"] == "s3cr3t-value"
    assert set(inner) == {
        "clientId",
        "clientSecret",
        "issuer",
        "authorizationEndpoint",
        "tokenEndpoint",
    }


def test_build_included_provider_config_rejects_empty_secret():
    with pytest.raises(model.ValidationError):
        model.build_included_provider_config(
            client_id="abc123",
            client_secret="",
            issuer="https://i.example.com",
            authorization_endpoint="https://i.example.com/a",
            token_endpoint="https://i.example.com/t",
        )


def test_build_included_provider_config_rejects_non_https():
    with pytest.raises(model.ValidationError):
        model.build_included_provider_config(
            client_id="abc123",
            client_secret="x",
            issuer="http://insecure.example.com",
            authorization_endpoint="https://i.example.com/a",
            token_endpoint="https://i.example.com/t",
        )


# --------------------------------------------------------------------------
# Scope validation
# --------------------------------------------------------------------------


def test_validate_scopes():
    assert model.validate_scopes(["res-server/read"]) == ["res-server/read"]
    with pytest.raises(model.ValidationError):
        model.validate_scopes([])
    with pytest.raises(model.ValidationError):
        model.validate_scopes(["has space"])


# --------------------------------------------------------------------------
# State provenance
# --------------------------------------------------------------------------


def test_state_header_round_trip():
    marker = "e" * 32
    header = model.build_state_header(
        run_marker=marker,
        account_id="123456789012",
        region="us-west-2",
        prefix="aiaf-x",
        source_revision=SOURCE_REVISION,
    )
    got = model.assert_state_provenance(
        header,
        account_id="123456789012",
        region="us-west-2",
        prefix="aiaf-x",
        source_revision=SOURCE_REVISION,
    )
    assert got == marker


def test_state_provenance_rejects_mismatch():
    header = model.build_state_header(
        run_marker="e" * 32,
        account_id="123456789012",
        region="us-west-2",
        prefix="aiaf-x",
        source_revision=SOURCE_REVISION,
    )
    with pytest.raises(model.ProvenanceError):
        model.assert_state_provenance(
            header,
            account_id="000000000000",
            region="us-west-2",
            prefix="aiaf-x",
            source_revision=SOURCE_REVISION,
        )
    with pytest.raises(model.ProvenanceError):
        model.assert_state_provenance(
            {"schemaVersion": 999},
            account_id="123456789012",
            region="us-west-2",
            prefix="aiaf-x",
            source_revision=SOURCE_REVISION,
        )
    with pytest.raises(model.ProvenanceError):
        model.assert_state_provenance(
            header,
            account_id="123456789012",
            region="us-west-2",
            prefix="aiaf-x",
            source_revision="b" * 40,
        )


# --------------------------------------------------------------------------
# Secret / identifier safety -- the crux of the no-leak guarantee
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["authorization", "token", "clientSecret", "access_key", "credentialArn", "bearer"],
)
def test_secret_keys_are_rejected(key):
    assert model.is_secret_key(key)
    with pytest.raises(model.SecretLeakError):
        model.assert_no_secret_values({key: "whatever"})


@pytest.mark.parametrize(
    "key", ["tokenLength", "scopeFingerprint", "nameFingerprint", "modelCount", "accountSuffix"]
)
def test_derived_metadata_keys_are_allowed(key):
    # These contain a credential fragment but name a derived, non-secret value.
    assert not model.is_secret_key(key)
    model.assert_no_secret_values({key: 42})  # no raise


@pytest.mark.parametrize(
    "value",
    [
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",  # JWT header
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:token-vault/default/x",
        "123456789012",  # account id
        "AKIAIOSFODNN7EXAMPLE",
        "Bearer sometoken",
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",  # uuid
        "eyJhbGci.eyJzdWIiOiIxMjM0NTY3ODkw.SflKxwRJSMeKKF2QT4fwpM",  # jwt triple
    ],
)
def test_secret_shaped_values_are_rejected(value):
    with pytest.raises(model.SecretLeakError):
        model.assert_no_secret_values({"note": value})


def test_safe_evidence_payload_passes():
    payload = {
        "found": True,
        "modelCount": 49,
        "tokenLength": 812,
        "nameFingerprint": model.fingerprint("aiaf_x_wl_deadbeef0000"),
        "accountSuffix": "9012",
    }
    assert model.assert_no_secret_values(payload) is payload


def test_fingerprint_is_stable_and_short():
    f = model.fingerprint("some-non-secret-id")
    assert len(f) == 32
    assert model.fingerprint("some-non-secret-id") == f


def test_account_suffix():
    assert model.account_suffix("123456789012") == "9012"
    with pytest.raises(model.ValidationError):
        model.account_suffix("nope")


def test_safe_token_length_never_returns_value():
    assert model.safe_token_length("abcdef") == 6
    assert model.safe_token_length("") is None
    assert model.safe_token_length(None) is None


def test_sanitize_error_redacts_ids():
    msg = "failed for account 123456789012 arn:aws:iam::123456789012:role/x"
    out = model.sanitize_error(msg, prefix="verify")
    assert "123456789012" not in out
    model.assert_no_secret_values(out)  # no raise


def test_token_shape_proofs():
    assert model.workload_access_token_ok("x" * 40)
    assert not model.workload_access_token_ok("short")
    assert not model.workload_access_token_ok(None)
    assert model.resource_token_ok("y" * 40)
    assert not model.resource_token_ok("")
