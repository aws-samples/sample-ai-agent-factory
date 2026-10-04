#!/usr/bin/env python3
"""AWS-free model for the AgentCore Gateway PolicyEngine compatibility spike.

Everything in this module is a pure function or a frozen value object: no boto3
import, no network, no filesystem. That keeps the security-critical parts of the
spike -- Cedar statement generation, the decision truth table, resource-name
ownership, and secret-safety scanning -- unit-testable with zero AWS dependency,
matching the discipline of ``tests/adversarial`` (see its
``test_no_aws_dependency.py``).

Facts this module encodes, each taken from the pinned ``bedrock-agentcore-control``
service model (live-inspected at ``boto3==1.43.97``) or the AgentCore Policy
documentation, not from guesswork:

* Principal for a ``CUSTOM_JWT`` gateway is ``AgentCore::OAuthUser::"<sub>"``.
* Action is ``AgentCore::Action::"<TargetName>___<ToolName>"`` (three
  underscores).
* Resource is ``AgentCore::Gateway::"<gateway ARN>"`` and MUST be a concrete
  ARN -- wildcard-scoped statements are rejected by validation.
* Tool-call arguments arrive as ``context.input``.
* Policy and policy-engine *names* match ``[A-Za-z][A-Za-z0-9_]*`` with a
  48-character maximum -- hyphens are invalid, so a hyphenated run prefix has to
  be translated.
* Listing policies for cleanup/verification uses ``ListPolicySummaries`` (the
  lightweight summary listing), whose response is ``{"policies": [...],
  "nextToken": ...}`` where each summary carries ``policyId``, ``name``,
  ``status`` and ``enforcementMode``. See :data:`LIST_POLICIES_OPERATION`.
* Cedar's only pattern operator is ``like`` with ``*`` wildcards, which is
  substring matching.

The ``cognito:groups`` claim is a JSON **array** exposed to Cedar as a principal
tag. AWS documents scalar JWT claims as tags but does not document the wire
representation of array-valued claims. This compatibility spike therefore
exercises one *candidate*: a delimiter-aware match on the quoted JSON element
boundary (``"<group>"``), never a bare ``*group*`` substring. Product code MUST
NOT adopt the candidate unless the live spike proves an exact-member positive
and prefix/suffix-collision negatives against the real Gateway PolicyEngine.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import hashlib
import re
import secrets
import string
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

# --------------------------------------------------------------------------
# Cedar vocabulary and service constraints
# --------------------------------------------------------------------------

CEDAR_PRINCIPAL_TYPE = "AgentCore::OAuthUser"
CEDAR_ACTION_TYPE = "AgentCore::Action"
CEDAR_GATEWAY_TYPE = "AgentCore::Gateway"
ACTION_SEPARATOR = "___"

#: ``CreatePolicy.validationMode`` -- keep the strict default so that automated
#: reasoning findings reject a policy instead of deploying it.
POLICY_VALIDATION_MODE = "FAIL_ON_ANY_FINDINGS"
#: ``CreatePolicy.enforcementMode`` -- the policy itself must be ACTIVE, which is
#: independent of the gateway association mode below.
POLICY_ENFORCEMENT_MODE = "ACTIVE"
#: ``GatewayPolicyEngineConfiguration.mode`` enum.
GATEWAY_MODE_LOG_ONLY = "LOG_ONLY"
GATEWAY_MODE_ENFORCE = "ENFORCE"
GATEWAY_MODES = (GATEWAY_MODE_LOG_ONLY, GATEWAY_MODE_ENFORCE)

#: ``PolicyStatus``/``PolicyEngineStatus`` enums.
POLICY_ACTIVE_STATUS = "ACTIVE"
POLICY_TERMINAL_FAILURES = frozenset(
    {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}
)

#: The canonical operation the spike uses to list policies for verification and
#: cleanup. ``ListPolicySummaries`` returns the lightweight summary shape below;
#: the boto3 method name is the snake_case form. Pinned by a service-model
#: contract test in ``test_policy_engine_spike.py``.
LIST_POLICIES_OPERATION = "ListPolicySummaries"
LIST_POLICIES_METHOD = "list_policy_summaries"
#: Members that ``ListPolicySummaries`` guarantees on each summary. The spike
#: reads only these; it never depends on the full-definition ``ListPolicies``
#: shape for listing.
POLICY_SUMMARY_MEMBERS = ("policyId", "name", "policyEngineId", "status", "enforcementMode")

#: MCP protocol version this spike negotiates. The blueprint live-verified that
#: the header must be sent verbatim after ``initialize`` (see
#: ``packages/agent-protocols/src/mcp.ts``).
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_PROTOCOL_HEADER = "MCP-Protocol-Version"

#: Documentation-derived GA region list for Policy in AgentCore. This is a
#: documentation claim, not a live-verified matrix: the spike refuses any other
#: region so that a run cannot silently probe an unsupported one, and records the
#: region it actually exercised as the only live evidence.
POLICY_SUPPORTED_REGIONS = frozenset(
    {
        "us-east-1",
        "us-east-2",
        "us-west-2",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "eu-central-1",
        "eu-north-1",
        "ap-south-1",
        "ap-northeast-1",
        "ap-northeast-2",
        "ap-southeast-1",
        "ap-southeast-2",
    }
)
#: Subset of the above that is in scope for the EMEA region matrix this
#: repository is required to reason about explicitly.
POLICY_EMEA_REGIONS = frozenset(
    {"eu-west-1", "eu-west-2", "eu-west-3", "eu-central-1", "eu-north-1"}
)

GROUP_CLAIM_NAME = "cognito:groups"
GROUP_CLAIM_CANDIDATE_REPRESENTATION = "quoted-json-element"

#: Operators that would turn an entitlement check into substring matching, or
#: that read a principal tag whose representation this spike has not verified.
#: ``like``/``hasTag``/``getTag`` are permitted **only** inside the exact,
#: audited group-candidate fragment recognised by
#: :func:`assert_no_pattern_matching`.
FORBIDDEN_CEDAR_OPERATORS = (
    "like",
    "contains",
    "containsAll",
    "containsAny",
    "hasTag",
    "getTag",
)

# Service-model patterns, transcribed from the pinned SDK.
POLICY_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")
GATEWAY_NAME_PATTERN = re.compile(r"^(?:[0-9a-zA-Z]-?){1,48}$")
GATEWAY_ARN_PATTERN = re.compile(
    r"^arn:aws:bedrock-agentcore:[a-z0-9-]+:\d{12}:gateway/[A-Za-z0-9][A-Za-z0-9_-]{0,99}$"
)
POLICY_ENGINE_ARN_PATTERN = re.compile(
    r"^arn:aws:bedrock-agentcore:[a-z0-9-]+:\d{12}:policy-engine/"
    r"[A-Za-z][A-Za-z0-9_-]{0,99}-[A-Za-z0-9_]{10}$"
)
#: Cognito ``sub`` is a v4 UUID. Anything else is refused rather than quoted into
#: a Cedar statement.
SUBJECT_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
TARGET_NAME_PATTERN = re.compile(r"^[0-9a-zA-Z][0-9a-zA-Z-]{0,99}$")
PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,39}$")
ACCOUNT_PATTERN = re.compile(r"^\d{12}$")
INPUT_FIELD_PATTERN = re.compile(r"^[a-z][A-Za-z0-9]{0,63}$")
INPUT_VALUE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
#: A Cognito group name may contain a wide range of characters. For the spike we
#: restrict the *generated* group names to a charset that has no Cedar/JSON
#: metacharacters, so the quoted-element candidate cannot be broken by an
#: embedded quote or backslash. This is a generator constraint, not a claim
#: about what Cognito itself permits.
GROUP_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


class ModelError(RuntimeError):
    """A fail-closed error raised by this AWS-free model."""


class CedarPolicyError(ModelError):
    """The requested Cedar statement cannot be generated safely."""


class UnknownClaimRepresentationError(CedarPolicyError):
    """A claim's representation is undocumented, so no policy is generated."""


