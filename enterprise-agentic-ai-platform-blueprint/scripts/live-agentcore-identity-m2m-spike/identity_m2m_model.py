#!/usr/bin/env python3
"""AWS-free model for the AgentCore Identity M2M compatibility spike.

Everything in this module is a pure function or a frozen value object: no boto3
import, no network, no filesystem. That isolates the security-critical parts of
the spike -- input validation, resource-name ownership, run-marker token
derivation, provider status classification, state provenance, and secret-safety
scanning -- so they are unit-testable with zero AWS dependency, matching the
discipline of the sibling PolicyEngine and Runtime+Memory spikes.

Facts this module encodes are transcribed from the pinned service models
(offline-inspected at ``boto3==1.43.98`` / ``botocore==1.43.98``), not guessed:

* Workload identity lifecycle lives on ``bedrock-agentcore-control``:
  ``CreateWorkloadIdentity`` (required ``name``; optional
  ``allowedResourceOauth2ReturnUrls``, ``tags``) -> ``name``,
  ``workloadIdentityArn``; ``GetWorkloadIdentity`` (required ``name``);
  ``ListWorkloadIdentities`` (paginated; summaries carry only ``name`` +
  ``workloadIdentityArn``); ``DeleteWorkloadIdentity`` (required ``name``).
* OAuth2 credential-provider lifecycle also lives on
  ``bedrock-agentcore-control``: ``CreateOauth2CredentialProvider`` (required
  ``name``, ``credentialProviderVendor``, ``oauth2ProviderConfigInput``;
  optional ``tags``); ``GetOauth2CredentialProvider`` /
  ``DeleteOauth2CredentialProvider`` (required ``name``);
  ``ListOauth2CredentialProviders`` (paginated).
* The token data plane lives on ``bedrock-agentcore``:
  ``GetWorkloadAccessToken`` (required ``workloadName``) -> ``workloadAccessToken``;
  ``GetResourceOauth2Token`` (required ``workloadIdentityToken``,
  ``resourceCredentialProviderName``, ``scopes``, ``oauth2Flow``) -> ``accessToken``.

Official API behavior honestly reflected here:

* Neither ``GetWorkloadIdentity`` nor ``GetOauth2CredentialProvider`` returns
  the ``tags`` set. ``ListTagsForResource`` is therefore called separately for
  every ownership proof. This was read-only live-verified for an AgentCore
  workload identity in ``us-west-2`` despite conflicting wording on the generic
  API page. Destructive cleanup requires the exact run-derived name, exact ARN
  account/region scope, and all five exact run tags; no single signal is trusted
  alone.
* ``CreateOauth2CredentialProvider`` echoes ``clientSecretArn`` and a
  ``credentialProviderArn`` in its response. Those are treated as secrets/ids
  and never persisted or logged; only booleans, lengths, and one-way
  fingerprints of non-secret identifiers are recorded.
* The provider ``Status`` enum is
  ``{CREATING, CREATE_FAILED, UPDATING, UPDATE_FAILED, READY, DELETING,
  DELETE_FAILED}``. A workload identity has no status field in the pinned model.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

# --------------------------------------------------------------------------
# Service-model pins (offline-verified against boto3==1.43.98)
# --------------------------------------------------------------------------

#: Exact pinned SDK version. Tested for equality, not just presence.
REQUIRED_BOTO3_VERSION = "1.43.98"
REQUIRED_BOTOCORE_VERSION = "1.43.98"

CONTROL_SERVICE = "bedrock-agentcore-control"
DATA_SERVICE = "bedrock-agentcore"

#: ``bedrock-agentcore-control`` operations and the required members the spike
#: depends on. Pinned by the offline SDK-capability test in the runner suite.
CONTROL_OPERATIONS: dict[str, tuple[str, ...]] = {
    "CreateWorkloadIdentity": ("name",),
    "GetWorkloadIdentity": ("name",),
    "ListWorkloadIdentities": (),
    "DeleteWorkloadIdentity": ("name",),
    "CreateOauth2CredentialProvider": (
        "name",
        "credentialProviderVendor",
        "oauth2ProviderConfigInput",
    ),
    "GetOauth2CredentialProvider": ("name",),
    "ListOauth2CredentialProviders": (),
    "DeleteOauth2CredentialProvider": ("name",),
    "ListTagsForResource": ("resourceArn",),
}
#: ``bedrock-agentcore`` (data plane) operations and their required members.
DATA_OPERATIONS: dict[str, tuple[str, ...]] = {
    "GetWorkloadAccessToken": ("workloadName",),
    "GetResourceOauth2Token": (
        "workloadIdentityToken",
        "resourceCredentialProviderName",
        "scopes",
        "oauth2Flow",
    ),
}
#: Optional members the spike relies on beyond the required set. The offline SDK
#: test asserts each is modeled so a serialized request cannot silently drop it.
CONTROL_OPTIONAL_MEMBERS: dict[str, tuple[str, ...]] = {
    "CreateWorkloadIdentity": ("allowedResourceOauth2ReturnUrls", "tags"),
    "CreateOauth2CredentialProvider": ("tags",),
    "ListWorkloadIdentities": ("maxResults", "nextToken"),
    "ListOauth2CredentialProviders": ("maxResults", "nextToken"),
}
DATA_OPTIONAL_MEMBERS: dict[str, tuple[str, ...]] = {
    "GetResourceOauth2Token": ("customParameters",),
}
#: List-output fields the discovery code reads. operation -> (items, nextToken).
LIST_OUTPUT_FIELDS: dict[str, tuple[str, str]] = {
    "ListWorkloadIdentities": ("workloadIdentities", "nextToken"),
    "ListOauth2CredentialProviders": ("credentialProviders", "nextToken"),
}

#: ``cognito-idp`` operations the spike uses to mint, list, and delete its own
#: ephemeral app-client secret. Pinned so a serialized request cannot silently
#: drop a required member. ``AddUserPoolClientSecret`` returns the value only at
#: creation (in ``ClientSecretDescriptor``); there is no read-back-by-id, which
#: is why a client rotated to the multi-secret lifecycle cannot be read via
#: ``DescribeUserPoolClient.ClientSecret`` and the spike must mint its own.
COGNITO_SERVICE = "cognito-idp"
COGNITO_OPERATIONS: dict[str, tuple[str, ...]] = {
    "DescribeUserPoolClient": ("UserPoolId", "ClientId"),
    "DescribeUserPool": ("UserPoolId",),
    "AddUserPoolClientSecret": ("UserPoolId", "ClientId"),
    "DeleteUserPoolClientSecret": ("UserPoolId", "ClientId", "ClientSecretId"),
    "ListUserPoolClientSecrets": ("UserPoolId", "ClientId"),
}
#: The descriptor member that carries our minted secret's stable id.
CLIENT_SECRET_DESCRIPTOR_MEMBER = "ClientSecretDescriptor"
CLIENT_SECRET_ID_MEMBER = "ClientSecretId"
CLIENT_SECRET_VALUE_MEMBER = "ClientSecretValue"

#: The exact vendor discriminant this spike uses.
CREDENTIAL_PROVIDER_VENDOR = "CognitoOauth2"
#: The exact provider-config union member for the Cognito vendor. Its only
#: required child is ``clientId``; ``clientSecret`` is optional in the model but
#: mandatory for a working M2M provider, so the runner always supplies it.
PROVIDER_CONFIG_MEMBER = "includedOauth2ProviderConfig"
#: The exact OAuth2 flow discriminant for machine-to-machine token exchange.
OAUTH2_FLOW_M2M = "M2M"

# --------------------------------------------------------------------------
# Provider status classification -- pinned to the official ``Status`` enum
# --------------------------------------------------------------------------

PROVIDER_READY_STATUS = "READY"
PROVIDER_STATUSES = frozenset(
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
PROVIDER_TERMINAL_FAILURES = frozenset(
    {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}
)

# --------------------------------------------------------------------------
# Region boundary -- this isolated campaign targets one retained deployment
# --------------------------------------------------------------------------

SUPPORTED_REGIONS = frozenset({"us-west-2"})

# --------------------------------------------------------------------------
# Patterns transcribed from the pinned SDK / documented constraints
# --------------------------------------------------------------------------

ACCOUNT_PATTERN = re.compile(r"^\d{12}$")
REGION_PATTERN = re.compile(r"^[a-z]{2}-[a-z]+-\d$")
#: A hyphenated run prefix used for tags and human-readable identifiers.
PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,39}$")
#: Workload/provider resource names. The AgentCore identity name charset is
#: ``[A-Za-z0-9_]`` (hyphens are invalid), so a hyphenated prefix is translated
#: to underscores before a name is built.
RESOURCE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")
#: Cognito user-pool ID bound to the configured Region.
USER_POOL_ID_PATTERN = re.compile(r"^([a-z]{2}-[a-z]+-\d)_[A-Za-z0-9]+$")
#: Cognito app-client identifiers are bounded but otherwise opaque.
CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,128}$")
#: A minted client-secret id is ``<client-id>--<epoch-millis>`` per the
#: AddUserPoolClientSecret contract. It is an identifier, not the secret value;
#: it is persisted in state (never scanned) but only ever fingerprinted in
#: evidence, since its length can trip the opaque-token value scanner.
CLIENT_SECRET_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,128}--\d{1,20}$")
#: HTTPS endpoints supplied for the Cognito provider config.
HTTPS_URL_PATTERN = re.compile(r"^https://[A-Za-z0-9.\-/_:?=&%]{3,512}$")
#: Cognito resource-server scopes are capped at the data API's 128 characters.
SCOPE_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
#: Gateway inference routes must carry exactly one target/model separator.
TARGET_QUALIFIED_MODEL_PATTERN = re.compile(
    r"^[A-Za-z0-9._-]+/[A-Za-z0-9._:-]+$"
)
SOURCE_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
STACK_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,127}$")
#: A persisted run marker: a hex nonce that seeds every idempotency token.
RUN_MARKER_PATTERN = re.compile(r"^[0-9a-f]{32}$")

#: Control-plane workload-identity ARN shape for scope proof.
WORKLOAD_IDENTITY_ARN_PATTERN = re.compile(
    r"^arn:aws:bedrock-agentcore:([a-z0-9-]+):(\d{12}):"
    r"workload-identity-directory/default/workload-identity/[A-Za-z0-9_-]+$"
)
#: Control-plane credential-provider ARN shape (treated as a secret id; only
#: matched to prove account/region scope, never persisted raw).
CREDENTIAL_PROVIDER_ARN_PATTERN = re.compile(
    r"^arn:aws:bedrock-agentcore:([a-z0-9-]+):(\d{12}):"
    r"token-vault/default/oauth2credentialprovider/[A-Za-z0-9_-]+$"
)

#: Idempotency-style tokens must be at least this long.
MIN_CLIENT_TOKEN_LENGTH = 33
MIN_DIGEST_RETAINED = 32


class ModelError(RuntimeError):
    """A fail-closed error raised by this AWS-free model."""


class ValidationError(ModelError):
    """A caller-supplied value failed validation."""


class SecretLeakError(ModelError):
    """A value that looks like a credential/identifier was about to be persisted."""


class StatusError(ModelError):
    """A resource reached a terminal failure or unknown status."""


class OwnershipError(ModelError):
    """A live resource did not match the exact expected owner/config."""


class ProvenanceError(ModelError):
    """Persisted state does not belong to this run."""


def _require(pattern: re.Pattern[str], value: str, label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValidationError(f"{label} {value!r} does not match {pattern.pattern}")
    return value


# --------------------------------------------------------------------------
# ARN helpers + caller-supplied prerequisite validation
# --------------------------------------------------------------------------


def account_of_arn(arn: str) -> str:
    parts = str(arn).split(":")
    if len(parts) < 5 or not ACCOUNT_PATTERN.fullmatch(parts[4]):
        raise ValidationError(f"ARN {arn!r} has no account id in position 5")
    return parts[4]


def region_of_arn(arn: str) -> str:
    parts = str(arn).split(":")
    if len(parts) < 4:
        raise ValidationError(f"ARN {arn!r} is not a valid ARN")
    return parts[3]


def region_is_supported(region: str) -> bool:
    return region in SUPPORTED_REGIONS


def validate_user_pool_id(user_pool_id: str, *, region: str) -> str:
    value = _require(USER_POOL_ID_PATTERN, user_pool_id, "Cognito user pool id")
    if USER_POOL_ID_PATTERN.fullmatch(value).group(1) != region:
        raise ValidationError("Cognito user pool id is not in the configured region")
    return value


def validate_client_id(client_id: str) -> str:
    return _require(CLIENT_ID_PATTERN, client_id, "Cognito client id")


def validate_client_secret_id(client_secret_id: str, *, client_id: str) -> str:
    """Validate a minted client-secret id and bind it to the target client.

    The id is ``<client-id>--<epoch-millis>``; requiring its prefix to equal the
    configured client id is a second guard that cleanup can never delete a
    secret belonging to a different app client.
    """
    value = _require(CLIENT_SECRET_ID_PATTERN, client_secret_id, "Client secret id")
    if value.split("--", 1)[0] != client_id:
        raise ValidationError(
            "Client secret id is not prefixed by the configured client id"
        )
    return value


def validate_source_revision(source_revision: str) -> str:
    return _require(SOURCE_REVISION_PATTERN, source_revision, "Source revision")


def validate_stack_name(stack_name: str) -> str:
    return _require(STACK_NAME_PATTERN, stack_name, "CloudFormation stack name")


def validate_target_qualified_model_id(model_id: str) -> str:
    return _require(
        TARGET_QUALIFIED_MODEL_PATTERN,
        model_id,
        "Target-qualified model id",
    )


def validate_https_url(url: str, *, label: str) -> str:
    return _require(HTTPS_URL_PATTERN, url, label)


def _https_parts(url: str, *, label: str):
    validate_https_url(url, label=label)
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValidationError(f"{label} must be an HTTPS endpoint without user info")
    if parts.port not in (None, 443) or parts.query or parts.fragment:
        raise ValidationError(f"{label} must not use a custom port, query, or fragment")
    return parts


def validate_cognito_endpoint_bundle(
    *,
    region: str,
    user_pool_id: str,
    issuer: str,
    authorization_endpoint: str,
    token_endpoint: str,
) -> tuple[str, str, str]:
    """Bind OAuth metadata to the exact managed Cognito pool and hosted domain."""
    validate_user_pool_id(user_pool_id, region=region)
    issuer_parts = _https_parts(issuer, label="issuer")
    auth_parts = _https_parts(authorization_endpoint, label="authorizationEndpoint")
    token_parts = _https_parts(token_endpoint, label="tokenEndpoint")
    if issuer_parts.hostname != f"cognito-idp.{region}.amazonaws.com":
        raise ValidationError("issuer host is not Cognito in the configured region")
    if issuer_parts.path.rstrip("/") != f"/{user_pool_id}":
        raise ValidationError("issuer path does not identify the configured user pool")
    hosted_suffix = f".auth.{region}.amazoncognito.com"
    if not auth_parts.hostname.endswith(hosted_suffix):
        raise ValidationError("authorizationEndpoint is not a managed Cognito domain")
    if auth_parts.hostname != token_parts.hostname:
        raise ValidationError("authorization and token endpoints use different hosts")
    if auth_parts.path.rstrip("/") != "/oauth2/authorize":
        raise ValidationError("authorizationEndpoint must end in /oauth2/authorize")
    if token_parts.path.rstrip("/") != "/oauth2/token":
        raise ValidationError("tokenEndpoint must end in /oauth2/token")
    return issuer, authorization_endpoint, token_endpoint


def validate_gateway_url(url: str, *, region: str) -> str:
    """Prevent an M2M bearer token from being sent to a caller-chosen host."""
    parts = _https_parts(url, label="Gateway URL")
    expected_suffix = f".gateway.bedrock-agentcore.{region}.amazonaws.com"
    if not parts.hostname.endswith(expected_suffix):
        raise ValidationError("Gateway URL is not AgentCore in the configured region")
    if parts.path.rstrip("/") != "/mcp":
        raise ValidationError("Gateway URL must identify the Gateway /mcp base path")
    return url.rstrip("/")


def validate_scope(scope: str) -> str:
    return _require(SCOPE_PATTERN, scope, "OAuth2 scope")


def validate_scopes(scopes: Any) -> list[str]:
    if not isinstance(scopes, (list, tuple)) or not scopes:
        raise ValidationError("At least one OAuth2 scope is required")
    return [validate_scope(str(scope)) for scope in scopes]


# --------------------------------------------------------------------------
# Run marker + run-marker-derived idempotency tokens
# --------------------------------------------------------------------------


def new_run_marker() -> str:
    """A fresh 128-bit hex run nonce persisted before the first mutation."""
    return secrets.token_hex(16)


def validate_run_marker(marker: str) -> str:
    return _require(RUN_MARKER_PATTERN, marker, "Run marker")


def client_token(run_marker: str, operation: str) -> str:
    """A deterministic, >=33-char idempotency token seeded by the run marker.

    Deterministic per (run_marker, operation) so a retry after a partial failure
    reuses the exact same token. The digest slice always retains at least
    :data:`MIN_DIGEST_RETAINED` hex chars so truncation never weakens collision
    resistance.
    """
    validate_run_marker(run_marker)
    if not operation or not operation.isidentifier():
        raise ValidationError(f"Operation {operation!r} is not a valid token seed")
    digest = hashlib.sha256(f"{run_marker}:{operation}".encode("utf-8")).hexdigest()
    tag = operation[:16]
    digest_room = 63 - len(tag) - 1
    digest_len = max(MIN_DIGEST_RETAINED, digest_room)
    token = f"{tag}-{digest[:digest_len]}"
    if len(token) < MIN_CLIENT_TOKEN_LENGTH:  # pragma: no cover - digest is 64 hex
        token = (token + digest)[:MIN_CLIENT_TOKEN_LENGTH]
    return token


def marker_suffix(run_marker: str) -> str:
    """A short, non-secret, run-specific name suffix derived from the marker.

    A fingerprint of the run marker (not the marker itself) so that a resource
    name proves it belongs to THIS run without embedding the raw nonce. 12 hex
    chars = 48 bits of run entropy, ample for a per-run name suffix.
    """
    validate_run_marker(run_marker)
    return hashlib.sha256(f"{run_marker}:name".encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------
# Provider status helpers
# --------------------------------------------------------------------------


def classify_provider_status(status: str) -> str:
    if status not in PROVIDER_STATUSES:
        raise StatusError(f"Credential provider returned unknown status {status!r}")
    if status == PROVIDER_READY_STATUS:
        return "ready"
    if status in PROVIDER_TERMINAL_FAILURES:
        return "terminal"
    return "pending"


def assert_not_terminal_provider(status: str) -> None:
    if status not in PROVIDER_STATUSES:
        raise StatusError(f"Credential provider returned unknown status {status!r}")
    if status in PROVIDER_TERMINAL_FAILURES:
        raise StatusError(f"Credential provider reached terminal status {status!r}")


# --------------------------------------------------------------------------
# Resource naming and ownership
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SpikeNames:
    """Every resource name this spike may create, derived from prefix + marker.

    The probe owns exactly two AWS resources: one AgentCore workload identity and
    one OAuth2 credential provider. It never creates or owns the Cognito user
    pool, its app client, the resource server, or its scopes -- those are caller
    inputs read in-process only.

    Get/List responses do not inline tags, so ownership combines the exact
    run-derived name with an account/region-bound ARN and a separate readback of
    all five exact tags. A foreign resource must never satisfy only a subset.
    """

    prefix: str
    run_marker: str

    def __post_init__(self) -> None:
        _require(PREFIX_PATTERN, self.prefix, "Prefix")
        validate_run_marker(self.run_marker)
        _require(RESOURCE_NAME_PATTERN, self.workload_name, "Workload identity name")
        _require(RESOURCE_NAME_PATTERN, self.provider_name, "Credential provider name")

    @property
    def underscore_prefix(self) -> str:
        return self.prefix.replace("-", "_")

    @property
    def _suffix(self) -> str:
        return marker_suffix(self.run_marker)

    @property
    def workload_name(self) -> str:
        return f"{self.underscore_prefix}_wl_{self._suffix}"

    @property
    def provider_name(self) -> str:
        return f"{self.underscore_prefix}_cp_{self._suffix}"

    @property
    def all_names(self) -> tuple[str, ...]:
        return (self.workload_name, self.provider_name)

    def owns_workload(self, name: str) -> bool:
        return isinstance(name, str) and name == self.workload_name

    def owns_provider(self, name: str) -> bool:
        return isinstance(name, str) and name == self.provider_name

    def allocation_tags(self) -> dict[str, str]:
        """The five allocation tags every emitted resource carries."""
        return {
            "application-id": self.prefix,
            "agent-id": f"{self.prefix}-identity-m2m-spike",
            "tenant-id": "platform",
            "cost-centre": "agentic-ai-platform",
            "environment": "nonprod",
        }


def assert_allocation_tags(
    tags: Mapping[str, Any], *, expected: Mapping[str, str]
) -> None:
    if not isinstance(tags, Mapping):
        raise OwnershipError("Resource tags are not an object")
    normalized = {str(key): str(value) for key, value in tags.items()}
    if normalized != dict(expected):
        raise OwnershipError("Resource tags do not exactly match this run")


def assert_workload_owned(
    record: Mapping[str, Any],
    *,
    tags: Mapping[str, Any],
    names: "SpikeNames",
    account_id: str,
    region: str,
) -> None:
    """Prove a live workload identity is this run's before delete."""
    name = str(record.get("name", ""))
    if not names.owns_workload(name):
        raise OwnershipError("Workload identity name does not match this run's name")
    arn = str(record.get("workloadIdentityArn", ""))
    m = WORKLOAD_IDENTITY_ARN_PATTERN.fullmatch(arn)
    if not m or m.group(2) != account_id or m.group(1) != region:
        raise OwnershipError(
            "Workload identity ARN is not in the expected account/region"
        )
    assert_allocation_tags(tags, expected=names.allocation_tags())


