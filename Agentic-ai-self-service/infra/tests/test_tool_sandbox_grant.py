"""The tool-test sandbox's Lambda grant, pinned to what the service actually calls.

The live failure that started this file: the F-11 hardening began tagging the
temporary test function on creation, so teardown could tell it apart from a
foreign ``AgentCore-*`` function. ``create_function``'s ``Tags`` argument is
authorized as **lambda:TagResource on the function being created**, not as part
of ``lambda:CreateFunction``, and the deployment role did not hold it. Every tool
test then failed at ``CreateFunction`` with

    not authorized to perform: lambda:TagResource on resource:
    arn:aws:lambda:us-east-1:...:function:AgentCore-ToolTest-3da96f68

which is a *complete feature outage* produced by a security fix, discovered only
because the live probe exercised a benign tool rather than only a rejected one.
Unit tests could not have caught it: the AWS call is mocked in every one of them.

Two invariants, in opposite directions.

**Enough.** Every Lambda API the sandbox path calls must be granted on the
sandbox's own name prefix. The set of calls is read out of the backend source, so
adding ``lambda_client.update_function_configuration(...)`` to that path without
a grant fails here instead of in production. The prefix is read out of the
backend too, so renaming ``TOOL_TEST_FN_PREFIX`` cannot silently orphan the grant.

**Not too much.** ``lambda:TagResource`` must never be account-wide. Tags are how
this repo decides ownership (``services/resource_ownership.py``): an
account-wide TagResource would let the platform stamp its own owner tags onto a
function it did not create, and every ownership check downstream would then agree
that it did. That is F-7's shape — a name is not authority — reintroduced through
a tag instead of a name.

**The two directions need different scopes, and conflating them made the "enough"
half unfalsifiable.** Found 2026-09-22, by mutation: deleting the sandbox's entire
``lambda:TagResource`` statement from ``stacks/platform/lambdas.py`` left all eight
tests in this file GREEN. The helper they shared collected statements from *every*
IAM principal in the synthesized template, so the deployment role silently borrowed
``lambda:TagResource`` from the unrelated **gateway step role**, whose grant sits on
``function:AgentCore*`` and therefore ``_covers`` the sandbox prefix. IAM does not
work that way: a role holds only what is attached to it, so an "is it granted"
question answered template-wide is not an answer at all -- this file had zero
coverage of the outage it was written for.

So the two directions now use different scopes, deliberately:

* **Enough** (the outage direction) resolves statements by ACTUAL ATTACHMENT to the
  deployment Lambda's execution role -- the one principal that runs ``tool_tester``
  -- through ``iam_attachment.statements_for_role``. Another principal's grant cannot satisfy it.
* **Not too much** (the over-reach direction) stays template-wide on purpose. No
  principal in this account should hold account-wide ``lambda:TagResource`` or
  ``logs:TagResource``, so narrowing those scans to one role would let the next one
  added elsewhere pass unnoticed.

Both actions were confirmed against the AWS Service Reference feed
(https://servicereference.us-east-1.amazonaws.com/v1/lambda/lambda.json) rather
than the docs, per the usual rule here that "the tool says it doesn't exist" is
never enough either way.

ARCC guidance applied: ``cnt_AGx9pUNpmdOVZB`` (scope policies to the necessary
actions and resource ARNs; least privilege over convenience),
``cnt_MSVB0Kk8WMwmmW`` (isolation for customer-provided code -- the reason the
sandbox function exists at all).
"""

import ast
import re
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform.config import governance_tag_key_globs
from stacks.platform_stack import PlatformStack

from tests.iam_attachment import resolve_role_policies, role_logical_id, statements_for_role

REGION = "us-east-1"
ACCOUNT = "123456789012"
#: The synth inputs, named so the expected owner tag VALUE can be derived from them
#: rather than hardcoded. The grants pin aws:RequestTag/AgentCoreStack to
#: ``{project}-{env}-${aws:RequestedRegion}``, and IAM resolves that variable to the
#: region the call is made in -- REGION here, because the resource ARNs are pinned to it.
PROJECT = "acf"
ENV = "test"

_SERVICES = Path(__file__).resolve().parents[2] / "backend" / "src" / "app" / "services"
TOOL_TESTER = _SERVICES / "tool_tester.py"
_OWNERSHIP_PY = _SERVICES / "resource_ownership.py"

#: boto3 method -> IAM action, for the calls this path can make. Only methods the
#: sandbox path actually invokes are asserted; the mapping exists because the two
#: names differ often enough (``invoke`` -> ``lambda:InvokeFunction``) that
#: camel-casing the method name would produce actions that do not exist.
METHOD_TO_ACTION = {
    "create_function": "lambda:CreateFunction",
    "get_function": "lambda:GetFunction",
    "delete_function": "lambda:DeleteFunction",
    "invoke": "lambda:InvokeFunction",
    "put_function_concurrency": "lambda:PutFunctionConcurrency",
    "update_function_code": "lambda:UpdateFunctionCode",
    "update_function_configuration": "lambda:UpdateFunctionConfiguration",
    "tag_resource": "lambda:TagResource",
    "list_tags": "lambda:ListTags",
}


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


