"""The grant that makes the F-7 tool-Lambda ownership check work, and cannot be abused.

``gateway_deployer._authorize_tool_function_replacement`` refuses to replace a
function's code unless a tag proves the function belongs to this deployment. That only
works if the deploy role can (a) READ the tag and (b) WRITE it at creation. Neither
action was on the deployed role — measured, not assumed: the live
``acfe2e-p0920-StepGatewayRoleAAFE0C07-m9tHG9ZTMy3k`` inline policy granted
``CreateFunction``/``UpdateFunctionCode``/``UpdateFunctionConfiguration`` on
``function:AgentCore*`` and neither ``ListTags`` nor ``TagResource``.

So this is a change where the code and the grant MUST ship together, in both directions:

* Without ``lambda:ListTags`` the authorizer's AccessDenied branch fires and EVERY
  gateway deploy that reuses the shared singleton tool Lambda refuses. Total outage,
  not a silent weakening — which is the direction the authorizer deliberately chose.
* Without ``lambda:TagResource`` the ``create_function(Tags=...)`` call fails, and
  there is no retry-untagged fallback on this path on purpose, so the gateway step
  fails outright. ``Tags=`` on a create is authorized as ``lambda:TagResource`` on the
  function being created, not as part of ``lambda:CreateFunction`` — the third time
  this repo has paid for that (see ``test_tool_sandbox_grant.py`` for the first two:
  the sandbox function, then ``logs:TagResource`` under ``CreateLogGroup``).

**Why TagResource is conditioned — two separate properties, and only one of them was
here until 2026-09-22.**

*The key allowlist.* The tag-based grant is only as strong as the platform's inability
to write the tag it reads. A sibling statement in the same role allows
``lambda:AddPermission`` on ANY function carrying ``AgentCoreGatewayTarget=allow`` (the
BYO-Lambda opt-in, see ``test_byo_lambda_grant_is_opt_in_only.py``). An UNCONDITIONED
``lambda:TagResource`` on ``function:AgentCore*`` would let the platform write that
opt-in tag onto a foreign ``AgentCore*`` function and self-grant the very capability the
opt-in tag exists to gate. ``ForAllValues:StringEquals`` on ``aws:TagKeys`` denies the
WHOLE request when any key falls outside the list, so that particular chain really is
closed by the key allowlist — that claim is kept because it is true of *that* chain.

*The owner value.* What the key allowlist does NOT cover is the VALUE of
``AgentCoreStack``, which was left caller-chosen. That is a different hole and it was
real: with an arbitrary value this role could stamp ANOTHER deployment's stack id onto a
function, and ``AgentCoreStack`` is exactly what teardown and
``assert_this_deployment_may_mutate`` match on — so ownership could be forged (make a
sibling deployment's teardown delete a function) or overwritten (take one over). Both
values are now pinned, with the region as ``${aws:RequestedRegion}`` so the pin stays
exact if the statement is ever widened past the home region.

**What the conditions do NOT buy, stated so no test name below implies it.**
``lambda:TagResource`` has no create-only / called-from-create condition key: its only
keys are ``aws:RequestTag/${TagKey}`` and ``aws:TagKeys``. So a dependent tag-on-create
is indistinguishable in policy from a standalone retag, and this role can still stamp
*our own* ownership pair onto an untagged foreign function whose name starts with
``AgentCore``. A request tag bounds which VALUE may be written, not which RESOURCE it
lands on. What is bought, exactly: no caller-chosen owner value, no key outside the
ownership pair, and the value bound to the region the call is actually made in.

Both keys are supported on ``lambda:TagResource`` and ``lambda:CreateFunction`` —
confirmed against the AWS Service Reference feed
(``servicereference.us-east-1.amazonaws.com/v1/lambda/lambda.json``), which is the
oracle for whether an action or condition key exists. The docs are not. The same feed is
what establishes the absence of a create-only key above.

The operator is plain ``StringEquals`` and must stay that way: ARCC
``cnt_SFJJhkOueCPRkd`` records that ``IfExists`` evaluates the condition as TRUE when
the key is absent from the request, so ``StringEqualsIfExists`` here would authorize a
request that sends no ownership tags at all.

The allowed key list is checked against ``resource_ownership.owner_tags``'s own source
rather than transcribed, because the failure mode of a drift here is a DENY at deploy
time on a code path with no fallback. That is also the deliberate tripwire: adding a
third governance key via ``owner_tags(extra=...)`` breaks this test instead of shipping
a role that denies the create.
"""