def assert_provider_owned(
    record: Mapping[str, Any],
    *,
    tags: Mapping[str, Any],
    names: "SpikeNames",
    account_id: str,
    region: str,
) -> None:
    """Prove a live credential provider is this run's before delete."""
    name = str(record.get("name", ""))
    if not names.owns_provider(name):
        raise OwnershipError("Credential provider name does not match this run's name")
    vendor = str(record.get("credentialProviderVendor", ""))
    if vendor != CREDENTIAL_PROVIDER_VENDOR:
        raise OwnershipError("Credential provider vendor differs from the expected vendor")
    arn = str(record.get("credentialProviderArn", ""))
    m = CREDENTIAL_PROVIDER_ARN_PATTERN.fullmatch(arn)
    if not m or m.group(2) != account_id or m.group(1) != region:
        raise OwnershipError(
            "Credential provider ARN is not in the expected account/region"
        )
    assert_allocation_tags(tags, expected=names.allocation_tags())


def is_workload_owned(record: Mapping[str, Any], **kwargs: Any) -> bool:
    try:
        assert_workload_owned(record, **kwargs)
        return True
    except OwnershipError:
        return False


def is_provider_owned(record: Mapping[str, Any], **kwargs: Any) -> bool:
    try:
        assert_provider_owned(record, **kwargs)
        return True
    except OwnershipError:
        return False