@pytest.fixture(scope="module")
def tool_tester_source() -> str:
    return TOOL_TESTER.read_text()


def _statements(template_json: dict) -> list[tuple[str, dict]]:
    """(logical id, statement) for every statement in every IAM policy/role.

    Template-wide **by design**, and only for the over-reach scans: "nobody in this
    account may hold X" is a property of the whole template. Do NOT use it to answer
    "is the deployment role allowed to do X" -- see the module docstring for the
    mutation that survived because it was used that way. ``statements_for_role``
    is the helper for that question.
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


#: Logical-id prefix of the Lambda execution role that runs ``tool_tester``. ``tool_tester``
#: is imported and called inside the deployment Lambda, so that function's role is the only
#: principal whose grants can make a tool test succeed. Matched by prefix, never by the
#: full hashed id -- see ``iam_attachment.role_logical_id``.
DEPLOYMENT_ROLE_PREFIX = "DeploymentLambdaRole"


def _deployment_role_logical_id(template_json: dict) -> str:
    return role_logical_id(template_json, DEPLOYMENT_ROLE_PREFIX)


@pytest.fixture(scope="module")
def deployment_role_statements(template_json) -> list[tuple[str, dict]]:
    """Only what the deployment Lambda's execution role holds. See the module docstring."""
    return statements_for_role(template_json, _deployment_role_logical_id(template_json))


def test_the_role_scoped_resolver_reaches_both_the_default_and_the_overflow_policies(template_json):
    """Non-vacuity for the resolver itself, which is now load-bearing for five tests.

    The sandbox grants live in a CDK *overflow* managed policy, not in the role's
    ``DefaultPolicy``. So a resolver bug that dropped either attachment shape would not
    announce itself: dropping the overflow makes the enough-tests fail (loud, fine), but
    dropping the default policy silently shrinks the set the conditioned-grant
    assertions iterate over, and those iterate-and-assert loops pass over nothing.

    This pins the structure the resolver must keep reaching: the base policy CDK builds
    for ``add_to_policy`` **and** at least one overflow managed policy. It deliberately
    does not pin the overflow COUNT -- that tracks total policy size and would fail on
    any unrelated grant being added or removed.
    """
    role_lid = _deployment_role_logical_id(template_json)
    sources, _found = resolve_role_policies(template_json, role_lid)
    assert sources, f"the resolver drew statements from no named policy resource for {role_lid}"

    base = [lid for lid, kind in sources.items() if kind in {"AWS::IAM::Policy", "Inline"}]
    overflow = [lid for lid, kind in sources.items() if kind == "AWS::IAM::ManagedPolicy"]
    assert base, (
        f"the resolver found no inline policy and no AWS::IAM::Policy attached to {role_lid}; "
        f"it saw only {sources}. CDK puts every add_to_policy grant in a DefaultPolicy until "
        "it overflows, so finding none means the Roles-Ref resolution is broken."
    )
    assert overflow, (
        f"the resolver found no AWS::IAM::ManagedPolicy attached to {role_lid}; it saw only "
        f"{sources}. This role overflows the inline-policy limit and the sandbox's Lambda and "
        "Logs grants live in the overflow, so a resolver that cannot see managed policies "
        "cannot see the grants this file exists to protect."
    )


def test_the_sandbox_tag_grant_is_scoped_to_the_sandbox_prefix_on_this_very_role(template_json, tool_tester_source):
    """The sandbox's ``lambda:TagResource`` exists *narrowly* and *on this role*.

    Stronger than the enough-test on purpose, and it closes the last route by which the
    original mutation could survive. The enough-test accepts a broader grant -- a
    ``function:AgentCore*`` statement genuinely does cover ``AgentCore-ToolTest-<uuid>``,
    and demanding the exact prefix there would have reported four working actions as
    missing. But that tolerance means a wide grant on this same role could still stand in
    for the deleted sandbox statement. So assert the narrow statement's existence
    separately: exactly-prefix-scoped, attached to this role, and satisfiable by the
    sandbox's own request.
    """
    prefix = _prefix_from_backend(tool_tester_source)
    role_lid = _deployment_role_logical_id(template_json)
    exact = [
        (lid, st)
        for lid, st in statements_for_role(template_json, role_lid)
        if st.get("Effect") == "Allow"
        and "lambda:TagResource" in _actions(st)
        and any(r.endswith(f":function:{prefix}*") for r in _resource_strings(st))
    ]
    assert exact, (
        f"no policy attached to {role_lid} grants lambda:TagResource on exactly "
        f"function:{prefix}*. A wider AgentCore* grant may make tool testing work today, but "
        "the sandbox's own least-privilege statement is gone -- restore it in "
        "stacks/platform/lambdas.py rather than relaxing this assertion."
    )
    for lid, st in exact:
        cond = st.get("Condition") or {}
        assert _condition_is_satisfied_by_the_sandbox_request(cond), (
            f"{lid} conditions the sandbox lambda:TagResource in a way "
            f"create_function(Tags=owner_tag_list(region)) does not satisfy ({cond!r}) -- that "
            "denies every tool test at CreateFunction, the exact live outage in this file's "
            "docstring"
        )