class SecretLeakError(ModelError):
    """A value that looks like a credential was about to be persisted."""


class ToolResultError(ModelError):
    """A tool result could not be classified as an allow or a policy denial."""


# --------------------------------------------------------------------------
# Literals
# --------------------------------------------------------------------------


def _require(pattern: re.Pattern[str], value: str, label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise CedarPolicyError(f"{label} {value!r} does not match {pattern.pattern}")
    return value


def qualified_action(target_name: str, tool_name: str) -> str:
    """Return the exact AgentCore action name ``<TargetName>___<ToolName>``."""
    _require(TARGET_NAME_PATTERN, target_name, "Target name")
    _require(TOOL_NAME_PATTERN, tool_name, "Tool name")
    return f"{target_name}{ACTION_SEPARATOR}{tool_name}"


def action_literal(target_name: str, tool_name: str) -> str:
    return f'{CEDAR_ACTION_TYPE}::"{qualified_action(target_name, tool_name)}"'


def subject_literal(subject: str) -> str:
    _require(SUBJECT_PATTERN, subject, "JWT subject")
    return f'{CEDAR_PRINCIPAL_TYPE}::"{subject}"'


def gateway_literal(gateway_arn: str) -> str:
    _require(GATEWAY_ARN_PATTERN, gateway_arn, "Gateway ARN")
    if "*" in gateway_arn:
        raise CedarPolicyError("Wildcard gateway resources are refused")
    return f'{CEDAR_GATEWAY_TYPE}::"{gateway_arn}"'


# --------------------------------------------------------------------------
# Group-claim membership: the narrow, audited candidate generator
# --------------------------------------------------------------------------

def group_membership_candidate(group: str) -> str:
    """Return the delimiter-aware candidate for one ``cognito:groups`` member.

    AWS documents OAuth claims as Cedar tags but does not document how an array
    claim is serialized. The candidate assumes compact JSON array text and
    anchors on the quoted element boundary. ``hasTag`` protects ``getTag`` when
    the claim is absent. The live spike must prove exact-member allow plus
    prefix/suffix-collision denial before this shape can be adopted elsewhere.
    """
    _require(GROUP_NAME_PATTERN, group, "Group name")
    quoted = '\\"' + group + '\\"'
    return (
        f'principal.hasTag("{GROUP_CLAIM_NAME}") && '
        f'principal.getTag("{GROUP_CLAIM_NAME}") like "*{quoted}*"'
    )


def quoted_element_matches(tag_value: str, group: str) -> bool:
    """Executable model of the quoted-element ``like`` comparison.

    ``tag_value`` is the JSON serialisation of the ``cognito:groups`` array as a
    single string (for example ``'["tools","admins"]'``). Returns ``True`` only
    when ``"<group>"`` (with both quotes) appears as a substring -- exactly the
    semantics of ``like "*\\\"<group>\\\"*"``. This is the model the collision
    fuzz tests compare against; it must never be satisfied by a prefix/suffix
    collision.
    """
    _require(GROUP_NAME_PATTERN, group, "Group name")
    return f'"{group}"' in tag_value


def serialize_groups_claim(groups: Sequence[str]) -> str:
    """Model the compact JSON-array candidate used by the live probe."""
    for group in groups:
        _require(GROUP_NAME_PATTERN, group, "Group name")
    return "[" + ",".join(f'"{group}"' for group in groups) + "]"


def group_scoped_statement(
    *,
    group: str,
    tool: str,
    target_name: str,
    gateway_arn: str,
    subject: str | None = None,
) -> str:
    """Emit the narrowly audited group candidate used only by this spike."""
    _require(GROUP_NAME_PATTERN, group, "Group name")
    principal = (
        f"principal == {subject_literal(subject)}"
        if subject is not None
        else f"principal is {CEDAR_PRINCIPAL_TYPE}"
    )
    lines = [
        "permit(",
        f"  {principal},",
        f"  action == {action_literal(target_name, tool)},",
        f"  resource == {gateway_literal(gateway_arn)}",
        ")",
        f"when {{ {group_membership_candidate(group)} }};",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Structured policy model -- one source of truth for rendering and expectation
# --------------------------------------------------------------------------


class ConditionKind(Enum):
    HAS = "has"
    EQUALS = "equals"


class Effect(Enum):
    PERMIT = "permit"
    FORBID = "forbid"


class Decision(Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class PrincipalScope(Enum):
    """How a policy's principal clause is scoped."""

    ANY = "any"           # ``principal is AgentCore::OAuthUser`` (all OAuth users)
    SUBJECT = "subject"   # ``principal == AgentCore::OAuthUser::"<sub>"``
    GROUP = "group"       # membership in a ``cognito:groups`` element (candidate)
    SUBJECT_AND_GROUP = "subject-and-group"  # exact ``sub`` AND exact group member


@dataclass(frozen=True)
class InputCondition:
    """A condition over ``context.input``. No pattern or tag operators."""

    kind: ConditionKind
    field: str
    value: str | None = None

    def __post_init__(self) -> None:
        _require(INPUT_FIELD_PATTERN, self.field, "Input field")
        if self.kind is ConditionKind.EQUALS:
            if self.value is None:
                raise CedarPolicyError("An EQUALS condition requires a value")
            _require(INPUT_VALUE_PATTERN, self.value, "Input value")
        elif self.value is not None:
            raise CedarPolicyError("A HAS condition must not carry a value")

    def render(self) -> str:
        presence = f"context.input has {self.field}"
        if self.kind is ConditionKind.HAS:
            return presence
        return f'{presence} && context.input.{self.field} == "{self.value}"'

    def holds(self, arguments: Mapping[str, Any]) -> bool:
        if self.field not in arguments:
            return False
        if self.kind is ConditionKind.HAS:
            return True
        return arguments[self.field] == self.value


@dataclass(frozen=True)
class ToolPolicy:
    """A single Cedar statement, scoped to one action and one gateway.

    A policy's principal is one of three scopes:

    * ``PrincipalScope.SUBJECT`` -- an exact ``sub`` (``subject`` set).
    * ``PrincipalScope.GROUP`` -- membership in a ``cognito:groups`` element
      (``group`` set); rendered via the audited candidate generator and only
      when a representation is live-proven.
    * ``PrincipalScope.ANY`` -- every OAuth principal; only valid on a
      ``forbid`` (an unconstrained ``permit`` is the shape ``FAIL_ON_ANY_FINDINGS``
      rejects as overly permissive).
    """

    name: str
    effect: Effect
    tool: str
    scope: PrincipalScope = PrincipalScope.ANY
    subject: str | None = None
    group: str | None = None
    when_all: tuple[InputCondition, ...] = ()
    unless_all: tuple[InputCondition, ...] = ()

    def __post_init__(self) -> None:
        _require(POLICY_NAME_PATTERN, self.name, "Policy name")
        _require(TOOL_NAME_PATTERN, self.tool, "Tool name")
        if self.scope is PrincipalScope.SUBJECT:
            if self.subject is None:
                raise CedarPolicyError(f"Subject-scoped {self.name!r} needs a subject")
            _require(SUBJECT_PATTERN, self.subject, "JWT subject")
            if self.group is not None:
                raise CedarPolicyError(f"Subject-scoped {self.name!r} must not set a group")
        elif self.scope is PrincipalScope.GROUP:
            if self.group is None:
                raise CedarPolicyError(f"Group-scoped {self.name!r} needs a group")
            _require(GROUP_NAME_PATTERN, self.group, "Group name")
            if self.subject is not None:
                raise CedarPolicyError(f"Group-scoped {self.name!r} must not set a subject")
        elif self.scope is PrincipalScope.SUBJECT_AND_GROUP:
            if self.subject is None or self.group is None:
                raise CedarPolicyError(
                    f"Subject-and-group policy {self.name!r} needs both values"
                )
            _require(SUBJECT_PATTERN, self.subject, "JWT subject")
            _require(GROUP_NAME_PATTERN, self.group, "Group name")
        else:  # ANY
            if self.subject is not None or self.group is not None:
                raise CedarPolicyError(f"ANY-scoped {self.name!r} must not name a principal")
            if self.effect is Effect.PERMIT:
                raise CedarPolicyError(
                    f"Permit {self.name!r} must name an exact subject or group"
                )
        if self.when_all and self.unless_all:
            raise CedarPolicyError(
                f"Policy {self.name!r} must use either when or unless, not both"
            )

    # -- rendering ------------------------------------------------------
    def _principal_line(self) -> str:
        if self.scope in (PrincipalScope.SUBJECT, PrincipalScope.SUBJECT_AND_GROUP):
            return f"principal == {subject_literal(self.subject)}"  # type: ignore[arg-type]
        return f"principal is {CEDAR_PRINCIPAL_TYPE}"

    def _scope_lines(self, *, target_name: str, gateway_arn: str) -> list[str]:
        return [
            f"  {self._principal_line()},",
            f"  action == {action_literal(target_name, self.tool)},",
            f"  resource == {gateway_literal(gateway_arn)}",
        ]

    def render(self, *, target_name: str, gateway_arn: str) -> str:
        lines = [f"{self.effect.value}("]
        lines.extend(self._scope_lines(target_name=target_name, gateway_arn=gateway_arn))
        lines.append(")")
        conditions: list[str] = []
        for condition in self.when_all:
            conditions.append(condition.render())
        if self.scope in (PrincipalScope.GROUP, PrincipalScope.SUBJECT_AND_GROUP):
            conditions.append(group_membership_candidate(self.group))  # type: ignore[arg-type]
        if conditions:
            lines.append("when { " + " && ".join(conditions) + " }")
        for condition in self.unless_all:
            lines.append(f"unless {{ {condition.render()} }}")
        statement = "\n".join(lines) + ";"
        assert_no_pattern_matching(statement)
        return statement

    # -- evaluation -----------------------------------------------------
    def matches_scope(self, *, subject: str, groups: Sequence[str], tool: str) -> bool:
        if self.tool != tool:
            return False
        if self.scope is PrincipalScope.SUBJECT:
            return self.subject == subject
        if self.scope is PrincipalScope.GROUP:
            return self.group in tuple(groups)
        if self.scope is PrincipalScope.SUBJECT_AND_GROUP:
            return self.subject == subject and self.group in tuple(groups)
        return True  # ANY

    def applies(
        self,
        *,
        subject: str,
        groups: Sequence[str],
        tool: str,
        arguments: Mapping[str, Any],
    ) -> bool:
        if not self.matches_scope(subject=subject, groups=groups, tool=tool):
            return False
        if any(not condition.holds(arguments) for condition in self.when_all):
            return False
        # Cedar: `unless { A }` means the policy does NOT apply when A.
        if self.unless_all and all(
            condition.holds(arguments) for condition in self.unless_all
        ):
            return False
        return True

    @property
    def unconditional(self) -> bool:
        return (
            not self.when_all
            and not self.unless_all
            and self.scope
            not in (PrincipalScope.GROUP, PrincipalScope.SUBJECT_AND_GROUP)
        )


def assert_no_pattern_matching(statement: str) -> str:
    """Reject every tag/pattern expression except the exact group candidate.

    The compatibility exception is the delimiter-aware fragment emitted by
    :func:`group_membership_candidate`. It is stripped before the blanket scan;
    all other tag reads and substring expressions remain forbidden.
    """
    candidate = re.compile(
        r'principal\.hasTag\("cognito:groups"\) && '
        r'principal\.getTag\("cognito:groups"\) like '
        r'"\*\\"[A-Za-z][A-Za-z0-9_-]{0,63}\\"\*"'
    )
    scan_target = candidate.sub("", statement)
    for operator in FORBIDDEN_CEDAR_OPERATORS:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(operator)}\b", scan_target):
            raise CedarPolicyError(
                f"Generated Cedar uses forbidden operator {operator!r}: refusing "
                "substring or unverified-claim matching"
            )
    return statement


def render_policies(
    policies: Sequence[ToolPolicy], *, target_name: str, gateway_arn: str
) -> dict[str, str]:
    """Render each policy, keyed by its ``CreatePolicy`` name."""
    rendered: dict[str, str] = {}
    for policy in policies:
        if policy.name in rendered:
            raise CedarPolicyError(f"Duplicate policy name {policy.name!r}")
        rendered[policy.name] = policy.render(
            target_name=target_name, gateway_arn=gateway_arn
        )
    return rendered


def evaluate(
    policies: Sequence[ToolPolicy],
    *,
    subject: str,
    groups: Sequence[str],
    tool: str,
    arguments: Mapping[str, Any],
) -> Decision:
    """Cedar decision order: forbid wins, then permit, then default deny."""
    for policy in policies:
        if policy.effect is Effect.FORBID and policy.applies(
            subject=subject, groups=groups, tool=tool, arguments=arguments
        ):
            return Decision.DENY
    for policy in policies:
        if policy.effect is Effect.PERMIT and policy.applies(
            subject=subject, groups=groups, tool=tool, arguments=arguments
        ):
            return Decision.ALLOW
    return Decision.DENY


def tool_is_listable(
    policies: Sequence[ToolPolicy], *, subject: str, groups: Sequence[str], tool: str
) -> bool:
    """Partial-evaluation model: visible if some argument set could be allowed.

    Mirrors the documented ``tools/list`` semantics -- a tool is omitted only when
    it would be denied under *all* circumstances.
    """
    for policy in policies:
        if (
            policy.effect is Effect.FORBID
            and policy.unconditional
            and policy.matches_scope(subject=subject, groups=groups, tool=tool)
        ):
            return False
    return any(
        policy.effect is Effect.PERMIT
        and policy.matches_scope(subject=subject, groups=groups, tool=tool)
        for policy in policies
    )


# --------------------------------------------------------------------------
# The exercised policy set and its truth table
# --------------------------------------------------------------------------

# Five tools expose the five entitlement shapes the spike must observe.
TOOL_SUBJECT_ONLY = "subject-tool"      # permitted by an exact subject
TOOL_GROUP_ONLY = "group-tool"          # permitted by group membership only
TOOL_COMBINED_ANY = "any-tool"          # permitted by subject OR group (disjunction)
TOOL_COMBINED_ALL = "all-tool"          # permitted by subject AND group
TOOL_UNPERMITTED = "denied-tool"        # covered by no permit -> default deny
SPIKE_TOOLS = (
    TOOL_SUBJECT_ONLY,
    TOOL_GROUP_ONLY,
    TOOL_COMBINED_ANY,
    TOOL_COMBINED_ALL,
    TOOL_UNPERMITTED,
)

AUDIT_FIELD = "audit"
MODE_FIELD = "mode"
READONLY_MODE = "readonly"
WRITE_MODE = "write"

#: Deterministic marker returned by the ephemeral echo Lambda. Its presence is
#: how an allow is confirmed without recording any tool response text.
ECHO_MARKER = "policy-engine-spike-echo-ok"

# Four users spanning the subject x group matrix (B6).
ALPHA = "alpha"   # allowed subject + allowed group
BETA = "beta"     # allowed group only
GAMMA = "gamma"   # allowed subject only + a prefix/suffix-collision group
DELTA = "delta"   # neither: only collision groups
SUBJECT_LABELS = (ALPHA, BETA, GAMMA, DELTA)
#: The users whose ``sub`` is entitled by a subject-scoped permit.
ALLOWED_SUBJECT_LABELS = (ALPHA, GAMMA)


@dataclass(frozen=True)
class UserProfile:
    """A user's subject entitlement and Cognito group memberships."""

    label: str
    has_allowed_subject: bool
    groups: tuple[str, ...]


def group_layout(names: "SpikeNames") -> dict[str, str]:
    """The named groups the spike creates, including collision decoys.

    * ``allowed`` -- the entitling group.
    * ``suffix_collision`` -- ``<allowed>x``: a trailing-character decoration
      that a naive ``*<allowed>*`` match would wrongly admit but the quoted
      candidate must reject (the closing quote differs).
    * ``inner_collision`` -- ``<allowed>zz``: a second, longer decoration
      standing in for a plausible sibling group such as ``<allowed>_admins``.

    Both decoys are **prefix-owned** (they start with ``<allowed>``, which itself
    starts with the run prefix) so cleanup's ownership guard can still delete
    them. The *prefix*-side boundary (a name ending in ``<allowed>``) is not a
    prefix-owned name, so it is proven at the model layer instead --
    :func:`quoted_element_matches` is fuzzed against arbitrary prefix *and*
    suffix decorations in the tests.
    """
    allowed = names.allowed_group_name
    return {
        "allowed": allowed,
        "suffix_collision": f"{allowed}x",
        "inner_collision": f"{allowed}zz",
    }


def groups_for_label(names: "SpikeNames", label: str) -> tuple[str, ...]:
    """Return the exact Cognito group memberships for one matrix row."""
    layout = group_layout(names)
    memberships = {
        ALPHA: (layout["allowed"],),
        BETA: (layout["allowed"],),
        GAMMA: (layout["suffix_collision"],),
        DELTA: (layout["suffix_collision"], layout["inner_collision"]),
    }
    try:
        return memberships[label]
    except KeyError as error:
        raise CedarPolicyError(f"Unknown subject label {label!r}") from error


def user_profiles(names: "SpikeNames", subjects: Mapping[str, str]) -> dict[str, UserProfile]:
    """Build the four user profiles from resolved subjects and group layout."""
    missing = [label for label in SUBJECT_LABELS if label not in subjects]
    if missing:
        raise CedarPolicyError(f"Missing subjects for {missing}")
    return {
        label: UserProfile(
            label,
            label in ALLOWED_SUBJECT_LABELS,
            groups_for_label(names, label),
        )
        for label in SUBJECT_LABELS
    }


def build_policy_set(
    *, names: "SpikeNames", subjects: Mapping[str, str]
) -> tuple[ToolPolicy, ...]:
    """Build the candidate policy set exercised by the compatibility spike.

    The two allowed-subject holders are ``alpha`` and ``gamma``; ``beta`` and
    ``delta`` hold no subject entitlement. ``alpha``/``beta`` are members of the
    exact allowed group, while ``gamma``/``delta`` hold only collision groups.

    * ``subject-tool`` -- exact subject only.
    * ``group-tool`` -- exact group-member candidate only.
    * ``any-tool`` -- exact subject OR exact group member.
    * ``all-tool`` -- exact subject AND exact group member.
    * ``denied-tool`` -- no permit (default deny and list filtering).

    The group representation is intentionally a candidate. A run is successful
    only if real Gateway decisions reproduce the full matrix, including both
    collision-group negatives.
    """
    # Validate the full four-user subject set up front so a missing subject
    # fails closed regardless of which labels the policies happen to reference.
    user_profiles(names, subjects)
    allowed_group = group_layout(names)["allowed"]

    policies: list[ToolPolicy] = []
    for label in ALLOWED_SUBJECT_LABELS:
        subject = subjects[label]
        policies.append(
            ToolPolicy(
                name=names.policy_name(f"subject_only_{label}"),
                effect=Effect.PERMIT,
                tool=TOOL_SUBJECT_ONLY,
                scope=PrincipalScope.SUBJECT,
                subject=subject,
            )
        )
        policies.append(
            ToolPolicy(
                name=names.policy_name(f"any_subject_{label}"),
                effect=Effect.PERMIT,
                tool=TOOL_COMBINED_ANY,
                scope=PrincipalScope.SUBJECT,
                subject=subject,
            )
        )
        policies.append(
            ToolPolicy(
                name=names.policy_name(f"all_subject_{label}"),
                effect=Effect.PERMIT,
                tool=TOOL_COMBINED_ALL,
                scope=PrincipalScope.SUBJECT_AND_GROUP,
                subject=subject,
                group=allowed_group,
            )
        )
    policies.extend(
        (
            ToolPolicy(
                name=names.policy_name("group_only"),
                effect=Effect.PERMIT,
                tool=TOOL_GROUP_ONLY,
                scope=PrincipalScope.GROUP,
                group=allowed_group,
            ),
            ToolPolicy(
                name=names.policy_name("any_group"),
                effect=Effect.PERMIT,
                tool=TOOL_COMBINED_ANY,
                scope=PrincipalScope.GROUP,
                group=allowed_group,
            ),
        )
    )
    return tuple(policies)


def probe_arguments(*, mode: str, audit: bool) -> dict[str, Any]:
    """Fixed, non-sensitive tool arguments. Values are never recorded."""
    arguments: dict[str, Any] = {"message": "fixed-probe", MODE_FIELD: mode}
    if audit:
        arguments[AUDIT_FIELD] = "spike"
    return arguments


#: The argument variants used per tool in the truth table.
ARGUMENT_MATRIX = tuple(
    (
        f"{mode}-{'audit' if audit else 'noaudit'}",
        probe_arguments(mode=mode, audit=audit),
    )
    for mode in (READONLY_MODE, WRITE_MODE)
    for audit in (True, False)
)


@dataclass(frozen=True)
class DecisionCase:
    """One row of the truth table the live run must reproduce exactly."""

    case_id: str
    subject_label: str
    tool: str
    arguments: Mapping[str, Any]
    expected: Decision
    semantics: str


def _semantics(tool: str) -> str:
    return {
        TOOL_SUBJECT_ONLY: "subject-only",
        TOOL_GROUP_ONLY: "group-only",
        TOOL_COMBINED_ANY: "ANY-disjunction",
        TOOL_COMBINED_ALL: "ALL-conjunction",
        TOOL_UNPERMITTED: "default-deny",
    }[tool]


def _tool_argument_variants(tool: str) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    # Identity-combination semantics are independent of tool input. Keep one
    # deterministic payload per user/tool pair so the live matrix is bounded.
    return (
        ("readonly-audit", probe_arguments(mode=READONLY_MODE, audit=True)),
    )


def build_truth_table(
    policies: Sequence[ToolPolicy],
    *,
    names: "SpikeNames",
    subjects: Mapping[str, str],
) -> tuple[DecisionCase, ...]:
    """Expand every (user, tool, arguments) triple with its expectation."""
    profiles = user_profiles(names, subjects)
    cases: list[DecisionCase] = []
    for label in SUBJECT_LABELS:
        profile = profiles[label]
        subject = subjects[label]
        for tool in SPIKE_TOOLS:
            for suffix, arguments in _tool_argument_variants(tool):
                expected = evaluate(
                    policies,
                    subject=subject,
                    groups=profile.groups,
                    tool=tool,
                    arguments=arguments,
                )
                cases.append(
                    DecisionCase(
                        case_id=f"{label}-{tool}-{suffix}",
                        subject_label=label,
                        tool=tool,
                        arguments=arguments,
                        expected=expected,
                        semantics=_semantics(tool),
                    )
                )
    return tuple(cases)


def expected_listing(
    policies: Sequence[ToolPolicy],
    *,
    names: "SpikeNames",
    subjects: Mapping[str, str],
    target_name: str,
) -> dict[str, tuple[str, ...]]:
    """Expected ``tools/list`` contents per user label, fully qualified."""
    profiles = user_profiles(names, subjects)
    listing: dict[str, tuple[str, ...]] = {}
    for label in SUBJECT_LABELS:
        profile = profiles[label]
        listing[label] = tuple(
            qualified_action(target_name, tool)
            for tool in SPIKE_TOOLS
            if tool_is_listable(
                policies, subject=subjects[label], groups=profile.groups, tool=tool
            )
        )
    return listing


# --------------------------------------------------------------------------
# Resource naming and ownership
# --------------------------------------------------------------------------

_POLICY_SUFFIXES = (
    "subject_only_alpha",
    "subject_only_gamma",
    "any_subject_alpha",
    "any_subject_gamma",
    "all_subject_alpha",
    "all_subject_gamma",
    "group_only",
    "any_group",
)


@dataclass(frozen=True)
class SpikeNames:
    """Every resource name this spike may create, derived from one prefix."""

    prefix: str

    def __post_init__(self) -> None:
        _require(PREFIX_PATTERN, self.prefix, "Prefix")
        _require(GATEWAY_NAME_PATTERN, self.gateway_name, "Gateway name")
        _require(POLICY_NAME_PATTERN, self.engine_name, "Policy engine name")
        _require(TARGET_NAME_PATTERN, self.target_name, "Target name")
        for suffix in _POLICY_SUFFIXES:
            _require(POLICY_NAME_PATTERN, self.policy_name(suffix), "Policy name")
        for group in group_layout(self).values():
            _require(GROUP_NAME_PATTERN, group, "Group name")

    @property
    def underscore_prefix(self) -> str:
        """Prefix translated for the ``[A-Za-z][A-Za-z0-9_]*`` name charset."""
        return self.prefix.replace("-", "_")

    @property
    def engine_name(self) -> str:
        return f"{self.underscore_prefix}_engine"

    def policy_name(self, suffix: str) -> str:
        return f"{self.underscore_prefix}_{suffix}"

    @property
    def policy_names(self) -> tuple[str, ...]:
        return tuple(self.policy_name(suffix) for suffix in _POLICY_SUFFIXES)

    @property
    def gateway_name(self) -> str:
        return f"{self.prefix}-pe-gw"

    @property
    def target_name(self) -> str:
        return f"{self.prefix}-echo"

    @property
    def gateway_role_name(self) -> str:
        return f"{self.prefix}-pe-gw-role"

    @property
    def gateway_invoke_policy_name(self) -> str:
        return f"{self.prefix}-pe-invoke"

    @property
    def gateway_authz_engine_policy_name(self) -> str:
        return f"{self.prefix}-pe-authz-engine"

    @property
    def gateway_authz_gateway_policy_name(self) -> str:
        return f"{self.prefix}-pe-authz-gw"

    @property
    def lambda_name(self) -> str:
        return f"{self.prefix}-pe-echo"

    @property
    def log_group_name(self) -> str:
        return f"/aws/lambda/{self.lambda_name}"

    @property
    def lambda_role_name(self) -> str:
        return f"{self.prefix}-pe-echo-role"

    @property
    def lambda_logs_policy_name(self) -> str:
        return f"{self.prefix}-pe-echo-logs"

    @property
    def lambda_permission_id(self) -> str:
        return f"{self.prefix}-pe-gw-invoke"

    @property
    def user_pool_name(self) -> str:
        return f"{self.prefix}-pe-users"

    @property
    def primary_client_name(self) -> str:
        return f"{self.prefix}-pe-primary"

    @property
    def foreign_client_name(self) -> str:
        return f"{self.prefix}-pe-foreign"

    @property
    def allowed_group_name(self) -> str:
        # Group names use the underscore charset so the quoted-element candidate
        # is never broken by a hyphen boundary ambiguity.
        return f"{self.underscore_prefix}_tools"

    def user_name(self, label: str) -> str:
        if label not in SUBJECT_LABELS:
            raise ModelError(f"Unknown subject label {label!r}")
        return f"{self.prefix}-{label}"

    @property
    def all_names(self) -> tuple[str, ...]:
        return (
            self.engine_name,
            self.gateway_name,
            self.target_name,
            self.gateway_role_name,
            self.gateway_invoke_policy_name,
            self.gateway_authz_engine_policy_name,
            self.gateway_authz_gateway_policy_name,
            self.lambda_name,
            self.lambda_role_name,
            self.lambda_logs_policy_name,
            self.lambda_permission_id,
            self.user_pool_name,
            self.primary_client_name,
            self.foreign_client_name,
            *group_layout(self).values(),
            *(self.user_name(label) for label in SUBJECT_LABELS),
            *(self.policy_name(s) for s in _POLICY_SUFFIXES),
        )

    def owns(self, name: str) -> bool:
        """True only for names this prefix could have created."""
        if not isinstance(name, str) or not name:
            return False
        return name.startswith(self.prefix) or name.startswith(self.underscore_prefix)

    def allocation_tags(self) -> dict[str, str]:
        """The five allocation tags every emitted resource carries."""
        return {
            "application-id": self.prefix,
            "agent-id": f"{self.prefix}-policy-engine-spike",
            "tenant-id": "platform",
            "cost-centre": "agentic-ai-platform",
            "environment": "nonprod",
        }


# --------------------------------------------------------------------------
# Secret safety
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
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"eyJ[A-Za-z0-9_-]{6,}"),  # base64url JWT header
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+\S+", re.IGNORECASE),
    re.compile(r"[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{6,}"),
)
_REDACTED = "[redacted]"


