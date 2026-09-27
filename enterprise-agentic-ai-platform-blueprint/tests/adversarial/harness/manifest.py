"""Account/role manifest for the three-account deployment.

The manifest is the single declaration of *which* accounts and *which* roles
the adversarial suite is allowed to touch, and of how credentials for each are
obtained. It is a committed, non-secret document:

* account ids are declared **indirectly**, by naming the environment variable
  that supplies them (``accountIdEnv``). A literal ``accountId`` is rejected so
  that no account number is ever committed.
* credential sources are declared by *kind* (env prefix, named profile, or an
  assume-role chain). Any reference to an on-disk credential export (an
  access-key ``*.csv``) is rejected outright — the harness never reads one.
* the document hashes to a ``manifestSha`` that every evidence record carries,
  so a bundle can be tied back to the exact account/role topology it was
  produced against.

The three accounts are fixed by the architecture: Management/Governance,
Platform, Workstream.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .errors import CredentialSourceError, ManifestError, ManifestSchemaError
from .sanitize import assert_sanitized, forbid_credential_file

SCHEMA_VERSION = "1.0"

MANAGEMENT_ACCOUNT = "management-governance"
PLATFORM_ACCOUNT = "platform"
WORKSTREAM_ACCOUNT = "workstream"

#: Account keys that must all be present. The architecture has exactly three.
REQUIRED_ACCOUNTS: tuple[str, ...] = (
    MANAGEMENT_ACCOUNT,
    PLATFORM_ACCOUNT,
    WORKSTREAM_ACCOUNT,
)

#: Role refs each account must declare. Catalog cases address principals by
#: ``"<account>.<role_ref>"``, so this set is the contract between the catalog
#: and any concrete manifest.
REQUIRED_ROLES: Mapping[str, tuple[str, ...]] = {
    MANAGEMENT_ACCOUNT: (
        "org_admin",
        "audit_reader",
        "evidence_archive_writer",
        "unprivileged_probe",
    ),
    PLATFORM_ACCOUNT: (
        "platform_admin",
        "inference_gateway_invoker",
        "registry_admin",
        "registry_approver",
        "registry_reader",
        "agent_builder_inspect",
        "pipeline_deploy",
        "unprivileged_probe",
    ),
    WORKSTREAM_ACCOUNT: (
        "workstream_admin",
        "agent_runtime",
        "tool_gateway_invoker",
        "memory_client",
        "cicd_deploy",
        "developer_readonly",
        "unprivileged_probe",
    ),
}

_ACCOUNT_ID_RE = re.compile(r"\A[0-9]{12}\Z")
_ENV_NAME_RE = re.compile(r"\A[A-Z][A-Z0-9_]{2,63}\Z")
_ALIAS_RE = re.compile(r"\A[A-Z][A-Z0-9_]{1,31}\Z")
_ROLE_NAME_RE = re.compile(r"\A[A-Za-z0-9+=,.@_-]{1,64}\Z")


class CredentialSourceType(str, Enum):
    """How credentials for an account are obtained."""

    ENV_PREFIX = "env-prefix"
    PROFILE = "profile"
    ASSUME_ROLE_CHAIN = "assume-role-chain"


#: Explicitly forbidden source kinds, named so the error is unambiguous.
FORBIDDEN_CREDENTIAL_SOURCE_TYPES: frozenset[str] = frozenset(
    {"csv", "csv-file", "file", "access-key-file", "static", "inline"}
)


@dataclass(frozen=True)
class CredentialSource:
    type: CredentialSourceType
    value: str
    external_id_env: str | None = None
    via: str | None = None

    def required_env(self) -> tuple[str, ...]:
        """Env vars that must be set for this source to be usable."""
        if self.type is CredentialSourceType.ENV_PREFIX:
            return (
                f"AWS_ACCESS_KEY_ID_{self.value}",
                f"AWS_SECRET_ACCESS_KEY_{self.value}",
            )
        if self.type is CredentialSourceType.PROFILE:
            return ()
        return ()

    def alternative_env(self) -> tuple[str, ...]:
        """Alternative env vars that also satisfy this source."""
        if self.type is CredentialSourceType.ENV_PREFIX:
            return (f"AWS_PROFILE_{self.value}",)
        if self.type is CredentialSourceType.PROFILE:
            return (f"AWS_PROFILE_{self.value.upper().replace('-', '_')}",)
        return ()


@dataclass(frozen=True)
class RoleSpec:
    """A role the suite may act as, or attack."""

    ref: str
    account_key: str
    role_name: str
    purpose: str
    privilege: str
    assumable_by: tuple[str, ...] = ()

    @property
    def principal_ref(self) -> str:
        return f"{self.account_key}.{self.ref}"


@dataclass(frozen=True)
class AccountSpec:
    key: str
    alias: str
    purpose: str
    account_id_env: str
    credential_source: CredentialSource
    roles: Mapping[str, RoleSpec]


@dataclass(frozen=True)
class AccountManifest:
    """A validated manifest declaration (no resolved account ids)."""

    schema_version: str
    partition: str
    primary_region: str
    additional_regions: tuple[str, ...]
    accounts: Mapping[str, AccountSpec]
    document: Mapping[str, Any]
    source_path: Path | None = None

    @property
    def manifest_sha(self) -> str:
        return manifest_sha(self.document)

    def account(self, key: str) -> AccountSpec:
        try:
            return self.accounts[key]
        except KeyError as exc:  # pragma: no cover - guarded by validation
            raise ManifestError(f"unknown account key {key!r}") from exc

    def role(self, principal_ref: str) -> RoleSpec:
        """Resolve ``"<account>.<role_ref>"`` to a :class:`RoleSpec`."""
        if "." not in principal_ref:
            raise ManifestError(
                f"principal ref {principal_ref!r} must be '<account>.<role_ref>'"
            )
        account_key, _, role_ref = principal_ref.partition(".")
        account = self.accounts.get(account_key)
        if account is None:
            raise ManifestError(
                f"principal ref {principal_ref!r} names unknown account "
                f"{account_key!r}"
            )
        role = account.roles.get(role_ref)
        if role is None:
            raise ManifestError(
                f"principal ref {principal_ref!r} names unknown role "
                f"{role_ref!r} in account {account_key!r}"
            )
        return role

    def principal_refs(self) -> tuple[str, ...]:
        refs: list[str] = []
        for account in self.accounts.values():
            for role in account.roles.values():
                refs.append(role.principal_ref)
        return tuple(sorted(refs))

    def resolve(self, env: Mapping[str, str] | None = None) -> "ResolvedManifest":
        return resolve_manifest(self, env)


@dataclass(frozen=True)
class ResolvedPrincipal:
    """A principal reference safe to place in evidence (no account id)."""

    principal_ref: str
    account_alias: str
    role_name: str
    privilege: str

    def to_dict(self) -> dict[str, str]:
        return {
            "principalRef": self.principal_ref,
            "accountAlias": self.account_alias,
            "roleName": self.role_name,
            "privilege": self.privilege,
        }


@dataclass(frozen=True)
class ResolvedManifest:
    """A manifest with account ids bound from the environment.

    ``missing`` lists every reason the mapping is incomplete. It is the input
    to the live-mode gate: a non-empty ``missing`` means live verification is
    not available.
    """

    manifest: AccountManifest
    account_ids: Mapping[str, str]
    missing: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def manifest_sha(self) -> str:
        return self.manifest.manifest_sha

    def account_aliases(self) -> dict[str, str]:
        """``{account_id: ALIAS}`` for the sanitizer's substitution table."""
        table: dict[str, str] = {}
        for key, account_id in self.account_ids.items():
            account = self.manifest.accounts.get(key)
            if account is not None and account_id:
                table[account_id] = account.alias
        return table

    def principal(self, principal_ref: str) -> ResolvedPrincipal:
        role = self.manifest.role(principal_ref)
        account = self.manifest.account(role.account_key)
        return ResolvedPrincipal(
            principal_ref=principal_ref,
            account_alias=account.alias,
            role_name=role.role_name,
            privilege=role.privilege,
        )


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------


