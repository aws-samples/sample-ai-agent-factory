"""AWS Agent Registry adapter (GA) — Phase 6 (Loom-inspired).

Federates deployed agents into the AWS-native Agent Registry — the org-wide
catalog with an approval gate — on top of our internal registry. OPT-IN: does
nothing unless an admin configures a registryId in Settings.

PREVIEW -> GA MIGRATION
-----------------------
Agent Registry graduated out of AgentCore into its own AWS service. Everything
else in AgentCore (Runtime, Gateway, Identity, Memory) stays on
``bedrock-agentcore``; only the Registry moved:

===================  ==============================  ============================
                     preview                         GA
===================  ==============================  ============================
control-plane boto3  ``bedrock-agentcore-control``    ``agent-registry-control``
data-plane boto3     ``bedrock-agentcore``           ``agent-registry``
IAM action prefix    ``bedrock-agentcore:``          ``agent-registry:``
ARN service          ``arn:...:bedrock-agentcore:``  ``arn:...:agent-registry:``
record classifier    ``descriptorType=``             ``recordType=``
classifier values    MCP/A2A/CUSTOM/AGENT_SKILLS     MCP/AGENT/CUSTOM/SKILL
A2A descriptor       ``a2a.agentCard.inlineContent`` ``a2aAgentCard.data``
custom descriptor    ``custom.inlineContent``        ``custom.data``
schema-version key   ``schemaVersion``               ``dataSchemaVersion``
search operation     ``SearchRegistryRecords``       ``SearchDiscoverableRegistryRecords``
===================  ==============================  ============================

The rename is a silent trap, not a loud one: the legacy
``bedrock-agentcore-control`` model still carries the Registry operations (with
the OLD ``descriptorType`` parameter), so preview calls keep "working" against a
shim whose IAM prefix and payload shape have both moved on. Only the data-plane
search fails loudly. Hence the hard pin below.

Requires boto3 >= 1.43.66 (first release carrying the agent-registry models).

Verified against the boto3 1.43.72 service models:
  control: CreateRegistry, GetRegistry, UpdateRegistry, DeleteRegistry,
           ListRegistries, CreateRegistryRecord, GetRegistryRecord,
           ListRegistryRecords, UpdateRegistryRecord, DeleteRegistryRecord,
           SubmitRegistryRecordForApproval, UpdateRegistryRecordStatus,
           TagResource, UntagResource, ListTagsForResource
  data:    SearchDiscoverableRegistryRecords, ListDiscoverableRegistryRecords,
           BatchGetDiscoverableRegistryRecord
  recordType ∈ {MCP, AGENT, CUSTOM, SKILL, GATEWAY}
  status     ∈ {DRAFT, PENDING_APPROVAL, APPROVED, REJECTED, DEPRECATED,
                CREATING, UPDATING, CREATE_FAILED, UPDATE_FAILED}
  Descriptors has six members: mcpServer, a2aAgentCard, agentSkillsDefinition,
  custom, http, agui. The first four are recordType-keyed and carry inline
  content. http/agui are PROTOCOL-keyed, not recordType-keyed ("populated for
  records detected from an HTTP/AG-UI protocol source"), and are source-only:
  their shape has a `source` member and no inline data. The model documents no
  descriptor pairing for GATEWAY — see DESCRIPTOR_KEY_FOR_TYPE for the choice
  made here.

Degrades gracefully: the Registry may be absent in a region, on an account, or
in an older boto3 bundle. Every call is best-effort; failures are logged and
surfaced as a disabled feature, never a 500 on the deploy path.
"""

from __future__ import annotations

import json
import logging
import os
import re

import boto3

logger = logging.getLogger(__name__)

# A2A agent-card schema version, passed as `dataSchemaVersion` at GA.
A2A_CARD_SCHEMA_VERSION = "0.3"

# GA boto3 service names. Registry is its own service now — using the
# bedrock-agentcore names here silently targets the deprecated preview shim.
CONTROL_SERVICE = "agent-registry-control"
DATA_SERVICE = "agent-registry"

# First boto3 release shipping the agent-registry service models.
MIN_BOTO3 = (1, 43, 66)

# GA recordType enum (was descriptorType in preview). GATEWAY is real — verified
# against the botocore agent-registry-control 2025-12-01 model, whose RecordType
# enum is {MCP, AGENT, CUSTOM, SKILL, GATEWAY}. Omitting it made
# normalize_record_type("GATEWAY") fall through and silently return "CUSTOM",
# which was latent only because production had no gateway-provider concept to
# register. It has one now.
RECORD_TYPES = ("MCP", "AGENT", "CUSTOM", "SKILL", "GATEWAY")

# Preview spellings we still accept from persisted rows / older callers, mapped
# onto their GA equivalents. "a2a"/"custom" are the lowercase descriptor keys the
# preview call sites passed as descriptor_type.
_LEGACY_RECORD_TYPES = {
    "A2A": "AGENT",
    "AGENT_SKILLS": "SKILL",
    "AGENTSKILLS": "SKILL",
}

