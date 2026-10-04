"""Resolve which IAM **role** actually holds a synthesized policy statement.

Not a test module. This exists because the obvious shortcut is wrong in a way that
silently disables an entire test file, and it did.

Found 2026-09-22 by mutation. ``test_tool_sandbox_grant.py`` answered "is the
deployment Lambda allowed to tag its sandbox function?" by scanning every IAM policy
in the synthesized template, so deleting the sandbox's own ``lambda:TagResource``
statement outright left all eight of its tests green: the assertion was satisfied by
the unrelated **gateway step role's** grant on ``function:AgentCore*``, which happens
to cover the sandbox prefix. IAM does not work that way — a role holds only what is
attached to it — so a template-wide answer to a per-principal question is not an
answer at all. The file had zero coverage of the outage it was written for.

Two rules follow, and both matter:

* **"Can principal P do X?" must be resolved by attachment.** Use
  :func:`statements_for_role`.
* **"May ANYBODY do X?" must stay template-wide.** Over-reach rules like "nothing in
  this account may hold account-wide ``lambda:TagResource``" are properties of the
  whole template; narrowing them to one role would let the next offending grant
  appear elsewhere unnoticed.

Sibling capabilities (``UpdateFunctionCode`` needing ``ListTags``; ``CreateFunction``
needing ``TagResource``) must likewise be paired **by role, not by policy logical
id**. Pairing by policy id is wrong in the strict direction: CDK spills statements
past the inline-policy size limit into ``<Role>OverflowPolicy<N>`` managed policies,
so two grants a single role genuinely holds can sit in different policy documents.

Three attachment shapes are resolved, because CDK uses all of them here:

1. the role's own inline ``Policies``,
2. every ``AWS::IAM::Policy`` whose ``Roles`` list ``Ref``s the role — what CDK builds
   for ``add_to_policy`` until it overflows,
3. every ``AWS::IAM::ManagedPolicy`` attached either through its own ``Roles`` list or
   through the role's ``ManagedPolicyArns``.

``ManagedPolicyArns`` entries that are not a local ``Ref`` are skipped rather than
guessed at: AWS-managed policies such as ``AWSLambdaBasicExecutionRole`` arrive as an
``Fn::Join`` and their documents are not in this template, so nothing can be read from
them. That is the safe direction for an outage test — an action only an AWS-managed
policy grants reads as missing, and a human looks.

ARCC ``cnt_AGx9pUNpmdOVZB`` (scope policies to the necessary actions and resource
ARNs) is the reason the per-principal question is the interesting one: least privilege
is a statement about a principal, so an oracle that cannot name the principal cannot
measure it.
"""

from __future__ import annotations

#: Attachment kinds :func:`resolve_role_policies` reports, so a caller can assert it
#: reached both a base policy and an overflow one instead of only appearing to.
INLINE = "Inline"


def role_logical_id(template_json: dict, prefix: str) -> str:
    """The single ``AWS::IAM::Role`` whose logical id starts with *prefix*.

    Matched by prefix because CDK appends an 8-hex-char hash that changes whenever the
    construct tree above the role moves; a hardcoded ``...Role0C81F80B`` would turn a
    refactor into a silent no-match. Uniqueness is asserted, not assumed: an ambiguous
    match would quietly scope every downstream assertion to the wrong principal, which
    is the same class of defect this module exists to prevent.
    """
    ids = sorted(
        lid
        for lid, res in template_json["Resources"].items()
        if res["Type"] == "AWS::IAM::Role" and lid.startswith(prefix)
    )
    assert len(ids) == 1, (
        f"expected exactly one AWS::IAM::Role whose logical id starts with {prefix!r}, "
        f"found {ids}. Assertions scoped to an ambiguous role stop being evidence."
    )
    return ids[0]


def resolve_role_policies(template_json: dict, role_lid: str) -> tuple[dict[str, str], list[tuple[str, dict]]]:
    """``({policy logical id: attachment kind}, [(policy logical id, statement)])``.

    The sources map is returned alongside the statements so callers can make the
    non-vacuity assertion that both a base policy and an overflow managed policy were
    actually inspected. Without it, a resolver bug that dropped one attachment shape
    would shrink the statement set silently, and every ``for ... assert`` loop over the
    remainder would pass over nothing.
    """
    resources = template_json["Resources"]
    assert role_lid in resources, f"{role_lid} is not a resource in this template"
    props = resources[role_lid].get("Properties", {})

    managed_refs = {
        arn["Ref"] for arn in (props.get("ManagedPolicyArns") or []) if isinstance(arn, dict) and "Ref" in arn
    }

    sources: dict[str, str] = {}
    out: list[tuple[str, dict]] = []

    for idx, policy in enumerate(props.get("Policies") or []):
        src = f"{role_lid}.Policies[{idx}]"
        sources[src] = INLINE
        for st in policy.get("PolicyDocument", {}).get("Statement", []) or []:
            out.append((src, st))

    for lid, res in resources.items():
        if res["Type"] not in {"AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"}:
            continue
        roles = res.get("Properties", {}).get("Roles") or []
        attached = any(isinstance(r, dict) and r.get("Ref") == role_lid for r in roles)
        if not attached and res["Type"] == "AWS::IAM::ManagedPolicy":
            attached = lid in managed_refs
        if not attached:
            continue
        sources[lid] = res["Type"]
        for st in res.get("Properties", {}).get("PolicyDocument", {}).get("Statement", []) or []:
            out.append((lid, st))

    return sources, out


def statements_for_role(template_json: dict, role_lid: str) -> list[tuple[str, dict]]:
    """Just the statements half of :func:`resolve_role_policies`, refusing to be empty.

    A role with no resolvable statements is a resolver failure, never a pass: every
    "is it granted" assertion would then be answered from an empty set.
    """
    sources, statements = resolve_role_policies(template_json, role_lid)
    assert statements, (
        f"no policy statements resolve to {role_lid} (sources seen: {sources}). That is a "
        "resolver failure, not a pass -- grants would all read as missing."
    )
    return statements


def statements_by_role(template_json: dict) -> dict[str, list[tuple[str, dict]]]:
    """Every role in the template mapped to the statements attached to it.

    Used for the "every role that can do X must also be able to do Y" shape, where the
    pairing has to be per-principal: one role holding both halves does not excuse
    another role holding only the dangerous one.

    A role with no statements is kept with an empty list rather than dropped, so a
    caller iterating roles sees it exists; the emptiness itself is never load-bearing
    for a pass here because the tests using this select roles by what they DO grant.
    """
    return {
        lid: resolve_role_policies(template_json, lid)[1]
        for lid, res in template_json["Resources"].items()
        if res["Type"] == "AWS::IAM::Role"
    }