import fnmatch
import pathlib
import re

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform.config import governance_tag_key_globs
from stacks.platform_stack import PlatformStack

from tests.iam_attachment import statements_by_role

REGION = "us-east-1"
ACCOUNT = "123456789012"
PROJECT = "acf"
ENV = "test"

#: The exact owner value the grants must pin, DERIVED from the synth inputs rather than
#: hardcoded, so a test that stops tracking the stack it is generated for fails instead
#: of passing. ``resource_ownership.stack_id`` builds ``{project}-{env}-{region}``, and
#: the region component is the IAM policy variable so the pin survives a non-home target
#: region -- ``${aws:RequestedRegion}`` resolves to whatever region the call is made in,
#: which is the same region ``owner_tags(region)`` was given (the caller threads one
#: region into both the boto3 client and the tag value).
EXPECTED_OWNER_VALUE = f"{PROJECT}-{ENV}-${{aws:RequestedRegion}}"

_REPO = pathlib.Path(__file__).resolve().parents[2]
_OWNERSHIP_PY = _REPO / "backend" / "src" / "app" / "services" / "resource_ownership.py"
_GATEWAY_DEPLOYER_PY = _REPO / "backend" / "src" / "app" / "services" / "gateway_deployer.py"

#: Actions that replace what a function DOES. ARCC cnt_pXauQr9E6bKwke names
#: lambda:UpdateFunctionCode as a privilege-escalation primitive: the code runs with the
#: permissions of the function's existing execution role. cnt_L4ZLZgjrCctfxl lists it as
#: named escalation pattern 3. Any role holding one of these on a name pattern it does
#: not exclusively own needs the ownership read to go with it.
CODE_REPLACING = {"lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration"}


@pytest.fixture(scope="module")
def template_json():
    app = cdk.App()
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name=ENV,
        project_name=PROJECT,
        env=cdk.Environment(region=REGION, account=ACCOUNT),
    )
    return Template.from_stack(stack).to_json()


def _statements(template_json: dict) -> list[tuple[str, dict]]:
    """(logical id, statement) for every statement in every IAM policy/role.

    Includes ``AWS::IAM::ManagedPolicy`` because CDK spills statements past the
    10,240-character inline-policy limit into ``<Role>OverflowPolicy<N>`` managed
    policies — a scan that looked only at ``AWS::IAM::Policy`` would miss whichever
    statements happened to land past the cut and pass vacuously.
    """
    out: list[tuple[str, dict]] = []
    for lid, res in template_json["Resources"].items():
        if res["Type"] in {"AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"}:
            docs = [res["Properties"].get("PolicyDocument", {})]
        elif res["Type"] == "AWS::IAM::Role":
            docs = [p.get("PolicyDocument", {}) for p in res["Properties"].get("Policies", []) or []]
        else:
            continue
        for doc in docs:
            for st in doc.get("Statement", []) or []:
                out.append((lid, st))
    return out


def _actions(st: dict) -> list[str]:
    act = st.get("Action")
    if isinstance(act, str):
        return [act]
    return [a for a in (act or []) if isinstance(a, str)]


def _resource_strings(st: dict) -> list[str]:
    """Resource entries flattened to strings, resolving ``Fn::Join`` of literals.

    The synthesized ARNs are ``Fn::Join`` over literals plus ``AWS::Partition``, so a
    naive string check sees no resource at all and every assertion passes vacuously.
    """
    res = st.get("Resource")
    entries = [res] if not isinstance(res, list) else res
    out: list[str] = []
    for e in entries:
        if isinstance(e, str):
            out.append(e)
        elif isinstance(e, dict) and "Fn::Join" in e:
            _sep, parts = e["Fn::Join"]
            out.append(_sep.join(p if isinstance(p, str) else "<ref>" for p in parts))
        else:
            out.append(repr(e))
    return out


