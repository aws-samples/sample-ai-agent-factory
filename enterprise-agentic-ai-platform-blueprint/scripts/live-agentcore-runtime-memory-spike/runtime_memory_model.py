#!/usr/bin/env python3
"""AWS-free model for the AgentCore Runtime+Memory compatibility spike.

Everything in this module is a pure function or a frozen value object: no boto3
import, no network, no filesystem. That keeps the security-critical parts of the
spike -- input validation, resource-name ownership, run-marker token derivation,
status classification, provenance checks, and secret-safety scanning --
unit-testable with zero AWS dependency, matching the discipline of the sibling
PolicyEngine spike and of ``tests/adversarial``.

Facts this module encodes are taken from the pinned service models
(offline-inspected at ``boto3==1.43.97``), not from guesswork:

* Runtime lifecycle lives on ``bedrock-agentcore-control``:
  ``CreateAgentRuntime`` (required ``agentRuntimeName``, ``agentRuntimeArtifact``,
  ``roleArn``), ``GetAgentRuntime`` (required ``agentRuntimeId``),
  ``DeleteAgentRuntime`` (required ``agentRuntimeId``), ``ListAgentRuntimes``
  (paginated), and ``ListTagsForResource`` (Runtime ARNs only).
* Memory lifecycle also lives on ``bedrock-agentcore-control``:
  ``CreateMemory`` (required ``name``, ``eventExpiryDuration``), ``GetMemory``
  (required ``memoryId``), ``DeleteMemory`` (required ``memoryId``),
  ``ListMemories`` (paginated; summaries do NOT carry ``name``).
* The data plane lives on ``bedrock-agentcore``: ``InvokeAgentRuntime``
  (required ``agentRuntimeArn``, ``payload``), ``CreateEvent`` (required
  ``memoryId``, ``actorId``, ``eventTimestamp``, ``payload``), ``GetEvent``
  (required ``memoryId``, ``sessionId``, ``actorId``, ``eventId``).

Official API limitations honestly reflected here:

* ``ListTagsForResource`` supports AgentCore **Runtime** ARNs, not Memory, so
  Memory ownership is proven by exact name + run-specific description +
  encryption key + event expiry instead of by tags.
* ``ListMemories`` summaries lack ``name``; discovery must ``GetMemory`` each
  candidate to read its name.
* An ECR **tag** is mutable unless the repository has image-tag immutability
  enabled, which this probe cannot prove, so only a ``@sha256:<digest>``
  reference is accepted.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

# --------------------------------------------------------------------------
# Deterministic handshake shared with agent/agent.py
# --------------------------------------------------------------------------

#: Must match ``agent/agent.py``'s ``HANDSHAKE_MARKER``. ``verify`` confirms a
#: live invocation by parsing the JSON response and checking this marker plus
#: the expected ping fingerprint and ``runtimeReady is True`` -- never by a
#: substring scan, and never by recording the response body.
HANDSHAKE_MARKER = "agentcore-runtime-memory-spike-ok"
#: The one scalar field the agent reads from an invocation payload.
PING_FIELD = "ping"
#: The fixed ping value the probe sends.
PING_VALUE = "handshake"

# --------------------------------------------------------------------------
# Service-model pins (offline-verified against boto3==1.43.97)
# --------------------------------------------------------------------------

#: Exact pinned SDK version. Tested for equality, not just presence.
REQUIRED_BOTO3_VERSION = "1.43.97"
REQUIRED_BOTOCORE_VERSION = "1.43.97"

#: ``bedrock-agentcore-control`` operations and the required members the spike
#: depends on. Pinned by an offline service-model contract test.
RUNTIME_OPERATIONS: dict[str, tuple[str, ...]] = {
    "CreateAgentRuntime": ("agentRuntimeName", "agentRuntimeArtifact", "roleArn"),
    "GetAgentRuntime": ("agentRuntimeId",),
    "DeleteAgentRuntime": ("agentRuntimeId",),
    "ListAgentRuntimes": (),
    "ListTagsForResource": ("resourceArn",),
}
MEMORY_OPERATIONS: dict[str, tuple[str, ...]] = {
    "CreateMemory": ("name", "eventExpiryDuration"),
    "GetMemory": ("memoryId",),
    "DeleteMemory": ("memoryId",),
    "ListMemories": (),
}
#: ``bedrock-agentcore`` (data plane) operations and their required members.
DATA_PLANE_OPERATIONS: dict[str, tuple[str, ...]] = {
    "InvokeAgentRuntime": ("agentRuntimeArn", "payload"),
    "CreateEvent": ("memoryId", "actorId", "eventTimestamp", "payload"),
    "GetEvent": ("memoryId", "sessionId", "actorId", "eventId"),
}

#: Optional members the spike relies on beyond the required set. The offline SDK
#: test asserts each is modeled so a serialized request cannot silently drop it.
RUNTIME_OPTIONAL_MEMBERS: dict[str, tuple[str, ...]] = {
    "CreateAgentRuntime": ("description", "networkConfiguration", "clientToken", "tags"),
    "ListAgentRuntimes": ("maxResults", "nextToken"),
    "ListTagsForResource": (),
    "DeleteAgentRuntime": ("clientToken",),
}
MEMORY_OPTIONAL_MEMBERS: dict[str, tuple[str, ...]] = {
    "CreateMemory": ("description", "encryptionKeyArn", "clientToken", "tags"),
    "ListMemories": ("maxResults", "nextToken"),
    "DeleteMemory": ("clientToken",),
}
DATA_OPTIONAL_MEMBERS: dict[str, tuple[str, ...]] = {
    "InvokeAgentRuntime": ("contentType", "accept", "runtimeSessionId", "qualifier"),
    "CreateEvent": ("sessionId", "clientToken"),
    "GetEvent": (),
}
#: List-output fields the discovery code reads.
LIST_OUTPUT_FIELDS: dict[str, tuple[str, str]] = {
    # operation -> (items key, next-token key)
    "ListAgentRuntimes": ("agentRuntimes", "nextToken"),
    "ListMemories": ("memories", "nextToken"),
}

CONTROL_SERVICE = "bedrock-agentcore-control"
DATA_SERVICE = "bedrock-agentcore"

# --------------------------------------------------------------------------
# Status classification -- pinned to the official enums
# --------------------------------------------------------------------------

#: A runtime is usable once it reports READY.
RUNTIME_READY_STATUS = "READY"
#: A memory is usable once it reports ACTIVE.
MEMORY_ACTIVE_STATUS = "ACTIVE"
#: Official terminal statuses. Reaching one fails the run immediately rather
#: than waiting for a timeout. Runtime uses the three ``*_FAILED`` values; the
#: Memory ``Status`` enum's only failure value is ``FAILED``.
RUNTIME_STATUSES = frozenset(
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
MEMORY_STATUSES = frozenset({"CREATING", "ACTIVE", "FAILED", "DELETING", "UPDATING"})
RUNTIME_TERMINAL_FAILURES = frozenset(
    {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}
)
MEMORY_TERMINAL_FAILURES = frozenset({"FAILED"})

# --------------------------------------------------------------------------
# Region allow-list (documentation-derived, not a live-verified matrix)
# --------------------------------------------------------------------------

SUPPORTED_REGIONS = frozenset(
    {
        "us-east-1",
        "us-west-2",
        "eu-west-1",
        "eu-west-2",
        "eu-central-1",
        "ap-southeast-1",
        "ap-southeast-2",
        "ap-northeast-1",
    }
)
#: Subset in scope for the EMEA matrix this repository reasons about explicitly.
EMEA_REGIONS = frozenset({"eu-west-1", "eu-west-2", "eu-central-1"})

# --------------------------------------------------------------------------
# Patterns transcribed from the pinned SDK / documented constraints
# --------------------------------------------------------------------------

ACCOUNT_PATTERN = re.compile(r"^\d{12}$")
REGION_PATTERN = re.compile(r"^[a-z]{2}-[a-z]+-\d$")
#: Runtime and memory names use the ``[A-Za-z][A-Za-z0-9_]*`` charset; hyphens
#: are invalid there, so a hyphenated run prefix is translated to underscores.
RESOURCE_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")
#: A hyphenated prefix used for tags and identifiers that allow hyphens.
PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,39}$")
ROLE_ARN_PATTERN = re.compile(r"^arn:aws:iam::(\d{12}):role/[\w+=,.@/-]{1,512}$")
KMS_ARN_PATTERN = re.compile(
    r"^arn:aws:kms:([a-z0-9-]+):(\d{12}):key/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
#: An ECR image reference the caller supplies as the runtime artifact. Only a
#: ``@sha256:<digest>`` reference is accepted: a tag (even a non-``latest`` one)
#: is mutable unless the repository has image-tag immutability enabled, which
#: this probe cannot verify, so a tag reference is refused outright.
ECR_IMAGE_PATTERN = re.compile(
    r"^(\d{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/"
    r"[a-z0-9][a-z0-9._/-]{0,255}"
    r"@sha256:[0-9a-f]{64}$"
)
_IMAGE_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")

#: Actor / session / event / marker values are confined to a safe charset. They
#: exist only inside scratch state and are hashed before any evidence is written.
IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
#: A persisted run marker: a hex nonce that seeds every idempotency token.
RUN_MARKER_PATTERN = re.compile(r"^[0-9a-f]{32}$")
#: A runtime session id for InvokeAgentRuntime (>=33 chars per the review).
RUNTIME_SESSION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{33,128}$")

#: Idempotency and session tokens must be at least this long.
MIN_CLIENT_TOKEN_LENGTH = 33
#: How much of the seed digest a truncated token must always retain, so
#: truncation never destroys collision resistance.
MIN_DIGEST_RETAINED = 32

#: AgentCore control-plane ARN shapes for scope proof.
RUNTIME_ARN_PATTERN = re.compile(
    r"^arn:aws:bedrock-agentcore:([a-z0-9-]+):(\d{12}):runtime/[A-Za-z0-9_-]+$"
)
MEMORY_ARN_PATTERN = re.compile(
    r"^arn:aws:bedrock-agentcore:([a-z0-9-]+):(\d{12}):memory/[A-Za-z0-9_-]+$"
)


class ModelError(RuntimeError):
    """A fail-closed error raised by this AWS-free model."""


class ValidationError(ModelError):
    """A caller-supplied value failed validation."""


class SecretLeakError(ModelError):
    """A value that looks like a credential/identifier was about to be persisted."""


class StatusError(ModelError):
    """A resource reached a terminal failure status."""


class OwnershipError(ModelError):
    """A live resource did not match the exact expected owner/config."""


class ProvenanceError(ModelError):
    """Persisted state does not belong to this run."""


def _require(pattern: re.Pattern[str], value: str, label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValidationError(f"{label} {value!r} does not match {pattern.pattern}")
    return value


# --------------------------------------------------------------------------
# Caller-supplied prerequisite validation
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


def validate_role_arn(role_arn: str, *, account_id: str) -> str:
    _require(ROLE_ARN_PATTERN, role_arn, "Runtime role ARN")
    if account_of_arn(role_arn) != account_id:
        raise ValidationError("Runtime role ARN is not in the target account")
    return role_arn


def validate_kms_key_arn(key_arn: str, *, account_id: str, region: str) -> str:
    _require(KMS_ARN_PATTERN, key_arn, "Memory KMS key ARN")
    if account_of_arn(key_arn) != account_id:
        raise ValidationError("Memory KMS key ARN is not in the target account")
    if region_of_arn(key_arn) != region:
        raise ValidationError("Memory KMS key ARN is not in the target region")
    return key_arn


def image_is_digest_pinned(image_uri: str) -> bool:
    """True only when the image is pinned by a ``@sha256:<digest>`` reference.

    A tag reference -- even a non-``latest`` one -- is mutable unless the
    repository has image-tag immutability enabled, which this probe cannot
    prove, so it does not count as immutable here.
    """
    return bool(_IMAGE_DIGEST_RE.search(str(image_uri)))


def validate_container_uri(image_uri: str, *, account_id: str, region: str) -> str:
    """The runtime artifact must be a digest-pinned ECR image in this account/region.

    The probe never builds, pushes, or mutates an image. It only accepts one the
    caller already published, referenced by digest, so a run cannot silently
    pick up a different image later.
    """
    _require(ECR_IMAGE_PATTERN, image_uri, "Container image URI")
    host = image_uri.split("/", 1)[0]
    host_account, host_region = host.split(".")[0], host.split(".")[3]
    if host_account != account_id:
        raise ValidationError("Container image is not in the target account registry")
    if host_region != region:
        raise ValidationError("Container image is not in the target region registry")
    if not image_is_digest_pinned(image_uri):
        raise ValidationError("Container image must be pinned by an @sha256 digest")
    return image_uri


def validate_identifier(value: str, *, label: str) -> str:
    return _require(IDENTIFIER_PATTERN, value, label)


def region_is_supported(region: str) -> bool:
    return region in SUPPORTED_REGIONS


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
    reuses the exact same token and AgentCore's idempotency prevents a duplicate
    resource. The token is ``<op>-<digest>`` truncated to 63 chars, and the
    digest slice is guaranteed to retain at least :data:`MIN_DIGEST_RETAINED`
    hex chars so truncation never weakens collision resistance.
    """
    validate_run_marker(run_marker)
    if not operation or not operation.isidentifier():
        raise ValidationError(f"Operation {operation!r} is not a valid token seed")
    digest = hashlib.sha256(f"{run_marker}:{operation}".encode("utf-8")).hexdigest()
    # Keep a short human-readable operation tag, then a strong digest slice that
    # is never shorter than MIN_DIGEST_RETAINED regardless of the tag length.
    tag = operation[:16]
    max_len = 63
    digest_room = max_len - len(tag) - 1  # 1 for the '-'
    digest_len = max(MIN_DIGEST_RETAINED, digest_room)
    token = f"{tag}-{digest[:digest_len]}"
    if len(token) < MIN_CLIENT_TOKEN_LENGTH:  # pragma: no cover - digest is 64 hex
        token = (token + digest)[:MIN_CLIENT_TOKEN_LENGTH]
    return token