# --------------------------------------------------------------------------
# Provider config assembly (pure) -- clientSecret is passed in, never persisted
# --------------------------------------------------------------------------


def build_included_provider_config(
    *,
    client_id: str,
    client_secret: str,
    issuer: str,
    authorization_endpoint: str,
    token_endpoint: str,
) -> dict[str, Any]:
    """Assemble the ``includedOauth2ProviderConfig`` union member.

    The ``client_secret`` is the live Cognito app-client secret, read in-process
    only. It is placed in the request body here and MUST NOT be logged, stored,
    or written to evidence anywhere. Callers pass it straight into
    ``create_oauth2_credential_provider`` and drop the reference immediately.
    """
    validate_client_id(client_id)
    if not isinstance(client_secret, str) or not client_secret:
        raise ValidationError("Cognito client secret must be a non-empty string")
    validate_https_url(issuer, label="issuer")
    validate_https_url(authorization_endpoint, label="authorizationEndpoint")
    validate_https_url(token_endpoint, label="tokenEndpoint")
    return {
        PROVIDER_CONFIG_MEMBER: {
            "clientId": client_id,
            "clientSecret": client_secret,
            "issuer": issuer,
            "authorizationEndpoint": authorization_endpoint,
            "tokenEndpoint": token_endpoint,
        }
    }