def _owner_tag_keys() -> set[str]:
    """The tag keys ``owner_tags()`` actually stamps, read from its own source.

    Parsed rather than transcribed: ``owner_tags`` sets exactly
    ``tags[PRODUCT_TAG_KEY]`` and ``tags[OWNER_TAG_KEY]`` after dropping any caller
    ``extra`` that collides, so the keys are whatever those two constants say.
    """
    src = _OWNERSHIP_PY.read_text()
    keys = set()
    for const in ("OWNER_TAG_KEY", "PRODUCT_TAG_KEY"):
        m = re.search(rf'^{const} = "([^"]+)"', src, re.M)
        assert m, f"{const} not found in resource_ownership.py -- did it move or get renamed?"
        keys.add(m.group(1))
    # Pin the shape too: if owner_tags starts writing a third key, the DENY would land
    # at deploy time on a path with no fallback, so fail here instead.
    body = src[src.index("def owner_tags(") : src.index("def owner_tag_list(")]
    assigned = set(re.findall(r"tags\[([A-Z_]+)\]", body))
    assert assigned == {"OWNER_TAG_KEY", "PRODUCT_TAG_KEY"}, (
        f"owner_tags() now writes {sorted(assigned)}. Widen the aws:TagKeys allowlist in "
        "infra/stacks/platform/step_lambdas.py in the SAME change, or lambda:TagResource "
        "will be denied and the gateway step will fail outright (no retry-untagged fallback)."
    )
    # F-7d: the per-deployment / per-scope BINDING keys gateway_deployer passes through
    # owner_tags(extra=...) / extra_tags on a tool Lambda create. Read off the source, so a
    # new binding key lands in the grant in the same change or this fails, and a key in the
    # grant that no create writes is caught as extra authority by the caller.
    gd = _GATEWAY_DEPLOYER_PY.read_text()
    # Only the forms a tool-Lambda create uses: ``extra_tags={"K": ...}`` (the shared and
    # custom functions), ``Tags=<tagger>(region, ..., extra={"K": ...})`` (the KB function) and
    # ``scope_tags = {"K": ...}`` (the custom-tool binding). Other extra=... callers in the
    # module tag secrets and roles through other grants and are not Lambda tag writes.
    #
    # The tagger name is matched loosely (``owner_tags`` OR ``governed_tags``) on purpose.
    # P0-B moved these call sites from ``owner_tags(region, extra=...)`` to
    # ``governed_tags(region, resource_tags, extra=...)``, and a regex pinned to the old
    # name would have matched NOTHING and silently dropped DeploymentId out of the expected
    # key set -- which reads as "the grant authorizes a key no create writes", i.e. it would
    # have reported extra authority that does not exist while hiding the real binding key.
    keys |= set(re.findall(r'extra_tags=\{\s*"([A-Za-z]+)"\s*:', gd))
    keys |= set(re.findall(r'Tags=(?:owner|governed)_tags\(region,[^)]*?extra=\{\s*"([A-Za-z]+)"\s*:', gd))
    keys |= set(re.findall(r'scope_tags = \{\s*"([A-Za-z]+)"\s*:', gd))
    assert "DeploymentId" in keys, (
        "no per-deployment binding key was found in gateway_deployer.py's tool-Lambda tag "
        "writes. The source forms this test matches on have changed; fix the patterns above "
        "rather than letting the expected key set shrink silently."
    )
    return keys