def runtime_session_id(run_marker: str) -> str:
    """A deterministic >=33-char runtimeSessionId derived from the run marker."""
    validate_run_marker(run_marker)
    digest = hashlib.sha256(f"{run_marker}:runtimeSession".encode("utf-8")).hexdigest()
    session = f"rmspike-{digest}"[:64]
    _require(RUNTIME_SESSION_PATTERN, session, "Runtime session id")
    return session


def derive_actor_id(prefix: str) -> str:
    return validate_identifier(f"{prefix}-actor", label="Actor id")


def derive_session_id(run_marker: str, prefix: str) -> str:
    validate_run_marker(run_marker)
    digest = hashlib.sha256(f"{run_marker}:memorySession".encode("utf-8")).hexdigest()[:16]
    return validate_identifier(f"{prefix}-{digest}", label="Session id")


def derive_event_marker(prefix: str) -> str:
    return validate_identifier(f"{prefix}-evt", label="Event marker")


# --------------------------------------------------------------------------
# Status helpers
# --------------------------------------------------------------------------


def classify_runtime_status(status: str) -> str:
    if status not in RUNTIME_STATUSES:
        raise StatusError(f"Runtime returned unknown status {status!r}")
    if status == RUNTIME_READY_STATUS:
        return "ready"
    if status in RUNTIME_TERMINAL_FAILURES:
        return "terminal"
    return "pending"


