"""Sanitization: nothing sensitive reaches an evidence bundle.

The fake secrets below are syntactically shaped like the real thing but are not
credentials for anything. No real key material appears in this repository.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import pytest

from harness.errors import SanitizationError
from harness.sanitize import (
    alias_map,
    assert_sanitized,
    findings_by_kind,
    forbid_credential_file,
    sanitize_text,
    sanitize_value,
    scan_for_secrets,
)

pytestmark = pytest.mark.adversarial

ALIASES = alias_map({"222222222222": "PLATFORM", "333333333333": "WORKSTREAM"})

# Shaped like AWS identifiers; not credentials for any account.
FAKE_ACCESS_KEY_ID = "AKIA" + "ABCDEFGHIJKLMNOP"
FAKE_SESSION_KEY_ID = "ASIA" + "ABCDEFGHIJKLMNOP"
FAKE_SESSION_TOKEN = "FwoG" + "Z" * 120
COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"


def test_known_account_id_becomes_its_alias():
    assert sanitize_text("arn:aws:iam::222222222222:role/X", ALIASES) == (
        "arn:aws:iam::<account:PLATFORM>:role/X"
    )


def test_unknown_account_id_is_redacted():
    assert "<account:REDACTED>" in sanitize_text("444444444444", ALIASES)


def test_access_key_id_is_redacted():
    assert FAKE_ACCESS_KEY_ID not in sanitize_text(FAKE_ACCESS_KEY_ID, ALIASES)


def test_session_key_id_is_redacted():
    assert FAKE_SESSION_KEY_ID not in sanitize_text(FAKE_SESSION_KEY_ID, ALIASES)


def test_session_token_is_redacted():
    assert FAKE_SESSION_TOKEN not in sanitize_text(FAKE_SESSION_TOKEN, ALIASES)


def test_bearer_token_is_redacted():
    cleaned = sanitize_text("Authorization: Bearer abc.def.ghijklmnop", ALIASES)
    assert "abc.def.ghijklmnop" not in cleaned


def test_private_key_header_is_redacted():
    cleaned = sanitize_text("-----BEGIN RSA PRIVATE KEY-----", ALIASES)
    assert "PRIVATE KEY" not in cleaned


def test_email_address_is_replaced_with_a_placeholder():
    assert sanitize_text("builder@example.com", ALIASES) == "<principal-email>"


def test_credential_file_reference_is_removed():
    cleaned = sanitize_text("read from /tmp/accessKeys.csv", ALIASES)
    assert ".csv" not in cleaned


def test_commit_sha_survives_sanitization():
    """A 40-hex SHA must not be mistaken for a secret."""
    assert sanitize_text(COMMIT_SHA, ALIASES) == COMMIT_SHA


def test_manifest_sha_survives_sanitization():
    sha256 = "a" * 64
    assert sanitize_text(sha256, ALIASES) == sha256


def test_request_id_survives_sanitization():
    request_id = "7f3c1b2a-4d5e-6f70-8901-23456789abcd"
    assert sanitize_text(request_id, ALIASES) == request_id


def test_nested_structures_are_sanitized():
    payload = {
        "principal": {"arn": "arn:aws:iam::333333333333:role/R"},
        "trail": ["222222222222", {"token": FAKE_SESSION_TOKEN}],
    }
    cleaned = sanitize_value(payload, ALIASES)
    assert cleaned["principal"]["arn"].endswith("<account:WORKSTREAM>:role/R")
    assert cleaned["trail"][0] == "<account:PLATFORM>"
    assert FAKE_SESSION_TOKEN not in str(cleaned)


def test_numeric_account_id_is_caught():
    findings = scan_for_secrets({"accountId": 222222222222})
    assert findings_by_kind(findings) == {"aws-account-id": 1}
    assert sanitize_value({"accountId": 222222222222}) == {
        "accountId": "<account:REDACTED>"
    }


def test_assert_sanitized_passes_for_clean_payloads():
    assert_sanitized(
        {
            "caseId": "SCP-01-N",
            "accountAlias": "PLATFORM",
            "commitSha": COMMIT_SHA,
            "errorCode": "AccessDeniedException",
        }
    )


def test_assert_sanitized_raises_for_an_account_id():
    with pytest.raises(SanitizationError) as excinfo:
        assert_sanitized({"message": "arn:aws:iam::222222222222:role/R"})
    assert "aws-account-id" in str(excinfo.value)


def test_sanitization_error_never_echoes_the_secret():
    with pytest.raises(SanitizationError) as excinfo:
        assert_sanitized({"token": FAKE_SESSION_TOKEN})
    rendered = str(excinfo.value)
    assert FAKE_SESSION_TOKEN not in rendered
    assert "session-token" in rendered


def test_findings_report_the_path():
    findings = scan_for_secrets({"observed": {"arn": "222222222222"}})
    assert findings[0].path == "$.observed.arn"


def test_forbid_credential_file_rejects_a_key_export():
    key_export = "/" + "/".join(
        ("Users", "someone", "Downloads", "platform_credentials.csv")
    )
    with pytest.raises(SanitizationError, match="credential-file-reference"):
        forbid_credential_file(key_export)


def test_forbid_credential_file_allows_a_normal_path():
    forbid_credential_file("tests/adversarial/fixtures/manifest.example.json")


def test_alias_map_builds_substitution_tokens():
    assert alias_map({"111111111111": "MANAGEMENT"}) == {
        "111111111111": "<account:MANAGEMENT>"
    }