def _tool_function_names() -> set[str]:
    """The concrete tool-Lambda names ``gateway_deployer`` creates, read from its source.

    Matching on the substring ``:function:AgentCore`` is NOT good enough and a surviving
    mutant proved it: deleting the whole ``function:AgentCore*`` TagResource grant left
    every assertion here passing, because the tool-test sandbox's unrelated
    ``function:AgentCore-ToolTest-*`` statement contains that substring too. So resolve
    real names and require a pattern that actually covers them.
    """
    # F-7d: every tool Lambda is named by the backend's naming helpers with the stack token,
    # so the concrete names are computed with THOSE helpers (loaded from the backend tree,
    # not re-implemented here) for the identity this template is synthesized with. The
    # source is still checked for the four kinds, so a fifth kind added to the backend
    # without a grant is caught rather than silently unmatched.
    import importlib.util

    spec = importlib.util.spec_from_file_location("backend_naming", _GATEWAY_DEPLOYER_PY.parent / "naming.py")
    naming = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(naming)
    src = _GATEWAY_DEPLOYER_PY.read_text()
    kinds = set(re.findall(r'scoped_function_name\(\s*"([A-Za-z]+)"', src))
    shared = re.search(r"_SHARED_TOOL_KINDS = \(([^)]*)\)", src)
    assert shared, "_SHARED_TOOL_KINDS not found in gateway_deployer.py"
    kinds |= set(re.findall(r'"([A-Za-z]+)"', shared.group(1)))
    assert kinds == {"DynamicTools", "CustomerSupportTools", "KBTool", "CustomTool"}, (
        f"tool Lambda kinds in gateway_deployer.py changed ({sorted(kinds)}); update this test and "
        "check the IAM prefix still covers every kind."
    )
    stack_identity = f"{PROJECT}-{ENV}-{REGION}"
    names = {naming.scoped_function_name(kind, stack_identity) for kind in ("DynamicTools", "CustomerSupportTools")}
    names.add(naming.scoped_function_name("KBTool", stack_identity, "0" * 12))
    names.add(naming.scoped_function_name("CustomTool", stack_identity, "tool-" + "0" * 12))
    return names


def _covers(resource: str, function_name: str) -> bool:
    """True when an IAM resource ARN reaches *function_name*."""
    marker = ":function:"
    if resource == "*":
        return True
    if marker not in resource:
        return False
    return fnmatch.fnmatchcase(function_name, resource.split(marker, 1)[1])


def _tool_lambda_statements(template_json: dict, action: str) -> list[tuple[str, dict]]:
    """Statements granting *action* on a pattern covering EVERY tool-Lambda name.

    Template-wide, and only sound for the questions it is used for below: "does this
    capability exist at all" and "is it ever granted too broadly". It cannot answer
    "does the gateway step role hold it" -- that needs :func:`_names_reached` over the
    statements actually attached to a role.
    """
    names = _tool_function_names()
    return [
        (lid, st)
        for lid, st in _statements(template_json)
        if action in _actions(st)
        and st.get("Effect", "Allow") == "Allow"
        and all(any(_covers(r, n) for r in _resource_strings(st)) for n in names)
    ]


#: One concrete function a grant reaches, in every ARN dimension that matters:
#: ``(partition, region, account, function name)``. Pairing on the NAME alone is not
#: enough and the hole is not hypothetical -- with only names compared, role A holding
#: ``CreateFunction`` in eu-west-1 and ``TagResource`` in us-east-1 while role B holds the
#: mirror image looks fine per-role AND nets out to equal region sets template-wide, so
#: neither oracle sees it. A grant in the wrong account passes just as silently. The
#: partition renders as ``<ref>`` for every ARN here (``Fn::Join`` over
#: ``AWS::Partition``), so it is compared like-for-like rather than resolved.
_Reach = tuple[str, str, str, str]


def _reach(statements: list[tuple[str, dict]], wanted: set[str], names: set[str]) -> set[_Reach]:
    """Every (partition, region, account, name) these statements grant *wanted* on."""
    out: set[_Reach] = set()
    for _lid, st in statements:
        if st.get("Effect", "Allow") != "Allow":
            continue
        if not (wanted & set(_actions(st))):
            continue
        for r in _resource_strings(st):
            hit = {n for n in names if _covers(r, n)}
            if not hit:
                continue
            if r == "*":
                # Reaches every function in every account and region. Kept as literal
                # "*" per dimension so a narrower sibling does NOT cover it -- which is
                # the correct verdict: an unscoped create paired with a scoped tag grant
                # is a create the caller cannot tag.
                out |= {("*", "*", "*", n) for n in hit}
                continue
            parts = r.split(":", 5)
            if len(parts) < 6 or parts[2] != "lambda":
                continue
            _arn, partition, _svc, region, account, _res = parts
            out |= {(partition, region or "*", account or "*", n) for n in hit}
    return out