def classify_memory_status(status: str) -> str:
    if status not in MEMORY_STATUSES:
        raise StatusError(f"Memory returned unknown status {status!r}")
    if status == MEMORY_ACTIVE_STATUS:
        return "active"
    if status in MEMORY_TERMINAL_FAILURES:
        return "terminal"
    return "pending"


def assert_not_terminal_runtime(status: str) -> None:
    if status not in RUNTIME_STATUSES:
        raise StatusError(f"Runtime returned unknown status {status!r}")
    if status in RUNTIME_TERMINAL_FAILURES:
        raise StatusError(f"Runtime reached terminal status {status!r}")


def assert_not_terminal_memory(status: str) -> None:
    if status not in MEMORY_STATUSES:
        raise StatusError(f"Memory returned unknown status {status!r}")
    if status in MEMORY_TERMINAL_FAILURES:
        raise StatusError(f"Memory reached terminal status {status!r}")


# --------------------------------------------------------------------------
# Resource naming and ownership
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SpikeNames:
    """Every resource name this spike may create, derived from one prefix.

    The probe owns exactly two AWS resources: one AgentCore Runtime and one
    AgentCore Memory. It never creates or owns the execution role, the container
    image, the CMK, endpoints, aliases, or grants -- those are caller inputs.
    """

    prefix: str

    def __post_init__(self) -> None:
        _require(PREFIX_PATTERN, self.prefix, "Prefix")
        _require(RESOURCE_NAME_PATTERN, self.runtime_name, "Runtime name")
        _require(RESOURCE_NAME_PATTERN, self.memory_name, "Memory name")

    @property
    def underscore_prefix(self) -> str:
        return self.prefix.replace("-", "_")

    @property
    def runtime_name(self) -> str:
        return f"{self.underscore_prefix}_runtime"

    @property
    def memory_name(self) -> str:
        return f"{self.underscore_prefix}_memory"

    @property
    def all_names(self) -> tuple[str, ...]:
        return (self.runtime_name, self.memory_name)

    def owns(self, name: str) -> bool:
        """Exact ownership: true only for a name this run creates verbatim.

        Never prefix-only -- a prefix match would admit a foreign resource that
        merely shares the prefix. Only the two exact generated names qualify.
        """
        return isinstance(name, str) and name in self.all_names

    def ownership_description(self, run_marker: str) -> str:
        """The run-specific description written to both owned resources.

        It embeds the run marker so a live resource can be proven to belong to
        THIS run, not just to some run with the same prefix.
        """
        validate_run_marker(run_marker)
        return f"agentcore-runtime-memory-spike run={run_marker} prefix={self.prefix}"

    def allocation_tags(self, run_marker: str) -> dict[str, str]:
        """The five allocation tags every emitted resource carries."""
        validate_run_marker(run_marker)
        return {
            "application-id": self.prefix,
            "agent-id": f"{self.prefix}-runtime-memory-spike",
            "tenant-id": "platform",
            "cost-centre": "agentic-ai-platform",
            "environment": "nonprod",
        }


