"""Gateway guardrails: Amazon Bedrock Guardrails evaluated by Policy in AgentCore at the Gateway.

A policy node can ask for a *Gateway guardrails profile*. The platform then writes, next to the
``allow_permitted_tools`` permit it already manages, one ``forbid … when guardrails {…}`` policy per
permitted tool and guardrail type. Policy in AgentCore calls Bedrock ``InvokeGuardrailChecks`` on the
tool's string arguments at every ``tools/call`` and denies the call when a score crosses the
threshold, so the decision happens before the tool Lambda, connector or MCP server ever runs and
outside the agent's own code. The generated agent's Bedrock Guardrail stays as defense in depth.

Contract (developer guide, "Guardrails in policies" and "Guardrail policy examples", read
2026-11-13):

* A guardrail statement is Dogwood, not plain Cedar, and must be sent as
  ``CreatePolicy.definition.policy.statement`` (``policy_lifecycle.policy_definition`` chooses the
  member from the text). It must name one action (``action == AgentCore::Action::"<Target>___<tool>"``)
  and the gateway ARN (``resource ==``); an unconstrained resource is rejected.
* An authorization effect may read only ``context.input.*``; a field path must resolve to a string
  (checked against the tool schema at create time), so only top-level ``string`` arguments are
  screened. A path that resolves to nothing denies the call for a missing field rather than
  screening it, so a tool is screened only when it has at least one REQUIRED string argument
  (optional string arguments ride along once a required one anchors the call).
* A lone forbid grants nothing: it subtracts from the platform's permit, which is why it is only
  emitted for tools that permit covers. The analyzer reports the pair as "Overly Restrictive", the
  same finding the permit path already skips with ``IGNORE_ALL_FINDINGS``.
* Scores are the discrete values {0, .2, .4, .6, .8, 1}. The documented defaults are used as the
  thresholds: 0.4 for prompt attack, 0.2 for content filters.
* The Gateway execution role needs ``bedrock:InvokeGuardrailChecks``; the API is resourceless
  (Bedrock user guide, "Set up permissions"), so the statement's resource is ``*``.
* Region-gated; see ``region_capabilities.GATEWAY_GUARDRAILS_SUPPORTED_REGIONS``.

Profiles:

* ``off`` (default): nothing changes.
* ``standard``: ``PromptAttack`` (``PROMPT_INJECTION``, ``JAILBREAK``) above 0.4 denies the call.
  Tools that cannot be screened (no inline schema, no required string argument) are reported in
  ``policy_result.gateway_guardrails.unscreened_tools`` and keep working under Cedar alone.
* ``strict``: ``standard`` plus ``ContentFilter`` (all five categories) above 0.2, and the deploy
  is refused when any permitted tool cannot be screened.

Not in this change: output screening (``suppressOutput`` over an MCP result needs a JSONPath into
``content[*].text`` that AgentCore does not validate at create time), JSONPath screening of
served-only targets (connectors, MCP servers), and the CloudFormation export, which refuses a
profile other than ``off``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from app.services.aws_pagination import list_all
from app.services.region_capabilities import require_gateway_guardrails_region

GATEWAY_GUARDRAILS_PROFILES: tuple[str, ...] = ("off", "standard", "strict")

PROMPT_ATTACK_CATEGORIES: tuple[str, ...] = ("PROMPT_INJECTION", "JAILBREAK")
#: Documented default threshold for prompt attack detection.
PROMPT_ATTACK_THRESHOLD = "0.4"
CONTENT_FILTER_CATEGORIES: tuple[str, ...] = ("HATE", "INSULTS", "MISCONDUCT", "SEXUAL", "VIOLENCE")
#: Documented default threshold for content filters.
CONTENT_FILTER_THRESHOLD = "0.2"

#: Policy-name stems. The policy step prefixes every name with the engine name (account-global
#: policy names, Bug 137) and caps the whole at 48, so a stem stays short: ``gr_pa_<tool>_<h4>``.
PROMPT_ATTACK_POLICY_STEM = "gr_pa"
CONTENT_FILTER_POLICY_STEM = "gr_cf"

_CEDAR_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: ``<Target>___<tool>``; MCP tool names are already constrained by the gateway.
_QUALIFIED_TOOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*___[A-Za-z0-9][A-Za-z0-9_.:/-]*$")
_GATEWAY_ARN = re.compile(r"^arn:aws[a-z-]*:bedrock-agentcore:[a-z0-9-]+:\d{12}:gateway/[A-Za-z0-9_-]+$")

UNSCREENED_NO_SCHEMA = "no inline input schema (served-only target); JSONPath screening is a follow-up"
UNSCREENED_NO_REQUIRED_STRING = (
    "no required string argument to screen (a guardrail on optional arguments alone denies every call that omits them)"
)


class GatewayGuardrailsContractError(ValueError):
    """The request asks for Gateway guardrails in a way this platform cannot honour."""


class GatewayGuardrailsUnscreenedToolsError(RuntimeError):
    """Profile ``strict`` and at least one permitted tool cannot be screened."""


def gateway_guardrails_profile(policy_config: Mapping | None) -> str:
    """The requested profile, ``off`` when the policy node names none.

    Accepts ``policyConfig.guardrails`` as ``{"profile": "standard"}`` or the bare string. A value
    outside ``GATEWAY_GUARDRAILS_PROFILES`` is a contract error, never a silent ``off``.
    """

    if not isinstance(policy_config, Mapping):
        return "off"
    raw = policy_config.get("guardrails")
    if raw is None:
        return "off"
    profile = raw.get("profile") if isinstance(raw, Mapping) else raw
    if profile is None or profile == "":
        return "off"
    if not isinstance(profile, str) or profile not in GATEWAY_GUARDRAILS_PROFILES:
        raise GatewayGuardrailsContractError(
            f"Unknown Gateway guardrails profile: {profile!r} (expected one of: off, standard, strict)"
        )
    return profile


def gateway_guardrails_requested(policy_config: Mapping | None) -> bool:
    """Whether this deployment needs guardrail policies and the role grant behind them."""

    return gateway_guardrails_profile(policy_config) != "off"


def require_policy_engine_for_guardrails(policy_config: Mapping | None) -> None:
    """A profile on a disabled policy engine would be silently dropped; refuse it instead."""

    if gateway_guardrails_requested(policy_config) and policy_config.get("enabled", True) is False:
        raise GatewayGuardrailsContractError(
            "Gateway guardrails need the policy engine: policyConfig.enabled is false while "
            f"policyConfig.guardrails.profile is {gateway_guardrails_profile(policy_config)!r}. "
            "Enable the policy engine or set the profile to off."
        )


def screenable_input_fields(input_schema: Mapping | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(required string arguments, optional string arguments)`` of one tool's JSON schema.

    Top-level ``string`` properties whose names are Cedar identifiers, in schema order. Anything
    else (numbers, objects, lists, hyphenated names) cannot be a ``context.input.<field>`` path.
    """

    schema = input_schema if isinstance(input_schema, Mapping) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
    required_names = {name for name in (schema.get("required") or ()) if isinstance(name, str)}
    required: list[str] = []
    optional: list[str] = []
    for name, spec in properties.items():
        if not isinstance(name, str) or not _CEDAR_IDENTIFIER.match(name) or not isinstance(spec, Mapping):
            continue
        if spec.get("type") != "string":
            continue
        (required if name in required_names else optional).append(name)
    return tuple(required), tuple(optional)