def canonical_json(document: Mapping[str, Any]) -> str:
    """Stable serialization used for the manifest hash."""
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def manifest_sha(document: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical manifest declaration."""
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# validation + loading
# ---------------------------------------------------------------------------


def _validate_credential_source(
    account_key: str, raw: Any, problems: list[str]
) -> CredentialSource | None:
    if not isinstance(raw, Mapping):
        problems.append(f"accounts.{account_key}.credentialSource must be an object")
        return None
    raw_type = raw.get("type")
    if not isinstance(raw_type, str) or not raw_type:
        problems.append(f"accounts.{account_key}.credentialSource.type is required")
        return None
    if raw_type in FORBIDDEN_CREDENTIAL_SOURCE_TYPES:
        raise CredentialSourceError(
            f"accounts.{account_key}.credentialSource.type={raw_type!r} is "
            "forbidden: credentials must come from short-lived environment "
            "credentials, a named profile, or an assume-role chain — never "
            "from a committed literal or an on-disk key export"
        )
    try:
        source_type = CredentialSourceType(raw_type)
    except ValueError:
        problems.append(
            f"accounts.{account_key}.credentialSource.type={raw_type!r} is not one of "
            + ", ".join(sorted(member.value for member in CredentialSourceType))
        )
        return None
    value = raw.get("value")
    if not isinstance(value, str) or not value:
        problems.append(f"accounts.{account_key}.credentialSource.value is required")
        return None
    forbid_credential_file(value)
    external_id_env = raw.get("externalIdEnv")
    if external_id_env is not None and not isinstance(external_id_env, str):
        problems.append(
            f"accounts.{account_key}.credentialSource.externalIdEnv must be a string"
        )
        external_id_env = None
    if external_id_env and not _ENV_NAME_RE.match(external_id_env):
        problems.append(
            f"accounts.{account_key}.credentialSource.externalIdEnv "
            f"{external_id_env!r} is not an UPPER_SNAKE env var name"
        )
    via = raw.get("via")
    if via is not None and not isinstance(via, str):
        problems.append(f"accounts.{account_key}.credentialSource.via must be a string")
        via = None
    if source_type is CredentialSourceType.ASSUME_ROLE_CHAIN and not via:
        problems.append(
            f"accounts.{account_key}.credentialSource.via is required for "
            "assume-role-chain sources"
        )
    return CredentialSource(
        type=source_type,
        value=value,
        external_id_env=external_id_env,
        via=via,
    )


def _validate_role(
    account_key: str, role_ref: str, raw: Any, problems: list[str]
) -> RoleSpec | None:
    path = f"accounts.{account_key}.roles.{role_ref}"
    if not isinstance(raw, Mapping):
        problems.append(f"{path} must be an object")
        return None
    role_name = raw.get("roleName")
    if not isinstance(role_name, str) or not role_name:
        problems.append(f"{path}.roleName is required")
        return None
    if role_name.startswith("arn:"):
        problems.append(
            f"{path}.roleName must be a bare role name, not an ARN "
            "(an ARN would embed an account id in a committed file)"
        )
        return None
    if not _ROLE_NAME_RE.match(role_name):
        problems.append(f"{path}.roleName {role_name!r} is not a valid IAM role name")
    purpose = raw.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip():
        problems.append(f"{path}.purpose is required (say what this principal is for)")
        purpose = ""
    privilege = raw.get("privilege")
    if privilege not in ("privileged", "least-privilege", "unprivileged"):
        problems.append(
            f"{path}.privilege must be one of 'privileged', 'least-privilege', "
            "'unprivileged'"
        )
        privilege = "unprivileged"
    assumable_by = raw.get("assumableBy", [])
    if not isinstance(assumable_by, (list, tuple)) or any(
        not isinstance(item, str) for item in assumable_by
    ):
        problems.append(f"{path}.assumableBy must be an array of principal refs")
        assumable_by = []
    return RoleSpec(
        ref=role_ref,
        account_key=account_key,
        role_name=role_name,
        purpose=purpose,
        privilege=privilege,
        assumable_by=tuple(assumable_by),
    )


def _validate_account(
    account_key: str, raw: Any, problems: list[str], seen_aliases: dict[str, str]
) -> AccountSpec | None:
    path = f"accounts.{account_key}"
    if not isinstance(raw, Mapping):
        problems.append(f"{path} must be an object")
        return None

    if "accountId" in raw:
        problems.append(
            f"{path}.accountId is forbidden — declare 'accountIdEnv' instead so "
            "no account number is committed"
        )

    alias = raw.get("alias")
    if not isinstance(alias, str) or not _ALIAS_RE.match(alias or ""):
        problems.append(f"{path}.alias must be an UPPER_SNAKE label")
        alias = account_key.upper().replace("-", "_")
    elif alias in seen_aliases:
        problems.append(
            f"{path}.alias {alias!r} duplicates accounts.{seen_aliases[alias]}.alias"
        )
    else:
        seen_aliases[alias] = account_key

    purpose = raw.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip():
        problems.append(f"{path}.purpose is required")
        purpose = ""

    account_id_env = raw.get("accountIdEnv")
    if not isinstance(account_id_env, str) or not _ENV_NAME_RE.match(
        account_id_env or ""
    ):
        problems.append(f"{path}.accountIdEnv must be an UPPER_SNAKE env var name")
        account_id_env = f"AGENTICAI_ACCOUNT_{alias}"

    credential_source = _validate_credential_source(
        account_key, raw.get("credentialSource"), problems
    )

    raw_roles = raw.get("roles")
    roles: dict[str, RoleSpec] = {}
    if not isinstance(raw_roles, Mapping) or not raw_roles:
        problems.append(f"{path}.roles must be a non-empty object")
    else:
        for role_ref, raw_role in raw_roles.items():
            if not isinstance(role_ref, str) or not role_ref:
                problems.append(f"{path}.roles has a non-string key")
                continue
            role = _validate_role(account_key, role_ref, raw_role, problems)
            if role is not None:
                roles[role_ref] = role

    for required_ref in REQUIRED_ROLES.get(account_key, ()):
        if required_ref not in roles:
            problems.append(
                f"{path}.roles.{required_ref} is required by the catalog contract"
            )

    if credential_source is None:
        return None

    return AccountSpec(
        key=account_key,
        alias=alias,
        purpose=purpose,
        account_id_env=account_id_env,
        credential_source=credential_source,
        roles=roles,
    )


def validate_manifest_document(document: Any) -> AccountManifest:
    """Validate a manifest document, collecting every problem found.

    Raises :class:`ManifestSchemaError` with the full problem list, or
    :class:`CredentialSourceError` for a forbidden credential source.
    """
    problems: list[str] = []
    if not isinstance(document, Mapping):
        raise ManifestSchemaError(["manifest root must be a JSON object"])

    assert_sanitized(document, path="$manifest")

    schema_version = document.get("schemaVersion")
    if schema_version != SCHEMA_VERSION:
        problems.append(
            f"schemaVersion must be {SCHEMA_VERSION!r} (got {schema_version!r})"
        )

    partition = document.get("partition", "aws")
    if partition not in ("aws", "aws-us-gov", "aws-cn"):
        problems.append(f"partition {partition!r} is not a known AWS partition")

    regions = document.get("regions")
    primary_region = ""
    additional_regions: tuple[str, ...] = ()
    if not isinstance(regions, Mapping):
        problems.append("regions must be an object with a 'primary' key")
    else:
        primary = regions.get("primary")
        if not isinstance(primary, str) or not primary:
            problems.append("regions.primary is required")
        else:
            primary_region = primary
        extra = regions.get("additional", [])
        if not isinstance(extra, (list, tuple)) or any(
            not isinstance(item, str) for item in extra
        ):
            problems.append("regions.additional must be an array of region strings")
        else:
            additional_regions = tuple(extra)

    raw_accounts = document.get("accounts")
    accounts: dict[str, AccountSpec] = {}
    if not isinstance(raw_accounts, Mapping):
        problems.append("accounts must be an object")
    else:
        unknown = [key for key in raw_accounts if key not in REQUIRED_ACCOUNTS]
        if unknown:
            problems.append(
                "accounts contains unsupported keys "
                + ", ".join(repr(key) for key in sorted(unknown))
                + "; the supported topology has exactly "
                + ", ".join(repr(key) for key in REQUIRED_ACCOUNTS)
            )
        for required in REQUIRED_ACCOUNTS:
            if required not in raw_accounts:
                problems.append(f"accounts.{required} is required")
        seen_aliases: dict[str, str] = {}
        for account_key in REQUIRED_ACCOUNTS:
            if account_key not in raw_accounts:
                continue
            account = _validate_account(
                account_key, raw_accounts[account_key], problems, seen_aliases
            )
            if account is not None:
                accounts[account_key] = account

    if problems:
        raise ManifestSchemaError(problems)

    return AccountManifest(
        schema_version=str(schema_version),
        partition=str(partition),
        primary_region=primary_region,
        additional_regions=additional_regions,
        accounts=accounts,
        document=document,
    )


def load_manifest(path: str | os.PathLike[str]) -> AccountManifest:
    """Load and validate a manifest from disk."""
    manifest_path = Path(path)
    forbid_credential_file(str(manifest_path))
    if not manifest_path.is_file():
        raise ManifestError(f"manifest not found at {manifest_path}")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest at {manifest_path} is not valid JSON: {exc}")
    manifest = validate_manifest_document(document)
    return AccountManifest(
        schema_version=manifest.schema_version,
        partition=manifest.partition,
        primary_region=manifest.primary_region,
        additional_regions=manifest.additional_regions,
        accounts=manifest.accounts,
        document=manifest.document,
        source_path=manifest_path,
    )


def resolve_manifest(
    manifest: AccountManifest, env: Mapping[str, str] | None = None
) -> ResolvedManifest:
    """Bind account ids and credential sources from ``env``.

    Never raises for a missing binding — the live-mode gate decides whether an
    incomplete resolution is a skip or an error.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    account_ids: dict[str, str] = {}
    missing: list[str] = []

    for key, account in manifest.accounts.items():
        raw_id = (environ.get(account.account_id_env) or "").strip()
        if not raw_id:
            missing.append(
                f"account '{key}' id not mapped: env {account.account_id_env} is unset"
            )
        elif not _ACCOUNT_ID_RE.match(raw_id):
            missing.append(
                f"account '{key}' id from env {account.account_id_env} is not a "
                "12-digit AWS account id"
            )
        else:
            account_ids[key] = raw_id

        source = account.credential_source
        required = source.required_env()
        alternatives = source.alternative_env()
        if required and not all(environ.get(name) for name in required):
            if not any(environ.get(name) for name in alternatives):
                missing.append(
                    f"account '{key}' credentials unavailable: set "
                    + " and ".join(required)
                    + (
                        (" (or " + " or ".join(alternatives) + ")")
                        if alternatives
                        else ""
                    )
                )
        if source.type is CredentialSourceType.ASSUME_ROLE_CHAIN:
            if source.via and source.via not in manifest.accounts:
                if "." in source.via:
                    try:
                        manifest.role(source.via)
                    except ManifestError as exc:
                        missing.append(
                            f"account '{key}' assume-role chain via {source.via!r} "
                            f"is unresolvable: {exc}"
                        )
                else:
                    missing.append(
                        f"account '{key}' assume-role chain via {source.via!r} "
                        "names no known account or role"
                    )
        if source.external_id_env and not environ.get(source.external_id_env):
            missing.append(
                f"account '{key}' external id unavailable: env "
                f"{source.external_id_env} is unset"
            )

    duplicate_ids = {
        account_id
        for account_id in account_ids.values()
        if list(account_ids.values()).count(account_id) > 1
    }
    if duplicate_ids:
        missing.append(
            "two or more accounts resolved to the same account id; the "
            "three-account topology requires three distinct accounts"
        )

    return ResolvedManifest(
        manifest=manifest, account_ids=account_ids, missing=tuple(missing)
    )


__all__ = [
    "AccountManifest",
    "AccountSpec",
    "CredentialSource",
    "CredentialSourceType",
    "FORBIDDEN_CREDENTIAL_SOURCE_TYPES",
    "MANAGEMENT_ACCOUNT",
    "PLATFORM_ACCOUNT",
    "REQUIRED_ACCOUNTS",
    "REQUIRED_ROLES",
    "ResolvedManifest",
    "ResolvedPrincipal",
    "RoleSpec",
    "SCHEMA_VERSION",
    "WORKSTREAM_ACCOUNT",
    "canonical_json",
    "load_manifest",
    "manifest_sha",
    "resolve_manifest",
    "validate_manifest_document",
]