def _actions(st: dict) -> list[str]:
    act = st.get("Action")
    if isinstance(act, str):
        return [act]
    return [a for a in (act or []) if isinstance(a, str)]


def _resource_strings(st: dict) -> list[str]:
    """Resource entries flattened to strings, resolving ``Fn::Join`` of literals.

    The synthesized ARNs are ``Fn::Join`` over literals plus ``AWS::Partition``, so a
    naive string check would see no resource at all and every assertion below would
    pass vacuously.
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


def _prefix_from_backend(source: str) -> str:
    m = re.search(r'^TOOL_TEST_FN_PREFIX\s*=\s*"([^"]+)"', source, re.M)
    assert m, "TOOL_TEST_FN_PREFIX not found in tool_tester.py -- did it move or get renamed?"
    return m.group(1)


def _sandbox_methods(source: str) -> set[str]:
    """Lambda client methods the sandbox path calls, read from the source.

    Deliberately scans the whole module rather than one function: the calls are
    spread across ``_deploy_temp_lambda``, ``test_tool`` and the ``finally``
    cleanup, and a future helper would otherwise escape the scan.
    """
    found = set(re.findall(r"lambda_client\.([a-z_]+)\(", source))
    unknown = found - set(METHOD_TO_ACTION)
    assert not unknown, (
        "tool_tester.py calls Lambda APIs this test cannot map to an IAM action: "
        f"{sorted(unknown)}. Add them to METHOD_TO_ACTION *and* confirm the grant "
        "in stacks/platform/lambdas.py covers them."
    )
    return found


def _tags_on_create(source: str) -> bool:
    """True if the sandbox's ``create_function`` is passed ``Tags``.

    This function is the whole point of the file, and the first version of this
    test did not have it -- which is why removing ``lambda:TagResource`` from the
    grant left all three tests green. The method scan above can only ever see
    ``create_function``, and ``lambda:CreateFunction`` *was* granted; the action
    that was missing is implied by an **argument**, not by the call. AWS
    authorizes ``create_function(Tags=...)`` as CreateFunction *and*
    ``lambda:TagResource`` on the function being created, and there is no way to
    infer that from the method name.

    Resolved by AST rather than by a substring search for ``"Tags"``, because the
    kwargs are built as a dict and splatted (``create_function(**kwargs)``): the
    literal and the call are several statements apart, and a file-wide substring
    match would also fire on an unrelated ``Tags`` elsewhere in the module and
    make this assertion unfalsifiable.
    """
    tree = ast.parse(source)
    functions = {fn.name: fn for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))}

    # Functions that call create_function directly, plus -- transitively -- the
    # functions that call *those*.
    #
    # The second half is not hypothetical. This originally looked only at the
    # function containing the call, and adding the IAM-propagation retry moved
    # ``create_function`` out of ``_deploy_temp_lambda`` and into
    # ``_create_function_with_role_retry(lambda_client, kwargs)``. The Tags were
    # still passed, in the same splatted dict as before, but the literal and the
    # call now lived in different functions -- so this returned False and the test
    # failed on a refactor. Worse, it would then have stayed blind to Tags actually
    # being dropped. Following the call edge keeps the scan scoped (a stray
    # ``Tags`` elsewhere in the module still cannot satisfy it) while letting the
    # kwargs be assembled by a caller and splatted by a callee.
    relevant = {
        name
        for name, fn in functions.items()
        if any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "create_function"
            for n in ast.walk(fn)
        )
    }
    for _ in range(len(functions)):
        callers = {
            name
            for name, fn in functions.items()
            if any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in relevant for n in ast.walk(fn)
            )
        }
        if callers <= relevant:
            break
        relevant |= callers

    for name, fn in functions.items():
        if name not in relevant:
            continue
        # Direct keyword, e.g. create_function(Tags={...}).
        for n in ast.walk(fn):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "create_function"
                and any(kw.arg == "Tags" for kw in n.keywords)
            ):
                return True
        # Or a "Tags" key in any dict literal in the same function -- the splatted
        # kwargs shape this module actually uses.
        for n in ast.walk(fn):
            if isinstance(n, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "Tags" for k in n.keys):
                return True
        # Or assigned onto the kwargs dict afterwards: kwargs["Tags"] = ...
        for n in ast.walk(fn):
            if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) and n.slice.value == "Tags":
                return True
    return False


def _required_actions(source: str) -> set[str]:
    needed = {METHOD_TO_ACTION[m] for m in _sandbox_methods(source)}
    if _tags_on_create(source):
        needed.add("lambda:TagResource")
    return needed


def _covers(resource: str, prefix: str) -> bool:
    """True if *resource* reaches EVERY function whose name starts with *prefix*.

    A broader grant counts: the lifecycle verbs are held on ``function:AgentCore*``,
    which covers ``AgentCore-ToolTest-<uuid>`` perfectly well, and requiring the
    exact sandbox prefix here would have reported four already-working actions as
    missing. A *narrower* pattern does not count -- ``function:AgentCore-ToolTest-a*``
    matches some sandbox names and not others, which is an outage for the rest.
    """
    marker = ":function:"
    if marker not in resource:
        return False
    stem = resource.split(marker, 1)[1]
    if not stem.endswith("*"):
        return stem == prefix  # an exact name can only cover a prefix of length 0
    return prefix.startswith(stem[:-1])


def _owner_tags_the_sandbox_sends() -> dict[str, str]:
    """The tag PAIRS ``create_function(Tags=owner_tag_list(region))`` actually sends.

    Read from ``resource_ownership.py`` rather than transcribed, values included.

    The owner value used to be recorded as ``None`` = "any value", on the grounds that
    this test could not know ``{project}-{env}-{region}``. It can: the project and env
    are the synth inputs in the fixture above and the region is ``REGION``. Leaving it
    unknowable was not neutral -- ``_condition_is_satisfied_by_the_sandbox_request``
    treats an unresolved pin as UNSATISFIED, so the moment the grant started pinning the
    owner value (2026-09-22) the evaluator would have reported a perfectly good
    ``lambda:TagResource`` grant as missing and failed the outage test. An evaluator that
    cannot model the condition cannot tell a fixed grant from a broken one.

    The value's SHAPE is taken from ``stack_id``'s own return expression so a change to
    the format breaks here rather than at deploy time.
    """
    src = _OWNERSHIP_PY.read_text()
    out: dict[str, str] = {}
    owner = re.search(r'^OWNER_TAG_KEY = "([^"]+)"', src, re.M)
    product = re.search(r'^PRODUCT_TAG_KEY = "([^"]+)"', src, re.M)
    product_val = re.search(r'^PRODUCT_TAG_VALUE = "([^"]+)"', src, re.M)
    assert owner and product and product_val, "owner/product tag constants moved in resource_ownership.py"
    shape = re.search(r'^    return f"\{project\}-\{env\}-\{_region\(region\)\}"$', src, re.M)
    assert shape, (
        "resource_ownership.stack_id no longer returns f'{project}-{env}-{region}'. The IAM "
        "grants pin aws:RequestTag/AgentCoreStack to that exact shape, so a format change "
        "denies every tag-on-create; update the grants in the same commit."
    )
    out[owner.group(1)] = f"{PROJECT}-{ENV}-{REGION}"
    out[product.group(1)] = product_val.group(1)
    return out


def _condition_is_satisfied_by_the_sandbox_request(cond: dict) -> bool:
    """True when the sandbox's own tag-on-create request satisfies *cond*.

    Blanket-ignoring every conditioned statement, which this test used to do, is wrong
    in the outage direction now that the ownership tag WRITE is conditioned: it reports
    a grant that works as missing. But counting every conditioned statement is wrong in
    the other direction -- the BYO-Lambda ``AgentCoreGatewayTarget=allow`` grant is
    conditioned on the TARGET's existing tags, which the sandbox request cannot satisfy,
    and counting it would report a grant the sandbox never gets.

    So evaluate, and only for the two request-tag operators the ownership grants use.
    Anything this function does not understand returns False, i.e. the action is
    reported as ungranted and somebody looks -- the safe direction for an outage test.

    ``${aws:RequestedRegion}`` in a condition VALUE is substituted with ``REGION``,
    because that is what IAM does at evaluation time and the statements are pinned to
    that region. Modelling it as an opaque literal would make every correctly-pinned
    grant read as unsatisfied.
    """
    sent = _owner_tags_the_sandbox_sends()
    for operator, kv in cond.items():
        for key, want in kv.items():
            if key == "aws:TagKeys":
                if operator != "ForAllValues:StringEquals":
                    return False
                allowed = {want} if isinstance(want, str) else set(want)
                if not set(sent) <= allowed:
                    return False
            elif key.startswith("aws:RequestTag/"):
                # StringEqualsIfExists would evaluate TRUE for a request that omits the
                # tag entirely (ARCC cnt_SFJJhkOueCPRkd), so it is not an acceptable
                # substitute for StringEquals here and is deliberately not accepted.
                if operator != "StringEquals":
                    return False
                tag = key.split("/", 1)[1]
                if tag not in sent:
                    return False
                if not isinstance(want, str):
                    return False
                if want.replace("${aws:RequestedRegion}", REGION) != sent[tag]:
                    return False
            else:
                return False
    return True


def test_every_lambda_api_the_sandbox_calls_is_granted(deployment_role_statements, tool_tester_source):
    """The outage direction. A granted-but-unused action is waste; an *ungranted*
    one is a feature that fails for every user, at the first AWS call.

    Scoped to the deployment Lambda's own execution role. Until 2026-09-22 this scanned
    the whole template and so could be satisfied by the gateway step role's broader
    ``function:AgentCore*`` grant -- deleting the sandbox statement outright left this
    test green. A grant held by another principal does nothing for this call path.
    """
    prefix = _prefix_from_backend(tool_tester_source)
    needed = _required_actions(tool_tester_source)
    assert needed, "no Lambda calls found in tool_tester.py -- the scan regex is broken"
    assert "lambda:TagResource" in needed, (
        "the sandbox function is supposed to be created WITH owner tags so teardown "
        "can tell it from a foreign AgentCore-* function; if Tags were dropped from "
        "create_function, fix that rather than this assertion"
    )

    granted: set[str] = set()
    for _lid, st in deployment_role_statements:
        if st.get("Effect") != "Allow":
            continue
        # A conditioned statement counts only if the sandbox's own request satisfies
        # the condition -- see _condition_is_satisfied_by_the_sandbox_request.
        if st.get("Condition") and not _condition_is_satisfied_by_the_sandbox_request(st["Condition"]):
            continue
        if not any(_covers(r, prefix) for r in _resource_strings(st)):
            continue
        granted |= {a for a in _actions(st) if a.startswith("lambda:")}

    missing = sorted(needed - granted)
    assert not missing, (
        f"tool_tester.py calls these but NO policy attached to the deployment Lambda's "
        f"execution role grants them on function:{prefix}*: {missing}. This is the shape that "
        "took tool testing down live: create_function(Tags=...) needs lambda:TagResource on "
        "the new function. A statement on another role does not count -- check that the grant "
        "is on the role built in build_deployment_lambda, not on a step role."
    )


def test_tag_resource_is_never_account_wide(template_json):
    """The over-reach direction, and the more dangerous one.

    Owner tags ARE the ownership proof in this repo. Account-wide TagResource
    would let the platform tag a function it did not create and then pass its own
    ownership check on it.
    """
    offenders = []
    for lid, st in _statements(template_json):
        if st.get("Effect") != "Allow":
            continue
        if "lambda:TagResource" not in _actions(st) and "lambda:*" not in _actions(st):
            continue
        for r in _resource_strings(st):
            if r.endswith(":function:*") or r.endswith(":function:") or r == "*":
                offenders.append((lid, _actions(st), r, st.get("Condition")))
    assert not offenders, (
        "account-wide lambda:TagResource -- the platform could stamp its own owner "
        f"tags onto a foreign function and then 'own' it: {offenders}"
    )


def test_the_grant_is_scoped_to_the_tool_test_prefix(template_json, tool_tester_source):
    """``PutFunctionConcurrency`` stays on the sandbox prefix; a broader ``TagResource``
    must be conditioned to the ownership keys.

    This assertion used to be "TagResource and PutFunctionConcurrency never reach beyond
    ``AgentCore-ToolTest-*``", and that premise is no longer true: F-7 gave the tool
    Lambdas (``AgentCoreDynamicTools``, ``AgentCore-KBTool-*``) tag-on-create so the
    platform can prove it owns a function before replacing its code, and those names sit
    on ``function:AgentCore*``. Recorded as a deliberate weakening rather than a quiet
    edit, with what replaced the removed strength: every ``lambda:TagResource`` statement
    that reaches past the sandbox prefix must pin ``aws:TagKeys`` to the two ownership
    keys and ``aws:RequestTag/ManagedBy`` to our own product value, so a wider scope
    cannot be used to write the ``AgentCoreGatewayTarget=allow`` opt-in tag that gates
    account-wide ``lambda:AddPermission``. The key allowlist itself is cross-checked
    against ``owner_tags()``'s own source in
    ``test_the_tool_lambda_ownership_grant.py``; here we only require that it exists.

    ``PutFunctionConcurrency`` keeps the original, stricter rule: nothing outside the
    sandbox needs it.
    """
    prefix = _prefix_from_backend(tool_tester_source)
    hits = []
    for lid, st in _statements(template_json):
        if st.get("Effect") != "Allow":
            continue
        acts = set(_actions(st))
        if not {"lambda:TagResource", "lambda:PutFunctionConcurrency"} & acts:
            continue
        hits.append((lid, sorted(acts), _resource_strings(st), st.get("Condition") or {}))

    assert hits, (
        "no statement grants lambda:TagResource / lambda:PutFunctionConcurrency at all "
        "-- tool testing cannot create its sandbox function"
    )
    for lid, acts, resources, cond in hits:
        beyond = [r for r in resources if not r.endswith(f":function:{prefix}*")]
        if "lambda:PutFunctionConcurrency" in acts:
            assert not beyond, (
                f"{lid} grants lambda:PutFunctionConcurrency beyond function:{prefix}* -- {beyond}. "
                "Only the sandbox needs to throttle a function."
            )
        if beyond and "lambda:TagResource" in acts:
            # The OPERATOR is not pinned, and that is the point of reading both. P0-B widened
            # this condition from ForAllValues:StringEquals to ForAllValues:StringLike so the
            # governance namespaces ("platform:*", "org:*") can be expressed at all -- an
            # enumeration is impossible because an admin creates the keys at runtime. Pinned to
            # the old operator this assertion read the allowlist as ABSENT and reported "no
            # aws:TagKeys allowlist" about a statement carrying a perfectly good one, which is
            # a false alarm in the direction that gets a real grant loosened to silence it.
            allowed = (cond.get("ForAllValues:StringLike") or cond.get("ForAllValues:StringEquals") or {}).get(
                "aws:TagKeys"
            )
            assert allowed, (
                f"{lid} grants lambda:TagResource on {beyond} with no aws:TagKeys allowlist. "
                "A scope wider than the sandbox prefix must pin which tags may be written, "
                "or the platform can stamp the AgentCoreGatewayTarget opt-in onto a foreign "
                "AgentCore* function and self-grant lambda:AddPermission on it."
            )
            # StringLike admits a NEW way for the allowlist to be decorative that StringEquals
            # could not express: a bare "*" (or "AgentCore*", or any pattern matching the
            # opt-in key) satisfies the assertion above while authorizing every tag key. So the
            # widening has to be paid for here -- the only wildcards permitted are the declared
            # governance namespaces.
            globs = governance_tag_key_globs()
            wild = [k for k in allowed if ("*" in k or "?" in k) and k not in globs]
            assert not wild, (
                f"{lid} allows tag keys {wild!r} outside the declared governance namespaces. A "
                "pattern here bounds nothing: 'AgentCoreGatewayTarget' matches '*' and "
                "'AgentCore*', which is exactly the opt-in tag this condition exists to keep "
                "the platform from stamping on a foreign function."
            )
            assert cond.get("StringEquals", {}).get("aws:RequestTag/ManagedBy"), (
                f"{lid} grants lambda:TagResource on {beyond} without pinning "
                "aws:RequestTag/ManagedBy to our own product value."
            )


def _log_group_statements(statements: list[tuple[str, dict]], prefix: str) -> list[tuple[str, dict]]:
    """Statements granting a ``logs:`` action on the sandbox's own log groups.

    Takes the statement list rather than the template so the caller chooses the scope:
    ``deployment_role_statements`` for "can the sandbox path do it", ``_statements``
    for "may anybody do it". Those are different questions and the file used to answer
    the first with the second -- see the module docstring.
    """
    out = []
    for lid, st in statements:
        if st.get("Effect") != "Allow":
            continue
        if not any(a.startswith("logs:") for a in _actions(st)):
            continue
        if not any(f":log-group:/aws/lambda/{prefix}" in r for r in _resource_strings(st)):
            continue
        out.append((lid, st))
    return out


def test_the_sandbox_log_group_grant_includes_tag_on_create(deployment_role_statements, tool_tester_source):
    """The same defect as ``lambda:TagResource``, in CloudWatch, found the same way.

    ``tool_tester._ensure_sandbox_log_group`` creates the group itself so it can set
    retention before Lambda creates it without any, and it passes ``tags=`` so the
    group carries the same ownership identity as everything else the deployment
    makes. The grant originally held ``logs:CreateLogGroup`` and
    ``logs:PutRetentionPolicy`` only, on the documented reasoning that a ``tags``
    argument to ``CreateLogGroup`` is authorized through ``aws:RequestTag``.

    That reasoning was wrong, and CloudWatch says so itself. Live tool test on
    ``acfe2e-p0920``, 2026-09-21::

        AccessDeniedException ... is not authorized to perform CreateLogGroup with
        Tags. An additional permission "logs:TagResource" is required.

    Nothing failed. ``_ensure_sandbox_log_group`` retries the create untagged, so the
    group existed with ``retentionInDays: 7`` and the tool test passed -- and
    ``list-tags-for-resource`` on the group returned ``{}``. A governed but
    *unattributable* log group, which is the one outcome no test in the suite can
    see, because every one of them mocks the CloudWatch client. The only oracle was
    reading the deployed group's tags after a real test.

    Asserted as a set membership rather than equality so adding a future action
    does not fail here; the over-reach direction is the next test.
    """
    prefix = _prefix_from_backend(tool_tester_source)
    hits = _log_group_statements(deployment_role_statements, prefix)
    assert hits, (
        f"the deployment Lambda's execution role holds no logs: action on "
        f"log-group:/aws/lambda/{prefix}* -- the sandbox log group cannot be pre-created, so "
        "Lambda makes it implicitly with no retention and CloudWatch keeps it forever"
    )

    granted: set[str] = set()
    for _lid, st in hits:
        granted |= {a for a in _actions(st) if a.startswith("logs:")}

    needed = {"logs:CreateLogGroup", "logs:PutRetentionPolicy", "logs:TagResource"}
    missing = sorted(needed - granted)
    assert not missing, (
        f"the deployment role's sandbox log-group grant is missing {missing}. logs:TagResource is NOT "
        "implied by CreateLogGroup's tags argument -- proven live, see this test's "
        "docstring for the exact AccessDenied. Without it the group is created "
        "untagged and teardown cannot attribute it to this deployment."
    )


def test_logs_tag_resource_is_never_account_wide(template_json):
    """The over-reach direction, and the reason the grant is not simply widened.

    This account holds roughly 1,877 ``/aws/bedrock-agentcore/runtimes/*`` log groups
    and 53 pre-existing ``AgentCore-ToolTest-*`` groups that belong to other agents'
    work. Owner tags are how this repo decides ownership, so an account-wide
    ``logs:TagResource`` would let the platform stamp its own identity onto any of
    them and then pass every ownership check on the result -- F-7's shape ("a name is
    not authority") reintroduced through a tag, which is exactly what
    ``test_tag_resource_is_never_account_wide`` forbids for Lambda.
    """
    offenders = []
    for lid, st in _statements(template_json):
        if st.get("Effect") != "Allow":
            continue
        if lid.startswith("AgentCoreRoleBoundary"):
            # AgentCoreRoleBoundary is the permissions boundary for roles the backend mints (F-06,
            # stacks/platform/role_boundary.py). It is attached to no principal, so it grants nothing:
            # it is the cap on what a CREATED role may be granted, and its wildcards are that ceiling.
            # test_f06_role_permissions_boundary.py pins that nothing references it and that it is the
            # only unattached managed policy, so this exemption cannot hide a real grant.
            continue
        acts = _actions(st)
        if "logs:TagResource" not in acts and "logs:*" not in acts:
            continue
        for r in _resource_strings(st):
            if r == "*" or r.endswith(":log-group:*") or r.endswith(":log-group:/aws/lambda/*"):
                offenders.append((lid, sorted(acts), r, st.get("Condition")))
    assert not offenders, (
        "account-wide logs:TagResource -- the platform could stamp its owner tags "
        f"onto a foreign log group and then 'own' it: {offenders}"
    )


def test_the_log_tag_write_is_pinned_and_split_off_the_unconditionable_actions(
    deployment_role_statements, tool_tester_source
):
    """The ownership pins belong on ``logs:TagResource`` and NOWHERE else in that grant.

    Both halves of this are load-bearing, and each has a distinct failure mode.

    *The pins.* ``logs:TagResource`` was unconditioned, so the platform could write any
    owner value under the sandbox prefix -- the same caller-chosen-value hole as the
    Lambda and runtime grants. ``ManagedBy`` and ``AgentCoreStack`` are now both pinned,
    with the region as ``${aws:RequestedRegion}``: ``tool_tester`` threads ONE region into
    ``_create_logs_client`` and into ``owner_tag_list``, so the requested region always
    equals the value's region component.

    *The split.* ``logs:PutRetentionPolicy`` supports NO condition keys at all and
    ``logs:CreateLogGroup``'s untagged retry sends no request tags, so conditioning the
    statement they used to share with ``TagResource`` would have denied both. The
    retention failure in particular would have been SILENT in the worst direction: the
    group still gets created, just kept forever, which is the exact defect
    ``_ensure_sandbox_log_group`` exists to prevent. Confirmed against the AWS Service
    Reference feed for ``logs``, not the docs.

    So: conditions on the tag write, no request-tag conditions on the other two.
    """
    prefix = _prefix_from_backend(tool_tester_source)
    hits = _log_group_statements(deployment_role_statements, prefix)
    assert hits, (
        f"the deployment Lambda's execution role holds no logs: grant on log-group:/aws/lambda/{prefix}* at all"
    )

    tag_stmts = [(lid, st) for lid, st in hits if "logs:TagResource" in _actions(st)]
    assert tag_stmts, (
        "no statement grants logs:TagResource on the sandbox prefix -- CreateLogGroup with "
        "tags is denied and the group becomes unattributable to this deployment"
    )
    for lid, st in tag_stmts:
        cond = st.get("Condition") or {}
        assert _condition_is_satisfied_by_the_sandbox_request(cond), (
            f"{lid} conditions logs:TagResource in a way the sandbox's own request does not "
            f"satisfy ({cond!r}). That is an outage, not hardening: the tagged create is "
            "denied, _ensure_sandbox_log_group swallows it, and the group ends up untagged."
        )
        equals = cond.get("StringEquals", {})
        assert equals.get("aws:RequestTag/ManagedBy") == "agentcore-flows", (
            f"{lid} does not pin aws:RequestTag/ManagedBy on logs:TagResource (got "
            f"{equals.get('aws:RequestTag/ManagedBy')!r})"
        )
        owner = equals.get("aws:RequestTag/AgentCoreStack")
        assert owner == f"{PROJECT}-{ENV}-${{aws:RequestedRegion}}", (
            f"{lid} pins aws:RequestTag/AgentCoreStack to {owner!r} on logs:TagResource, "
            f"expected {PROJECT}-{ENV}-${{aws:RequestedRegion}}. Unpinned, this role may "
            "stamp another deployment's stack id onto a log group under the shared prefix."
        )
        keys = cond.get("ForAllValues:StringEquals", {}).get("aws:TagKeys")
        assert sorted(keys or []) == sorted(_owner_tags_the_sandbox_sends()), (
            f"{lid} allows tag keys {keys!r} on logs:TagResource; the sandbox sends "
            f"{sorted(_owner_tags_the_sandbox_sends())}"
        )

    for lid, st in hits:
        acts = set(_actions(st))
        unconditionable = acts & {"logs:PutRetentionPolicy", "logs:CreateLogGroup"}
        if not unconditionable:
            continue
        request_tag_conds = {
            f"{op}/{key}"
            for op, kv in (st.get("Condition") or {}).items()
            for key in kv
            if key.startswith("aws:RequestTag/") or key == "aws:TagKeys"
        }
        assert not request_tag_conds, (
            f"{lid} puts request-tag conditions {sorted(request_tag_conds)} on "
            f"{sorted(unconditionable)}. logs:PutRetentionPolicy supports no condition keys "
            "at all, and CreateLogGroup's untagged retry sends no tags, so this DENIES them: "
            "the sandbox log group is then created by Lambda with no retention and kept "
            "forever, silently. Keep logs:TagResource in its own statement."
        )


def test_the_log_group_grant_is_scoped_to_the_backends_prefix(template_json, tool_tester_source):
    """A fourth copy of the prefix, and this one is read from the backend too.

    ``TOOL_TEST_FN_PREFIX`` now appears in the IAM function grant, the Logs endpoint
    policy, and here. A rename would leave this statement pointing at log groups no
    sandbox function ever creates, and the failure is silent in the usual direction:
    the create is denied, ``_ensure_sandbox_log_group`` swallows it by design, and
    Lambda quietly makes the never-expire group this whole mechanism exists to
    prevent.
    """
    prefix = _prefix_from_backend(tool_tester_source)
    # Deliberately template-wide: this is an over-reach rule. A logs: grant naming the
    # sandbox prefix must be scoped to it no matter which principal holds it.
    hits = _log_group_statements(_statements(template_json), prefix)
    for lid, st in hits:
        for r in _resource_strings(st):
            assert r.endswith(f":log-group:/aws/lambda/{prefix}*") or r.endswith(
                f":log-group:/aws/lambda/{prefix}*:*"
            ), f"{lid} grants {sorted(_actions(st))} on {r!r}, which is not scoped to {prefix!r}"


def test_the_endpoint_policy_cannot_drift_from_the_backends_prefix(template_json, tool_tester_source):
    """The third copy of the prefix, and the one whose drift is silent.

    ``tool_sandbox_net.py`` duplicates ``TOOL_TEST_FN_PREFIX`` a second time, to
    scope the CloudWatch Logs VPC endpoint policy to ``/aws/lambda/<prefix>*``. A
    rename on the backend side would leave that policy pointing at log groups no
    sandbox function ever writes to -- and unlike the IAM grant, the failure mode is
    not "tool testing breaks". Execution logs are delivered by the Lambda service
    from outside the VPC, so everything would still *look* fine; only a generated
    tool's own ``boto3`` Logs call would start being denied by the endpoint, in a
    code path nobody exercises on the happy path.

    So this is asserted against the backend's constant rather than against the
    literal, exactly as the IAM grant above is.
    """
    prefix = _prefix_from_backend(tool_tester_source)
    endpoints = [
        r["Properties"]
        for r in template_json["Resources"].values()
        if r["Type"] == "AWS::EC2::VPCEndpoint" and str(r["Properties"].get("ServiceName", "")).endswith(".logs")
    ]
    assert len(endpoints) == 1, f"expected exactly one CloudWatch Logs endpoint, got {len(endpoints)}"
    policy = endpoints[0].get("PolicyDocument")
    assert policy, "the Logs endpoint has no policy -- it would ship the AWS default of Action/Resource '*'"

    for st in policy["Statement"]:
        for resource in _resource_strings(st):
            assert resource.endswith(f":log-group:/aws/lambda/{prefix}*") or resource.endswith(
                f":log-group:/aws/lambda/{prefix}*:*"
            ), f"endpoint policy resource {resource!r} is not scoped to the sandbox prefix {prefix!r}"
