"""One pure validator for the deployment payload, in TWO PHASES, used by BOTH callers.

F-55. The platform used to compute a validation verdict and throw it away. Two defects that
masked each other perfectly:

1. Nothing read the verdict. ``grep -rn "is_valid" infra/stacks/`` returned ZERO, so the state
   machine went from ValidateWorkflow straight to the first resource-creating choice.
2. The verdict was invariably ``False`` anyway, because the handler looked up a workflow id in
   the ``workflows`` table and nothing ever writes the canvas there. Live: ``workflows`` held
   0 items, ``flows`` held 7, and ``DeployRequest`` carried no flow id at all -- the SFN input's
   ``workflow_id`` was ``request.node_id``, a canvas NODE id. The two are now separate:
   ``workflow_id`` is the optional, owner-checked ``flowId`` and ``node_id`` the required node.

Proven live by an execution that SUCCEEDED while emitting
``is_valid = False, errors = ["Workflow 'd47f6a7b85' not found"]`` and then creating every
resource anyway. A green deployment was not evidence the input was valid; it was not even
evidence the input existed.

Fixing either half alone is worse than fixing neither. Closing the gate while the handler still
read the empty table converts a 100% fail-open into a 100% outage. Repairing the lookup while
nothing reads the verdict changes nothing at all. So the verdict's SOURCE is replaced here --
the authoritative payload the event already carries -- and the gate is closed in the same change.

WHY TWO PHASES, AND WHY THE ONE-VALIDATOR RULE WOULD OTHERWISE BE AN OUTAGE
---------------------------------------------------------------------------
The requirement is one shared validator so the API boundary and ValidateStep cannot drift, run
as early as possible -- before any side effect. But the two callers see DIFFERENT payloads, and
the difference is exactly raw credential material:

* ``PayloadPhase.REQUEST`` is the client-supplied deploy request, BEFORE
  ``_prepare_deployment_credentials``. That function *pops* raw values --
  ``deployment_handler.py:426-479`` pops ``secret_value``/``secretValue`` off each connector and
  each external MCP selection, ``client_secret``/``clientSecret`` off each selection's ``oauth``,
  and ``litellm_api_key``/``litellmApiKey`` off the gateway config -- stages each into Secrets
  Manager and writes back only an ARN. Those raw keys are therefore LEGITIMATELY PRESENT at this
  phase: that is how a first-time credential is supplied at all.
* ``PayloadPhase.PREPARED`` is the ``sfn_input`` that actually enters the execution, AFTER
  staging. Here no raw secret may exist anywhere, and the server-authored fields exist.

A single phase-less validator that forbids raw secret keys everywhere would, at the API
boundary, reject every deployment that supplies a credential for the first time -- a 100%
outage for the credential path, the same failure shape as F-55 itself. A validator that instead
runs only AFTER staging cannot satisfy "validate before any side effect", because staging IS a
side effect (it writes secrets and appends strict manifest rows). Hence: one validator, two
phases, and in the REQUEST phase raw values are permitted ONLY at the exact approved write-only
paths in ``_RAW_SECRET_WRITE_ONLY_PATHS`` -- an allowlist of paths, not a relaxation of the rule.
Every other structural check runs identically in both phases.

WHY A TABLE LOOKUP CANNOT BE THE SOURCE OF TRUTH, beyond the empty table: the UI posts a node
id plus compiled component configs, with no flow id and no canvas snapshot. A lookup would
therefore have to be by an id the request does not contain. Validating the payload instead also
removes a staleness window (the canvas could change between save and deploy) and removes an
IDOR surface (there is no id to point at another tenant's row). Client-side validation is not a
gate: ARCC cnt_ik6StRHfs118ea -- "do not treat it as a security mitigation, as it is easily
bypassed".

ARCC guidance applied:
  * cnt_ik6StRHfs118ea -- validate before further processing, "apply the validation at the
    earliest logical time", prefer allowlists/enums over deny lists, and return a generic
    message to the caller while keeping detail in logs. This module is therefore PURE: no
    boto3, no environment reads, no IO. Purity is what lets the API boundary run it BEFORE any
    side effect, which is the point of running it at all.
  * cnt_QAWqFk4LdKNGAO -- every segment of a caller-supplied ARN must be validated
    (partition, service, region, 12-digit account, non-empty relative id, no URL-unsafe
    characters), because malformed ARNs entering a workflow cause incorrect resource-level
    authorization. Generic ARN syntax is NOT enough: a syntactically perfect
    ``arn:aws:s3:::some-bucket`` in ``target_role_arn`` is still an authorization defect, so
    each ARN field below declares the service, resource prefix, region policy and account it
    must actually match.
  * cnt_jljdNeOwgPnFx2 -- downstream services must independently authorize rather than trust
    an upstream check. That is why ValidateStep revalidates with the same function instead of
    trusting that the API already did: two callers, one validator, no drift.
  * cnt_LuG2TKuO0errRp -- raw credential material belongs in a secret store and is referenced,
    never carried. The PREPARED phase is what enforces that the carry never happens.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

#: Allowlists, taken from the Literal annotations on ``DeployRequest`` rather than invented
#: here. An allowlist is required over a deny list by cnt_ik6StRHfs118ea.
ALLOWED_DEPLOYMENT_MODES = frozenset({"runtime", "harness"})
ALLOWED_DEPLOYMENT_SLOTS = frozenset({"staging", "production"})

#: AWS partitions this platform deploys into. cnt_QAWqFk4LdKNGAO requires the partition to be
#: checked explicitly: accepting ``aws-us-gov`` in an ``aws`` deployment can bypass a deny
#: policy, and accepting an arbitrary string means the ARN was never parsed at all.
ALLOWED_PARTITIONS = frozenset({"aws", "aws-cn", "aws-us-gov"})

#: Syntactic region check only -- a real region list would go stale and reject new regions.
#: The AUTHORIZATION decision about which regions are permitted is made elsewhere (the
#: admin-gated allowlist); this rejects values that are not region-shaped at all. Coherence
#: with the partition IS checked, by ``_partition_for_region``.
_REGION_RE = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d$")

_ACCOUNT_RE = re.compile(r"^\d{12}$")

#: A node id reaches S3 keys and AWS resource names. Restrict to a conservative charset rather
#: than enumerating what to reject: the URL-unsafe and reserved character lists in
#: cnt_QAWqFk4LdKNGAO are long, and an allowlist cannot be incomplete in the dangerous
#: direction.
#:
#: LENGTH IS DELIBERATELY 256, NOT 128. ``DeployRequest.node_id`` is declared
#: ``max_length=256`` (models/deployment_models.py), and the prepared ``node_id`` is exactly
#: that value. A validator stricter than the contract it validates rejects input the API already
#: accepted -- that is an outage, not a control. Length is not the security boundary here; the
#: charset is, and it is stricter than the model's own ``[a-zA-Z0-9_-]+`` because it also
#: forbids a leading non-alphanumeric. Downstream name derivation truncates to each AWS
#: service's own limit (e.g. ``agentcore_runtime_name`` is built from a 39-char slice), so a
#: long-but-safe id cannot overflow a resource name.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")

#: A FLOW id, which is a different object from a node id with a different contract: exactly
#: ``DeployRequest.flow_id`` and ``routers/flows.py::_validate_flow_id`` (1-128, ``[a-zA-Z0-9_-]``).
_FLOW_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

#: The URL-unsafe and reserved characters cnt_QAWqFk4LdKNGAO names for an ARN's relative id.
_ARN_UNSAFE_CHARS = set(' <>"#%*{}|\\^~[]`?&=$,;') | {"\t", "\n", "\r"}


class PayloadPhase(str, Enum):
    """Which payload is being validated. See the module docstring for why this exists."""

    #: The client-supplied deploy request, before any credential has been staged. Raw secret
    #: material is permitted ONLY at the approved write-only paths.
    REQUEST = "request"
    #: The ``sfn_input`` that enters the execution. No raw secret material anywhere, and the
    #: server-authored fields must all be present.
    PREPARED = "prepared"


#: Traversal budget. Exceeding any of these is itself a REJECTION, never a silent stop.
#:
#: This is the correction to a real bypass: the first version of this module returned silently
#: at ``depth > 8``, so a raw secret nested at depth 9 was not merely unreported -- the security
#: control simply did not run on it. A payload that cannot be fully inspected has not been
#: validated, and "not validated" must fail closed. The limits are set well above any legitimate
#: payload (the deepest real nesting is config -> observability -> a handful of scalars) so
#: refusing at the cap cannot reject real input, while still bounding the work an attacker can
#: force this function to do (cnt_QQz2pERJ9yvemV).
_MAX_DEPTH = 16
_MAX_NODES = 20_000
_MAX_SEQUENCE_ITEMS = 2_000
_MAX_MAPPING_KEYS = 500

#: Every top-level key the ONE producer of the prepared payload emits
#: (``deployment_handler.py:1552-1651``, the only ``start_execution`` call site in the codebase,
#: via the helper at ``:124``). Because there is exactly one producer and one consumer, this
#: set can be CLOSED: an unexpected top-level key in the prepared payload means either an
#: injected field or a producer change that never updated this validator, and both must stop
#: the deployment rather than be ignored. ``test_prepared_allowlist_matches_the_sfn_input_builder``
#: reconciles this set against the builder's source so the two cannot drift.
_PREPARED_TOP_LEVEL_FIELDS = frozenset(
    {
        # unconditional
        "deployment_id",
        "workflow_id",
        "node_id",
        "config",
        "connected_tools",
        "template_id",
        "resource_tags",
        "target_account_id",
        "target_region",
        "target_role_arn",
        "target_runtime_role_arn",
        "target_mcp_runtime_role_arn",
        "target_harness_role_arn",
        "target_artifact_bucket",
        "version_id",
        "friendly_runtime_name",
        "agentcore_runtime_name",
        "deployment_slot",
        "parent_version_id",
        "owner_sub",
        "deployment_mode",
        # conditional
        "recorded_secret_arns",
        "gateway_config",
        "gateway_tools",
        "identity_config",
        "custom_tools",
        "connectors",
        "external_mcp_servers",
        "memory_config",
        "evaluation_config",
        "policy_config",
        "mcp_server_config",
        "knowledge_base_config",
        "guardrails_config",
        "observability_config",
        "platform_observability_defaults",
        "a2a_config",
    }
)

#: Fields that are ValidateStep's OUTPUT and the state machine's failure state -- never its
#: input. They are listed separately, and REJECTED, rather than quietly allowed.
#:
#: The first version of this allowlist included them, reasoning that a retried validation might
#: see its own output. It cannot: Step Functions re-invokes a retried task with the ORIGINAL
#: input, and ValidateWorkflow is the first state, so nothing upstream can have written them.
#: Accepting them was a real hole, and the worst of the five is ``no_resources_created``: the
#: success path returns ``{**event, ...}``, so an injected marker would survive into the state,
#: and a later genuine failure would then find a cleanup-suppressing flag it never authored.
#: (``_proven_no_resources`` additionally refuses to act on the marker unless the manifest is
#: actually empty, so this is the second of two independent barriers -- input validation is
#: defense in depth, not the sole control: ARCC cnt_ik6StRHfs118ea.)
_VALIDATOR_OUTPUT_FIELDS = frozenset({"is_valid", "errors", "error", "error_info", "no_resources_created"})

#: Server-authored fields the prepared payload cannot proceed without. Every one of these is
#: assigned unconditionally by the producer, so requiring them cannot reject a legitimate
#: payload -- their absence means the payload was not built by the producer at all.
#:
#: ``owner_sub`` is included on purpose even though the producer writes ``user_id or ""``:
#: ``_get_user_id`` returns ``None`` when there is no JWT authorizer claim, so an EMPTY
#: ``owner_sub`` is reachable and means an OWNERLESS deployment. Every downstream ownership
#: check then compares against "" and degenerates -- the same shape as an absent tag comparing
#: equal to an unset env var. Refusing is the safe direction: no tenant, no deployment.
#:
#: Deliberately NOT required, because the producer legitimately leaves them unset:
#: ``parent_version_id`` (absent for a first version), ``target_artifact_bucket`` (set only on
#: the cross-account branch at ``deployment_handler.py:1273``), ``template_id`` (optional).
_PREPARED_REQUIRED_FIELDS = (
    "deployment_id",
    "node_id",
    "owner_sub",
    "version_id",
    "friendly_runtime_name",
    "agentcore_runtime_name",
    "deployment_mode",
    "deployment_slot",
)

#: Payload members that MUST be objects when present, with every key spelling each phase uses.
#: The REQUEST phase carries pydantic aliases (camelCase); the PREPARED phase carries the
#: snake_case keys the producer writes. Checking both spellings in both phases is deliberate:
#: it costs nothing and removes the failure mode where a check silently applies to neither.
#:
#: A scalar here does not fail loudly downstream -- it fails deep inside a step handler with an
#: AttributeError, after resources have been created. The state machine's own ``is_present``
#: choices make this worse: a ``gateway_config`` of ``"true"`` is *present*, so the gateway task
#: runs and then dies.
_OBJECT_FIELD_KEYS: tuple[tuple[str, ...], ...] = (
    ("config",),
    ("gateway_config", "gatewayConfig"),
    ("memory_config", "memoryConfig"),
    ("identity_config", "identityConfig"),
    ("knowledge_base_config", "knowledgeBaseConfig"),
    ("guardrails_config", "guardrailsConfig"),
    ("policy_config", "policyConfig"),
    ("evaluation_config", "evaluationConfig"),
    ("mcp_server_config", "mcpServerConfig"),
    ("observability_config", "observabilityConfig"),
    ("platform_observability_defaults", "platformObservabilityDefaults"),
    ("a2a_config", "a2aConfig"),
    ("resource_tags", "resourceTags"),
)

#: Payload members that must be arrays when present, for the same reason. Each entry also
#: declares what the ITEMS must be and how many are allowed, because a list of strings where
#: the handler expects objects fails just as deep as a scalar does, and an unbounded list is a
#: work multiplier for every downstream step.
#:
#: The bounds are taken from ``DeployRequest``'s own ``max_length`` declarations
#: (models/deployment_models.py:696-710) rather than invented, for the same reason the enums
#: are: a validator stricter than the contract rejects input the API already accepted.
#: ``connected_tools`` and ``gateway_tools`` are TOOL ID lists -- ``connected_tools`` is put
#: through ``set()`` at ``:800`` (so its items must be hashable strings) and ``gateway_tools``
#: is documented as "Tool IDs to deploy as Lambda targets" -- so "any item" was too weak:
#: an object item there reaches ``codegen_step`` and ``gateway_step`` and fails inside them.
#: ``custom_tools`` has no declared bound because it is a typed pydantic model list; the
#: generic node budget still applies to it.
_SEQUENCE_FIELD_KEYS: tuple[tuple[tuple[str, ...], str, int], ...] = (
    (("connected_tools", "connectedTools"), "string", 20),
    (("gateway_tools", "gatewayTools"), "string", 20),
    (("custom_tools", "customTools"), "object", _MAX_SEQUENCE_ITEMS),
    (("connectors",), "object", 20),
    (("external_mcp_servers", "externalMcpServers"), "object", 20),
    (("recorded_secret_arns", "recordedSecretArns"), "string", _MAX_SEQUENCE_ITEMS),
)

#: Keys that must NEVER carry a raw value in the PREPARED payload, and must carry one only at
#: an approved path in the REQUEST payload. A raw value in the execution input would be
#: persisted in the execution history and echoed by DescribeExecution -- the same exposure class
#: as an env var and unfixable after the fact, because execution history is retained 90 days.
#:
#: Matched on a CANONICAL form of the key, not the literal. The first version of this compared
#: ``key in {...}`` against literal spellings, which meant ``ApiKey``, ``API_KEY``, ``api-key``,
#: ``Authorization`` and ``access_token`` all passed a control whose stated guarantee is "no raw
#: secret anywhere". Only the exact lowercase spellings were ever checked. Canonicalizing
#: collapses case and separators so one token covers every spelling a caller might send, and the
#: set below is the set of TOKENS, not of field names.
_FORBIDDEN_SECRET_TOKENS = frozenset(
    {
        # Credential values popped by _prepare_deployment_credentials.
        "secretvalue",
        "clientsecret",
        "apikey",
        "litellmapikey",
        "virtualkey",
        # Generic credential material. Absent from the first version, so each of these was a
        # live bypass of the whole scan.
        "secret",
        "password",
        "passwd",
        "pwd",
        "privatekey",
        "signingkey",
        "secretkey",
        "accesskey",
        "secretaccesskey",
        "credentials",
        "connectionstring",
        # Bearer material. ``authorization`` and ``xapikey`` are header NAMES: they appear as
        # MAP KEYS under observabilityConfig.extraHeaders, whose values are serialized verbatim
        # into OTEL_EXPORTER_OTLP_EXTRA_HEADERS (services/observability.py:343). That is a
        # runtime environment variable, and GetAgentRuntime returns runtime env vars in
        # plaintext -- so a plaintext header is TWO exposures from one input: the execution
        # history and the runtime's own describe call. The supported alternative already exists
        # and takes a reference rather than a value: ``authHeaderSecretArn``
        # (services/observability.py:274, namespace-constrained at :48), which is the shape ARCC
        # cnt_dwzZ05hLnqhYXQ requires -- "the service should integrate with Secrets Manager
        # instead, and accept a reference to a Secrets Manager secret as input".
        "authorization",
        "proxyauthorization",
        "xapikey",
        "xauthtoken",
        "accesstoken",
        "refreshtoken",
        "sessiontoken",
        "idtoken",
        "bearertoken",
        "token",
    }
)

#: Separators stripped when canonicalizing a key, so ``api_key``/``api-key``/``API.KEY``/
#: ``Api Key`` all collapse to ``apikey``.
_KEY_SEPARATORS = str.maketrans({c: None for c in "_-. "})


#: Inside an ``extraHeaders`` map ONLY, a credential name is matched by WHOLE TOKEN rather than by
#: exact whole-name equality. Exact-name equality caught ``Authorization`` and ``X-API-Key`` but
#: not ``X-Authorization``, ``Api-Key-Value`` or ``X-Custom-Auth``, and every one of those has its
#: value serialized verbatim into ``OTEL_EXPORTER_OTLP_EXTRA_HEADERS``
#: (services/observability.py:343) -- an environment variable, which ARCC cnt_n8LpZcqYi2t3I2 says
#: must never hold a secret, and which GetAgentRuntime returns in plaintext.
#:
#: The first version of this rule matched by SUBSTRING against the CANONICAL key, and that was
#: wrong in a way worth recording, because it was an outage rather than a leak. ``_canon_key``
#: strips separators, so the canonical form carries no word boundaries at all: ``X-Monkey`` and
#: ``X-Hockey`` become ``xmonkey``/``xhockey`` and matched ``key$``; ``X-Authorship`` became
#: ``xauthorship`` and matched ``auth``; ``X-Tokenization`` matched ``token``. Legitimate
#: extension headers were refused with a credential error. Boundary-aware matching is not
#: possible on a form whose boundaries have been deleted, so the classifier below tokenizes the
#: RAW header name instead -- on separators AND on camel-case humps, so ``apiKey`` and
#: ``Api-Key-Value`` tokenize alike.
#:
#: This is mitigation, not the compliant shape. cnt_77BHvX7WzuG1X8 requires credentials to arrive
#: only as a Secrets Manager reference, which is why the refusal names ``authHeaderSecretArn``:
#: the supported path already exists and takes a reference.
#:
#: Whole tokens that are credential material in any header. Each is a complete word: ``auth``
#: is here and matches ``X-Custom-Auth``, but does NOT match ``authorship``, which is the whole
#: point of tokenizing.
_CREDENTIAL_HEADER_TOKENS = frozenset(
    {
        "auth",
        "authorization",
        "authorisation",
        "authn",
        "token",
        "secret",
        "credential",
        "credentials",
        "password",
        "passwd",
        "pwd",
        "bearer",
        "jwt",
        "hmac",
        "sig",
        "session",
        "psk",
        # Deliberate dispositions, asked for explicitly by the Codex reviewer rather than left
        # to emerge from a pattern. Both are REFUSED.
        #
        # ``cookie``: a Cookie header is credential-bearing by definition, so ``X-Cookie-Policy``
        # is refused too. A policy string is a plausible reading of that name, but a mislabelled
        # session cookie is a likelier one inside a map of outbound headers, and the two costs are
        # not symmetric -- refusing costs an explicit 4xx that names the field, admitting costs a
        # session cookie in plaintext in a runtime env var that GetAgentRuntime returns.
        "cookie",
        # ``signature``: an HMAC signature is derived from a key and is replayable, and
        # ``X-Signature-Version`` -- a scheme version, genuinely not secret -- is one token away
        # from ``X-Signature``. Refused for the same asymmetry.
        "signature",
    }
)

#: ``key`` alone is too common to refuse outright: ``X-Partition-Key`` and ``X-Idempotency-Key``
#: are routine and carry nothing secret. It counts as credential material when it sits next to a
#: token that makes it one, which is what catches the two names this rule exists for
#: (``X-API-Key``, ``Api-Key-Value``), or when it is the only word in the name.
_KEY_QUALIFYING_TOKENS = frozenset(
    {"api", "access", "secret", "private", "signing", "subscription", "license", "auth", "app"}
)

#: Splits a raw header name into words on separators and on camel-case humps. ``X-API-Key`` ->
#: ``x api key``; ``apiKey`` -> ``api key``; ``XAPIKey`` -> ``xapi key`` (an acronym run followed
#: by a capitalized word), which still yields the ``key`` token the rule needs.
_HEADER_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|[0-9]+")


def _header_name_tokens(key: str) -> list[str]:
    """The lowercase words of a raw header name, for whole-token credential matching."""
    return [w.casefold() for w in _HEADER_WORD_RE.findall(key)]


def _header_name_is_credential(key: str) -> bool:
    """Whether a header NAME denotes credential material, by whole token not by substring.

    Scoped to ``extraHeaders`` maps. It is deliberately not applied to ordinary payload fields,
    where reference fields such as ``providerApiKeyRef`` would tokenize to
    ``provider api key ref`` and be refused even though a reference is the shape we WANT.
    """
    tokens = _header_name_tokens(key)
    if any(t in _CREDENTIAL_HEADER_TOKENS for t in tokens):
        return True
    if "key" not in tokens:
        return False
    # ``key`` qualified by an adjacent word, or standing alone as the whole name.
    meaningful = [t for t in tokens if t != "x"]
    if meaningful == ["key"]:
        return True
    return any(
        tokens[i - 1] in _KEY_QUALIFYING_TOKENS or (i + 1 < len(tokens) and tokens[i + 1] in _KEY_QUALIFYING_TOKENS)
        for i, t in enumerate(tokens)
        if t == "key" and i > 0
    )


#: The canonical name of the free-text header map whose values reach the exporter env var.
_HEADER_MAP_TOKEN = "extraheaders"


def _canon_key(key: str) -> str:
    """The comparison form of a payload key: casefolded, separators removed.

    Deliberately NOT used for the write-only path allowlist below. That allowlist must stay on
    literal spellings, because it enumerates exactly what ``_prepare_deployment_credentials``
    pops -- a canonical allowlist would admit ``secret-value`` at the request boundary, which
    staging does not pop, so the raw value would survive into the execution input.
    """
    return key.casefold().translate(_KEY_SEPARATORS)


#: The EXACT paths at which the REQUEST phase permits raw secret material, normalized so every
#: sequence index reads ``[*]``. Each one corresponds to a ``.pop(...)`` in
#: ``_prepare_deployment_credentials`` -- that is, to a value the server is about to remove from
#: the payload and write into Secrets Manager. Nothing else may carry a raw secret, at any
#: depth, in either phase. Both spellings appear because the staging code pops both.
_RAW_SECRET_WRITE_ONLY_PATHS = frozenset(
    {
        # deployment_handler.py:426-427
        "$.connectors[*].secret_value",
        "$.connectors[*].secretValue",
        # deployment_handler.py:439-440
        "$.external_mcp_servers[*].secret_value",
        "$.external_mcp_servers[*].secretValue",
        "$.externalMcpServers[*].secret_value",
        "$.externalMcpServers[*].secretValue",
        # deployment_handler.py:451-452
        "$.external_mcp_servers[*].oauth.client_secret",
        "$.external_mcp_servers[*].oauth.clientSecret",
        "$.externalMcpServers[*].oauth.client_secret",
        "$.externalMcpServers[*].oauth.clientSecret",
        # deployment_handler.py:470-471
        "$.gateway_config.litellm_api_key",
        "$.gateway_config.litellmApiKey",
        "$.gatewayConfig.litellm_api_key",
        "$.gatewayConfig.litellmApiKey",
    }
)


@dataclass(frozen=True)
class ValidationContext:
    """Trusted, NON-payload facts about where this deployment actually runs.

    Why this exists rather than reading the target fields out of the payload: for a HOME
    deployment ``target_account_id`` and ``target_region`` are legitimately ``None``
    (``deployment_handler.py:1243-1248`` -- they are only populated when the request names a
    target), so with the payload as the only source there is nothing to compare a staged secret
    ARN against, and a secret ARN naming ANOTHER account would pass. cnt_QAWqFk4LdKNGAO is
    explicit that a caller-specified account segment must match the resolved account; an
    unconstrained one is the cross-account-access threat it names.

    Provenance matters and is worth stating precisely, because it is easy to overstate:
      * ``target_region`` and all three ``target_*_role_arn`` values in the prepared payload are
        server-minted from the admin-registered target row (``:1267-1272``); a caller cannot
        supply them.
      * ``target_account_id`` is caller-CHOSEN but server-AUTHORIZED: it must resolve through
        ``resolve_registered_account_target`` or the request is refused with HTTP 400 (``:1278``).
      * The values HERE are neither -- they come from the executing Lambda itself (its own
        invoked-function ARN and region), so they cannot be influenced by the request at all.
        That keeps this module pure: deriving them requires no IO and no STS call.

    Both fields are optional so the validator still runs (and still checks ARN syntax,
    partition/region coherence and every other rule) when a caller cannot supply them. Absent
    context weakens exactly one check -- which account a staged secret must live in -- and does
    not silently weaken any other.
    """

    home_account_id: str | None = None
    home_region: str | None = None


@dataclass(frozen=True)
class PayloadError:
    """One rejection. ``field`` is the payload path; ``code`` is stable for tests."""

    field: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"field": self.field, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class PayloadValidation:
    """The verdict. ``is_valid`` is the field the state machine's Choice reads."""

    errors: tuple[PayloadError, ...] = field(default=())

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def as_error_dicts(self) -> list[dict[str, str]]:
        return [e.as_dict() for e in self.errors]

    def codes(self) -> set[str]:
        """Stable codes, for tests and for callers that branch on the kind of rejection."""
        return {e.code for e in self.errors}

    def summary(self, limit: int = 3) -> str:
        """A short operator-facing line for the deployment record's ``error_details``.

        Truncated on purpose. The full list goes to ``errors`` and to the log; this string is
        what a human reads first, and an unbounded concatenation of every field error is how a
        useful message becomes an unreadable one.
        """
        if not self.errors:
            return ""
        head = "; ".join(f"{e.field}: {e.message}" for e in self.errors[:limit])
        if len(self.errors) > limit:
            head += f" (+{len(self.errors) - limit} more)"
        return f"Deployment input rejected before any resource was created. {head}"