# Each recordType carries its payload under exactly one descriptor key. Getting
# this pairing wrong is a ValidationException from AWS; we catch it locally with
# an actionable message instead.
DESCRIPTOR_KEY_FOR_TYPE = {
    "MCP": "mcpServer",
    "AGENT": "a2aAgentCard",
    "SKILL": "agentSkillsDefinition",
    "CUSTOM": "custom",
    # A GATEWAY record describes an MCP endpoint (that is what a gateway serves),
    # so it carries the mcpServer descriptor. The service model documents a
    # descriptor for each of the other four record types but names none for
    # GATEWAY, so this pairing is our choice rather than a documented one — it is
    # also the only inline-content descriptor that fits. An entry here is not
    # optional: register_record() indexes this dict by recordType, so a GATEWAY
    # with no entry would be a KeyError on the deploy path.
    "GATEWAY": "mcpServer",
}

# Descriptor members that carry no inline content at all — the service
# synchronizes them from a configured source URL ("source-only"). They are keyed
# to a PROTOCOL, not to a recordType, which is why they are deliberately absent
# from DESCRIPTOR_KEY_FOR_TYPE above: no recordType requires one, and any
# recordType may legitimately arrive carrying one.
SOURCE_ONLY_DESCRIPTOR_KEYS = ("http", "agui")

# Every descriptor member the GA Descriptors shape accepts. Exactly one is
# populated per record.
DESCRIPTOR_KEYS = (*dict.fromkeys(DESCRIPTOR_KEY_FOR_TYPE.values()), *SOURCE_ONLY_DESCRIPTOR_KEYS)

# Reverse map for normalize_record_type: descriptor key -> recordType. Spelled out
# rather than inverted from DESCRIPTOR_KEY_FOR_TYPE because that mapping is no
# longer injective — MCP and GATEWAY both use "mcpServer", so an inversion would
# resolve "mcpServer" by dict insertion order. MCP is the right answer (a bare
# descriptor key names a plain MCP server; a gateway is only ever named by the
# explicit "GATEWAY" recordType), and this makes that intentional.
_TYPE_FOR_DESCRIPTOR_KEY = {
    "mcpServer": "MCP",
    "a2aAgentCard": "AGENT",
    "agentSkillsDefinition": "SKILL",
    "custom": "CUSTOM",
}

# CreateRegistryRecord input constraints (from the service model).
_NAME_MAX = 255
_DESCRIPTION_MAX = 4096
_NAME_ALLOWED = re.compile(r"[^a-zA-Z0-9_\-./]")
# Every descriptor `data` member is capped at 102400 bytes by the service model.
_DATA_MAX = 102400

# The only registry status that accepts writes. CreateRegistryRecord against a
# registry in any other state fails with ConflictException.
REGISTRY_STATUS_READY = "READY"


class RegistryQueryFailed(RuntimeError):
    """A registry query did not complete — as opposed to completing and finding
    nothing.

    This distinction is load-bearing. `list_records()` returns [] both when the
    registry genuinely holds no matching records and when the call blew up
    (AccessDenied, throttle, wrong filter shape). Those two facts demand OPPOSITE
    responses from a fail-closed policy check: the first is a real verdict, the
    second is an absence of information. Conflating them lets an infrastructure
    error masquerade as an authorization decision — a deploy gets rejected with
    "your integrations are not APPROVED" when the truth is "we could not ask".

    Fail-closed logic must therefore branch on this exception rather than on an
    empty list. `partial` carries whatever pages were read before the failure.
    """

    def __init__(self, message: str, partial: list[dict] | None = None):
        super().__init__(message)
        self.partial: list[dict] = partial or []


def _region() -> str:
    return os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))


def _record_id_from_arn(arn: str) -> str:
    """AWS returns recordArn (no recordId); the id is the last ARN segment.

    Safe by construction at GA: recordArn matches
    ``arn:aws:agent-registry:<region>:<acct>:registry/<12-16>/record/<12>`` and
    recordId is exactly that trailing 12-char token, so no lookup is needed (and
    we avoid racing the eventual-consistent record index right after create).
    """
    return arn.rsplit("/", 1)[-1] if arn else ""


def boto3_version() -> tuple[int, ...]:
    """Parsed boto3 version, or (0,) when it cannot be determined."""
    try:
        return tuple(int(p) for p in boto3.__version__.split(".")[:3])
    except Exception:  # noqa: BLE001
        return (0,)


def agent_registry_supported() -> bool:
    """True when this boto3 bundle actually carries the agent-registry models.

    A version check alone is not enough — a Lambda bundle can pin a new boto3
    while an older botocore supplies the service models — so probe the session's
    service list too.
    """
    if boto3_version() < MIN_BOTO3:
        return False
    try:
        services = boto3.session.Session().get_available_services()
    except Exception:  # noqa: BLE001
        return False
    return CONTROL_SERVICE in services and DATA_SERVICE in services