def guardrail_policy_name(stem: str, qualified_tool: str) -> str:
    """``<stem>_<bare tool, 11 chars>_<4 hex of the qualified name>``: readable, collision-free, short."""

    bare = qualified_tool.rsplit("___", 1)[-1]
    bare = re.sub(r"[^A-Za-z0-9_]", "_", bare)[:11].strip("_") or "tool"
    digest = hashlib.sha256(qualified_tool.encode()).hexdigest()[:4]
    return f"{stem}_{bare}_{digest}"


def guardrail_forbid_statement(
    qualified_tool: str,
    gateway_arn: str,
    fields: Iterable[str],
    *,
    guardrail_type: str,
    categories: Iterable[str],
    threshold: str,
) -> str:
    """One ``forbid … when guardrails {…}`` statement in the documented MCP-tool shape."""

    if not _QUALIFIED_TOOL.match(qualified_tool or ""):
        raise GatewayGuardrailsContractError(f"Not a <Target>___<tool> action id: {qualified_tool!r}")
    if not _GATEWAY_ARN.match(gateway_arn or ""):
        raise GatewayGuardrailsContractError(
            "A guardrail policy must name the exact gateway ARN (resource ==); "
            f"refusing to build one for {gateway_arn!r}"
        )
    field_list = list(fields)
    if not field_list or any(not _CEDAR_IDENTIFIER.match(f) for f in field_list):
        raise GatewayGuardrailsContractError(f"Guardrail data paths must be Cedar identifiers: {field_list!r}")
    category_list = ", ".join(f'"{c}"' for c in categories)
    paths = ", ".join(f"context.input.{f}" for f in field_list)
    return (
        f'forbid(principal, action == AgentCore::Action::"{qualified_tool}", '
        f'resource == AgentCore::Gateway::"{gateway_arn}") '
        f"when guardrails {{ BedrockGuardrails::{guardrail_type}([{category_list}], [{paths}])"
        f'.maxConfidenceScore().greaterThan(decimal("{threshold}")) }};'
    )