# --------------------------------------------------------------------------
# Live-resource ownership proofs (pure predicates over describe output)
# --------------------------------------------------------------------------


def assert_runtime_owned(
    record: Mapping[str, Any],
    *,
    names: "SpikeNames",
    run_marker: str,
    account_id: str,
    region: str,
    container_uri: str,
    role_arn: str,
    tags: Mapping[str, str],
) -> None:
    """Prove a live Runtime is this run's before invoke/delete.

    Checks exact name, ARN account/region scope, run-specific description,
    immutable inputs (container digest + role ARN), and the five allocation tags
    (read separately via ``ListTagsForResource``, passed in as ``tags``).
    """
    name = str(record.get("agentRuntimeName", ""))
    if not names.owns(name) or name != names.runtime_name:
        raise OwnershipError("Runtime name does not match the expected name")
    runtime_id = str(record.get("agentRuntimeId", ""))
    arn = str(record.get("agentRuntimeArn", ""))
    m = RUNTIME_ARN_PATTERN.fullmatch(arn)
    if not runtime_id or not m or m.group(2) != account_id or m.group(1) != region:
        raise OwnershipError("Runtime identity is not in the expected account/region")
    if arn.rsplit("/", 1)[-1] != runtime_id:
        raise OwnershipError("Runtime id does not match its ARN")
    if str(record.get("description", "")) != names.ownership_description(run_marker):
        raise OwnershipError("Runtime description does not match this run's marker")
    artifact = record.get("agentRuntimeArtifact")
    if not isinstance(artifact, Mapping):
        raise OwnershipError("Runtime artifact is missing or malformed")
    container = artifact.get("containerConfiguration")
    if not isinstance(container, Mapping):
        raise OwnershipError("Runtime container configuration is missing or malformed")
    if str(container.get("containerUri", "")) != container_uri:
        raise OwnershipError("Runtime container image differs from the expected digest")
    if str(record.get("roleArn", "")) != role_arn:
        raise OwnershipError("Runtime role ARN differs from the expected role")
    expected = dict(tags)
    for key, value in names.allocation_tags(run_marker).items():
        if expected.get(key) != value:
            raise OwnershipError(f"Runtime tag {key!r} is missing or wrong")