def _make_client(service: str, region: str):
    """Build a boto3 client, or None when this bundle has no such service.

    Returning None (instead of letting UnknownServiceError escape) is what keeps
    an old-boto3 Lambda reporting "configured but unreachable" rather than 500ing
    the registry router.
    """
    try:
        return boto3.client(service, region_name=region)
    except Exception as e:  # noqa: BLE001
        logger.info(
            "Agent Registry client %s unavailable (boto3 %s, need >=%s): %s",
            service,
            boto3.__version__,
            ".".join(str(p) for p in MIN_BOTO3),
            str(e)[:160],
        )
        return None


def normalize_record_type(value: str | None) -> str:
    """Map any accepted spelling onto the GA recordType enum.

    Accepts the GA values, the preview values (``A2A`` -> ``AGENT``,
    ``AGENT_SKILLS`` -> ``SKILL``) and the lowercase descriptor keys the preview
    call sites used (``"a2a"``, ``"custom"``). Unknown values fall back to
    ``CUSTOM`` — a record in the catalog beats a hard failure on the deploy path.
    """
    raw = (value or "").strip()
    if not raw:
        return "CUSTOM"
    upper = raw.upper()
    upper = _LEGACY_RECORD_TYPES.get(upper, upper)
    if upper in RECORD_TYPES:
        return upper
    # Lowercase descriptor keys ("a2aAgentCard", "mcpServer", ...) -> their type.
    if raw in _TYPE_FOR_DESCRIPTOR_KEY:
        return _TYPE_FOR_DESCRIPTOR_KEY[raw]
    logger.info("Unknown recordType %r — registering as CUSTOM", raw)
    return "CUSTOM"


def sanitize_record_name(name: str) -> str:
    """Coerce a name to the GA record-name pattern.

    The service enforces ``[a-zA-Z0-9][a-zA-Z0-9_\\-./]*`` (1-255). Our record
    names come from user-chosen agent names, so a leading underscore or a space
    would otherwise surface as a ValidationException on an already-succeeded
    deploy.
    """
    cleaned = _NAME_ALLOWED.sub("_", (name or "").strip())
    cleaned = cleaned.lstrip("_-./")
    if not cleaned:
        cleaned = "agent"
    if not cleaned[0].isalnum():
        cleaned = f"a{cleaned}"
    return cleaned[:_NAME_MAX]


def _encode_data(payload: object, *, what: str) -> str:
    """JSON-encode a descriptor payload, enforcing the service's size cap.

    Every descriptor ``data`` member is capped at 102400 bytes. Exceeding it is a
    ValidationException from AWS with no indication of WHICH descriptor was too
    big — and on the deploy path that arrives inside a best-effort handler, so it
    would surface only as a truncated log line. Failing here names the culprit.
    """
    encoded = json.dumps(payload)
    size = len(encoded.encode("utf-8"))
    if size > _DATA_MAX:
        raise ValueError(f"{what} descriptor data is {size} bytes; the Agent Registry limit is {_DATA_MAX}")
    return encoded


def _normalize_a2a_skill(skill: dict, index: int) -> dict:
    """Fill in the A2A-0.3 skill fields the registry requires, keeping the rest.

    Verified against the live GA service: a skill entry is rejected unless it
    carries ALL of ``id``, ``name``, ``description`` and ``tags`` — an empty
    ``tags`` list is fine, but an absent one is not. AWS reports this as a
    card-wide "content is not in compliance with schema version '0.3'" naming
    neither the skill nor the field, so an under-specified skill from a workflow
    config would sink the whole record with an unactionable error.

    Repairs rather than raises: this runs on the best-effort auto-register-on-deploy
    path, where losing an entire governance record because one skill lacked a
    description is the worse failure. Unknown keys are preserved — the schema is
    open, and callers may legitimately pass A2A extras like ``examples``.
    """
    entry = dict(skill or {})
    name = str(entry.get("name") or entry.get("id") or f"skill-{index + 1}")
    entry["name"] = name
    entry["id"] = str(entry.get("id") or name)
    entry["description"] = str(entry.get("description") or name)
    tags = entry.get("tags")
    entry["tags"] = [str(t) for t in tags] if isinstance(tags, list) else []
    return entry


def build_a2a_descriptor(name: str, description: str, url: str, skills: list | None = None) -> dict:
    """A2A agentCard descriptor for a GA ``AGENT`` record.

    Reuses the shape our runtime already serves at /.well-known/agent-card.json.
    At GA the card goes under ``a2aAgentCard`` as a ``{data, dataSchemaVersion}``
    pair — preview nested it as ``a2a.agentCard.inlineContent``.

    ``url`` must be present but the registry does not require it to be a URL, so
    the deploy path's runtime-ARN fallback is accepted as-is.
    """
    card = {
        "protocolVersion": A2A_CARD_SCHEMA_VERSION,
        "name": name,
        "description": (description or name)[:100],
        "version": "1.0",
        "url": url,
        "capabilities": {"streaming": True},
        "skills": [_normalize_a2a_skill(s, i) for i, s in enumerate(skills or [])],
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
    }
    return {
        "a2aAgentCard": {
            "data": _encode_data(card, what="a2aAgentCard"),
            "dataSchemaVersion": A2A_CARD_SCHEMA_VERSION,
        }
    }