def is_secret_key(key: str) -> bool:
    normalized = str(key).lower().replace("_", "").replace("-", "")
    return any(fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS)


def assert_no_secret_values(payload: Any, *, path: str = "$") -> Any:
    """Recursively refuse credential-shaped keys and values before persistence."""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if is_secret_key(str(key)):
                raise SecretLeakError(f"{path}.{key} names a credential field")
            assert_no_secret_values(value, path=f"{path}.{key}")
        return payload
    if isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            assert_no_secret_values(value, path=f"{path}[{index}]")
        return payload
    if isinstance(payload, str):
        for pattern in _SECRET_VALUE_PATTERNS:
            if pattern.search(payload):
                raise SecretLeakError(f"{path} holds a credential-shaped value")
    return payload


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Keep header *names* and drop any value that could carry a credential."""
    return {
        str(key): (_REDACTED if is_secret_key(str(key)) else str(value))
        for key, value in headers.items()
    }


def fingerprint(value: str) -> str:
    """Non-reversible correlation handle for an identifier or a response body."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def sanitize_error(message: str, *, prefix: str) -> str:
    """Reduce an error message to a non-sensitive, bounded diagnostic string.

    Error text can quote ARNs, tokens, or raw request bodies. This keeps only a
    short, single-line summary: any credential-shaped span is dropped, ARNs are
    collapsed to their service, and the result is length-capped and
    scan-verified so the failure record cannot become a leak channel.
    """
    text = " ".join(str(message).split())
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    # Collapse ARNs to arn:...:<service> so identifiers do not leak wholesale.
    text = re.sub(
        r"arn:aws[a-z-]*:([a-z0-9-]+):[^\s\"']*",
        r"arn:aws:\1:[redacted]",
        text,
    )
    if len(text) > 300:
        text = text[:297] + "..."
    # Final guard: if anything credential-shaped survived, drop to a stub.
    try:
        assert_no_secret_values(text)
    except SecretLeakError:
        return f"{prefix}: [error message redacted]"
    return text