def _partition_for_region(region: str) -> str:
    """The only partition a given region can belong to.

    Coherence, not taste: ``arn:aws:iam::...`` naming a ``cn-north-1`` resource cannot exist,
    and accepting the pair means the ARN was never really parsed (cnt_QAWqFk4LdKNGAO).
    """
    if region.startswith("cn-"):
        return "aws-cn"
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    return "aws"


def _check_arn(
    errors: list[PayloadError],
    path: str,
    value: object,
    *,
    expect_service: str | None = None,
    expect_resource_prefix: str | None = None,
    region_policy: str = "optional",
    expect_region: str | None = None,
    account_policy: str = "optional",
    expect_account: str | None = None,
    allowed_accounts: Sequence[str | None] = (),
) -> None:
    """Validate every segment of a caller-supplied ARN, IN CONTEXT.

    ARCC cnt_QAWqFk4LdKNGAO. Generic syntax is not sufficient: the caller controls these
    strings, and a well-formed ARN pointing at the wrong service, the wrong account or the wrong
    partition is precisely the "incorrect resource-level authorization" the guidance names. So
    each call site declares what the ARN must actually be.

    ``region_policy`` is one of ``empty`` (IAM and other global services -- a NON-empty region
    is the defect), ``required``, or ``optional``. The empty/required distinction is the one a
    naive "all six segments non-empty" check gets wrong, and getting it wrong rejects every
    legitimate IAM role ARN the payload carries.
    """
    if not isinstance(value, str) or not value:
        errors.append(PayloadError(path, "arn_not_a_string", "must be a non-empty ARN string"))
        return
    parts = value.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn":
        errors.append(PayloadError(path, "arn_malformed", "must have all six ARN segments and start with 'arn'"))
        return
    _, partition, service, region, account, relative = parts

    if partition not in ALLOWED_PARTITIONS:
        # Not cosmetic: a cross-partition ARN can bypass a deny policy written for one
        # partition while still parsing as a valid ARN.
        errors.append(PayloadError(path, "arn_bad_partition", f"partition {partition!r} is not recognized"))

    if not service:
        errors.append(PayloadError(path, "arn_empty_service", "the service segment is empty"))
    elif expect_service is not None and service != expect_service:
        errors.append(
            PayloadError(
                path,
                "arn_wrong_service",
                f"must be a {expect_service} ARN, got service {service!r}",
            )
        )

    if region:
        if not _REGION_RE.match(region):
            errors.append(PayloadError(path, "arn_bad_region", f"region {region!r} is not region-shaped"))
        elif region_policy == "empty":
            errors.append(
                PayloadError(
                    path,
                    "arn_region_must_be_empty",
                    f"{expect_service or 'this'} is a global service; the region segment must be empty",
                )
            )
        else:
            if partition in ALLOWED_PARTITIONS and _partition_for_region(region) != partition:
                errors.append(
                    PayloadError(
                        path,
                        "arn_partition_region_mismatch",
                        f"region {region!r} does not belong to partition {partition!r}",
                    )
                )
            if expect_region is not None and region != expect_region:
                errors.append(
                    PayloadError(
                        path,
                        "arn_wrong_region",
                        f"must be in the deployment's target region {expect_region!r}, got {region!r}",
                    )
                )
    elif region_policy == "required":
        errors.append(PayloadError(path, "arn_missing_region", "the region segment is required for this ARN"))

    if account:
        if not _ACCOUNT_RE.match(account):
            errors.append(PayloadError(path, "arn_bad_account", "account segment must be exactly 12 digits"))
        else:
            # An explicit, resolved allowlist -- never "any account", and never an account taken
            # from the request. ``allowed_accounts`` exists for exactly one case: a SOURCE
            # credential the platform is about to copy, which ``_source_client`` accepts from the
            # platform's own account OR the selected target and refuses from anywhere else
            # (deployment_handler.py:545-551). cnt_QAWqFk4LdKNGAO treats a documented
            # cross-account flow as something to threat-model and narrow, not to wave through,
            # so this is a two-member allowlist and every other segment is still validated.
            permitted = (
                tuple(a for a in allowed_accounts if a)
                if allowed_accounts
                else ((expect_account,) if expect_account is not None else ())
            )
            if permitted and account not in permitted:
                errors.append(
                    PayloadError(
                        path,
                        "arn_wrong_account",
                        "must name the deployment's target account; a role or secret in another "
                        "account would be assumed or read outside the deployment's authorization",
                    )
                )
    elif account_policy == "required":
        errors.append(PayloadError(path, "arn_missing_account", "the account segment is required for this ARN"))

    if not relative:
        errors.append(PayloadError(path, "arn_empty_relative_id", "the resource segment is empty"))
        return
    if set(relative) & _ARN_UNSAFE_CHARS:
        bad = "".join(sorted(set(relative) & _ARN_UNSAFE_CHARS))
        errors.append(PayloadError(path, "arn_unsafe_chars", f"resource segment contains unsafe characters: {bad!r}"))
    if expect_resource_prefix is not None and not relative.startswith(expect_resource_prefix):
        errors.append(
            PayloadError(
                path,
                "arn_wrong_resource_type",
                f"resource segment must start with {expect_resource_prefix!r}, got {relative[:32]!r}",
            )
        )