def build_agent_skills_descriptor(skills: list | None = None) -> dict:
    """agentSkillsDefinition descriptor for a GA ``SKILL`` record.

    Two live-verified quirks, both silent otherwise:

    * ``dataSchemaVersion`` must be OMITTED. Unlike every other descriptor this
      one accepts no version at all — each of the A2A/MCP/date-stamped candidates
      comes back "Schema version 'X' is not supported for descriptor type
      'agent_skills'".
    * ``data`` must be the object ``{"skills": [...]}``, never a bare array.

    Skill entries take the same required-field set as A2A card skills.
    """
    payload = {"skills": [_normalize_a2a_skill(s, i) for i, s in enumerate(skills or [])]}
    return {"agentSkillsDefinition": {"data": _encode_data(payload, what="agentSkillsDefinition")}}


def build_custom_descriptor(payload: dict) -> dict:
    """CUSTOM descriptor — arbitrary JSON under ``custom.data``.

    Note ``custom`` is the one descriptor with no ``dataSchemaVersion`` member.
    """
    return {"custom": {"data": _encode_data(payload, what="custom")}}


# Default descriptor schema versions the service assumes; pinning them makes a
# record self-describing and immune to a future default change.
MCP_SERVER_SCHEMA_VERSION = "2025-12-11"
MCP_TOOLS_SCHEMA_VERSION = "2025-11-25"

# `mcpServer.data` is an MCP-registry server.json document, whose `name` is
# namespaced: exactly one "/", a reverse-DNS-style namespace on the left and a
# server name on the right. A bare "my-server" is REJECTED (live-verified), as
# are two slashes, an empty half, and "_" inside the namespace.
_MCP_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.\-]*/[a-zA-Z0-9][a-zA-Z0-9._\-]*$")
_MCP_NAMESPACE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.\-]*$")
_MCP_SERVER_DISALLOWED = re.compile(r"[^a-zA-Z0-9._\-]")

# Namespace applied when a caller supplies an unnamespaced server name. Reverse
# DNS per the MCP registry convention, so platform-published servers are
# attributable rather than colliding in a flat namespace.
MCP_DEFAULT_NAMESPACE = "com.amazonaws.agentcore"


def sanitize_mcp_server_name(name: str, namespace: str = MCP_DEFAULT_NAMESPACE) -> str:
    """Coerce an arbitrary server name into a valid ``<namespace>/<server>`` pair.

    Left unchanged when already valid. Otherwise the caller's value becomes the
    server half — with "/" and other illegal characters folded to "-" — under
    ``namespace``. This is not cosmetic: an unnamespaced name is the difference
    between a registered MCP server and a ValidationException that blames the
    whole descriptor.
    """
    raw = (name or "").strip()
    if _MCP_NAME_RE.match(raw):
        return raw
    ns, sep, server = raw.partition("/")
    if not sep or not _MCP_NAMESPACE_RE.match(ns):
        ns, server = namespace, raw
    server = _MCP_SERVER_DISALLOWED.sub("-", server).strip("-._") or "server"
    return f"{ns}/{server}"


def _normalize_mcp_tool(tool: dict, index: int) -> dict:
    """Ensure an ``additionalData.tools`` entry carries its required members.

    A tool needs ``name`` and ``inputSchema``; ``description`` is optional
    (live-verified). Raises on a nameless tool rather than inventing one: unlike
    the A2A card, this builder has no best-effort caller, and a synthesized name
    would publish a tool nothing can invoke.
    """
    entry = dict(tool or {})
    if not entry.get("name"):
        raise ValueError(f"mcpServer tool #{index + 1} has no 'name'; the MCP tools schema requires one")
    if not isinstance(entry.get("inputSchema"), dict):
        entry["inputSchema"] = {"type": "object", "properties": {}}
    return entry


