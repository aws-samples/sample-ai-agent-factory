"""Unit tests for the AWS-free runtime+memory spike model.

Pure-function tests: no AWS call. They pin validation rules, digest-only image
policy, run-marker token derivation with strong-digest truncation, region
allow-list, status enums, exact ownership, provenance, exact handshake, memory
round-trip, and expanded secret/identifier sanitization.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import pytest

import runtime_memory_model as model

ACCOUNT = "123456789012"
REGION = "us-west-2"
PREFIX = "aiaf-rm-test"
DIGEST_IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/repo@sha256:{'a' * 64}"
TAG_IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/repo:v1"
LATEST_IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/repo:latest"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/exec"
KMS_ARN = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/12345678-1234-1234-1234-123456789012"
MARKER = "0123456789abcdef0123456789abcdef"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/aiaf_rm_test_runtime-Abc0123xyz"
MEMORY_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:memory/aiaf_rm_test_memory-Def0456uvw"


# --------------------------------------------------------------------------
# Names / exact ownership
# --------------------------------------------------------------------------


def test_names_and_exact_ownership() -> None:
    names = model.SpikeNames(prefix=PREFIX)
    assert names.runtime_name == "aiaf_rm_test_runtime"
    assert names.memory_name == "aiaf_rm_test_memory"
    # Exact, never prefix-only.
    assert names.owns(names.runtime_name) and names.owns(names.memory_name)
    assert not names.owns("aiaf_rm_test_runtime_evil")
    assert not names.owns("aiaf-rm-test-anything")
    assert not names.owns("")


@pytest.mark.parametrize("bad", ["", "-bad", "AB", "a" * 41, "has space", "UPPER"])
def test_invalid_prefix_is_refused(bad: str) -> None:
    with pytest.raises(model.ValidationError):
        model.SpikeNames(prefix=bad)


def test_allocation_tags_are_the_five_required_keys() -> None:
    tags = model.SpikeNames(prefix=PREFIX).allocation_tags(MARKER)
    assert set(tags) == {"application-id", "agent-id", "tenant-id", "cost-centre", "environment"}
    assert tags["environment"] == "nonprod"


def test_ownership_description_embeds_run_marker() -> None:
    names = model.SpikeNames(prefix=PREFIX)
    desc = names.ownership_description(MARKER)
    assert MARKER in desc and PREFIX in desc


# --------------------------------------------------------------------------
# Digest-only image policy
# --------------------------------------------------------------------------


def test_digest_image_passes() -> None:
    assert model.image_is_digest_pinned(DIGEST_IMAGE)
    assert model.validate_container_uri(DIGEST_IMAGE, account_id=ACCOUNT, region=REGION) == DIGEST_IMAGE


@pytest.mark.parametrize("image", [TAG_IMAGE, LATEST_IMAGE])
def test_tag_images_are_refused_as_mutable(image: str) -> None:
    # A tag -- even non-latest -- is mutable unless repo immutability is proven.
    assert not model.image_is_digest_pinned(image)
    with pytest.raises(model.ValidationError):
        model.validate_container_uri(image, account_id=ACCOUNT, region=REGION)


def test_container_uri_wrong_account_or_region_refused() -> None:
    with pytest.raises(model.ValidationError):
        model.validate_container_uri(DIGEST_IMAGE, account_id="999999999999", region=REGION)
    with pytest.raises(model.ValidationError):
        model.validate_container_uri(DIGEST_IMAGE, account_id=ACCOUNT, region="eu-west-1")


def test_role_and_kms_scope() -> None:
    assert model.validate_role_arn(ROLE_ARN, account_id=ACCOUNT) == ROLE_ARN
    with pytest.raises(model.ValidationError):
        model.validate_role_arn(ROLE_ARN.replace(ACCOUNT, "999999999999"), account_id=ACCOUNT)
    assert model.validate_kms_key_arn(KMS_ARN, account_id=ACCOUNT, region=REGION) == KMS_ARN
    with pytest.raises(model.ValidationError):
        model.validate_kms_key_arn(KMS_ARN, account_id=ACCOUNT, region="eu-west-1")


# --------------------------------------------------------------------------
# Region allow-list
# --------------------------------------------------------------------------


def test_region_allowlist_and_emea_subset() -> None:
    assert model.region_is_supported("us-west-2")
    assert not model.region_is_supported("ap-south-1")
    assert model.EMEA_REGIONS <= model.SUPPORTED_REGIONS


# --------------------------------------------------------------------------
# Run-marker tokens with strong-digest truncation
# --------------------------------------------------------------------------


def test_client_token_deterministic_and_strong() -> None:
    a = model.client_token(MARKER, "CreateMemory")
    b = model.client_token(MARKER, "CreateMemory")
    c = model.client_token(MARKER, "CreateAgentRuntime")
    assert a == b and a != c
    for token in (a, c):
        assert len(token) >= model.MIN_CLIENT_TOKEN_LENGTH
        # digest slice retained is at least MIN_DIGEST_RETAINED hex chars
        digest_part = token.split("-", 1)[1]
        assert len(digest_part) >= model.MIN_DIGEST_RETAINED


def test_client_token_truncation_preserves_min_digest_for_long_operation() -> None:
    token = model.client_token(MARKER, "AVeryLongOperationNameThatWouldCrowdOutTheDigest")
    digest_part = token.split("-", 1)[1]
    assert len(digest_part) >= model.MIN_DIGEST_RETAINED
    assert len(token) <= 63


def test_client_token_refuses_bad_marker_or_operation() -> None:
    with pytest.raises(model.ValidationError):
        model.client_token("not-hex", "CreateMemory")
    with pytest.raises(model.ValidationError):
        model.client_token(MARKER, "not an identifier")


def test_runtime_session_id_is_long_enough_and_deterministic() -> None:
    s = model.runtime_session_id(MARKER)
    assert model.RUNTIME_SESSION_PATTERN.fullmatch(s)
    assert len(s) >= model.MIN_CLIENT_TOKEN_LENGTH
    assert model.runtime_session_id(MARKER) == s


def test_derived_actor_session_marker_are_deterministic() -> None:
    assert model.derive_actor_id(PREFIX) == model.derive_actor_id(PREFIX)
    assert model.derive_session_id(MARKER, PREFIX) == model.derive_session_id(MARKER, PREFIX)
    assert model.derive_event_marker(PREFIX) == f"{PREFIX}-evt"


# --------------------------------------------------------------------------
# Status enums
# --------------------------------------------------------------------------


def test_runtime_status_enum() -> None:
    assert model.RUNTIME_STATUSES == {
        "CREATING", "CREATE_FAILED", "UPDATING", "UPDATE_FAILED",
        "READY", "DELETING", "DELETE_FAILED",
    }
    assert model.RUNTIME_TERMINAL_FAILURES == {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}
    assert model.classify_runtime_status("READY") == "ready"
    assert model.classify_runtime_status("CREATING") == "pending"
    for terminal in model.RUNTIME_TERMINAL_FAILURES:
        assert model.classify_runtime_status(terminal) == "terminal"
        with pytest.raises(model.StatusError):
            model.assert_not_terminal_runtime(terminal)
    with pytest.raises(model.StatusError):
        model.classify_runtime_status("UNKNOWN")


def test_memory_status_enum() -> None:
    assert model.MEMORY_STATUSES == {
        "CREATING", "ACTIVE", "FAILED", "DELETING", "UPDATING",
    }
    assert model.MEMORY_TERMINAL_FAILURES == {"FAILED"}
    assert model.classify_memory_status("ACTIVE") == "active"
    assert model.classify_memory_status("CREATING") == "pending"
    assert model.classify_memory_status("FAILED") == "terminal"
    with pytest.raises(model.StatusError):
        model.classify_memory_status("CREATE_FAILED")


# --------------------------------------------------------------------------
# Live-ownership proofs
# --------------------------------------------------------------------------


def _runtime_record(**over):  # type: ignore[no-untyped-def]
    names = model.SpikeNames(prefix=PREFIX)
    base = {
        "agentRuntimeName": names.runtime_name,
        "agentRuntimeId": "aiaf_rm_test_runtime-Abc0123xyz",
        "agentRuntimeArn": RUNTIME_ARN,
        "description": names.ownership_description(MARKER),
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": DIGEST_IMAGE}},
        "roleArn": ROLE_ARN,
    }
    base.update(over)
    return base


def _runtime_kwargs(**over):  # type: ignore[no-untyped-def]
    names = model.SpikeNames(prefix=PREFIX)
    kwargs = dict(
        names=names, run_marker=MARKER, account_id=ACCOUNT, region=REGION,
        container_uri=DIGEST_IMAGE, role_arn=ROLE_ARN, tags=names.allocation_tags(MARKER),
    )
    kwargs.update(over)
    return kwargs


def test_runtime_ownership_accepts_matching_record() -> None:
    model.assert_runtime_owned(_runtime_record(), **_runtime_kwargs())


@pytest.mark.parametrize("mutate", [
    {"agentRuntimeName": "someone_elses_runtime"},
    {"agentRuntimeId": "other_runtime-Abc0123xyz"},
    {"description": "agentcore-runtime-memory-spike run=deadbeefdeadbeefdeadbeefdeadbeef prefix=aiaf-rm-test"},
    {"roleArn": f"arn:aws:iam::{ACCOUNT}:role/other"},
    {"agentRuntimeArtifact": "malformed"},
    {"agentRuntimeArtifact": {"containerConfiguration": "malformed"}},
    {"agentRuntimeArtifact": {"containerConfiguration": {"containerUri": DIGEST_IMAGE.replace('a' * 64, 'b' * 64)}}},
    {"agentRuntimeArn": f"arn:aws:bedrock-agentcore:eu-west-1:{ACCOUNT}:runtime/aiaf_rm_test_runtime-Abc0123xyz"},
])
def test_runtime_ownership_rejects_mismatch(mutate: dict) -> None:
    with pytest.raises(model.OwnershipError):
        model.assert_runtime_owned(_runtime_record(**mutate), **_runtime_kwargs())


def test_runtime_ownership_rejects_missing_tag() -> None:
    with pytest.raises(model.OwnershipError):
        model.assert_runtime_owned(_runtime_record(), **_runtime_kwargs(tags={"application-id": PREFIX}))


def _memory_record(**over):  # type: ignore[no-untyped-def]
    names = model.SpikeNames(prefix=PREFIX)
    base = {
        "id": "aiaf_rm_test_memory-Def0456uvw",
        "name": names.memory_name,
        "arn": MEMORY_ARN,
        "description": names.ownership_description(MARKER),
        "encryptionKeyArn": KMS_ARN,
        "eventExpiryDuration": 7,
    }
    base.update(over)
    return base


def _memory_kwargs(**over):  # type: ignore[no-untyped-def]
    kwargs = dict(
        names=model.SpikeNames(prefix=PREFIX), run_marker=MARKER, account_id=ACCOUNT,
        region=REGION, kms_key_arn=KMS_ARN, event_expiry_days=7,
    )
    kwargs.update(over)
    return kwargs


def test_memory_ownership_accepts_matching_record() -> None:
    model.assert_memory_owned(_memory_record(), **_memory_kwargs())


@pytest.mark.parametrize("mutate", [
    {"id": "other_memory-Def0456uvw"},
    {"name": "someone_elses_memory"},
    {"description": "wrong"},
    {"encryptionKeyArn": KMS_ARN.replace("12345678", "87654321")},
    {"eventExpiryDuration": None},
    {"eventExpiryDuration": "not-an-integer"},
    {"eventExpiryDuration": 30},
    {"arn": f"arn:aws:bedrock-agentcore:eu-west-1:{ACCOUNT}:memory/aiaf_rm_test_memory-Def0456uvw"},
])
def test_memory_ownership_rejects_mismatch(mutate: dict) -> None:
    with pytest.raises(model.OwnershipError):
        model.assert_memory_owned(_memory_record(**mutate), **_memory_kwargs())


# --------------------------------------------------------------------------
# State provenance
# --------------------------------------------------------------------------


def test_state_header_round_trips() -> None:
    header = model.build_state_header(run_marker=MARKER, account_id=ACCOUNT, region=REGION, prefix=PREFIX)
    assert model.assert_state_provenance(header, account_id=ACCOUNT, region=REGION, prefix=PREFIX) == MARKER


@pytest.mark.parametrize("bad_kwargs", [
    {"account_id": "999999999999"},
    {"region": "eu-west-1"},
    {"prefix": "other-prefix"},
])
def test_state_provenance_rejects_foreign_state(bad_kwargs: dict) -> None:
    header = model.build_state_header(run_marker=MARKER, account_id=ACCOUNT, region=REGION, prefix=PREFIX)
    call = dict(account_id=ACCOUNT, region=REGION, prefix=PREFIX)
    call.update(bad_kwargs)
    with pytest.raises(model.ProvenanceError):
        model.assert_state_provenance(header, **call)


def test_state_provenance_rejects_wrong_schema() -> None:
    with pytest.raises(model.ProvenanceError):
        model.assert_state_provenance(
            {"schemaVersion": 1, "runMarker": MARKER, "accountId": ACCOUNT, "region": REGION, "prefix": PREFIX},
            account_id=ACCOUNT, region=REGION, prefix=PREFIX,
        )


# --------------------------------------------------------------------------
# Exact handshake + memory round-trip
# --------------------------------------------------------------------------


def test_handshake_requires_marker_fingerprint_and_ready_flag() -> None:
    good = {
        "marker": model.HANDSHAKE_MARKER,
        "echoFingerprint": model.expected_ping_fingerprint(),
        "runtimeReady": True,
    }
    assert model.handshake_is_exact(good)
    # substring is not proof
    assert not model.handshake_is_exact(f"...{model.HANDSHAKE_MARKER}...")
    assert not model.handshake_is_exact({**good, "runtimeReady": "true"})
    assert not model.handshake_is_exact({**good, "echoFingerprint": "wrong"})
    assert not model.handshake_is_exact({**good, "marker": "nope"})


def test_memory_round_trip_requires_every_field() -> None:
    event = {
        "eventId": "evt-1", "actorId": "a", "sessionId": "s", "memoryId": "m",
        "payload": [{"conversational": {"role": "USER", "content": {"text": "aiaf-rm-test-evt"}}}],
    }
    common = dict(expected_event_id="evt-1", expected_actor_id="a", expected_session_id="s",
                  expected_memory_id="m", expected_marker="aiaf-rm-test-evt")
    assert model.memory_round_trip_ok(event, **common)
    assert not model.memory_round_trip_ok({**event, "eventId": "other"}, **common)
    assert not model.memory_round_trip_ok({**event, "actorId": "b"}, **common)
    bad_payload = {
        **event,
        "payload": [
            {"conversational": {"role": "USER", "content": {"text": "x"}}}
        ],
    }
    assert not model.memory_round_trip_ok(bad_payload, **common)


# --------------------------------------------------------------------------
# Expanded secret / identifier sanitization
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["token", "authorization", "payload", "arn", "requestId", "sessionToken"])
def test_secret_shaped_keys_refused(key: str) -> None:
    with pytest.raises(model.SecretLeakError):
        model.assert_no_secret_values({key: "x"})


@pytest.mark.parametrize("value", [
    "123456789012",  # bare account id
    f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/repo@sha256:{'a' * 64}",  # ECR uri
    "12345678-1234-1234-1234-123456789012",  # uuid/request id
    "aiaf_rm_test_runtime-Abc0123xyz",  # agentcore resource id
    "sha256:" + "a" * 64,  # digest
    RUNTIME_ARN,  # arn
])
def test_secret_shaped_values_refused(value: str) -> None:
    with pytest.raises(model.SecretLeakError):
        model.assert_no_secret_values({"note": value})


def test_benign_values_pass() -> None:
    model.assert_no_secret_values({"status": "READY", "ok": True, "count": 3, "handshakeOk": False})


def test_sanitize_error_redacts_everything_sensitive() -> None:
    raw = (
        f"boom account {ACCOUNT} image {ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/r@sha256:{'a'*64} "
        f"id 12345678-1234-1234-1234-123456789012 res aiaf_rm_test_runtime-Abc0123xyz " + "z" * 400
    )
    out = model.sanitize_error(raw, prefix="verify")
    assert len(out) <= 300
    model.assert_no_secret_values(out)


def test_account_suffix_is_last_four() -> None:
    assert model.account_suffix(ACCOUNT) == "9012"