class _Budget:
    """Traversal budget. Exhausting it is a rejection, so the scan can never silently skip."""

    def __init__(self) -> None:
        self.nodes = 0
        self.exceeded = False

    def spend(self, errors: list[PayloadError], path: str) -> bool:
        self.nodes += 1
        if self.nodes > _MAX_NODES:
            if not self.exceeded:
                self.exceeded = True
                errors.append(
                    PayloadError(
                        "$",
                        "payload_too_many_nodes",
                        f"the payload exceeds {_MAX_NODES} values; it cannot be fully validated, "
                        "so it is refused rather than partially inspected",
                    )
                )
            return False
        return True


def _scan_for_raw_secrets(
    errors: list[PayloadError],
    node: object,
    phase: PayloadPhase,
    budget: _Budget,
    path: str = "$",
    norm: str = "$",
    depth: int = 0,
    in_header_map: bool = False,
) -> None:
    """Recursively assert no raw secret material is anywhere it is not explicitly approved.

    ``path`` is the concrete path used in error messages (with real indices); ``norm`` is the
    same path with every index rendered ``[*]``, which is what is matched against
    ``_RAW_SECRET_WRITE_ONLY_PATHS``. Two paths rather than one so an error message can name
    ``$.connectors[3]`` while the allowlist stays index-independent.

    Exceeding the depth or breadth budget is reported as an error and stops that subtree. It is
    NOT a silent return: a subtree that was not inspected has not been cleared, and the first
    version of this function silently skipped everything below depth 8, which meant a raw secret
    nested one level deeper bypassed the control entirely.
    """
    if not budget.spend(errors, path):
        return
    if depth > _MAX_DEPTH:
        if not budget.exceeded:
            budget.exceeded = True
            errors.append(
                PayloadError(
                    path,
                    "payload_too_deep",
                    f"nesting exceeds {_MAX_DEPTH} levels; the payload cannot be fully validated, "
                    "so it is refused rather than partially inspected",
                )
            )
        return

    if isinstance(node, Mapping):
        if len(node) > _MAX_MAPPING_KEYS:
            errors.append(
                PayloadError(
                    path,
                    "object_too_many_keys",
                    f"an object with more than {_MAX_MAPPING_KEYS} keys cannot be validated",
                )
            )
            return
        for key, value in node.items():
            if not isinstance(key, str):
                errors.append(PayloadError(path, "non_string_key", "object keys must be strings"))
                continue
            child = f"{path}.{key}"
            child_norm = f"{norm}.{key}"
            canon = _canon_key(key)
            # The broad rule is classified on the RAW key, not on ``canon``: canonicalization
            # deletes the word boundaries the classifier needs. The exact-token rule keeps using
            # ``canon``, which is what makes it spelling-insensitive.
            is_credential = canon in _FORBIDDEN_SECRET_TOKENS or (in_header_map and _header_name_is_credential(key))
            if is_credential and value not in (None, ""):
                approved = phase is PayloadPhase.REQUEST and child_norm in _RAW_SECRET_WRITE_ONLY_PATHS
                if not approved:
                    # The offending VALUE is never included in the message. The message travels
                    # into the deployment record and the logs, which is the exact place a secret
                    # must not be echoed to.
                    errors.append(
                        PayloadError(
                            child,
                            "raw_secret_in_payload",
                            "raw secret material is not accepted at this path; supply a Secrets "
                            "Manager reference instead -- 'authHeaderSecretArn' for an OTLP auth "
                            "header, or a staged 'secret_arn' for a connector. A value here "
                            "enters the execution input, which is retained and readable for 90 "
                            "days, and an auth header additionally becomes a runtime environment "
                            "variable that GetAgentRuntime returns in plaintext",
                        )
                    )
                elif not isinstance(value, str):
                    # An approved write-only path still may not carry a structure: the staging
                    # code passes this value straight to Secrets Manager as a string.
                    errors.append(
                        PayloadError(
                            child,
                            "raw_secret_not_a_string",
                            f"must be a string when supplied, got {type(value).__name__}",
                        )
                    )
            # Descending INTO an extraHeaders map turns on broad matching for the keys one
            # level down, and the flag stays on beneath that so a nested container cannot be
            # used to smuggle a header past it. It is never turned back OFF on the way down:
            # anything under a header map is header material.
            _scan_for_raw_secrets(
                errors,
                value,
                phase,
                budget,
                child,
                child_norm,
                depth + 1,
                in_header_map or canon == _HEADER_MAP_TOKEN,
            )
    elif isinstance(node, (list, tuple)):
        if len(node) > _MAX_SEQUENCE_ITEMS:
            errors.append(
                PayloadError(
                    path,
                    "sequence_too_long",
                    f"an array longer than {_MAX_SEQUENCE_ITEMS} items cannot be validated",
                )
            )
            return
        for i, item in enumerate(node):
            _scan_for_raw_secrets(
                errors,
                item,
                phase,
                budget,
                f"{path}[{i}]",
                f"{norm}[*]",
                depth + 1,
                in_header_map,
            )


