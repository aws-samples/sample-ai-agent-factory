"""``secretsmanager:TagResource`` on the shared connector prefix must be bounded.

This was the last unconditioned tag grant of its class, and the consequence is the worst of
the set. ``agentcore-connector/`` names the PRODUCT, not a deployment, so every deployment in
the account mints its connector credentials -- raw customer API keys and OAuth2 client secrets
-- under one shared prefix. Teardown therefore finds them by TAG rather than by prefix:
``discover_deployment_bound_secrets`` filters on the name ``agentcore-connector/`` plus a
matching ``AgentCoreStack`` and ``DeploymentId``. ``_put_connector_secret``'s own comment names
the failure the owner tag exists to prevent: "one customer teardown destroying another live
deployment's raw customer API keys".

With ``TagResource`` unconditioned, a gateway step could stamp its own ``AgentCoreStack`` and
``DeploymentId`` onto another live deployment's connector secret, and that deployment's next
teardown would delete it -- reopening exactly that case, for credentials a redeploy cannot
regenerate. The equivalent grants for bedrock-agentcore, lambda and iam were already bounded;
this one was not, and nothing asserted the difference.

The connector prefix is where the class was found, but it was not the only instance. Five
shared prefixes across three roles had an unconditioned ``TagResource``, written by five
different callers sending five different tag sets -- which is why the grants are split BY
PREFIX rather than conditioned in place. Two further traps make the split load-bearing rather
than tidy:

* ``aws:RequestTag`` is absent on ``GetSecretValue``/``DescribeSecret``/``PutSecretValue``, and
  a StringEquals against an absent request tag does not match. Folding the ownership pin onto
  a statement that also carries a read would deny every secret read -- fail-closed, an outage.
* ``bedrock-agentcore-*`` must stay unconditioned. The service writes that namespace on its own
  side during ``CreateOauth2CredentialProvider``; this platform sends no tags there for a
  condition to match. Splitting ``TagResource`` out of a combined statement and forgetting to
  re-grant that prefix looks exactly like a tightening and breaks gateway creation, so both
  directions are asserted below.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform.config import governance_tag_key_globs
from stacks.platform_stack import PlatformStack

TAG_ACTION = "secretsmanager:TagResource"
CONNECTOR = "secret:agentcore-connector/"
_BACKEND_APP = Path(__file__).resolve().parents[2] / "backend" / "src" / "app"
# The keys ``_put_connector_secret`` actually sends:
# ``governed_tag_list(region, resource_tags, extra={"Purpose": ...})`` (which merges
# ``owner_tags`` last -> ManagedBy + AgentCoreStack) plus
# ``secret_binding_tags(owner_sub, deployment_id)`` -> OwnerSubHash + DeploymentId.
# F-01 (b): plus IdentityMode=<shared|per_agent>, stamped at mint time so the shared runtime
# role's GetSecretValue can be conditioned on aws:ResourceTag/IdentityMode=shared.
SENT_KEYS = {"ManagedBy", "AgentCoreStack", "Purpose", "IdentityMode", "OwnerSubHash", "DeploymentId"}
#: The only two values the backend sends. Closed here because the tag is an ABAC input on the
#: shared runtime role's read; any other value mints a secret no runtime can read.
IDENTITY_MODE_VALUES = {"shared", "per_agent"}
# Reads carry no request tags, so a condition on aws:RequestTag would deny them outright.
# These must never share a statement with the conditioned tag grant.
READ_ACTIONS = {"secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"}


@pytest.fixture(scope="module")
def statements() -> list[tuple[str, dict]]:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "ConnectorSecretTagGrantStack",
        environment_name="test",
        project_name="acf",
        env=cdk.Environment(region="us-east-1", account="123456789012"),
    )
    tpl = Template.from_stack(stack).to_json()
    out: list[tuple[str, dict]] = []
    for lid, res in tpl["Resources"].items():
        if res["Type"] not in {"AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"}:
            continue
        for st in res["Properties"]["PolicyDocument"]["Statement"]:
            out.append((lid, st))
    return out


def _actions(st: dict) -> list[str]:
    a = st.get("Action")
    return [a] if isinstance(a, str) else (a or [])


def _resources(st: dict) -> list[str]:
    r = st.get("Resource")
    r = [r] if not isinstance(r, list) else r
    # Resources are Fn::Sub/Fn::Join structures once the account is a token. The prefix we
    # match on is a literal inside them, so flatten to text rather than trying to parse the
    # intrinsic -- a parser that failed to understand one shape would silently match nothing.
    return [str(x) for x in r]


def _tag_statements(statements) -> list[tuple[str, dict]]:
    return [(lid, st) for lid, st in statements if TAG_ACTION in _actions(st) and st.get("Effect") == "Allow"]


def test_the_walk_finds_the_grants_it_is_about(statements):
    """Reach before verdict: a walk that matched nothing would report a compliant stack.

    Two steps mint connector secrets (gateway, mcp_server) and the harness step holds a
    separate secret grant, so anything under three statements means the walk broke rather
    than the stack being clean.
    """
    found = _tag_statements(statements)
    assert len(found) >= 3, f"only {len(found)} {TAG_ACTION} statements were inspected; the walk is broken"
    assert any(any(CONNECTOR in r for r in _resources(st)) for _lid, st in found), (
        f"no {TAG_ACTION} statement mentions {CONNECTOR!r}. Either the connector secret grant "
        "is gone or this walk no longer sees it; both make every assertion below vacuous."
    )


def test_no_unconditioned_tag_grant_on_the_shared_connector_prefix(statements):
    """The assertion that fails on the pre-fix tree.

    Scoped to the connector prefix deliberately. The harness step keeps an unconditioned
    ``TagResource`` on ``bedrock-agentcore-*``, which is a refusal to risk an outage rather
    than an oversight: that namespace is written by CreateOauth2CredentialProvider on the
    service's side, this platform sends no tags of its own there, and whether a
    forward-access-session tag write satisfies an ``aws:RequestTag`` condition is unproven --
    getting it wrong fails closed. It is also not where the escalation lives, because
    teardown's tag discovery only scans ``agentcore-connector/``.
    """
    offenders = []
    for lid, st in _tag_statements(statements):
        if not any(CONNECTOR in r for r in _resources(st)):
            continue
        if not (st.get("Condition") or {}):
            offenders.append(lid)
    assert not offenders, (
        f"{offenders}: {TAG_ACTION} is granted on the shared {CONNECTOR!r} prefix with no "
        "condition. Any deployment holding this role can stamp its own ownership onto another "
        "deployment's connector secret, and that deployment's teardown then deletes it -- raw "
        "customer API keys a redeploy cannot regenerate."
    )


def test_both_ownership_values_are_pinned_not_just_the_product(statements):
    """Pinning ``ManagedBy`` alone leaves the grant useless.

    A request carrying ``ManagedBy=agentcore-flows`` plus an ARBITRARY ``AgentCoreStack``
    satisfies it -- and ``AgentCoreStack`` is the value teardown matches on, so that is the
    forgery. Same reasoning as the bedrock-agentcore grant's own comment.
    """
    checked = 0
    for lid, st in _tag_statements(statements):
        if not any(CONNECTOR in r for r in _resources(st)):
            continue
        eq = (st.get("Condition") or {}).get("StringEquals") or {}
        assert eq.get("aws:RequestTag/ManagedBy") == "agentcore-flows", f"{lid}: ManagedBy not pinned: {eq}"
        stack_pin = eq.get("aws:RequestTag/AgentCoreStack")
        assert stack_pin, f"{lid}: AgentCoreStack is not pinned, so any stack value is accepted: {eq}"
        # The region component must be a policy variable, not a literal: session_for_event
        # honours event["target_region"] using this same role in another region, and a literal
        # would deny every such deploy at CreateSecret.
        assert "aws:RequestedRegion" in str(stack_pin), (
            f"{lid}: the AgentCoreStack pin {stack_pin!r} does not use ${{aws:RequestedRegion}}, "
            "so a cross-region deploy through this role is denied at secret creation"
        )
        checked += 1
    assert checked, "no conditioned connector-secret tag grant was examined"


def test_the_key_allowlist_admits_what_the_code_sends_and_no_wildcard_beyond_the_namespaces(statements):
    """Both directions. Too narrow is an outage; too wide is the escalation.

    ``CreateSecret`` with ``Tags=`` is authorized as ``TagResource`` too, so a key the code
    sends but the allowlist omits does not fail a tagging step -- it fails the creation of the
    credential secret, part-way through a deploy.
    """
    globs = governance_tag_key_globs()
    checked = 0
    for lid, st in _tag_statements(statements):
        if not any(CONNECTOR in r for r in _resources(st)):
            continue
        allowed = ((st.get("Condition") or {}).get("ForAllValues:StringLike") or {}).get("aws:TagKeys")
        assert allowed, f"{lid}: no aws:TagKeys allowlist: {st.get('Condition')}"
        missing = sorted(SENT_KEYS - set(allowed)) + [g for g in globs if g not in allowed]
        assert not missing, (
            f"{lid}: the allowlist {allowed!r} omits {missing!r}. Every key _put_connector_secret "
            "sends must appear, or CreateSecret is denied and the deploy fails after other "
            "resources exist."
        )
        wild = [k for k in allowed if ("*" in k or "?" in k) and k not in globs]
        assert not wild, (
            f"{lid}: wildcard entries {wild!r} outside the declared governance namespaces bound "
            "nothing; the condition becomes decorative"
        )
        checked += 1
    assert checked, "no conditioned connector-secret tag grant was examined"


def test_identity_mode_is_admitted_and_its_value_is_closed_on_every_connector_tag_grant(statements):
    """F-01 (b), both directions. Without ``IdentityMode`` in the allowlist, the backend's
    CreateSecret (authorized as TagResource) is denied and every connector, MCP, LiteLLM and
    gateway client secret fails to mint. With the key admitted but the value open, a wrong
    value would surface later as a GetSecretValue denial on the shared runtime role. The pin
    is safe to require because no Secrets Manager ``tag_resource`` call exists in the backend:
    every TagResource on this prefix is the one ``_put_connector_secret`` CreateSecret."""
    checked = 0
    for lid, st in _tag_statements(statements):
        if not any(CONNECTOR in r for r in _resources(st)):
            continue
        cond = st.get("Condition") or {}
        allowed = (cond.get("ForAllValues:StringLike") or {}).get("aws:TagKeys") or []
        assert "IdentityMode" in allowed, f"{lid}: IdentityMode missing from the aws:TagKeys allowlist {allowed!r}"
        values = (cond.get("StringEquals") or {}).get("aws:RequestTag/IdentityMode")
        values = {values} if isinstance(values, str) else set(values or [])
        assert values == IDENTITY_MODE_VALUES, (
            f"{lid}: aws:RequestTag/IdentityMode not closed to {IDENTITY_MODE_VALUES}: {values}"
        )
        checked += 1
    assert checked >= 3, f"only {checked} connector tag grants inspected (deployment, gateway, mcp_server expected)"


def test_the_conditioned_statement_does_not_carry_the_read_actions(statements):
    """The trap that makes the split necessary rather than tidy.

    ``aws:RequestTag`` is absent on ``GetSecretValue``/``DescribeSecret``, and a StringEquals
    against an absent request tag does not match. Folding the condition onto the combined
    statement would therefore deny every secret READ these steps make -- fail-closed, which is
    an outage rather than a tightening, and it would pass every other assertion in this file.
    """
    for lid, st in _tag_statements(statements):
        if not (st.get("Condition") or {}):
            continue
        clash = READ_ACTIONS & set(_actions(st))
        assert not clash, (
            f"{lid}: {sorted(clash)} share a statement with a conditioned {TAG_ACTION}. Those "
            "actions send no request tags, so the condition denies them and every secret read "
            "in these steps fails."
        )


def test_the_identity_namespace_never_falls_under_an_ownership_pin(statements):
    """The split must stay split, in the direction that causes an outage.

    CDK merges statements whose action sets are equal by unioning their resources, and
    every one of these statements has the action set ``["secretsmanager:TagResource"]``
    exactly -- differing conditions are the only thing keeping them apart. If a merge (or
    a hand-added resource) ever pulls ``bedrock-agentcore-`` under the ownership pin, the
    service's own forward-access-session tag write during
    ``CreateOauth2CredentialProvider`` is evaluated against a condition nothing in the
    request can satisfy, and gateway creation fails part-way through a deploy.

    The opposite direction -- the unconditioned statement absorbing the connector prefix
    -- is the security half, and is caught by the test above.
    """
    for lid, st in _tag_statements(statements):
        if not (st.get("Condition") or {}):
            continue
        pinned = [r for r in _resources(st) if "secret:bedrock-agentcore-" in r]
        assert not pinned, (
            f"{lid}: {pinned!r} sits under an aws:RequestTag condition. That namespace is "
            "written by the service on its own side and this platform sends no tags there, "
            "so the condition cannot be satisfied and credential-provider creation fails."
        )


def test_the_identity_namespace_tag_grant_still_exists_on_the_provider_creators(statements):
    """And the other half: splitting TagResource out must not have dropped that prefix.

    ``gateway_deployer`` calls ``create_api_key_credential_provider`` and
    ``create_oauth2_credential_provider``, and ``harness_deployer`` calls the latter, all
    on step roles; the deployment Lambda's direct-deploy path calls them too. Each needs
    ``secretsmanager:TagResource`` on ``bedrock-agentcore-*`` for the service-side write.
    Removing the action from a combined statement and forgetting to re-grant one prefix is
    a two-line slip that no other test in this file would notice -- it looks exactly like
    a tightening.
    """
    holders = {
        lid
        for lid, st in _tag_statements(statements)
        if not (st.get("Condition") or {}) and any("secret:bedrock-agentcore-" in r for r in _resources(st))
    }
    assert len(holders) >= 3, (
        f"only {sorted(holders)} grant unconditioned {TAG_ACTION} on bedrock-agentcore-*. "
        "The gateway step, the harness step and the deployment Lambda all create credential "
        "providers, so a count below three means one of them lost the grant."
    )


def _writer_tag_keys(module: str, func: str) -> set[str]:
    """The tag keys one ``create_secret`` writer actually sends, read off its own source.

    Hardcoding them here would let the writer and the grant drift apart in the direction
    that hurts: the writer adds a key, the allowlist does not, and the CREATION of a
    credential secret starts failing with an authorization error naming a tagging action
    nobody called -- because ``CreateSecret`` with ``Tags=`` is authorized as
    ``TagResource`` too.

    Both shapes in this codebase are handled: a plain list literal, and
    ``owner_tag_list(region, extra={...})``, whose helper appends ManagedBy and
    AgentCoreStack itself.
    """
    path = _BACKEND_APP / module
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == func):
            continue
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call) and getattr(call.func, "attr", None) == "create_secret"):
                continue
            tags = next((kw.value for kw in call.keywords if kw.arg == "Tags"), None)
            assert tags is not None, f"{module}::{func} calls create_secret with no Tags="
            if isinstance(tags, ast.List):
                keys = set()
                for entry in tags.elts:
                    for k, v in zip(entry.keys, entry.values, strict=True):
                        if isinstance(k, ast.Constant) and k.value == "Key" and isinstance(v, ast.Constant):
                            keys.add(v.value)
                return keys
            # owner_tag_list(region, extra={...}) -> the extra keys plus the two the
            # helper appends last. owner_tags RAISES rather than defaulting when
            # PROJECT_NAME/ENVIRONMENT are unset, so both are always in the request and
            # a plain StringEquals pin on them is safe.
            assert isinstance(tags, ast.Call) and tags.func.id == "owner_tag_list", (
                f"{module}::{func} builds Tags with an unrecognized expression "
                f"{ast.unparse(tags)!r}; this test can no longer derive its key set"
            )
            extra = next((kw.value for kw in tags.keywords if kw.arg == "extra"), None)
            assert isinstance(extra, ast.Dict), f"{module}::{func}: owner_tag_list extra= is not a dict literal"
            return {k.value for k in extra.keys if isinstance(k, ast.Constant)} | {"ManagedBy", "AgentCoreStack"}
    raise AssertionError(f"could not read the create_secret Tags= of {func} in {path}")


#: prefix -> (writer module, writer function, the constant values that must be pinned).
#:
#: Each of these prefixes is shared by every deployment in the account, and each had an
#: UNCONDITIONED ``secretsmanager:TagResource`` grant. The pins differ because the writers
#: differ: the two router namespaces go through ``owner_tag_list`` so AgentCoreStack is
#: always present and pinnable, while the trigger and git secrets are plain literals with
#: no AgentCoreStack at all -- pinning it there would deny every creation.
_BOUNDED_PREFIXES = {
    "secret:agentcore-trigger/": ("routers/triggers.py", "_store_webhook_secret", {"Purpose": "trigger-webhook-hmac"}),
    "secret:agentcore-git/": ("services/git_sync.py", "store_git_token", {"Purpose": "git-sync-token"}),
    "secret:agentcore-otel/": ("routers/observability.py", "store_credentials", {}),
    "secret:agentcore-provider/": ("routers/provider_credentials.py", "store_provider_credential", {}),
}


@pytest.mark.parametrize("prefix", sorted(_BOUNDED_PREFIXES))
def test_each_shared_secret_prefix_is_bounded_to_the_keys_its_writer_sends(statements, prefix):
    """One statement per prefix, and each allowlist equals its writer's key set exactly.

    Equality in both directions is the point. Too narrow denies the creation of a
    credential secret part-way through a request. Too wide is a tag this role may stamp on
    a shared-prefix secret for no reason the code needs -- and tags carry ABAC decisions
    (ARCC cnt_6gBImtb08AJqCB), so an unused allowlist entry is standing authority.

    What this does NOT enforce, for the two owner-scoped prefixes: ``owner_sub`` is
    per-user and these roles are shared by every caller of the API, so IAM has no
    principal-tag binding that could stop one user's request from writing another's sub.
    The routers enforce that by taking owner_sub from the authenticated caller.
    """
    module, func, pins = _BOUNDED_PREFIXES[prefix]
    sent = _writer_tag_keys(module, func)
    assert sent, f"{module}::{func} parsed to an empty key set; this case proves nothing"
    checked = 0
    for lid, st in _tag_statements(statements):
        if not any(prefix in r for r in _resources(st)):
            continue
        cond = st.get("Condition") or {}
        eq = cond.get("StringEquals") or {}
        assert eq.get("aws:RequestTag/ManagedBy") == "agentcore-flows", f"{lid}: ManagedBy not pinned: {eq}"
        for key, value in pins.items():
            assert eq.get(f"aws:RequestTag/{key}") == value, (
                f"{lid}: {key} is not pinned to {value!r}: {eq}. The delete gate for this "
                "prefix requires that exact value, so leaving it open lets the role mint a "
                "secret its own teardown will then refuse to remove."
            )
        if "AgentCoreStack" in sent:
            stack_pin = eq.get("aws:RequestTag/AgentCoreStack")
            assert stack_pin, f"{lid}: the writer sends AgentCoreStack but it is not pinned: {eq}"
            assert "aws:RequestedRegion" in str(stack_pin), (
                f"{lid}: the AgentCoreStack pin {stack_pin!r} hardcodes a region, so the same "
                "role operating in another region is denied at secret creation"
            )
        allowed = (cond.get("ForAllValues:StringLike") or {}).get("aws:TagKeys")
        assert allowed, f"{lid}: no aws:TagKeys allowlist on {prefix!r}: {cond}"
        missing = sorted(sent - set(allowed))
        assert not missing, (
            f"{lid}: the allowlist {allowed!r} omits {missing!r}, which {module}::{func} "
            "sends. CreateSecret would be denied and the request would fail."
        )
        surplus = sorted(set(allowed) - sent)
        assert not surplus, f"{lid}: the allowlist admits {surplus!r}, which {module}::{func} does not send"
        checked += 1
    assert checked, f"no {TAG_ACTION} statement covers {prefix!r}; its writer's CreateSecret would fail"


def test_the_reads_are_still_granted_somewhere(statements):
    """And the other half of that trap: the split must not have dropped the reads.

    Moving TagResource out of the combined statement is a two-line edit that can just as
    easily delete the reads. Without this, the test above is satisfied by a stack that cannot
    read a secret at all.
    """
    for action in sorted(READ_ACTIONS):
        granted = [
            lid
            for lid, st in statements
            if action in _actions(st)
            and st.get("Effect") == "Allow"
            and not (st.get("Condition") or {})
            and any("secret" in r for r in _resources(st))
        ]
        assert granted, f"{action} is granted by no unconditioned statement; secret reads would fail"


def _covered_prefixes(st: dict) -> set[str]:
    """Which bounded prefixes a statement's resource GLOBS reach, not which it spells out.

    This is the whole point of the function. Every other check in this file matches a prefix
    by substring, and substring matching cannot see that ``secret:agentcore-*`` reaches
    ``secret:agentcore-git/``: the literal is absent, so the statement looks unrelated to
    the git prefix and every per-prefix assertion skips it. Expand the glob instead.
    """
    globs: set[str] = set()
    for r in _resources(st):
        globs.update(re.findall(r":secret:([A-Za-z0-9!\-/*]*)", r))
    reached = set()
    for prefix in _ALL_BOUNDED:
        bare = prefix.removeprefix("secret:")
        for g in globs:
            stem = g[:-1] if g.endswith("*") else g
            if bare.startswith(stem) or stem.startswith(bare):
                reached.add(prefix)
    return reached


_ALL_BOUNDED = set(_BOUNDED_PREFIXES) | {CONNECTOR}


def test_no_conditioned_tag_grant_spans_two_writers_prefixes(statements):
    """A per-prefix allowlist on one role is worthless if another role's glob spans it.

    The five bounded prefixes have five different writers sending five different key sets,
    and each conditioned statement's allowlist is EQUAL to exactly one of them. So a
    statement whose resources reach two of those prefixes is, by construction, granting one
    writer's key set over another writer's secret -- here it let the gateway and mcp_server
    steps stamp ``DeploymentId``, ``OwnerSubHash`` and the ``platform:``/``org:`` governance
    namespaces onto a git token or a provider credential. Tags carry ABAC decisions and
    teardown matches on them, so that is a tag-integrity boundary, not a reporting detail.

    Found only by reading the SYNTHESIZED template: ``secret:agentcore-*`` was left in the
    conditioned statement when ``TagResource`` was split out of the combined one, and
    because no per-prefix check matches a glob against a prefix it does not literally
    contain, all twelve assertions above passed over it.
    """
    spanning = {
        lid: sorted(reached)
        for lid, st in _tag_statements(statements)
        if (st.get("Condition") or {}) and len(reached := _covered_prefixes(st)) > 1
    }
    assert not spanning, (
        "conditioned TagResource statements reach more than one bounded prefix, so one "
        f"writer's key allowlist governs another writer's secret: {spanning!r}"
    )


def test_the_glob_expansion_actually_reaches_something(statements):
    """Reach before verdict, for the check above: an expander that matched nothing passes it.

    ``_covered_prefixes`` is a regex over a flattened intrinsic. If the flattening shape
    changes, it returns the empty set for every statement and the span test above reports a
    clean stack forever. Pin that the connector prefix -- which is spelled out literally in
    two conditioned statements -- is still reached through the expansion path.
    """
    reaching = [lid for lid, st in _tag_statements(statements) if CONNECTOR in _covered_prefixes(st)]
    assert len(reaching) >= 3, (
        f"the glob expander reached {CONNECTOR!r} from only {len(reaching)} statements "
        "(deployment, gateway and mcp_server roles each hold one); it is broken"
    )


def test_no_tag_grant_reaches_the_unwritten_capital_namespace(statements):
    """``secret:AgentCore*`` has no writer, so no role may stamp tags on it.

    A different namespace to ``agentcore-*``: IAM resource matching is case-sensitive. It was
    held for two review rounds because narrowing a grant with an unproven blast radius risks
    an outage, and three independent oracles then showed it unreachable -- no writer in an AST
    pass over every ``create_secret`` in the backend, no Secrets Manager name constructed with
    that prefix anywhere in the repo (the capital-A literals are a DynamoDB table, a Memory,
    an IAM Sid, two Cognito pool names and FUNCTION_NAME_PREFIX), and zero instances in a live
    account that does hold real secrets under the neighbouring prefixes.

    It mattered most on the deployment role, where the grant was UNCONDITIONED: standing
    authority to stamp ownership tags on a namespace any principal in the account can create a
    secret in.
    """
    offenders = [lid for lid, st in _tag_statements(statements) if any("secret:AgentCore" in r for r in _resources(st))]
    assert not offenders, (
        f"{offenders}: {TAG_ACTION} is granted on secret:AgentCore*, which no writer in the "
        "backend ever creates. An unwritten namespace needs no tag action."
    )


def test_narrowing_that_namespace_did_not_touch_the_other_actions(statements):
    """The other direction, so the check above cannot be satisfied by deleting too much.

    Only the TAG action was narrowed. A grant to STAMP OWNERSHIP on a namespace any principal
    can populate is a different blast radius from an inert read on a namespace nothing
    populates, and removing both at once would conflate them -- which is how a tightening
    turns into the outage the previous two review rounds were right to avoid.

    Counted per POLICY, not "at least one statement anywhere". An any-match here passed while a
    mutant deleted the deployment role's grant, because three unrelated statements in
    step_lambdas.py still mentioned the prefix and satisfied it. Four policies hold one -- the
    deployment, gateway and mcp_server roles (create/read/put/delete) and the status_update role
    (delete/describe for teardown) -- so the count is the assertion.
    """
    holders = {
        lid
        for lid, st in statements
        if st.get("Effect") == "Allow"
        and TAG_ACTION not in _actions(st)
        and any(a.startswith("secretsmanager:") for a in _actions(st))
        and any("secret:AgentCore" in r for r in _resources(st))
    }
    assert len(holders) >= 4, (
        f"only {len(holders)} policies still hold a non-tag secretsmanager grant on "
        f"secret:AgentCore* ({sorted(holders)}); four did. The narrowing was meant to remove the "
        "TAG action only -- the create/read/delete and teardown grants were not in scope."
    )