def _is_covered(target: _Reach, siblings: set[_Reach]) -> bool:
    """True when some sibling grant reaches *target* in every ARN dimension.

    Dimension-wise ``fnmatch``, so a sibling pinned to region ``*`` legitimately covers
    a create pinned to one region, while the reverse (a wildcard create, a pinned tag)
    does not -- the asymmetry is the point.

    ``strict=True`` matters here rather than being lint appeasement: both sides are
    ``_Reach`` 4-tuples, and a short zip would compare only the leading dimensions and
    silently report a cross-account or cross-region grant as covered -- the very defect
    this function was written to catch.
    """
    return any(all(fnmatch.fnmatchcase(t, s) for t, s in zip(target, sibling, strict=True)) for sibling in siblings)


# ---------------------------------------------------------------------------
# The read
# ---------------------------------------------------------------------------


def test_every_role_that_can_replace_tool_lambda_code_can_also_read_its_tags(template_json) -> None:
    """The pairing, asserted as a pairing rather than as a literal action list.

    A role that can overwrite code on ``AgentCore*`` but cannot read a tag cannot tell
    its own function from anyone else's. Before this change every such role was in that
    position, and the code that now refuses would refuse on every deploy.

    **Paired by attached ROLE, not by policy logical id (fixed 2026-09-22).** The first
    version required both halves to appear under the same policy id, which is wrong in
    both directions. Too strict: CDK spills statements past the inline-policy size limit
    into ``<Role>OverflowPolicy<N>`` managed policies, so a role genuinely holding
    ``UpdateFunctionCode`` and ``ListTags`` can hold them in two different documents and
    would have been reported as an offender. Too loose, and this is the one that bit the
    sibling file: an ``AWS::IAM::Policy`` may attach to several roles, so a match on
    "some policy" says nothing about which principal can actually make the call. IAM
    evaluates the union of what is attached to ONE role, so that is the unit here.
    """
    names = _tool_function_names()
    by_role = statements_by_role(template_json)
    assert by_role, "the template contains no IAM roles at all -- resolver failure"

    offenders = []
    considered = 0
    for role_lid, statements in by_role.items():
        reached = _reach(statements, CODE_REPLACING, names)
        if not reached:
            continue
        considered += 1
        readable = _reach(statements, {"lambda:ListTags", "lambda:*"}, names)
        gaps = sorted(t for t in reached if not _is_covered(t, readable))
        if gaps:
            offenders.append((role_lid, gaps))
    assert not offenders, (
        "these ROLES can replace the code of a tool Lambda without being able to read its "
        f"ownership tag (role, unreadable (partition, region, account, name)): {offenders}. A "
        "grant held by a different role does not help this one -- IAM evaluates only what is "
        "attached -- and neither does one in another region or account."
    )
    assert considered, (
        "no role holds lambda:UpdateFunctionCode/UpdateFunctionConfiguration on any tool "
        f"Lambda name ({sorted(names)}), so this test proved nothing. Either the gateway "
        "step lost the grant it needs to replace the shared tool Lambda's code -- a total "
        "outage of that path -- or _tool_function_names/_covers stopped matching the real "
        "ARNs. Both are failures, not passes."
    )


def test_every_role_that_creates_a_tool_lambda_can_also_tag_it(template_json) -> None:
    """Tag-on-create is one request, so one role must hold both halves.

    The existence tests below prove the capability is in the template somewhere. That is
    not the same claim, and the difference is not academic: the sibling file's
    ``test_tool_sandbox_grant.py`` shipped for a week with an "is it granted" assertion
    that a *different* role's grant satisfied, so deleting the real statement changed
    nothing (see that file's docstring). ``create_function(Tags=...)`` is authorized as
    ``CreateFunction`` **and** ``TagResource`` against the same caller in the same
    request, and ``gateway_deployer`` has no retry-untagged fallback, so a role holding
    only ``CreateFunction`` fails the gateway step outright.
    """
    names = _tool_function_names()
    by_role = statements_by_role(template_json)

    offenders = []
    considered = 0
    for role_lid, statements in by_role.items():
        created = _reach(statements, {"lambda:CreateFunction"}, names)
        if not created:
            continue
        considered += 1
        taggable = _reach(statements, {"lambda:TagResource", "lambda:*"}, names)
        gaps = sorted(t for t in created if not _is_covered(t, taggable))
        if gaps:
            offenders.append((role_lid, gaps))
    assert not offenders, (
        "these ROLES can create a tool Lambda but cannot tag it (role, untaggable "
        f"(partition, region, account, name)): {offenders}. Tags= on a create is authorized "
        "as lambda:TagResource on the new function IN THE SAME REQUEST, so the two grants must "
        "agree on account and region as well as name; without that the gateway step fails at "
        "create_function with no retry-untagged fallback. Narrow the create to where the tag "
        "grant reaches rather than widening the tag grant, unless a reachable production call "
        "site needs the wider name -- name it if so."
    )
    assert considered, (
        f"no role holds lambda:CreateFunction on any tool Lambda name ({sorted(names)}). "
        "The gateway step cannot create the shared tool Lambda at all, which is a total "
        "outage -- or the name/ARN matching in this file has drifted. Not a pass."
    )