@dataclass(frozen=True)
class GatewayGuardrailPlan:
    """What the policy step adds for one deployment, and what it could not cover."""

    profile: str
    policies: tuple[dict, ...] = ()
    screened_tools: tuple[str, ...] = ()
    unscreened_tools: dict[str, str] = field(default_factory=dict)

    @classmethod
    def off(cls) -> GatewayGuardrailPlan:
        return cls(profile="off")

    def as_result(self) -> dict:
        """The ``policy_result.gateway_guardrails`` receipt."""

        return {
            "profile": self.profile,
            "policy_names": [p["name"] for p in self.policies],
            "screened_tools": list(self.screened_tools),
            "unscreened_tools": dict(self.unscreened_tools),
        }


def _guardrail_types_for(profile: str) -> tuple[tuple[str, str, tuple[str, ...], str], ...]:
    """``(stem, guardrail type, categories, threshold)`` rows a profile emits per screened tool."""

    rows = [(PROMPT_ATTACK_POLICY_STEM, "PromptAttack", PROMPT_ATTACK_CATEGORIES, PROMPT_ATTACK_THRESHOLD)]
    if profile == "strict":
        rows.append((CONTENT_FILTER_POLICY_STEM, "ContentFilter", CONTENT_FILTER_CATEGORIES, CONTENT_FILTER_THRESHOLD))
    return tuple(rows)


def build_gateway_guardrail_plan(
    profile: str,
    *,
    region: str,
    gateway_arn: str,
    permitted_tools: Iterable[str],
    tool_input_schemas: Mapping[str, Mapping | None],
) -> GatewayGuardrailPlan:
    """Pure planner: the guardrail policies for ``permitted_tools`` under ``profile``.

    Fails closed on an unsupported Region, a missing gateway ARN, or (``strict``) an unscreenable
    tool. ``off`` returns an empty plan without touching anything.
    """

    if profile not in GATEWAY_GUARDRAILS_PROFILES:
        raise GatewayGuardrailsContractError(f"Unknown Gateway guardrails profile: {profile!r}")
    if profile == "off":
        return GatewayGuardrailPlan.off()
    require_gateway_guardrails_region(region)
    if not _GATEWAY_ARN.match(gateway_arn or ""):
        raise GatewayGuardrailsContractError(
            "Gateway guardrails need the gateway ARN for `resource ==`, and the gateway step "
            f"provided {gateway_arn!r}. Refusing to deploy unscreened."
        )

    policies: list[dict] = []
    screened: list[str] = []
    unscreened: dict[str, str] = {}
    for tool in permitted_tools:
        if tool not in tool_input_schemas:
            unscreened[tool] = UNSCREENED_NO_SCHEMA
            continue
        required, optional = screenable_input_fields(tool_input_schemas[tool])
        if not required:
            unscreened[tool] = UNSCREENED_NO_REQUIRED_STRING
            continue
        fields = (*required, *optional)
        for stem, guardrail_type, categories, threshold in _guardrail_types_for(profile):
            policies.append(
                {
                    "name": guardrail_policy_name(stem, tool),
                    "description": (
                        f"Gateway guardrails ({profile}): deny {tool} when Bedrock Guardrails {guardrail_type} "
                        f"scores {'/'.join(categories)} above {threshold} on {', '.join(fields)}."
                    ),
                    "statement": guardrail_forbid_statement(
                        tool,
                        gateway_arn,
                        fields,
                        guardrail_type=guardrail_type,
                        categories=categories,
                        threshold=threshold,
                    ),
                }
            )
        screened.append(tool)

    if profile == "strict" and unscreened:
        detail = "; ".join(f"{tool} ({reason})" for tool, reason in unscreened.items())
        raise GatewayGuardrailsUnscreenedToolsError(
            "Gateway guardrails profile 'strict' requires every permitted tool to be screened, but these "
            f"cannot be: {detail}. Use profile 'standard' (screens what it can and reports the rest), remove "
            "those tools, or give each a required string argument."
        )
    return GatewayGuardrailPlan(
        profile=profile,
        policies=tuple(policies),
        screened_tools=tuple(screened),
        unscreened_tools=unscreened,
    )