def assert_memory_owned(
    record: Mapping[str, Any],
    *,
    names: "SpikeNames",
    run_marker: str,
    account_id: str,
    region: str,
    kms_key_arn: str,
    event_expiry_days: int,
) -> None:
    """Prove a live Memory is this run's before event/delete.

    ``ListTagsForResource`` does not support Memory, so ownership is proven by
    exact name, ARN account/region scope, run-specific description, encryption
    key, and event-expiry duration -- not by tags.
    """
    name = str(record.get("name", ""))
    if not names.owns(name) or name != names.memory_name:
        raise OwnershipError("Memory name does not match the expected name")
    memory_id = str(record.get("id", record.get("memoryId", "")))
    if not memory_id:
        raise OwnershipError("Memory id is missing")
    arn = str(record.get("arn", record.get("memoryArn", "")))
    if arn:
        m = MEMORY_ARN_PATTERN.fullmatch(arn)
        if not m or m.group(2) != account_id or m.group(1) != region:
            raise OwnershipError("Memory ARN is not in the expected account/region")
        if arn.rsplit("/", 1)[-1] != memory_id:
            raise OwnershipError("Memory id does not match its ARN")
    if str(record.get("description", "")) != names.ownership_description(run_marker):
        raise OwnershipError("Memory description does not match this run's marker")
    if str(record.get("encryptionKeyArn", "")) != kms_key_arn:
        raise OwnershipError("Memory encryption key differs from the expected CMK")
    try:
        expiry = int(record.get("eventExpiryDuration"))
    except (TypeError, ValueError) as error:
        raise OwnershipError("Memory event expiry is missing or malformed") from error
    if expiry != int(event_expiry_days):
        raise OwnershipError("Memory event expiry differs from the expected value")


