"""Account/role manifest: schema, credential-source safety, resolution, hashing.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from harness.errors import (
    CredentialSourceError,
    ManifestError,
    ManifestSchemaError,
    SanitizationError,
)
from harness.manifest import (
    MANAGEMENT_ACCOUNT,
    PLATFORM_ACCOUNT,
    REQUIRED_ACCOUNTS,
    REQUIRED_ROLES,
    WORKSTREAM_ACCOUNT,
    canonical_json,
    load_manifest,
    manifest_sha,
    validate_manifest_document,
)

pytestmark = pytest.mark.adversarial

EXAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "manifest.example.json"


@pytest.fixture()
def document() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


@pytest.fixture()
def complete_env() -> dict[str, str]:
    return {
        "AGENTICAI_ACCOUNT_MANAGEMENT": "111111111111",
        "AGENTICAI_ACCOUNT_PLATFORM": "222222222222",
        "AGENTICAI_ACCOUNT_WORKSTREAM": "333333333333",
        "AWS_ACCESS_KEY_ID_MANAGEMENT": "redacted-by-test",
        "AWS_SECRET_ACCESS_KEY_MANAGEMENT": "redacted-by-test",
        "AWS_ACCESS_KEY_ID_PLATFORM": "redacted-by-test",
        "AWS_SECRET_ACCESS_KEY_PLATFORM": "redacted-by-test",
        "AWS_ACCESS_KEY_ID_WORKSTREAM": "redacted-by-test",
        "AWS_SECRET_ACCESS_KEY_WORKSTREAM": "redacted-by-test",
        "AGENTICAI_ADVERSARIAL_EXTERNAL_ID": "external-id-placeholder",
    }


# ---------------------------------------------------------------------------
# the shipped example manifest
# ---------------------------------------------------------------------------


def test_example_manifest_is_valid():
    manifest = load_manifest(EXAMPLE)
    assert set(manifest.accounts) == set(REQUIRED_ACCOUNTS)
    assert manifest.primary_region


def test_example_manifest_declares_all_three_accounts_with_distinct_aliases():
    manifest = load_manifest(EXAMPLE)
    aliases = {account.alias for account in manifest.accounts.values()}
    assert aliases == {"MANAGEMENT", "PLATFORM", "WORKSTREAM"}
    assert MANAGEMENT_ACCOUNT in manifest.accounts
    assert PLATFORM_ACCOUNT in manifest.accounts
    assert WORKSTREAM_ACCOUNT in manifest.accounts


def test_example_manifest_declares_every_required_role():
    manifest = load_manifest(EXAMPLE)
    for account_key, refs in REQUIRED_ROLES.items():
        for ref in refs:
            role = manifest.role(f"{account_key}.{ref}")
            assert role.role_name
            assert role.purpose


def test_example_manifest_contains_no_account_numbers():
    """The committed manifest must not embed any account id."""
    raw = EXAMPLE.read_text(encoding="utf-8")
    import re

    assert not re.search(r"(?<![0-9])[0-9]{12}(?![0-9])", raw)


def test_example_manifest_does_not_reference_a_credential_file():
    raw = EXAMPLE.read_text(encoding="utf-8").lower()
    assert ".csv" not in raw
    assert "credentials.csv" not in raw


# ---------------------------------------------------------------------------
# schema enforcement
# ---------------------------------------------------------------------------


def test_literal_account_id_is_rejected(document):
    document["accounts"][PLATFORM_ACCOUNT]["accountId"] = "222222222222"
    with pytest.raises(SanitizationError) as excinfo:
        validate_manifest_document(document)
    assert "aws-account-id" in str(excinfo.value)


def test_literal_account_id_without_a_valid_number_is_still_rejected(document):
    document["accounts"][PLATFORM_ACCOUNT]["accountId"] = "not-an-account"
    with pytest.raises(ManifestSchemaError, match="accountId is forbidden"):
        validate_manifest_document(document)


def test_csv_credential_source_is_rejected(document):
    document["accounts"][PLATFORM_ACCOUNT]["credentialSource"] = {
        "type": "csv-file",
        "value": "keys",
    }
    with pytest.raises(CredentialSourceError, match="forbidden"):
        validate_manifest_document(document)


def test_credential_file_path_in_a_source_is_rejected(document):
    document["accounts"][PLATFORM_ACCOUNT]["credentialSource"] = {
        "type": "profile",
        "value": "./platform_credentials.csv",
    }
    with pytest.raises(SanitizationError, match="credential-file-reference"):
        validate_manifest_document(document)


def test_missing_account_is_rejected(document):
    del document["accounts"][WORKSTREAM_ACCOUNT]
    with pytest.raises(ManifestSchemaError) as excinfo:
        validate_manifest_document(document)
    assert any("workstream is required" in problem for problem in excinfo.value.problems)


def test_unsupported_extra_account_is_rejected(document):
    document["accounts"]["shadow-account"] = copy.deepcopy(
        document["accounts"][PLATFORM_ACCOUNT]
    )
    with pytest.raises(ManifestSchemaError, match="unsupported keys"):
        validate_manifest_document(document)


def test_missing_required_role_is_rejected(document):
    del document["accounts"][WORKSTREAM_ACCOUNT]["roles"]["agent_runtime"]
    with pytest.raises(ManifestSchemaError) as excinfo:
        validate_manifest_document(document)
    assert any("agent_runtime is required" in p for p in excinfo.value.problems)


def test_role_arn_instead_of_role_name_is_rejected(document):
    document["accounts"][PLATFORM_ACCOUNT]["roles"]["platform_admin"]["roleName"] = (
        "arn:aws:iam::222222222222:role/Admin"
    )
    with pytest.raises(SanitizationError):
        validate_manifest_document(document)


def test_role_arn_without_an_account_number_is_still_rejected(document):
    document["accounts"][PLATFORM_ACCOUNT]["roles"]["platform_admin"]["roleName"] = (
        "arn:aws:iam::aws:role/Admin"
    )
    with pytest.raises(ManifestSchemaError, match="must be a bare role name"):
        validate_manifest_document(document)


def test_duplicate_alias_is_rejected(document):
    document["accounts"][PLATFORM_ACCOUNT]["alias"] = "WORKSTREAM"
    with pytest.raises(ManifestSchemaError, match="duplicates"):
        validate_manifest_document(document)


def test_wrong_schema_version_is_rejected(document):
    document["schemaVersion"] = "0.9"
    with pytest.raises(ManifestSchemaError, match="schemaVersion"):
        validate_manifest_document(document)


def test_all_problems_are_reported_together(document):
    document["schemaVersion"] = "0.9"
    del document["regions"]
    with pytest.raises(ManifestSchemaError) as excinfo:
        validate_manifest_document(document)
    assert len(excinfo.value.problems) >= 2


def test_missing_manifest_file_raises(tmp_path: Path):
    with pytest.raises(ManifestError, match="not found"):
        load_manifest(tmp_path / "absent.json")


def test_invalid_json_raises(tmp_path: Path):
    path = tmp_path / "manifest.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ManifestError, match="not valid JSON"):
        load_manifest(path)


def test_loading_a_credential_csv_is_refused(tmp_path: Path):
    """The loader must refuse a path that looks like a key export."""
    path = tmp_path / "platform_credentials.csv"
    with pytest.raises(SanitizationError, match="credential-file-reference"):
        load_manifest(path)


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def test_resolution_is_complete_with_a_full_environment(complete_env):
    resolved = load_manifest(EXAMPLE).resolve(complete_env)
    assert resolved.complete
    assert resolved.missing == ()
    assert resolved.account_ids[PLATFORM_ACCOUNT] == "222222222222"


def test_resolution_reports_a_missing_account_id(complete_env):
    complete_env.pop("AGENTICAI_ACCOUNT_WORKSTREAM")
    resolved = load_manifest(EXAMPLE).resolve(complete_env)
    assert not resolved.complete
    assert any("AGENTICAI_ACCOUNT_WORKSTREAM is unset" in r for r in resolved.missing)


def test_resolution_rejects_a_malformed_account_id(complete_env):
    complete_env["AGENTICAI_ACCOUNT_PLATFORM"] = "22222"
    resolved = load_manifest(EXAMPLE).resolve(complete_env)
    assert any("12-digit" in reason for reason in resolved.missing)


def test_resolution_reports_missing_credentials(complete_env):
    complete_env.pop("AWS_ACCESS_KEY_ID_PLATFORM")
    complete_env.pop("AWS_SECRET_ACCESS_KEY_PLATFORM")
    resolved = load_manifest(EXAMPLE).resolve(complete_env)
    assert any("credentials unavailable" in reason for reason in resolved.missing)


def test_a_named_profile_satisfies_the_credential_requirement(complete_env):
    complete_env.pop("AWS_ACCESS_KEY_ID_PLATFORM")
    complete_env.pop("AWS_SECRET_ACCESS_KEY_PLATFORM")
    complete_env["AWS_PROFILE_PLATFORM"] = "agenticai-platform"
    resolved = load_manifest(EXAMPLE).resolve(complete_env)
    assert resolved.complete


def test_resolution_reports_a_missing_external_id(complete_env):
    complete_env.pop("AGENTICAI_ADVERSARIAL_EXTERNAL_ID")
    resolved = load_manifest(EXAMPLE).resolve(complete_env)
    assert any("external id unavailable" in reason for reason in resolved.missing)


def test_duplicate_account_ids_are_rejected(complete_env):
    complete_env["AGENTICAI_ACCOUNT_WORKSTREAM"] = complete_env[
        "AGENTICAI_ACCOUNT_PLATFORM"
    ]
    resolved = load_manifest(EXAMPLE).resolve(complete_env)
    assert any("three distinct accounts" in reason for reason in resolved.missing)


def test_principal_lookup_returns_an_alias_not_an_account_id(complete_env):
    resolved = load_manifest(EXAMPLE).resolve(complete_env)
    principal = resolved.principal("workstream.agent_runtime")
    payload = json.dumps(principal.to_dict())
    assert principal.account_alias == "WORKSTREAM"
    assert "333333333333" not in payload


def test_unknown_principal_ref_raises():
    manifest = load_manifest(EXAMPLE)
    with pytest.raises(ManifestError, match="unknown role"):
        manifest.role("platform.no_such_role")
    with pytest.raises(ManifestError, match="unknown account"):
        manifest.role("nowhere.platform_admin")
    with pytest.raises(ManifestError, match="must be"):
        manifest.role("platform_admin")


def test_account_aliases_table_maps_ids_to_aliases(complete_env):
    resolved = load_manifest(EXAMPLE).resolve(complete_env)
    assert resolved.account_aliases()["222222222222"] == "PLATFORM"


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------


def test_manifest_sha_is_stable_and_key_order_independent(document):
    reordered = json.loads(json.dumps(document, sort_keys=True))
    assert manifest_sha(document) == manifest_sha(reordered)
    assert len(manifest_sha(document)) == 64


def test_manifest_sha_changes_when_a_role_changes(document):
    before = manifest_sha(document)
    document["accounts"][PLATFORM_ACCOUNT]["roles"]["platform_admin"]["roleName"] = (
        "AgenticAI-Platform-Admin2"
    )
    assert manifest_sha(document) != before


def test_canonical_json_is_compact_and_sorted(document):
    text = canonical_json(document)
    assert "\n" not in text
    assert text.startswith('{"accounts":')
    assert '"schemaVersion":"1.0"' in text