def test_the_authorizer_reads_tags_with_a_separate_api_call(template_json) -> None:
    """``ListTags``, not ``GetFunction``'s ``Tags`` field — and the grant must match.

    ``GetFunction`` returns a ``Tags`` map, so the check could have been written without
    a second action. It deliberately was not: a missing ``lambda:ListTags`` grant then
    surfaces as an AccessDenied the authorizer REFUSES on, instead of an empty tag map
    that would make every function look unowned, make the "belongs to another
    deployment" refusal unreachable, and leave every deploy green. This test pins both
    halves so nobody "simplifies" the read back into ``GetFunction``.
    """
    src = _GATEWAY_DEPLOYER_PY.read_text()
    body_start = src.index("def _authorize_tool_function_replacement(")
    body = src[body_start : src.index("\ndef ", body_start + 1)]
    assert "list_tags(" in body, (
        "_authorize_tool_function_replacement no longer calls list_tags. If it now reads "
        "GetFunction's Tags field, a missing grant reads as 'untagged' and the refusal "
        "branch becomes unreachable while every deploy still succeeds."
    )
    assert _tool_lambda_statements(template_json, "lambda:ListTags"), (
        "no policy grants lambda:ListTags on function:AgentCore*"
    )


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------


def test_tool_lambdas_can_be_tagged_at_creation(template_json) -> None:
    assert _tool_lambda_statements(template_json, "lambda:TagResource"), (
        "no policy grants lambda:TagResource on function:AgentCore*. create_function(Tags=...) "
        "is authorized as lambda:TagResource on the new function, and gateway_deployer has no "
        "retry-untagged fallback, so the gateway step fails outright without this."
    )