_PASSWORD_ALPHABET = string.ascii_lowercase + string.ascii_uppercase + string.digits


def generate_transient_password(length: int = 32) -> str:
    """A single-use password held only in process memory.

    Never returned to evidence, never written to state, never printed. Includes
    one character of each class Cognito's default policy requires. The 16
    character floor keeps it above the blueprint's 12-character platform
    baseline rather than Cognito's own minimum of 8.
    """
    if length < 16:
        raise ModelError("Transient passwords must be at least 16 characters")
    body = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length - 4))
    return (
        body
        + secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%^&*()-_=+")
    )


# --------------------------------------------------------------------------
# Token forgery helpers (adversarial inputs, constructed in memory only)
# --------------------------------------------------------------------------


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    import base64  # local import keeps the module's public surface small

    return base64.urlsafe_b64decode(segment + padding)


def _b64url_encode(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _tamper_token_claims(token: str, updates: Mapping[str, Any]) -> str:
    """Rewrite JWT payload claims while preserving the original signature."""
    import json

    parts = token.split(".")
    if len(parts) != 3:
        raise ModelError("Expected a three-segment compact JWS")
    claims = json.loads(_b64url_decode(parts[1]))
    if not isinstance(claims, dict):
        raise ModelError("Token payload is not a JSON object")
    claims.update(updates)
    forged_payload = _b64url_encode(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return f"{parts[0]}.{forged_payload}.{parts[2]}"


def tamper_token_subject(token: str, new_subject: str) -> str:
    """Rewrite ``sub`` while preserving the original signature bytes."""
    _require(SUBJECT_PATTERN, new_subject, "Forged subject")
    return _tamper_token_claims(token, {"sub": new_subject})


def tamper_token_groups(token: str, groups: Sequence[str]) -> str:
    """Rewrite ``cognito:groups`` while preserving the original signature."""
    for group in groups:
        _require(GROUP_NAME_PATTERN, group, "Forged group")
    return _tamper_token_claims(token, {GROUP_CLAIM_NAME: list(groups)})


def unsigned_token(*, subject: str, issuer: str, client_id: str, expires_at: int) -> str:
    """Build an ``alg=none`` token -- the classic signature-stripping attack."""
    import json

    _require(SUBJECT_PATTERN, subject, "Subject")
    header = {"alg": "none", "typ": "JWT"}
    claims = {
        "sub": subject,
        "iss": issuer,
        "client_id": client_id,
        "token_use": "access",
        "exp": int(expires_at),
    }
    encode = lambda value: _b64url_encode(  # noqa: E731 - tiny local helper
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return f"{encode(header)}.{encode(claims)}."


# --------------------------------------------------------------------------
# MCP response classification
# --------------------------------------------------------------------------

_POLICY_DENIAL_MARKERS = (
    "authorizeactionexception",
    "policy enforcement",
    "denied by default",
    "not allowed due to policy",
)


@dataclass(frozen=True)
class ToolOutcome:
    """A classified tool result that carries no response text."""

    decision: Decision
    reason: str
    body_fingerprint: str


def _result_text(result: Mapping[str, Any]) -> str:
    blocks = result.get("content")
    if not isinstance(blocks, list):
        return ""
    parts = [
        str(block.get("text", ""))
        for block in blocks
        if isinstance(block, Mapping) and block.get("type") in (None, "text")
    ]
    return "\n".join(parts)


def classify_tool_result(payload: Mapping[str, Any]) -> ToolOutcome:
    """Map an MCP ``tools/call`` response to ALLOW or a *policy* DENY.

    Fail closed in both directions: a tool error that does not name a policy
    denial is an error, not a denial (otherwise a broken Lambda would look like
    enforcement), and a success that lacks the deterministic echo marker is an
    error too.
    """
    text = ""
    if "error" in payload and isinstance(payload["error"], Mapping):
        text = str(payload["error"].get("message", ""))
        lowered = text.lower()
        for marker in _POLICY_DENIAL_MARKERS:
            if marker in lowered:
                return ToolOutcome(Decision.DENY, marker, fingerprint(text))
        raise ToolResultError("JSON-RPC error is not a policy denial")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ToolResultError("Response has neither a result nor an error object")
    text = _result_text(result)
    lowered = text.lower()
    if result.get("isError"):
        for marker in _POLICY_DENIAL_MARKERS:
            if marker in lowered:
                return ToolOutcome(Decision.DENY, marker, fingerprint(text))
        raise ToolResultError("Tool reported an error that is not a policy denial")
    if ECHO_MARKER not in text:
        raise ToolResultError("Allowed tool result is missing the echo marker")
    return ToolOutcome(Decision.ALLOW, "echo-marker", fingerprint(text))


def extract_tool_names(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Tool names from a ``tools/list`` response, sorted and de-duplicated."""
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ToolResultError("tools/list response has no result object")
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise ToolResultError("tools/list result has no tools array")
    names = {
        str(tool["name"])
        for tool in tools
        if isinstance(tool, Mapping) and tool.get("name")
    }
    return tuple(sorted(names))


def echo_tool_schema(tool: str) -> dict[str, Any]:
    """Inline ``toolSchema`` entry for one ephemeral echo tool."""
    _require(TOOL_NAME_PATTERN, tool, "Tool name")
    return {
        "name": tool,
        "description": f"Deterministic {tool} probe for the PolicyEngine spike",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Fixed probe string"},
                MODE_FIELD: {"type": "string", "description": "readonly or write"},
                AUDIT_FIELD: {"type": "string", "description": "Audit justification"},
            },
            "required": ["message"],
        },
    }


def region_is_supported(region: str) -> bool:
    return region in POLICY_SUPPORTED_REGIONS


def unknown(code: str, detail: str) -> dict[str, str]:
    """A structured, explicit live unknown for the evidence document."""
    return {"unknown": code, "detail": detail}