def is_runtime_owned(record: Mapping[str, Any], **kwargs: Any) -> bool:
    try:
        assert_runtime_owned(record, **kwargs)
        return True
    except OwnershipError:
        return False


def is_memory_owned(record: Mapping[str, Any], **kwargs: Any) -> bool:
    try:
        assert_memory_owned(record, **kwargs)
        return True
    except OwnershipError:
        return False


# --------------------------------------------------------------------------
# State provenance
# --------------------------------------------------------------------------

STATE_SCHEMA_VERSION = 2


def build_state_header(
    *, run_marker: str, account_id: str, region: str, prefix: str
) -> dict[str, Any]:
    """The provenance header written atomically before the first mutation."""
    validate_run_marker(run_marker)
    _require(ACCOUNT_PATTERN, account_id, "Account id")
    _require(PREFIX_PATTERN, prefix, "Prefix")
    return {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "runMarker": run_marker,
        "accountId": account_id,
        "region": region,
        "prefix": prefix,
    }


def assert_state_provenance(
    state: Mapping[str, Any], *, account_id: str, region: str, prefix: str
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
    "session",
    "payload",
    "arn",
    "requestid",
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"eyJ[A-Za-z0-9_-]{6,}"),  # base64url JWT header
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+\S+", re.IGNORECASE),
    re.compile(r"arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:\d{12}:\S+"),
    # ECR image URI (with or without digest/tag).
    re.compile(r"\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/\S+"),
    # Standalone 12-digit account id.
    re.compile(r"\b\d{12}\b"),
    # UUID / request id.
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
    # AgentCore resource id suffix (name + 10-char id) e.g. foo_runtime-AbC0123xyz.
    re.compile(r"\b[A-Za-z][A-Za-z0-9_]*-[A-Za-z0-9]{10}\b"),
    # sha256 digest.
    re.compile(r"\bsha256:[0-9a-f]{64}\b"),
    # JWT-like triple.
    re.compile(r"[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{6,}"),
)
_REDACTED = "[redacted]"