def test_every_lambda_tag_write_is_pinned_to_this_deployments_own_ownership_pair(template_json) -> None:
    """Both halves: the key allowlist AND the owner value.

    Without the key allowlist, ``lambda:TagResource`` on ``function:AgentCore*`` lets the
    platform stamp ``AgentCoreGatewayTarget=allow`` onto any foreign function whose name
    starts with ``AgentCore`` and thereby self-grant the ``lambda:AddPermission``
    capability that opt-in tag gates.

    Without the owner-value pin, ``AgentCoreStack`` stays caller-chosen, which the key
    allowlist does not cover: this role could then write a DIFFERENT deployment's stack id
    onto a function, and that id is what teardown matches on. It does not follow that the
    grant cannot touch a foreign function at all -- see the module docstring for what the
    conditions genuinely buy.

    Deliberately asserted over EVERY ``lambda:TagResource`` statement rather than only
    the tool-Lambda one, because scope is not the control here: the tool-test sandbox's
    ``function:AgentCore-ToolTest-*`` grant is much narrower and had the identical hole
    (the live account holds 57 pre-existing groups under that prefix and a foreign
    ``AgentCoreToolTestRole``, so the prefix is not ours alone either).

    The two statements no longer send the same tag set, which is why the expectation
    below forks. ``gateway_deployer``'s tool-Lambda creates went from ``owner_tags()`` to
    ``governed_tags()`` for P0-B, so that statement must also admit the governance
    NAMESPACES -- admin-created keys cannot be enumerated at synth time. ``tool_tester``
    deliberately did NOT follow (a tool test has no deployment to attribute and a
    required-without-default policy key would fail-closed the whole feature), so the
    sandbox statement must still admit the bare ownership pair and nothing else. If
    tool_tester is ever converted, this test fails and names the grant to widen.

    What replaces exact-key enumeration on the forked side is the wildcard rule at the
    end: the ONLY glob entries permitted are the declared governance namespaces, each of
    which ends in ``:*``. That is what keeps the part-1 escalation closed --
    ``AgentCoreGatewayTarget`` is unnamespaced, so no admitted pattern can match it.
    """
    expected_keys = _owner_tag_keys()
    stmts = [
        (lid, st)
        for lid, st in _statements(template_json)
        if "lambda:TagResource" in _actions(st) and st.get("Effect", "Allow") == "Allow"
    ]
    assert stmts, "no lambda:TagResource grant at all -- tag-on-create cannot work"
    for lid, st in stmts:
        conds = st.get("Condition") or {}
        # Either operator is legitimate; which one is required is decided by whether the
        # allowlist carries a namespace glob, checked below. Reading only StringEquals (as
        # this test did before P0-B) would report "no allowlist at all" for a correctly
        # widened grant -- a false alarm on the most alarming wording in the file.
        by_operator = {
            op: (conds.get(f"ForAllValues:{op}") or {}).get("aws:TagKeys") for op in ("StringEquals", "StringLike")
        }
        present = {op: v for op, v in by_operator.items() if v is not None}
        assert present, (
            f"{lid} grants lambda:TagResource with no aws:TagKeys allowlist, so "
            "the platform may write ANY tag -- including the AgentCoreGatewayTarget=allow "
            "opt-in that gates account-wide lambda:AddPermission."
        )
        assert len(present) == 1, (
            f"{lid} carries BOTH ForAllValues:StringEquals and ForAllValues:StringLike on "
            f"aws:TagKeys ({present!r}). IAM ANDs them, so the effective allowlist is the "
            "intersection and the widening is silently undone."
        )
        operator, allowed = next(iter(present.items()))
        allowed_set = {allowed} if isinstance(allowed, str) else set(allowed)
        # F-7d: the statement that covers the tool-Lambda prefix must allow exactly the pair
        # PLUS the binding keys the tool creates write (DeploymentId, ToolScope); any other
        # TagResource statement (the tool-test sandbox) writes only the pair and must allow
        # only the pair. Either way: nothing in the grant the code does not write.
        covers_tool_lambdas = any(_covers(r, n) for r in _resource_strings(st) for n in _tool_function_names())
        globs = set(governance_tag_key_globs())
        wanted = (expected_keys | globs) if covers_tool_lambdas else {"ManagedBy", "AgentCoreStack"}
        assert allowed_set == wanted, (
            f"{lid} allows tag keys {sorted(allowed_set)}; the creates it covers write "
            f"{sorted(wanted)}. A key in the grant but not in the code is extra "
            "authority; a key in the code but not the grant is a deploy-time DENY."
        )
        # The wildcard rule, which is what bounds the forked side now that exact keys
        # cannot be enumerated: only declared namespaces may be patterns. A bare "*" here
        # would satisfy every assertion above while making the condition vacuous.
        stray = sorted(k for k in allowed_set if ("*" in k or "?" in k) and k not in globs)
        assert not stray, (
            f"{lid} admits wildcard tag keys {stray} that are not declared governance "
            "namespaces; a pattern outside GOVERNANCE_TAG_KEY_PREFIXES bounds nothing, and "
            "one that matches AgentCoreGatewayTarget reopens the AddPermission self-grant."
        )
        # And the operator has to match the content: a glob under StringEquals is a literal
        # key named "platform:*", which denies every governed deploy instead of allowing it.
        if allowed_set & globs:
            assert operator == "StringLike", (
                f"{lid} lists namespace globs {sorted(allowed_set & globs)} under "
                f"ForAllValues:{operator}, where they are compared literally. Every deploy "
                "carrying a governance tag is denied at create time."
            )
        else:
            assert operator == "StringEquals", (
                f"{lid} uses ForAllValues:StringLike for an allowlist with no namespace glob "
                f"({sorted(allowed_set)}); StringLike treats any '*' or '?' a future editor "
                "adds as a wildcard, so pin it with StringEquals while it is exact."
            )
        product = conds.get("StringEquals", {}).get("aws:RequestTag/ManagedBy")
        assert product == "agentcore-flows", (
            f"{lid} does not pin aws:RequestTag/ManagedBy to our own product value "
            f"(got {product!r}); the key allowlist alone would still permit writing "
            "ManagedBy=<somebody else> onto a foreign function."
        )
        owner = conds.get("StringEquals", {}).get("aws:RequestTag/AgentCoreStack")
        assert owner == EXPECTED_OWNER_VALUE, (
            f"{lid} pins aws:RequestTag/AgentCoreStack to {owner!r}, expected "
            f"{EXPECTED_OWNER_VALUE!r}. Leaving this key unpinned is NOT covered by the "
            "aws:TagKeys allowlist: it lets this role write another deployment's stack id "
            "onto a function, and that id is what teardown matches on. Pinning it to a "
            "value owner_tags() does not send is the opposite failure -- every "
            "create_function(Tags=...) is then denied, with no retry-untagged fallback."
        )
        assert isinstance(owner, str) and owner.endswith("-${aws:RequestedRegion}"), (
            f"{lid}: the owner value {owner!r} must end in the IAM policy variable, not a "
            "synth-time region literal. A CDK token would render as Fn::Join/Fn::Sub and "
            "CloudFormation would consume the ${aws:...} before IAM ever saw it."
        )