def build_mcp_descriptor(server: dict, tools: list | None = None) -> dict:
    """MCP descriptor for a GA ``MCP`` record.

    ``mcpServer.data`` is an MCP-registry ``server.json`` document requiring
    ``name`` (namespaced — see :func:`sanitize_mcp_server_name`), ``description``
    and ``version``; the ``tools/list`` output rides along under
    ``additionalData.tools`` as ``{"tools": [...]}``. Each leaf is a
    ``{data, dataSchemaVersion}`` pair.

    Missing required members are filled in rather than forwarded: the service
    rejects the document as a whole and names no field, so a caller passing only
    ``{"name": ..., "version": ...}`` — which is what this builder used to emit —
    got an opaque failure. Every other key the caller sent is preserved; the
    schema is open, so ``remotes``, ``packages`` and ``repository`` pass through.
    Note ``remotes[].type`` is a closed enum ("streamable-http" / "sse"), which we
    deliberately do not police here — forwarding an unknown transport surfaces as
    an AWS error rather than being silently rewritten to the wrong one.
    """
    doc = dict(server or {})
    doc["name"] = sanitize_mcp_server_name(str(doc.get("name") or ""))
    doc["description"] = str(doc.get("description") or doc["name"])
    doc["version"] = str(doc.get("version") or "1.0.0")

    descriptor: dict = {
        "mcpServer": {
            "data": _encode_data(doc, what="mcpServer"),
            "dataSchemaVersion": MCP_SERVER_SCHEMA_VERSION,
        }
    }
    if tools:
        normalized = [_normalize_mcp_tool(t, i) for i, t in enumerate(tools)]
        descriptor["mcpServer"]["additionalData"] = {
            "tools": {
                "data": _encode_data({"tools": normalized}, what="mcpServer.additionalData.tools"),
                "dataSchemaVersion": MCP_TOOLS_SCHEMA_VERSION,
            }
        }
    return descriptor


def _opt(value):
    """Wrap a value in UpdateRegistryRecord's ``optionalValue`` envelope."""
    return {"optionalValue": value}


def _to_update_descriptors(descriptors: dict) -> dict:
    """Re-shape Create-style descriptors into UpdateRegistryRecord's patch form.

    UpdateRegistryRecord does NOT accept the same descriptor structure as
    CreateRegistryRecord. Every branch and every scalar leaf is wrapped in an
    ``optionalValue`` envelope so the service can tell "set this to X" apart from
    "leave it alone"::

        create:  {"custom": {"data": "..."}}
        update:  {"optionalValue": {"custom": {"optionalValue": {
                     "data": {"optionalValue": "..."}}}}}

    Handing update the Create shape fails inside botocore's own parameter
    validation — a client-side error that never even reaches AWS, and on the deploy
    path it lands in a best-effort handler that would reduce it to a log line.
    """
    out: dict = {}
    for key, leaf in (descriptors or {}).items():
        body: dict = {}
        for field in ("data", "dataSchemaVersion"):
            if field in leaf:
                body[field] = _opt(leaf[field])
        additional = leaf.get("additionalData")
        if additional:
            body["additionalData"] = _opt(
                {
                    sub: _opt({f: _opt(v) for f, v in payload.items() if f in ("data", "dataSchemaVersion")})
                    for sub, payload in additional.items()
                }
            )
        out[key] = _opt(body)
    return _opt(out)