# --------------------------------------------------------------------------
# State provenance
# --------------------------------------------------------------------------

STATE_SCHEMA_VERSION = 1


def build_state_header(
    *,
    run_marker: str,
    account_id: str,
    region: str,
    prefix: str,
    source_revision: str,
) -> dict[str, Any]:
    """The provenance header written atomically before the first mutation."""
    validate_run_marker(run_marker)
    _require(ACCOUNT_PATTERN, account_id, "Account id")
    _require(PREFIX_PATTERN, prefix, "Prefix")
    validate_source_revision(source_revision)
    return {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "runMarker": run_marker,
        "accountId": account_id,
        "region": region,
        "prefix": prefix,
        "sourceRevision": source_revision,
    }


def assert_state_provenance(
    state: Mapping[str, Any],
    *,
    account_id: str,
    region: str,
    prefix: str,
    source_revision: str,
) -> str:
    """Validate a resumed state file belongs to this run; return its marker."""
    if not isinstance(state, Mapping):
        raise ProvenanceError("State is not an object")
    if state.get("schemaVersion") != STATE_SCHEMA_VERSION:
        raise ProvenanceError("State schema version mismatch")
    if str(state.get("accountId", "")) != account_id:
        raise ProvenanceError("State account does not match --account-id")
    if str(state.get("region", "")) != region:
        raise ProvenanceError("State region does not match --region")
    if str(state.get("prefix", "")) != prefix:
        raise ProvenanceError("State prefix does not match --prefix")
    if str(state.get("sourceRevision", "")) != source_revision:
        raise ProvenanceError("State source revision does not match --source-revision")
    return validate_run_marker(str(state.get("runMarker", "")))