#: The hard limit on a Step Functions execution input, from the botocore service model:
#: ``StartExecution.input.metadata == {'max': 262144, 'sensitive': True}``. Remembered numbers
#: drift; this one was read from the model on this machine and is pinned by a test that reads it
#: the same way.
_SFN_INPUT_MAX_BYTES = 262_144

#: How much of that budget the REQUEST phase must leave unused. The request payload is NOT the
#: execution input: the server adds the deployment id, the version id, both runtime names, the
#: owner sub, the resolved target fields and ``recorded_secret_arns`` before StartExecution. So
#: an exact check at the request boundary would admit a payload that only becomes oversized
#: afterwards. Reserving headroom makes the request-phase refusal happen BEFORE the deployment
#: row, the pending version row and any staged secret exist.
_SFN_INPUT_REQUEST_HEADROOM_BYTES = 32_768


def _check_serialized_size(errors: list[PayloadError], payload: Mapping, phase: PayloadPhase) -> None:
    """Refuse a payload that cannot be passed to StartExecution.

    The size is measured with the SAME encoding the caller uses -- ``json.dumps(..., default=str)``
    encoded to UTF-8 -- because the limit is in BYTES and a character count is not a byte count
    for any non-ASCII payload. ``default=str`` also matches the caller, so a value the caller
    would serialize as a string is measured as one here rather than raising.
    """
    limit = (
        _SFN_INPUT_MAX_BYTES
        if phase is PayloadPhase.PREPARED
        else _SFN_INPUT_MAX_BYTES - _SFN_INPUT_REQUEST_HEADROOM_BYTES
    )
    try:
        size = len(json.dumps(payload, default=str).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        # Unserializable means StartExecution could never have carried it either. Reported as a
        # refusal rather than raised, so the caller gets a field error like every other rule.
        errors.append(
            PayloadError(
                "$",
                "payload_not_serializable",
                "the payload cannot be serialized to JSON, so it cannot be passed to the deployment state machine",
            )
        )
        return
    if size > limit:
        errors.append(
            PayloadError(
                "$",
                "payload_too_large",
                f"the payload serializes to {size} bytes, over the {limit}-byte budget for this "
                f"phase (StartExecution accepts at most {_SFN_INPUT_MAX_BYTES}). Reduce the "
                "number of tools, connectors or custom tool definitions",
            )
        )


#: Secret REFERENCES a later step dereferences, enumerated from the real consumers rather than
#: guessed: gateway_step.py:383/400/413/430/447, knowledge_base_step.py:379-383/459,
#: runtime_deployer.py:405, observability.py:274. ``identity_config.client_secret_ref`` is
#: deliberately absent -- it is a source reference on a different path, not a value staged at
#: this boundary, and including it would apply a rule its own flow does not satisfy.
_PROVIDER_REF_KEYS = ("provider_api_key_ref", "providerApiKeyRef")
_OTEL_REF_KEYS = ("auth_header_secret_arn", "authHeaderSecretArn")
_LITELLM_REF_KEYS = ("litellm_api_key_ref", "litellmApiKeyRef")
_KB_REF_KEYS = (
    "confluenceCredentialsSecretArn",
    "salesforceCredentialsSecretArn",
    "sharePointCredentialsSecretArn",
    "rdsCredentialsSecretArn",
)
_CONNECTOR_REF_KEYS = ("secret_arn", "secretArn")
_OAUTH_REF_KEYS = (
    "client_secret_arn",
    "clientSecretArn",
    "client_secret_ref",
    "clientSecretRef",
)


#: A reference the API stages through ``stage_runtime_secret_for_deployment`` or
#: ``stage_customer_secret_for_deployment``. Both call ``secrets_manager_arn_location``, whose
#: anchored ``fullmatch`` raises on anything that is not a COMPLETE Secrets Manager ARN
#: (gateway_deployer.py:727-737), and both return ``_put_connector_secret``'s ``resp["ARN"]``.
#: So a complete ARN is required in BOTH directions here: a bare name cannot be producer output,
#: and cannot be valid producer input either.
_KIND_STAGED_STRICT = "staged_strict"

#: A reference the API stages through ``bind_connector_secret_for_deployment``. That function
#: DOES accept a bare platform-managed name -- ``_is_platform_connector_secret`` falls back to
#: the whole string when there is no ``:secret:`` (gateway_deployer.py:881-891) -- and on the
#: exact-current-deployment branch it returns the caller's reference VERBATIM
#: (gateway_deployer.py:1043-1044), so a bare name is legitimate producer output. Requiring an
#: ARN shape here would refuse the documented idempotent-retry payload. Membership still applies:
#: ``_bind`` records whatever this returns, whatever its shape (deployment_handler.py:406-407).
_KIND_STAGED_BINDABLE = "staged_bindable"

#: A reference a higher-precedence source has OVERRIDDEN, so the API never stages it and it
#: reaches the state machine unchanged. It is a reference rather than a value, so leaving it in
#: history is consistent with cnt_dwzZ05hLnqhYXQ; it must not be held to the staged invariants.
_KIND_OVERRIDDEN = "overridden"

#: Staged by a LATER step rather than the API boundary (``mcp_server_step`` mints its own client
#: secret and appends it at mcp_server_step.py:524-526), so membership cannot be required here
#: without rejecting the normal path.
_KIND_LATE = "late"

_STAGED_KINDS = frozenset({_KIND_STAGED_STRICT, _KIND_STAGED_BINDABLE})

#: Kinds for which a bare secret NAME is not a supported shape, at EITHER phase.
#:
#: Beyond the strict staging paths this covers an OVERRIDDEN OTEL reference too, because
#: ``_validate_user_otel_secret_arn`` is applied to a per-canvas reference even on the branch
#: where platform defaults win (observability.py:216-220) and its regex is anchored on a complete
#: ``agentcore-otel/`` ARN. So a bare name there is refused downstream in every branch; refusing
#: it here only moves the same refusal to the boundary.
#:
#: A BINDABLE kind is absent because a bare platform-managed name is a documented REQUEST input
#: for it -- but see ``_arn_required`` below: it is still required in the PREPARED phase, where
#: it is no longer caller input but producer output.
_ARN_REQUIRED_KINDS = frozenset({_KIND_STAGED_STRICT, _KIND_OVERRIDDEN})


def _arn_required(kind: str, phase: PayloadPhase) -> bool:
    """Whether a bare secret NAME is refused for this kind at this phase.

    The asymmetry is the whole point, and it is a property of the PRODUCER, not a tolerance:

    * at REQUEST a bindable reference is still the caller's own, and
      ``bind_connector_secret_for_deployment`` accepts a bare platform-managed name by design.
    * at PREPARED it is no longer caller input. Every branch of that function now returns a
      canonical ARN -- ``_put_connector_secret`` returns ``resp["ARN"]``, and the
      exact-current-deployment branch resolves ``described["ARN"]`` rather than echoing the
      caller's reference (gateway_deployer.py:1043-1064). So a bare name in a prepared payload
      did not come from the producer.

    Requiring it at PREPARED is also what makes the membership rule SATISFIABLE rather than
    contradictory: ``recorded_secret_arns`` entries are themselves validated as ARNs, so a bare
    reference could never legitimately appear in that list. Demanding membership of a shape the
    list cannot hold would have refused every payload carrying one.
    """
    return kind in _ARN_REQUIRED_KINDS or (phase is PayloadPhase.PREPARED and kind == _KIND_STAGED_BINDABLE)


def _otel_ref_kinds(payload: Mapping) -> dict[str, str]:
    """Which OTEL auth reference the API actually stages, mirroring the producer exactly.

    ``_prepare_runtime_credentials`` (deployment_handler.py:597-627) stages exactly ONE OTEL auth
    reference, chosen by a precedence this must reproduce or it will refuse a legitimate payload:

    * a platform default carrying ``auth_header_secret_arn`` wins outright, and the ``elif`` at
      deployment_handler.py:615 means the canvas reference is then NOT staged and travels
      unchanged. ``build_otel_env_vars`` drops it too (observability.py:211-225), but deliberately
      still validates it, so the producer must not strip it either.
    * otherwise the effective block is the top-level ``observability_config`` when present, else
      the nested ``config.observability`` -- the same ``is not None`` precedence the consumer
      applies at runtime_configure_step.py:289-290.

    This stays PURE: the frozen platform defaults travel in the payload as their own field, so the
    decision is readable from the payload without ever reaching for SSM.
    """
    platform_paths = ("platform_observability_defaults", "platformObservabilityDefaults")
    canvas_top = ("observability_config", "observabilityConfig")
    config_block = payload.get("config")
    nested = config_block.get("observability") if isinstance(config_block, Mapping) else None

    kinds = {f"$.{key}": _KIND_OVERRIDDEN for key in (*platform_paths, *canvas_top)}
    kinds["$.config.observability"] = _KIND_OVERRIDDEN

    for key in platform_paths:
        block = payload.get(key)
        if isinstance(block, Mapping) and any(block.get(k) for k in _OTEL_REF_KEYS):
            kinds[f"$.{key}"] = _KIND_STAGED_STRICT
            return kinds

    for key in canvas_top:
        if isinstance(payload.get(key), Mapping):
            kinds[f"$.{key}"] = _KIND_STAGED_STRICT
            return kinds
    if isinstance(nested, Mapping):
        kinds["$.config.observability"] = _KIND_STAGED_STRICT
    return kinds


#: The namespace prefix a SOURCE reference must sit under, per path. This is not a convention the
#: validator invents: ``stage_runtime_secret_for_deployment`` refuses a source whose name does not
#: start with ``f"{namespace}/"`` (gateway_deployer.py:771) and its two callers pass
#: ``agentcore-provider`` and ``agentcore-otel`` (deployment_handler.py:558, :611, :622);
#: ``bind_connector_secret_for_deployment`` refuses anything outside ``agentcore-connector/``
#: (``_is_platform_connector_secret``, :890); and an overridden canvas OTEL reference is held to
#: ``secret:agentcore-otel/`` by ``_validate_user_otel_secret_arn`` (observability.py:35-49).
#:
#: Checking only ``secret:`` left the namespace portion of the relative id unvalidated, which
#: ARCC cnt_QAWqFk4LdKNGAO names directly: a service accepting a resource ARN must validate every
#: segment including the relative id, and its exit criteria require verifying that the relative id
#: "contains only expected values". The practical cost of not doing it is not a bypass but a LATE
#: failure: the reference is admitted, the deployment row and the pending version row are written,
#: and staging then refuses -- a persistent half-written deploy instead of a clean 4xx.
#:
#: The trailing slash is load-bearing and matches the producer's own ``f"{namespace}/"``: without
#: it, ``agentcore-provider-evil/x`` would pass a prefix test for ``agentcore-provider``.
_NS_PROVIDER = "agentcore-provider/"
_NS_OTEL = "agentcore-otel/"
_NS_CONNECTOR = "agentcore-connector/"

#: Knowledge Base sources are deliberately UNCONSTRAINED. They are the customer's own secrets,
#: gated by an explicit opt-in tag rather than by a platform namespace
#: (``_prepare_knowledge_base_credentials``' docstring: "active customer secrets must explicitly
#: opt in"). Inventing a namespace here would refuse every real Confluence/Salesforce/RDS
#: credential, so the rule stays "a complete ARN" and stops there.
_NS_UNCONSTRAINED: str | None = None


def _required_namespace(kind: str, source_ns: str | None, phase: PayloadPhase) -> str | None:
    """The namespace this reference must sit under AT THIS PHASE.

    Phase-dependent, because a staged reference is not the same secret in both phases. At REQUEST
    the value is still the caller's SOURCE credential, so it must satisfy the namespace its
    staging call enforces. At PREPARED it has been replaced by the deployment-bound COPY, and
    every copy -- provider, OTEL, KB and connector alike -- is minted by ``_put_connector_secret``
    as ``agentcore-connector/{safe_owner}/{uuid}`` (gateway_deployer.py:690, :700, and the
    ``return _put_connector_secret(...)`` that ends ``stage_runtime_secret_for_deployment``).
    Holding a prepared provider reference to ``agentcore-provider/`` would therefore refuse every
    successfully staged deployment, and holding the KB copy to no namespace at all would miss the
    one phase where it does have one.
    """
    if phase is PayloadPhase.PREPARED and kind in _STAGED_KINDS:
        return _NS_CONNECTOR
    return source_ns


def _secret_name_of(ref: str) -> str:
    """The Secrets Manager NAME a reference denotes, from an ARN or from a bare name.

    A secret ARN's relative id is ``secret:<name>-<6 random chars>``, and the name itself may
    contain ``/`` and ``-``. Splitting on ``:secret:`` rather than on the last ``:`` is what keeps
    a name containing a colon from truncating the namespace we are about to test.
    """
    marker = ":secret:"
    index = ref.find(marker)
    return ref[index + len(marker) :] if index >= 0 else ref


def _check_secret_namespace(errors: list[PayloadError], path: str, ref: str, required: str | None) -> None:
    """Refuse a secret reference outside the namespace its own producer enforces.

    ``required`` already carries the trailing slash, so ``agentcore-provider-evil/x`` fails: an
    adjacent prefix is a different namespace, and without the slash it would be admitted as this
    one. ``None`` means deliberately unconstrained -- see ``_NS_UNCONSTRAINED``.

    The NAME is echoed, truncated. That is safe and deliberate: a Secrets Manager name is not
    secret material -- it is already in the ARN the caller sent and in every CloudTrail event
    about it -- and without it the operator cannot tell which of several references was refused.
    The VALUE is never read here at all.
    """
    if required is None:
        return
    name = _secret_name_of(ref)
    if name.startswith(required):
        return
    errors.append(
        PayloadError(
            path,
            "secret_ref_wrong_namespace",
            f"must name a secret under {required!r}; the platform only stages, binds and deletes "
            f"secrets in its own namespaces, so a reference outside one cannot be resolved or "
            f"authorized. Got {name[:48]!r}",
        )
    )


def _collect_secret_refs(payload: Mapping) -> list[tuple[str, object, str, str | None]]:
    """Every (path, value, kind, source_namespace) tuple the payload carries as a secret handle.

    ``kind`` decides the rule, because the kinds have genuinely different invariants -- see the
    four ``_KIND_*`` constants above. The common thread for a staged kind: the API stages a
    deployment-bound COPY and records it before it builds ``sfn_input``.
    ``_prepare_deployment_credentials._bind`` fires on a bare reference, not only on a raw value
    (deployment_handler.py:429, :442, :459, :473), and appends to ``staged_arns``. Since
    ``deployment_handler`` holds the only ``start_execution`` call site, membership in
    ``recorded_secret_arns`` is an INVARIANT of the prepared payload, not a coincidence.
    ``gateway_step._bind_and_record`` tolerating a non-member is a legacy/direct-caller fallback
    -- its own comment says so -- and a defensive fallback downstream must not be read as
    permission to weaken the closed API contract.
    """
    found: list[tuple[str, object, str, str | None]] = []

    def take(path: str, container: object, keys: Sequence[str], kind: str, source_ns: str | None) -> None:
        if not isinstance(container, Mapping):
            return
        for key in keys:
            value = container.get(key)
            if value not in (None, ""):
                found.append((f"{path}.{key}", value, kind, source_ns))

    take("$.config", payload.get("config"), _PROVIDER_REF_KEYS, _KIND_STAGED_STRICT, _NS_PROVIDER)
    # The OTEL reference can arrive at three different paths and only ONE of them is the one the
    # API stages. A collector that treated all three as staged would refuse the entirely normal
    # platform-defaults deployment, because the overridden canvas reference is left in place by
    # design. A collector that looked only at the top level would leave the nested spelling
    # unvalidated. So each path gets the kind its own precedence earns it.
    otel_kinds = _otel_ref_kinds(payload)
    config_block = payload.get("config")
    if isinstance(config_block, Mapping):
        take(
            "$.config.observability",
            config_block.get("observability"),
            _OTEL_REF_KEYS,
            otel_kinds["$.config.observability"],
            _NS_OTEL,
        )
    for container_key in (
        "observability_config",
        "observabilityConfig",
        "platform_observability_defaults",
        "platformObservabilityDefaults",
    ):
        path = f"$.{container_key}"
        take(path, payload.get(container_key), _OTEL_REF_KEYS, otel_kinds[path], _NS_OTEL)
    for container_key in ("gateway_config", "gatewayConfig"):
        take(
            f"$.{container_key}",
            payload.get(container_key),
            _LITELLM_REF_KEYS,
            _KIND_STAGED_BINDABLE,
            _NS_CONNECTOR,
        )
    for container_key in ("knowledge_base_config", "knowledgeBaseConfig"):
        take(
            f"$.{container_key}",
            payload.get(container_key),
            _KB_REF_KEYS,
            _KIND_STAGED_STRICT,
            _NS_UNCONSTRAINED,
        )
    for container_key in ("mcp_server_config", "mcpServerConfig"):
        config = payload.get(container_key)
        take(f"$.{container_key}", config, _OAUTH_REF_KEYS, _KIND_LATE, _NS_CONNECTOR)
        if isinstance(config, Mapping):
            take(
                f"$.{container_key}.oauth",
                config.get("oauth"),
                _OAUTH_REF_KEYS,
                _KIND_LATE,
                _NS_CONNECTOR,
            )
    for container_key in ("connectors", "external_mcp_servers", "externalMcpServers"):
        items = payload.get(container_key)
        if not _is_sequence(items):
            continue
        for index, item in enumerate(items):
            item_path = f"$.{container_key}[{index}]"
            take(item_path, item, _CONNECTOR_REF_KEYS, _KIND_STAGED_BINDABLE, _NS_CONNECTOR)
            if isinstance(item, Mapping):
                take(
                    f"{item_path}.oauth",
                    item.get("oauth"),
                    _OAUTH_REF_KEYS,
                    _KIND_STAGED_BINDABLE,
                    _NS_CONNECTOR,
                )
    return found


def _first_present(payload: Mapping, keys: Sequence[str]) -> tuple[str, object] | None:
    """The first spelling of a logical field that is present, with its value."""
    for key in keys:
        if key in payload:
            return key, payload[key]
    return None


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, Mapping))