def is_secret_key(key: str) -> bool:
    normalized = str(key).lower().replace("_", "").replace("-", "")
    return any(fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS)


def assert_no_secret_values(payload: Any, *, path: str = "$") -> Any:
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
    """Non-reversible correlation handle for an identifier or a response body."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:32]


def account_suffix(account_id: str) -> str:
    _require(ACCOUNT_PATTERN, account_id, "Account id")
    return account_id[-4:]


def sanitize_error(message: str, *, prefix: str) -> str:
    """Reduce an error message to a non-sensitive, bounded diagnostic string.

    Redacts account ids, ECR URIs, ARNs, UUID/request ids, AgentCore resource
    ids, and digests before the message can reach evidence.
    """
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
# Handshake / event verification (pure)
# --------------------------------------------------------------------------


def build_invocation_payload(ping: str = PING_VALUE) -> dict[str, str]:
    return {PING_FIELD: _require(IDENTIFIER_PATTERN, ping, "Ping value")}


def expected_ping_fingerprint(ping: str = PING_VALUE) -> str:
    """The fingerprint the agent returns for a given ping value."""
    return fingerprint(_require(IDENTIFIER_PATTERN, ping, "Ping value"))


def handshake_is_exact(response: Mapping[str, Any], *, ping: str = PING_VALUE) -> bool:
    """Exact handshake proof: marker, expected ping fingerprint, runtimeReady.

    A substring is not proof. The response must be a JSON object carrying the
    exact marker, the fingerprint of the ping value we sent, and
    ``runtimeReady is True``.
    """
    if not isinstance(response, Mapping):
        return False
    if response.get("marker") != HANDSHAKE_MARKER:
        return False
    if response.get("echoFingerprint") != expected_ping_fingerprint(ping):
        return False
    return response.get("runtimeReady") is True


def build_memory_event_payload(marker: str) -> list[dict[str, dict[str, Any]]]:
    """A minimal, non-sensitive CreateEvent conversational content union."""
    _require(IDENTIFIER_PATTERN, marker, "Event marker")
    return [
        {
            "conversational": {
                "role": "USER",
                "content": {"text": marker},
            }
        }
    ]


def event_content_marker(event: Mapping[str, Any]) -> str | None:
    """Extract the text-union marker from a GetEvent event record."""
    payload = event.get("payload")
    if not isinstance(payload, list):
        return None
    for block in payload:
        if not isinstance(block, Mapping):
            continue
        conversational = block.get("conversational")
        if not isinstance(conversational, Mapping):
            continue
        content = conversational.get("content")
        if isinstance(content, Mapping) and isinstance(content.get("text"), str):
            return str(content["text"])
    return None


def memory_round_trip_ok(
    event: Mapping[str, Any],
    *,
    expected_event_id: str,
    expected_actor_id: str,
    expected_session_id: str,
    expected_memory_id: str,
    expected_marker: str,
) -> bool:
    """Prove a GetEvent record matches every field the run wrote."""
    if not isinstance(event, Mapping):
        return False
    return (
        str(event.get("eventId", "")) == expected_event_id
        and str(event.get("actorId", "")) == expected_actor_id
        and str(event.get("sessionId", "")) == expected_session_id
        and str(event.get("memoryId", "")) == expected_memory_id
        and event_content_marker(event) == expected_marker
    )


def unknown(code: str, detail: str) -> dict[str, str]:
    return {"unknown": code, "detail": detail}