# --------------------------------------------------------------------------
# Secret / identifier safety
# --------------------------------------------------------------------------

_SECRET_KEY_FRAGMENTS = (
    "authorization",
    "token",
    "secret",
    "password",
    "accesskey",
    "credential",
    "bearer",
    "cookie",
    "clientsecret",
    "arn",
    "requestid",
)
_SECRET_VALUE_PATTERNS = (
    # base64url JWT header segment.
    re.compile(r"eyJ[A-Za-z0-9_-]{6,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+\S+", re.IGNORECASE),
    re.compile(r"arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:\d{12}:\S+"),
    # Standalone 12-digit account id.
    re.compile(r"\b\d{12}\b"),
    # UUID / request id.
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
    # JWT-like triple (header.payload.signature).
    re.compile(r"[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{6,}"),
    # OAuth2 opaque access-token-ish long token blob.
    re.compile(r"\b[A-Za-z0-9._~+/-]{40,}={0,2}\b"),
)
_REDACTED = "[redacted]"

#: Safe metadata suffixes. A key that contains a credential fragment but ends in
#: one of these names a DERIVED, non-secret quantity (a length, a one-way
#: fingerprint, a count, an account suffix), never the raw secret. The value
#: scanner still independently rejects any credential-shaped value, so this
#: allowlist can only ever admit safe metadata, not a real token.
_SAFE_KEY_SUFFIXES = ("length", "fingerprint", "count", "suffix")