def validate_deployment_payload(
    payload: object,
    phase: PayloadPhase = PayloadPhase.PREPARED,
    context: ValidationContext | None = None,
) -> PayloadValidation:
    """Validate a deployment payload. Pure: no IO, no environment, no clients.

    Called at the API boundary with ``PayloadPhase.REQUEST`` BEFORE any side effect (before the
    state row, the pending version row, and credential staging), and again by ValidateStep with
    ``PayloadPhase.PREPARED`` on the payload the state machine actually received. Same function
    both times, so the two cannot drift -- and the second call is not redundant:
    cnt_jljdNeOwgPnFx2 requires the downstream step to authorize independently rather than trust
    that an upstream caller checked.
    """
    errors: list[PayloadError] = []

    if not isinstance(payload, Mapping):
        return PayloadValidation(
            (PayloadError("$", "payload_not_an_object", "the deployment payload must be an object"),)
        )

    # --- identity ---------------------------------------------------------------------
    # PREPARED requires the server-authored ids. REQUEST has not been assigned them yet, so it
    # checks the client's own id under either spelling, when present.
    if phase is PayloadPhase.PREPARED:
        identity_keys: tuple[tuple[str, ...], ...] = (("deployment_id",), ("node_id",))
        flow_keys: tuple[str, ...] = ("workflow_id",)
    else:
        identity_keys = (("node_id", "nodeId"),)
        flow_keys = ("flow_id", "flowId")
    for keys in identity_keys:
        found = _first_present(payload, keys)
        if found is None:
            if phase is PayloadPhase.PREPARED:
                errors.append(
                    PayloadError(f"$.{keys[0]}", "missing_identifier", "is required and must be a non-empty string")
                )
            continue
        key, value = found
        if not isinstance(value, str) or not value.strip():
            errors.append(PayloadError(f"$.{key}", "missing_identifier", "must be a non-empty string"))
        elif not _IDENTIFIER_RE.match(value):
            errors.append(
                PayloadError(
                    f"$.{key}",
                    "unsafe_identifier",
                    "must start alphanumeric and contain only letters, digits, '.', '_' or '-' "
                    "(it is used in S3 keys and AWS resource names)",
                )
            )

    # The FLOW id is optional in both phases, on purpose: a harness deploy and an unsaved canvas
    # belong to no flow, and absence (or an explicit null, which is what the SFN input builder
    # writes) must stay representable rather than be filled with the node id. When present it
    # must be a flow id -- the same grammar ``routers/flows.py::_validate_flow_id`` enforces.
    found_flow = _first_present(payload, flow_keys)
    if found_flow is not None and found_flow[1] is not None:
        key, value = found_flow
        if not isinstance(value, str) or not _FLOW_ID_RE.match(value):
            errors.append(
                PayloadError(
                    f"$.{key}",
                    "unsafe_identifier",
                    "must be a flow id: 1-128 letters, digits, '_' or '-'",
                )
            )

    # --- the rest of the server-authored set, and the closed key list ------------------
    if phase is PayloadPhase.PREPARED:
        for key in _PREPARED_REQUIRED_FIELDS:
            if key in ("deployment_id", "node_id"):
                continue  # already checked above, with the identifier charset
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(
                    PayloadError(
                        f"$.{key}",
                        "missing_server_field",
                        "is written by the deployment API on every request; its absence means "
                        "this payload was not produced by the deployment API",
                    )
                )
        for key in sorted(k for k in payload if k in _VALIDATOR_OUTPUT_FIELDS):
            errors.append(
                PayloadError(
                    f"$.{key}",
                    "validator_output_as_input",
                    "is this step's own OUTPUT, not its input; the deployment API never emits "
                    "it, so its presence means the execution input was forged or replayed",
                )
            )
        for key in sorted(
            k for k in payload if k not in _PREPARED_TOP_LEVEL_FIELDS and k not in _VALIDATOR_OUTPUT_FIELDS
        ):
            errors.append(
                PayloadError(
                    f"$.{key}",
                    "unknown_field",
                    "is not a field the deployment API emits; an unrecognized top-level field "
                    "is either injected or an un-reviewed producer change",
                )
            )

    # --- enums ------------------------------------------------------------------------
    mode_found = _first_present(payload, ("deployment_mode", "deploymentMode"))
    mode = mode_found[1] if mode_found else None
    if mode is not None and mode not in ALLOWED_DEPLOYMENT_MODES:
        errors.append(
            PayloadError(
                "$.deployment_mode",
                "unknown_deployment_mode",
                f"must be one of {sorted(ALLOWED_DEPLOYMENT_MODES)}",
            )
        )
    slot_found = _first_present(payload, ("deployment_slot", "deploymentSlot"))
    slot = slot_found[1] if slot_found else None
    if slot is not None and slot not in ALLOWED_DEPLOYMENT_SLOTS:
        errors.append(
            PayloadError(
                "$.deployment_slot",
                "unknown_deployment_slot",
                f"must be one of {sorted(ALLOWED_DEPLOYMENT_SLOTS)}",
            )
        )

    # --- shapes -----------------------------------------------------------------------
    # `config` is required in both phases; the rest are optional but must be well-shaped.
    if payload.get("config") is None:
        errors.append(PayloadError("$.config", "missing_config", "the runtime configuration is required"))
    for keys in _OBJECT_FIELD_KEYS:
        found = _first_present(payload, keys)
        if found is None:
            continue
        key, value = found
        if value is not None and not isinstance(value, Mapping):
            errors.append(
                PayloadError(
                    f"$.{key}",
                    "not_an_object",
                    f"must be an object when present, got {type(value).__name__}. The state "
                    "machine branches on presence, so a scalar here starts the step and then "
                    "fails inside it",
                )
            )
    for keys, item_kind, max_items in _SEQUENCE_FIELD_KEYS:
        found = _first_present(payload, keys)
        if found is None:
            continue
        key, value = found
        if value is None:
            continue
        if not _is_sequence(value):
            errors.append(
                PayloadError(f"$.{key}", "not_an_array", f"must be an array when present, got {type(value).__name__}")
            )
            continue
        if len(value) > max_items:
            errors.append(
                PayloadError(
                    f"$.{key}",
                    "sequence_too_long",
                    f"must hold at most {max_items} items, got {len(value)}",
                )
            )
            continue
        for i, item in enumerate(value):
            if item_kind == "object" and not isinstance(item, Mapping):
                errors.append(
                    PayloadError(
                        f"$.{key}[{i}]",
                        "item_not_an_object",
                        f"every item must be an object, got {type(item).__name__}",
                    )
                )
            elif item_kind == "string" and not (isinstance(item, str) and item.strip()):
                errors.append(
                    PayloadError(
                        f"$.{key}[{i}]",
                        "item_not_a_string",
                        "every item must be a non-empty string",
                    )
                )

    # --- the model the runtime path cannot start without -------------------------------
    # Harness mode is an EXPLICIT branch, not a fallthrough: the harness path runs no codegen
    # and no runtime configure/launch, so it legitimately carries no model id, and treating
    # its absence as valid-by-default is how a missing-field bypass gets built.
    #
    # The standalone FastMCP runtime (templateId 'mcp-server-runtime') is the second explicit
    # model-free branch: it emits a protocol tool server with no model loop, and the deploy
    # handler strips every model-only field from its Step Functions input, so requiring a model
    # here would reject the very contract this validator is meant to admit. Also an EXPLICIT
    # branch, not a fallthrough, and gated on the same template id the API-boundary validator
    # (DeployRequest._mcp_protocol_admission) keys the model-free contract on.
    config = payload.get("config")
    # Accept EITHER alias spelling: the REQUEST phase validates the raw body (camelCase
    # 'templateId'), the PREPARED phase validates the SFN input (snake 'template_id').
    _template_id_found = _first_present(payload, ("template_id", "templateId"))
    _is_model_free_mcp = bool(_template_id_found) and _template_id_found[1] == "mcp-server-runtime"
    if isinstance(config, Mapping) and mode != "harness" and not _is_model_free_mcp:
        model = config.get("model")
        if model is None:
            errors.append(PayloadError("$.config.model", "missing_model", "the runtime path requires a model"))
        elif not isinstance(model, Mapping):
            errors.append(PayloadError("$.config.model", "not_an_object", "must be an object"))
        else:
            model_id = model.get("modelId") or model.get("model_id")
            if not isinstance(model_id, str) or not model_id.strip():
                errors.append(
                    PayloadError("$.config.model.modelId", "missing_model_id", "a non-empty model id is required")
                )

    # --- cross-account / cross-region targeting ---------------------------------------
    # Resolved first, because every ARN below is checked FOR COHERENCE WITH THEM rather than
    # merely for syntax (cnt_QAWqFk4LdKNGAO).
    account_id = payload.get("target_account_id")
    resolved_account: str | None = None
    if account_id is not None:
        if isinstance(account_id, str) and _ACCOUNT_RE.match(account_id):
            resolved_account = account_id
        else:
            errors.append(PayloadError("$.target_account_id", "bad_account_id", "must be exactly 12 digits"))
    target_region = payload.get("target_region")
    resolved_region: str | None = None
    if target_region is not None:
        if isinstance(target_region, str) and _REGION_RE.match(target_region):
            resolved_region = target_region
        else:
            errors.append(PayloadError("$.target_region", "bad_region", "must be an AWS region such as 'eu-central-1'"))

    # The account and region a resource must live in, taken from the resolved target when the
    # deployment names one and otherwise from the TRUSTED context -- never left unconstrained.
    # For a home deployment the payload's target fields are legitimately null, so without the
    # context fallback a secret ARN naming any account at all would satisfy the check below.
    ctx = context or ValidationContext()
    expected_account = resolved_account or ctx.home_account_id
    expected_region = resolved_region or ctx.home_region

    # The three target role ARNs are IAM roles in the TARGET account: they are assumed to
    # create every resource. A wrong service, a wrong account or a non-empty region here is an
    # authorization defect, not a typo.
    for key in ("target_role_arn", "target_runtime_role_arn", "target_mcp_runtime_role_arn", "target_harness_role_arn"):
        value = payload.get(key)
        if value is not None:
            _check_arn(
                errors,
                f"$.{key}",
                value,
                expect_service="iam",
                expect_resource_prefix="role/",
                region_policy="empty",
                account_policy="required",
                expect_account=expected_account,
            )

    # Staged secrets are created BY this deployment, in the target account and region
    # (deployment_handler.py:391-422 binds them through a session for the target event), so
    # anything else in this list is a reference the deployment did not create and must not read.
    recorded = payload.get("recorded_secret_arns")
    if _is_sequence(recorded):
        for i, arn in enumerate(recorded):
            _check_arn(
                errors,
                f"$.recorded_secret_arns[{i}]",
                arn,
                expect_service="secretsmanager",
                expect_resource_prefix="secret:",
                region_policy="required",
                expect_region=expected_region,
                account_policy="required",
                expect_account=expected_account,
            )
            # Every entry is a deployment-bound COPY minted by ``_put_connector_secret``, whatever
            # namespace its source sat in, so the whole list is ``agentcore-connector/``. This is
            # also what makes the membership check meaningful: without it a recorded entry could
            # name a secret the platform's own delete path refuses to touch
            # (``delete_deployment_bound_secret`` -> ``_is_platform_connector_secret``) -- an entry
            # that authorizes a reference but can never be cleaned up.
            if isinstance(arn, str):
                _check_secret_namespace(errors, f"$.recorded_secret_arns[{i}]", arn, _NS_CONNECTOR)

    # --- the secret REFERENCES a downstream step will actually read ---------------------
    # Validating only ``recorded_secret_arns`` validated the coordination list and none of the
    # references the consumers dereference, so a foreign-account ARN rode through on every path
    # below while an empty list looked clean. cnt_QAWqFk4LdKNGAO: an unvalidated account segment
    # "can lead to bypass of a deny policy or cross account access", and these values reach IAM
    # policy Resource fields (runtime_deployer.py:405) and DescribeSecret calls.
    #
    # The rule is PHASE- and PATH-specific, because a source credential and a staged copy have
    # different legitimate homes:
    #
    #   REQUEST  -- the value is still the caller's SOURCE secret. ``_source_client`` accepts it
    #               from the platform's own account OR the selected target and refuses every
    #               other account (deployment_handler.py:545-551, :720-735), and it is read in
    #               the source ARN's own region. So the account rule here is a two-member
    #               allowlist and the region is not pinned. A uniform target-only rule would
    #               reject the documented cross-account staging path before it could run.
    #   PREPARED -- the value is the deployment-bound COPY the API just made, so it must be in
    #               the target account and region AND be a member of ``recorded_secret_arns``.
    #               Not requiring membership left a same-account reference owned by ANOTHER
    #               tenant acceptable, which is under-authorization, not leniency.
    staged_arns = {arn for arn in (recorded or ()) if isinstance(arn, str)} if _is_sequence(recorded) else set()
    for ref_path, ref_value, kind, source_ns in _collect_secret_refs(payload):
        if not isinstance(ref_value, str):
            errors.append(
                PayloadError(
                    ref_path,
                    "secret_ref_not_a_string",
                    f"a secret reference must be a string, got {type(ref_value).__name__}",
                )
            )
            continue
        enforce_staged = phase is PayloadPhase.PREPARED and kind in _STAGED_KINDS

        if not ref_value.startswith("arn:"):
            # A bare secret NAME is a legitimate form ONLY for a bindable REQUEST reference:
            # ``_is_platform_connector_secret`` accepts one (gateway_deployer.py:889) and
            # ``DescribeSecret`` resolves it inside the session's own account and region. A name
            # cannot name a foreign account at all, so the cross-account rule has nothing to
            # check and the namespace gate downstream remains the control.
            #
            # Everywhere else it is illegitimate in both directions. On a STRICT path
            # ``secrets_manager_arn_location``'s anchored fullmatch raises on it
            # (gateway_deployer.py:734-736), so the request would fail mid-staging AFTER the
            # deployment and version rows were written; and in a PREPARED payload no producer
            # branch can emit one at all.
            if _arn_required(kind, phase):
                errors.append(
                    PayloadError(
                        ref_path,
                        "secret_ref_not_an_arn",
                        "must be a complete Secrets Manager ARN; a bare secret name carries "
                        "neither the account nor the region this reference is resolved and "
                        "authorized against",
                    )
                )
            else:
                # A bare name is admitted only on a bindable REQUEST path, and there the namespace
                # is the ONLY thing constraining which secret it can resolve to. Skipping the
                # check here would leave the single legitimate bare-name form as the one shape
                # that escapes namespace validation entirely.
                _check_secret_namespace(errors, ref_path, ref_value, _required_namespace(kind, source_ns, phase))
            # Either way this reference is done. The membership check below is deliberately NOT
            # reached: a bare name can never be a member, because every ``recorded_secret_arns``
            # entry is itself validated as an ARN. Reporting both would be one defect reported
            # twice, the second time against a rule the payload cannot satisfy.
            continue

        # An OVERRIDDEN reference is never dereferenced by anything: a higher-precedence source
        # won, ``build_otel_env_vars`` drops it (observability.py:211-225), and no role is ever
        # granted it. So WHICH account it names cannot matter, and pinning it would refuse a
        # perfectly ordinary payload -- a canvas value left over from before the operator
        # configured platform defaults, pointing wherever it used to point. Its SHAPE is still
        # checked, so a malformed ARN is still caught. Everything else gets a real account rule.
        if kind == _KIND_OVERRIDDEN:
            allowed_accounts: tuple[str | None, ...] = ()
            expect_account_here = None
        elif enforce_staged:
            allowed_accounts = ()
            expect_account_here = expected_account
        else:
            allowed_accounts = (expected_account, ctx.home_account_id)
            expect_account_here = None

        _check_arn(
            errors,
            ref_path,
            ref_value,
            expect_service="secretsmanager",
            expect_resource_prefix="secret:",
            region_policy="required",
            # A source secret is read in its OWN region; only the staged copy is pinned to
            # the deployment's region.
            expect_region=expected_region if enforce_staged else None,
            account_policy="required",
            expect_account=expect_account_here,
            allowed_accounts=allowed_accounts,
        )
        _check_secret_namespace(errors, ref_path, ref_value, _required_namespace(kind, source_ns, phase))
        # Enforced UNCONDITIONALLY, not only when the list is non-empty. Every staged-kind
        # reference is staged the moment it is present -- ``if provider_ref:`` at
        # deployment_handler.py:589 and the same shape at :609/:620/:427 -- so a prepared payload
        # carrying such a reference with no matching recorded entry did not come from the API.
        # Making the check conditional on a non-empty list would let an absent list disable it.
        if enforce_staged and ref_value not in staged_arns:
            errors.append(
                PayloadError(
                    ref_path,
                    "secret_ref_not_staged",
                    "this reference is not one of the deployment's own staged secrets; the API "
                    "stages a deployment-bound copy of every credential it accepts and records "
                    "it, so a reference outside that set is another deployment's or another "
                    "tenant's credential",
                )
            )

    # --- raw secret material is only ever where it is explicitly approved --------------
    _scan_for_raw_secrets(errors, payload, phase, _Budget())

    # --- the payload must fit in what StartExecution will accept ------------------------
    # Checked LAST so a payload that is both oversized and malformed reports both. The bound is
    # the real service limit, read from the botocore service model rather than remembered:
    # StartExecution's ``input`` member carries {'max': 262144, 'sensitive': True}. Without this
    # check an oversized payload is refused by the AWS API only AFTER the deployment row, the
    # pending version row and any staged secrets already exist -- a persistent side effect for a
    # request that could never have run.
    _check_serialized_size(errors, payload, phase)

    return PayloadValidation(tuple(errors))