def read_gateway_tool_input_schemas(agentcore_ctrl, gateway_id: str) -> dict[str, dict]:
    """``{"<Target>___<tool>": inputSchema}`` for every tool declared inline on a Lambda target.

    Connector (Web Search, Web Answer, OpenAPI) and MCP-server targets declare nothing inline, so
    their tools are absent here and the plan reports them as unscreened. A read failure raises:
    a guardrail that cannot be planned must not become a deploy that quietly has none.
    """

    schemas: dict[str, dict] = {}
    targets = list_all(
        agentcore_ctrl,
        "list_gateway_targets",
        item_keys=("items", "targets", "gatewayTargetSummaries"),
        request={"gatewayIdentifier": gateway_id, "maxResults": 50},
    )
    for target in targets:
        target_name = target.get("name")
        target_id = target.get("targetId") or target.get("gatewayTargetId")
        if not target_name or not target_id:
            continue
        detail = agentcore_ctrl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
        mcp = (detail.get("targetConfiguration") or {}).get("mcp") or {}
        inline = ((mcp.get("lambda") or {}).get("toolSchema") or {}).get("inlinePayload") or []
        for tool in inline:
            tool_name = tool.get("name") if isinstance(tool, Mapping) else None
            if tool_name:
                schemas[f"{target_name}___{tool_name}"] = dict(tool.get("inputSchema") or {})
    return schemas


def plan_gateway_guardrails_for_step(
    policy_config: Mapping | None,
    *,
    region: str,
    gateway_arn: str,
    gateway_id: str,
    permitted_tools: Iterable[str],
    agentcore_ctrl,
) -> GatewayGuardrailPlan:
    """The policy step's entry point: resolve the profile, read the schemas, plan. ``off`` reads nothing."""

    profile = gateway_guardrails_profile(policy_config)
    if profile == "off":
        return GatewayGuardrailPlan.off()
    require_gateway_guardrails_region(region)
    schemas = read_gateway_tool_input_schemas(agentcore_ctrl, gateway_id)
    return build_gateway_guardrail_plan(
        profile,
        region=region,
        gateway_arn=gateway_arn,
        permitted_tools=permitted_tools,
        tool_input_schemas=schemas,
    )


def strict_profile_admission_blockers(
    *,
    template_id: str | None,
    gateway_tools: Iterable[object] | None,
    custom_tools: Iterable[object] | None,
    connectors: Iterable[object] | None,
    external_mcp_servers: Iterable[object] | None,
) -> list[str]:
    """Why ``strict`` would be refused by the policy step, knowable before any AWS side effect.

    Served-only targets (managed Web Search / Web Answer, connectors, external MCP servers) have
    no inline schema, and a custom tool without a required string argument has nothing to screen.
    The policy step re-checks against the live gateway; this only moves the certain refusals to
    admission so no Gateway is created for a deploy that cannot finish.
    """

    from app.services.managed_web_answer import managed_web_answer_requested
    from app.services.managed_web_search import managed_web_search_requested

    blockers: list[str] = []
    if managed_web_search_requested(template_id, gateway_tools):
        blockers.append("managed Web Search is a served-only connector target")
    if managed_web_answer_requested(template_id, gateway_tools):
        blockers.append("Web Answer is a served-only connector target")
    if connectors:
        blockers.append("connectors are served-only OpenAPI targets")
    if external_mcp_servers:
        blockers.append("external MCP servers publish their tools at runtime")
    for tool in custom_tools or ():
        # Accepts the request model (CustomToolDefinition: tool_name / input_schema) and the
        # state-machine payload dict (toolName|name / inputSchema|input_schema) alike.
        if isinstance(tool, Mapping):
            schema = tool.get("inputSchema", tool.get("input_schema"))
            name = tool.get("toolName") or tool.get("tool_name") or tool.get("name") or "<unnamed>"
        else:
            schema = getattr(tool, "input_schema", None)
            name = getattr(tool, "tool_name", None) or "<unnamed>"
        required, _optional = screenable_input_fields(schema)
        if not required:
            blockers.append(f"custom tool {name!s} has no required string argument")
    return blockers