def is_secret_key(key: str) -> bool:
    normalized = str(key).lower().replace("_", "").replace("-", "")
    if not any(fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS):
        return False
    # Allow derived-metadata keys (tokenLength, scopeFingerprint, ...): the value
    # is never the secret, and the recursive value scanner still guards it.
    return not normalized.endswith(_SAFE_KEY_SUFFIXES)


def assert_no_secret_values(payload: Any, *, path: str = "$") -> Any:
    """Recursively refuse credential-shaped keys or values before persistence."""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if is_secret_key(str(key)):
                raise SecretLeakError(f"{path}.{key} names a credential/id field")
            assert_no_secret_values(value, path=f"{path}.{key}")
        return payload
    if isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            assert_no_secret_values(value, path=f"{path}[{index}]")
        return payload
    if isinstance(payload, str):
        for pattern in _SECRET_VALUE_PATTERNS:
            if pattern.search(payload):
                raise SecretLeakError(f"{path} holds a credential/id-shaped value")
    return payload


def fingerprint(value: str) -> str:
    """Non-reversible correlation handle for a non-secret identifier."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:32]


def account_suffix(account_id: str) -> str:
    _require(ACCOUNT_PATTERN, account_id, "Account id")
    return account_id[-4:]


def safe_token_length(token: Any) -> int | None:
    """Return a token's length only when recording it cannot aid reconstruction.

    A booleans-and-lengths evidence policy allows the length of an opaque token
    *iff* the length itself is not sensitive. We only ever return the length for
    a non-empty string; ``None`` otherwise. The token value is never returned.
    """
    if isinstance(token, str) and token:
        return len(token)
    return None


def sanitize_error(message: str, *, prefix: str) -> str:
    """Reduce an error message to a non-sensitive, bounded diagnostic string."""
    text = " ".join(str(message).split())
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    if len(text) > 300:
        text = text[:297] + "..."
    try:
        assert_no_secret_values(text)
    except SecretLeakError:
        return f"{prefix}: [error message redacted]"
    return text


# --------------------------------------------------------------------------
# Token-shape proofs (pure) -- prove a token was obtained WITHOUT storing it
# --------------------------------------------------------------------------


def workload_access_token_ok(token: Any) -> bool:
    """True when GetWorkloadAccessToken returned a plausible non-empty token."""
    return isinstance(token, str) and len(token) >= MIN_DIGEST_RETAINED


def resource_token_ok(token: Any) -> bool:
    """True when GetResourceOauth2Token returned a plausible non-empty token."""
    return isinstance(token, str) and len(token) >= MIN_DIGEST_RETAINED


def unknown(code: str, detail: str) -> dict[str, str]:
    return {"unknown": code, "detail": detail}