def test_the_tag_grant_reaches_exactly_as_far_as_the_create_grant(template_json) -> None:
    """The tag ARN's region must track ``lambda:CreateFunction``'s, in both directions.

    A deliberate divergence from the runtime grant, recorded here because it looks like
    an inconsistency. ``bedrock-agentcore:TagResource`` uses region ``*`` because that
    path IS reachable cross-region today (``step_clients.session_for_event`` uses this
    role's own credentials in a non-home target region). The Lambda path is not: every
    sibling grant it depends on -- ``CreateFunction`` above all -- is pinned to the home
    region, so widening only the tag statement would grant reach the path cannot use,
    against ARCC ``cnt_AGx9pUNpmdOVZB`` (scope to the necessary resource ARNs).

    So the invariant is the COUPLING, not a literal: whatever region ``CreateFunction``
    can reach, ``TagResource`` must reach and no more. That fails loudly in both
    directions -- widening the tag grant alone (extra authority) and widening
    ``CreateFunction`` alone under F-41 (a deploy that creates a function it cannot tag,
    which has no retry-untagged fallback and so fails the whole gateway step).
    """

    def _regions_for(action: str) -> set[str]:
        out = set()
        for _lid, st in _statements(template_json):
            if action not in _actions(st) or st.get("Effect", "Allow") != "Allow":
                continue
            for r in _resource_strings(st):
                if ":function:AgentCore" not in r:
                    continue
                parts = r.split(":")
                if len(parts) > 3:
                    out.add(parts[3])
        return out

    create = _regions_for("lambda:CreateFunction")
    tag = _regions_for("lambda:TagResource")
    assert create, "no lambda:CreateFunction grant on an AgentCore* function was found at all"
    assert tag, "no lambda:TagResource grant on an AgentCore* function was found at all"
    assert tag == create, (
        f"lambda:TagResource reaches regions {sorted(tag)} while lambda:CreateFunction reaches "
        f"{sorted(create)}. Tag-on-create needs both on the same request: a tag grant that "
        "reaches further is unusable extra authority, and a create grant that reaches further "
        "fails the gateway step outright (no retry-untagged fallback on this path). Widen both "
        "in the same change, and keep aws:RequestTag/AgentCoreStack on ${aws:RequestedRegion} "
        "so the owner value stays exact in the new region."
    )


def test_tool_lambda_tag_writes_never_reach_every_function(template_json) -> None:
    """Scope, independently of the condition. Two gates, not one.

    ``AgentCore*`` is already wider than what the platform creates; ``function:*`` or a
    bare ``*`` would put every function in the account behind a single condition-key
    typo.
    """
    offenders = []
    for lid, st in _statements(template_json):
        if st.get("Effect", "Allow") != "Allow":
            continue
        granted = set(_actions(st))
        if not ({"lambda:TagResource", "lambda:*"} & granted):
            continue
        for r in _resource_strings(st):
            if r == "*" or r.endswith(":function:*") or r.endswith(":function:"):
                offenders.append((lid, sorted(granted & {"lambda:TagResource", "lambda:*"}), r))
    assert not offenders, f"lambda:TagResource granted on an unscoped resource: {offenders}"