def _is_conflict(exc: Exception) -> bool:
    """True for a ConflictException from the control plane.

    Note this covers two different situations — "a record with this name+version
    already exists" and "the registry is not in READY state" — so callers must not
    treat it as proof that a record exists. :meth:`AwsAgentRegistry.register`
    confirms by lookup and re-raises when it finds nothing, which keeps the
    not-READY case an error instead of silently doing nothing.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict) and response.get("Error", {}).get("Code") == "ConflictException":
        return True
    return type(exc).__name__ == "ConflictException"


class AwsAgentRegistry:
    """Thin adapter over the Agent Registry control + data planes (GA)."""

    def __init__(self, registry_id: str, region: str | None = None) -> None:
        self.registry_id = registry_id
        self.region = region or _region()
        self.control = _make_client(CONTROL_SERVICE, self.region)
        self.data = _make_client(DATA_SERVICE, self.region)

    # -- health ----------------------------------------------------------

    def registry_status(self) -> str | None:
        """The registry's lifecycle status, or None if it could not be read.

        None means "we could not ask" — bad registryId, missing IAM, or a boto3
        bundle with no agent-registry model. A string means the registry answered.
        Keeping those apart is the same distinction :class:`RegistryQueryFailed`
        draws for records: a caller that collapses both into "unavailable" tells
        an admin to go fix their registryId when the registry is merely CREATING.
        """
        if self.control is None:
            return None
        try:
            return self.control.get_registry(registryId=self.registry_id).get("status") or ""
        except Exception as e:  # noqa: BLE001
            logger.info("AWS Agent Registry unreachable: %s", str(e)[:120])
            return None

    def available(self) -> bool:
        """True only when the registry exists AND is READY to accept writes.

        get_registry() succeeding is NOT sufficient. A registry that exists but
        sits in CREATING/UPDATING/DELETING rejects CreateRegistryRecord with
        ``ConflictException: Registry is not in READY state``. The previous check
        returned True throughout the multi-second CREATING window, so enabling
        federation on a freshly created registry — the overwhelmingly common
        sequence — passed validation and then raced into that conflict on the
        first deploy, where auto-register is best-effort and swallows it.
        """
        return self.registry_status() == REGISTRY_STATUS_READY

    # -- records ---------------------------------------------------------

    def register(
        self,
        name: str,
        record_type: str,
        descriptors: dict,
        description: str = "",
        record_version: str = "1.0",
        display_name: str | None = None,
    ) -> dict:
        """Upsert a record and return {record_id, arn, status, record_type, name}.

        ``record_type`` is the GA recordType (MCP/AGENT/CUSTOM/SKILL); preview
        spellings are normalized. Returns an empty dict when the Registry client
        is unavailable, so callers stay best-effort.

        Upsert, not create: ``name`` + ``recordVersion`` is a uniqueness key, and
        ``recordVersion`` is "1.0" for everything this platform registers. So the
        SECOND deployment of an agent under the same name — an ordinary redeploy —
        raised ConflictException, which the deploy path swallows as best-effort. The
        visible symptom was a registry record frozen at the first deployment's
        runtime ARN and endpoint, silently stale forever, with no error surfaced
        anywhere. Falling back to UpdateRegistryRecord keeps exactly one record per
        agent, always describing the live runtime.

        Governance note: updating a record's content demotes it from APPROVED back
        to DRAFT (service behaviour, verified live). That is the desired outcome —
        a redeploy that changes what the agent exposes must be re-reviewed, and it
        means an upsert cannot be used to slip new content past an old approval.
        The corollary is that ``unapproved_integrations()`` will block a redeployed
        integration until it is approved again, which is the fail-closed reading.
        """
        if self.control is None:
            logger.info("register skipped — no %s client in this boto3 bundle", CONTROL_SERVICE)
            return {}

        rtype = normalize_record_type(record_type)
        expected_key = DESCRIPTOR_KEY_FOR_TYPE[rtype]
        # A source-only descriptor (http/agui) satisfies any recordType: its
        # content is synchronized from a source URL, so the type-specific inline
        # descriptor is legitimately absent. Rejecting those here would refuse a
        # record the service itself accepts.
        if descriptors and expected_key not in descriptors:
            if not any(k in descriptors for k in SOURCE_ONLY_DESCRIPTOR_KEYS):
                raise ValueError(
                    f"recordType {rtype} requires the {expected_key!r} descriptor, got {sorted(descriptors)}"
                )

        record_name = sanitize_record_name(name)
        # description has min length 1 in the service model — never send "".
        desc = (description or record_name)[:_DESCRIPTION_MAX]
        shown = (display_name or name or record_name)[:_NAME_MAX]

        try:
            resp = self.control.create_registry_record(
                registryId=self.registry_id,
                name=record_name,
                displayName=shown,
                description=desc,
                recordType=rtype,
                descriptors=descriptors,
                recordVersion=record_version,
            )
        except Exception as e:  # noqa: BLE001 — narrowed immediately below
            # ConflictException also means "registry not READY", so confirm a record
            # really exists before treating this as a redeploy; otherwise re-raise
            # and let the real error surface.
            existing = self._find_record(record_name, record_version) if _is_conflict(e) else None
            if existing is None:
                raise
            logger.info("registry record %s v%s exists — updating in place (redeploy)", record_name, record_version)
            return self._refresh_record(existing, descriptors, desc, shown, rtype, record_name)

        arn = resp.get("recordArn", "")
        return {
            "record_id": _record_id_from_arn(arn),
            "arn": arn,
            "status": resp.get("status", ""),
            "record_type": rtype,
            "name": record_name,
        }

    def _find_record(self, name: str, record_version: str) -> dict | None:
        """The record with this exact name + recordVersion, or None.

        That pair is the uniqueness key CreateRegistryRecord enforces, so it is what
        a ConflictException points at. List items carry ``recordId`` directly, so no
        ARN parsing is needed on this path.
        """
        try:
            # `filters[].values` is capped at one entry, so this is a single name.
            for rec in self.list_records_strict(filters=[{"name": "name", "values": [name]}]):
                if rec.get("recordVersion") == record_version:
                    return rec
        except RegistryQueryFailed as e:
            logger.info("could not look up an existing record named %s: %s", name, e)
        return None

    def _refresh_record(
        self,
        existing: dict,
        descriptors: dict,
        description: str,
        display_name: str,
        rtype: str,
        record_name: str,
    ) -> dict:
        """Point an existing record at the current deployment via UpdateRegistryRecord.

        ``name``/``recordType``/``recordVersion`` are deliberately not sent: they are
        the record's identity, and this call is only meant to refresh its content.
        """
        record_id = existing.get("recordId") or _record_id_from_arn(existing.get("recordArn", ""))
        resp = self.control.update_registry_record(
            registryId=self.registry_id,
            recordId=record_id,
            descriptors=_to_update_descriptors(descriptors),
            description=_opt(description),
            displayName=_opt(display_name),
        )
        return {
            "record_id": resp.get("recordId") or record_id,
            "arn": resp.get("recordArn") or existing.get("recordArn", ""),
            "status": resp.get("status", ""),
            "record_type": resp.get("recordType") or rtype,
            "name": resp.get("name") or record_name,
            "updated": True,
        }

    def submit_for_approval(self, record_id: str) -> None:
        if self.control is None:
            return
        self.control.submit_registry_record_for_approval(registryId=self.registry_id, recordId=record_id)

    def set_status(self, record_id: str, status: str, reason: str) -> None:
        """APPROVED / REJECTED / DEPRECATED — statusReason is required by the API."""
        if self.control is None:
            return
        self.control.update_registry_record_status(
            registryId=self.registry_id,
            recordId=record_id,
            status=status,
            statusReason=reason,
        )

    def get(self, record_id: str) -> dict | None:
        if self.control is None:
            return None
        try:
            return self.control.get_registry_record(registryId=self.registry_id, recordId=record_id)
        except Exception as e:  # noqa: BLE001
            logger.info("get_registry_record failed: %s", str(e)[:120])
            return None

    def list_records_strict(self, filters: list[dict] | None = None) -> list[dict]:
        """Every record in the registry, following nextToken; RAISES on failure.

        Use this — never the lenient `list_records()` — whenever an empty result
        would drive a policy decision. See RegistryQueryFailed for why an
        exception, not an empty list, is the only safe signal there.

        Pagination matters for correctness, not just completeness: the gating in
        unapproved_integrations() is fail-closed, so a truncated first page would
        block deploys against integrations that ARE approved further down the
        list. ``filters`` takes the GA control-plane shape
        ``[{"name": "name"|"status"|"recordType", "values": [...]}]``.
        """
        if self.control is None:
            raise RegistryQueryFailed(
                f"no {CONTROL_SERVICE} client available (boto3 {boto3.__version__}, "
                f"need >= {'.'.join(str(p) for p in MIN_BOTO3)})"
            )
        records: list[dict] = []
        token: str | None = None
        while True:
            try:
                resp = self.control.list_registry_records(
                    registryId=self.registry_id,
                    **({"nextToken": token} if token else {}),
                    **({"filters": filters} if filters else {}),
                )
            except Exception as e:  # noqa: BLE001
                raise RegistryQueryFailed(str(e)[:300], partial=records) from e
            records.extend(resp.get("registryRecords") or [])
            token = resp.get("nextToken")
            if not token:
                return records

    def list_records(self, filters: list[dict] | None = None) -> list[dict]:
        """Lenient listing for display/inventory: [] (or a partial page) on error.

        Safe for read-only surfaces where a short list is a cosmetic problem.
        NOT safe for fail-closed policy checks — use `list_records_strict()`.
        """
        try:
            return self.list_records_strict(filters=filters)
        except RegistryQueryFailed as e:
            logger.info("list_registry_records failed: %s", e)
            return e.partial

    def delete(self, record_id: str) -> bool:
        if self.control is None:
            return False
        try:
            self.control.delete_registry_record(registryId=self.registry_id, recordId=record_id)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("delete_registry_record failed: %s", str(e)[:120])
            return False

    def search(self, query: str, max_results: int = 20, record_types: list[str] | None = None) -> list[dict]:
        """Semantic search over DISCOVERABLE records.

        GA renamed this operation to SearchDiscoverableRegistryRecords and moved
        it onto the ``agent-registry`` data plane. ``registryIds`` takes exactly
        one entry (ARN or bare id). ``filters`` is a structured metadata filter
        supporting $eq/$ne/$in — not the control plane's list-of-filters shape.

        NEVER USE THIS FOR AN APPROVAL DECISION. The data plane is a search index,
        not the record store, and it lags the control plane: a record demoted from
        APPROVED back to DRAFT keeps being served here — still ``"status":
        "APPROVED"`` — long after the control plane reports DRAFT (live-verified,
        stable for minutes). Two planes, one field name, two trust levels.

        That divergence is reachable on the ordinary path, not an exotic one:
        :meth:`register` upserts on redeploy, and updating a record's content
        demotes it to DRAFT. So every redeploy of an approved integration opens
        the window. Gating therefore reads :meth:`list_records_strict` (control
        plane) and re-checks each record's status itself; routing it through this
        method for speed would let a stale index re-approve a record whose content
        has since changed — a governance bypass that no test of the happy path
        would notice. ``test_gating_never_reads_the_data_plane`` guards it.

        Returns [] on failure, deliberately: this is a browse/discovery feature
        where "no matches" and "search unavailable" are both empty result sets to
        the user. Do not copy this pattern into a policy path — that is exactly
        what :class:`RegistryQueryFailed` exists to prevent.
        """
        if self.data is None:
            return []
        kwargs: dict = {
            "registryIds": [self.registry_id],
            "searchQuery": query,
            "maxResults": max_results,
        }
        if record_types:
            normalized = [normalize_record_type(t) for t in record_types]
            kwargs["filters"] = {"recordType": {"$in": normalized}}
        try:
            resp = self.data.search_discoverable_registry_records(**kwargs)
            return resp.get("registryRecords") or []
        except Exception as e:  # noqa: BLE001
            logger.info("search_discoverable_registry_records failed: %s", str(e)[:120])
            return []


# ---------------------------------------------------------------------------
# Opt-in config (a Settings row holds the registryId; feature off when unset).
# Reuses the TagPolicy table as a generic Settings store to avoid a new table.
# ---------------------------------------------------------------------------

_SETTINGS_SK = "SETTING#aws_registry_id"


def get_configured_registry_id() -> str | None:
    """Return the configured AWS registryId, or None (feature disabled).

    Env override AWS_AGENT_REGISTRY_ID wins (useful for tests / static config);
    otherwise read the Settings row from the tag-policy table.
    """
    env = os.environ.get("AWS_AGENT_REGISTRY_ID")
    if env:
        return env
    try:
        table_name = os.environ.get("TAG_POLICY_TABLE_NAME", "TagPolicy")
        table = boto3.resource("dynamodb", region_name=_region()).Table(table_name)
        item = table.get_item(Key={"org_id": "default", "sk": _SETTINGS_SK}).get("Item")
        return item.get("value") if item else None
    except Exception as e:  # noqa: BLE001
        logger.info("get_configured_registry_id failed: %s", str(e)[:120])
        return None


def set_configured_registry_id(registry_id: str) -> None:
    table_name = os.environ.get("TAG_POLICY_TABLE_NAME", "TagPolicy")
    table = boto3.resource("dynamodb", region_name=_region()).Table(table_name)
    table.put_item(Item={"org_id": "default", "sk": _SETTINGS_SK, "value": registry_id})


def get_registry() -> AwsAgentRegistry | None:
    """Return a configured adapter, or None when the feature is disabled.

    Never raises: an adapter built on a boto3 bundle without the agent-registry
    models carries None clients and reports available() == False.
    """
    rid = get_configured_registry_id()
    if not rid:
        return None
    return AwsAgentRegistry(rid)


def unapproved_integrations(identifiers: list[str]) -> list[str]:
    """Integration gating (Loom-study 1.4): of the given external MCP/A2A
    identifiers (server name or endpoint URL), return those that are NOT
    APPROVED in the AWS Agent Registry.

    Provider-dispatched (Workstream B): when the active registry backend is
    LiteLLM, that backend answers the gate instead — see
    ``registry_providers.unapproved_integrations_for_provider``. Everything below
    describes the AWS Agent Registry path, which is what runs by default.

    No-op ([]) when federation is disabled — gating only applies once an org
    opts into registry governance. Matching is by record name OR by a URL
    substring within any descriptor, so a connected server is considered
    approved when an APPROVED record names it or points at it. An identifier
    with NO matching record at all is treated as UNAPPROVED (fail-closed: an
    unreviewed integration must not ship into a governed deployment).

    The APPROVED filter is applied server-side (GA control-plane ``filters``) and
    the listing is paginated, so a large catalog cannot silently truncate into a
    false "unapproved" verdict.

    Raises:
        RegistryQueryFailed: the registry could not be queried, so approval status
            is UNKNOWN. Deliberately propagated rather than degraded to "nothing
            is approved": an AccessDenied on
            ``agent-registry:ListRegistryRecords`` would otherwise render as a 403
            telling the operator their integrations were rejected, sending them to
            fix a governance record when the real fault is an IAM policy. The
            caller decides what an unknown verdict means — but it must not be
            silently treated as a denial.
    """
    if not identifiers:
        return []

    # Workstream B: the gate is provider-dispatched. The dispatch lives HERE, not
    # in deployment_handler, so the triad at the call site (RegistryQueryFailed ->
    # 503, non-empty -> 403, other -> logged and proceed) is untouched and keeps
    # its tests. `None` means "the active registry backend does not govern this
    # gate" and falls through to the AWS federation path below; a backend that
    # governs returns a list, and [] from it is a real "all approved" verdict.
    # Lazy import: registry_providers imports this module, so a top-level import
    # would be circular.
    from app.services.registry_providers import unapproved_integrations_for_provider

    delegated = unapproved_integrations_for_provider(identifiers)
    if delegated is not None:
        return delegated

    reg = get_registry()
    if reg is None:
        return []  # federation off → no gating

    records = reg.list_records_strict(filters=[{"name": "status", "values": ["APPROVED"]}])
    approved_names: set[str] = set()
    approved_blobs: list[str] = []
    for r in records:
        # Defence in depth: the filter already narrows to APPROVED, but a stubbed
        # or older control plane that ignores `filters` must not widen the gate.
        if (r.get("status") or "").upper() != "APPROVED":
            continue
        for key in ("name", "recordName", "displayName"):
            nm = r.get(key)
            if nm:
                approved_names.add(str(nm))
        # keep a coarse text blob per record for URL substring matching
        approved_blobs.append(json.dumps(r, default=str))

    unapproved: list[str] = []
    for ident in identifiers:
        if not ident:
            continue
        if ident in approved_names:
            continue
        if any(ident in blob for blob in approved_blobs):
            continue
        unapproved.append(ident)
    return unapproved
