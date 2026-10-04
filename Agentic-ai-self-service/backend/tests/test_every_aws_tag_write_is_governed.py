"""P0-B: every AWS tag write in the backend is either GOVERNED or documented as not.

Why a source census rather than per-resource behavioural tests. The defect this closes was
not "the governance tags are wrong" -- it was that a fully built, fully unit-tested tag
feature reached almost nothing. The resolved tag set was carried from the HTTP request into
the Step Functions state and then dropped, because each ``create_*`` call site independently
chose ``owner_tags`` and no test asserted across call sites. Behavioural tests per resource
cannot catch that: they pass for every resource they cover and say nothing about the resource
nobody thought to cover. The thing that has to be asserted is a property of the SET of tag
writes, so the set is what this file enumerates.

The control is delta-based and keyed by ``file::function``, not by line number: a new tag
write appears as a new census row and fails with a message naming it, and a documented
exception that no longer matches a real row also fails -- otherwise a deferral quietly
becomes permanent when the code it excused moves or is deleted.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "app"

# The kwargs and dict keys through which a tag set reaches an AWS API. ``Tagging`` is the S3
# form (a urlencoded string), ``UserPoolTags`` the Cognito form, ``tags``/``Tags`` everything
# else -- boto3 is not consistent about the case and a control that assumed one would miss
# whole services.
_TAG_KEYS = frozenset({"Tags", "tags", "UserPoolTags", "Tagging", "TagsMap"})

# Helpers that validate the governance set before it reaches AWS. Anything else stamping a
# resource is, by definition, writing tags nobody checked.
_GOVERNED = frozenset({"governed_tags", "governed_tag_list", "governed_lower_tag_list"})

# ``tags=`` also names a FastAPI router group and a pydantic field. Those are not AWS calls.
# The discriminator is the callee: boto3 methods are snake_case, model constructors are
# CamelCase or ``cls``. Only the two snake_case non-AWS callees are listed by name, so the
# exclusion cannot be stretched to cover a real client call.
_NOT_AWS_CALLEES = frozenset({"include_router", "cls"})


def _callee(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return "?"


# The helpers that stamp the two OWNERSHIP tags and nothing else. Named so a wrapped call
# reports the tagger rather than the wrapper: the S3 sites pass
# ``urlencode(owner_tags(...))``, and reading only the outer call would report ``urlencode``,
# which says nothing about whether the set was governed.
_OWNER_ONLY = frozenset({"owner_tags", "owner_tag_list", "owner_lower_tag_list"})


def _tagger(value: ast.AST, names: dict[str, str]) -> str:
    """Name the expression that produced the tag set.

    Resolution order matters. A ``Name`` is resolved through the enclosing function's own
    assignments -- the IAM ``create_role``/``tag_role`` pairs build the list once into a local
    and pass it twice, so reading the call site alone would report them as ungoverned. A
    ``Name`` that is a PARAMETER resolves to ``<param:...>`` rather than to some same-named
    local elsewhere in the module: a wrong attribution is worse than an unresolved one,
    because it silently scores a site as governed.
    """
    if isinstance(value, ast.Name):
        return names.get(value.id, f"<unresolved:{value.id}>")
    if isinstance(value, ast.Subscript):
        # ``tags=create_params["tags"]``: the tag set was built into a kwargs dict for the
        # CREATE call and the same entry is handed to a second API (the adopted-runtime
        # retag). Resolution is keyed on the SPECIFIC subscript, ``create_params['tags']``,
        # not on ``create_params`` as a whole -- scoring it by the dict would report any
        # governed call anywhere in that literal as governing this key, so a dict whose
        # ``tags`` entry was a bare literal would read as governed because some other entry
        # was not.
        if isinstance(value.value, ast.Name) and isinstance(value.slice, ast.Constant):
            return names.get(f"{value.value.id}[{value.slice.value!r}]", f"<unresolved:{value.value.id}>")
    inner = {_callee(n) for n in ast.walk(value) if isinstance(n, ast.Call)}
    if isinstance(value, ast.Call):
        # A wrapper's argument may be a local holding the tag set rather than the call itself:
        # ``Tagging=urlencode(tags)`` where ``tags = owner_tags(...)`` two lines up. Resolving
        # the nested names keeps that row attributed to ``owner_tags`` instead of to
        # ``urlencode``, which would read as an unrecognised tagger and get excused by hand.
        #
        # ONLY for a Call, and that restriction is load-bearing. Applied to a container it
        # reports the wrong thing: ``Tags=[{"Key": "ManagedBy", "Value": product}]`` walks to
        # a parameter used as a VALUE inside a literal, and the row then claims the whole tag
        # set is caller-supplied when in fact the keys are hardcoded.
        inner |= {names[n.id] for n in ast.walk(value) if isinstance(n, ast.Name) and n.id in names}
    for known in (_GOVERNED, _OWNER_ONLY):
        hit = sorted(inner & known)
        if hit:
            return "+".join(hit)
    if isinstance(value, ast.Call):
        return _callee(value)
    return "+".join(sorted(inner)) if inner else type(value).__name__


def _splatted(fn: ast.AST) -> set[str]:
    """Names used as ``**name`` in a call inside this function.

    This is the test for "this dict is a boto3 kwargs bag", and it is deliberately
    structural rather than a naming convention (``*_kwargs``): a dict that is splatted into a
    call IS call arguments whatever it is called, and a dict that is not -- such as the
    ``entry["tags"]`` A2A skill projection in ``aws_agent_registry`` -- is not an AWS tag
    write no matter how its key is spelled.
    """
    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg is None and isinstance(kw.value, ast.Name):
                    out.add(kw.value.id)
    return out


class _Census(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.rows: list[tuple[str, str, str, str]] = []
        self._fn: list[str] = ["<module>"]
        self._names: list[dict[str, str]] = [{}]
        self._splat: list[set[str]] = [set()]

    def _resolve(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for scope in self._names:
            merged.update(scope)
        return merged

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._fn.append(node.name)
        args = node.args
        params = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
        self._names.append({p: f"<param:{p}>" for p in params})
        self._splat.append(_splatted(node))
        self.generic_visit(node)
        self._names.pop()
        self._splat.pop()
        self._fn.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        for target in node.targets:
            # Every local is recorded, not only the ones assigned from a call. The cfn
            # provider's reconcile builds its tag sets with dict comprehensions and set
            # subtraction, and leaving those unrecorded left the census with
            # ``<unresolved:to_set>`` rows -- which would have had to be waived as a class,
            # and waiving "unresolved" as a class is how a genuinely new ungoverned site
            # slips through.
            if isinstance(target, ast.Name):
                self._names[-1][target.id] = _tagger(node.value, self._resolve())
                # Per-ENTRY resolution for a dict literal, so a later ``d["tags"]`` can be
                # scored by what built that one entry. See the Subscript branch of _tagger.
                if isinstance(node.value, ast.Dict):
                    # strict=True is safe: ast.Dict always pairs keys with values, and a
                    # ``**spread`` entry appears as a None key rather than as a length mismatch.
                    for k, v in zip(node.value.keys, node.value.values, strict=True):
                        if isinstance(k, ast.Constant):
                            self._names[-1][f"{target.id}[{k.value!r}]"] = _tagger(v, self._resolve())
            # ``put_kwargs["Tagging"] = ...``: the tag set never appears as a keyword because
            # the call is made with ``**put_kwargs``. Without this branch the two S3 object
            # writes are invisible to the census -- and they are precisely the sites that are
            # deliberately NOT governed, so the census would have "proved" full coverage by
            # failing to look.
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and target.slice.value in _TAG_KEYS
                and isinstance(target.value, ast.Name)
                and target.value.id in self._splat[-1]
            ):
                self.rows.append((self.rel, self._fn[-1], "**kwargs", _tagger(node.value, self._resolve())))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        callee = _callee(node)
        for kw in node.keywords:
            if kw.arg not in _TAG_KEYS:
                continue
            if callee in _NOT_AWS_CALLEES or (callee[:1].isupper()):
                continue
            self.rows.append((self.rel, self._fn[-1], callee, _tagger(kw.value, self._resolve())))
        self.generic_visit(node)


def _census() -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for path in sorted(_SRC.rglob("*.py")):
        visitor = _Census(str(path.relative_to(_SRC)))
        visitor.visit(ast.parse(path.read_text()))
        rows.extend(visitor.rows)
    return rows


# ---------------------------------------------------------------------------
# The documented exceptions. Each entry is a DECISION, not a backlog item; the reason is
# carried here so the census failure that would otherwise fire reads as a choice.
# ---------------------------------------------------------------------------
_EXCEPTIONS: dict[str, str] = {
    # An S3 OBJECT accepts at most 10 tags, a fifth of the 50 every other resource here
    # allows, and three slots are already spent on ownership + DeploymentId. A tag policy
    # with eight keys would take the connector-spec upload from "works" to "fails the whole
    # gateway deploy", at the 8th key, with nothing telling the operator 7 was the ceiling.
    # Closing it needs a per-sink ceiling in resource_tagging.
    "services/gateway_deployer.py::_build_openapi_schema": "S3 object 10-tag ceiling",
    "deployment_handler.py::_stage_export_bundle": "S3 object 10-tag ceiling",
    # Found by this census, not by reading: the agent code zip is a THIRD S3 object tag site
    # and it was not in the manual sweep that converted the rest of the module. Same ceiling,
    # same decision -- recorded here because an undocumented omission and a decided one look
    # identical in the source.
    "services/runtime_deployer.py::upload_code_to_s3": "S3 object 10-tag ceiling",
    # A tool test is not a deployment: there is no request carrying a tag policy, so
    # honouring one would mean reading the org's defaults here -- and a REQUIRED key with no
    # default would then fail every tool test in that org, removing a feature that creates
    # nothing billable beyond seconds of Lambda. All three resources are deleted in the
    # ``finally`` of the same request that made them.
    "services/tool_tester.py::_ensure_sandbox_role": "ephemeral tool-test sandbox",
    "services/tool_tester.py::_ensure_sandbox_log_group": "ephemeral tool-test sandbox",
    # The RETIRED in-process deploy path (see the module docstring: the route is permanently
    # gone). There is no tag resolution upstream to thread, and inventing an empty one would
    # make an untagged deploy look governed.
    "services/deployment.py::deploy": "retired in-process deploy path",
    # Per-user credential stores reached from a router, not from a deployment. Same shape as
    # the tool-test decision: no deploy request, so no policy to honour, and a required key
    # would break credential storage entirely. They are not deployment resources and no cost
    # report attributes them to an agent.
    "routers/observability.py::store_credentials": "router-scoped credential store",
    "routers/provider_credentials.py::store_provider_credential": "router-scoped credential store",
    "routers/triggers.py::_store_webhook_secret": "router-scoped credential store",
    "services/git_sync.py::store_git_token": "router-scoped credential store",
    # Customer-side reconciliation inside the generated CloudFormation template's custom
    # resource. The tags arrive from the template the customer deploys, in the customer's
    # own account; this platform's namespaces do not apply there.
    "services/cfn_provider/handler.py::_reconcile_agentcore_tags": "customer-side CFN reconcile",
    "services/cfn_provider/handler.py::_reconcile_log_group_tags": "customer-side CFN reconcile",
}

# The census rows whose tag set arrives as a parameter rather than being built in place. They
# are listed separately from _EXCEPTIONS because the reason is different in kind: the site
# itself is neutral and whether the set was governed depends on the CALLER. Each one is here
# because its callers were checked, not because the site looks harmless.
_CALLER_SUPPLIED: dict[str, str] = {
    # The generated template's custom resource, running in the CUSTOMER's account. The tags
    # come from the template the customer deploys, so this platform's namespaces do not apply
    # and validating against them would refuse a tag their own account would accept.
    "services/cfn_provider/handler.py::_apply_log_group_governance": "customer-side CFN reconcile",
}


def test_the_census_reaches_the_sites_it_claims_to_cover():
    """Assert the oracle's reach BEFORE asserting anything with it.

    A walker that silently matched nothing would make every assertion below pass. So pin
    the two shapes that are easy to lose: a governed keyword on a real client call, and the
    ``**kwargs`` dict form that carries no keyword at all.
    """
    rows = _census()
    assert len(rows) >= 25, f"the census found only {len(rows)} tag writes; the walker is broken"
    keyed = {f"{f}::{fn}::{callee}": tagger for f, fn, callee, tagger in rows}
    assert keyed.get("services/gateway_deployer.py::deploy_gateway::create_gateway") == "governed_tags"
    assert keyed.get("services/gateway_deployer.py::_build_openapi_schema::**kwargs") == "owner_tags"
    # And the wrapped form, where the tag set reaches the API through urlencode().
    assert keyed.get("deployment_handler.py::_stage_export_bundle::put_object") == "owner_tags"
    # The IAM pair that only resolves through a local variable.
    assert keyed.get("services/runtime_deployer.py::create_runtime_iam_role::tag_role") == "governed_tag_list"
    # And the subscript form: the adopted-runtime retag reuses the create's own kwargs entry.
    # Pinned here because it is the only site exercising that resolver branch, and a resolver
    # branch nothing asserts is a branch that can silently start returning "governed".
    assert keyed.get("services/runtime_deployer.py::create_agent_runtime::tag_resource") == "governed_tags"
    assert not [r for r in rows if r[3].startswith("<unresolved")], (
        f"unresolved taggers in the census: {[r for r in rows if r[3].startswith('<unresolved')]}. "
        "A site whose tag set cannot be traced to a helper must not be scored as either "
        "governed or excused -- extend the resolver."
    )
    # And the false positive the splat test exists to exclude: a plain dict's ``tags`` key.
    assert "services/aws_agent_registry.py::_normalize_a2a_skill::**kwargs" not in keyed, (
        "the A2A skill projection is being counted as an AWS tag write. It is a dict that is "
        "returned, never splatted into a client call, so the splat test should exclude it; a "
        "census padded with non-AWS rows invites blanket exceptions."
    )


def test_every_caller_supplied_tag_site_is_accounted_for():
    """A site that tags with whatever its caller passed cannot be scored from the site.

    These must be enumerated rather than skipped, because ``<param:...>`` is exactly the
    shape a half-threaded conversion leaves behind: the parameter exists, the tag write uses
    it, and nothing says whether any caller actually fills it with a governed set.
    """
    rows = [r for r in _census() if r[3].startswith("<param:")]
    unaccounted = sorted({f"{rel}::{fn}" for rel, fn, _c, _t in rows} - set(_CALLER_SUPPLIED))
    assert not unaccounted, (
        f"these tag writes stamp a caller-supplied set with no recorded caller analysis: "
        f"{unaccounted}. Trace the callers and add an entry to _CALLER_SUPPLIED, or route the "
        "set through a governed_* helper at this site."
    )


def test_every_aws_tag_write_is_governed_or_documented():
    offenders = []
    for rel, fn, callee, tagger in _census():
        if tagger in _GOVERNED or any(part in _GOVERNED for part in tagger.split("+")):
            continue
        if f"{rel}::{fn}" in _EXCEPTIONS or f"{rel}::{fn}" in _CALLER_SUPPLIED:
            continue
        offenders.append(f"{rel}:{fn} -> {callee}({tagger})")
    assert not offenders, (
        "these AWS tag writes use neither a governed_* helper nor a documented exception:\n  "
        + "\n  ".join(sorted(offenders))
        + "\n\nA resource stamped with owner_tags alone carries no cost-attribution or ABAC "
        "tag, and the operator has no way to see that from the UI. Either route it through "
        "app.services.resource_tagging, or add it to _EXCEPTIONS in this file WITH the reason "
        "and the consequence -- and widen the matching aws:TagKeys allowlist in infra/ in the "
        "same change if you govern it."
    )


def test_no_documented_exception_is_stale():
    """A deferral must not outlive the code it excused.

    If ``_ensure_sandbox_role`` is renamed or converted, the entry here stops matching. Left
    unchecked that is how a temporary exception becomes permanent: the census keeps passing,
    and the reason text keeps asserting a decision about code that no longer exists.
    """
    live = {f"{rel}::{fn}" for rel, fn, _callee, _tagger in _census()}
    stale = sorted((set(_EXCEPTIONS) | set(_CALLER_SUPPLIED)) - live)
    assert not stale, (
        f"these documented tag-write exceptions no longer match any tag write: {stale}. "
        "Delete the entry if the site is gone, or update the key if it moved."
    )


@pytest.mark.parametrize(
    "module",
    [
        "services/gateway_deployer.py",
        "services/harness_deployer.py",
        "services/runtime_deployer.py",
        "step_handlers/knowledge_base_step.py",
        "step_handlers/memory_step.py",
        "step_handlers/mcp_server_step.py",
        "step_handlers/policy_step.py",
        "step_handlers/iam_step.py",
        "step_handlers/evaluation_step.py",
    ],
)
def test_each_deploy_path_module_governs_at_least_one_tag_write(module: str):
    """Per-module, so converting one module cannot mask a module that reaches nothing.

    The aggregate assertion above passes the moment every offender is either governed or
    excused -- including the degenerate case where a whole module's tag writes were deleted.
    These are the modules a deploy actually runs through, so each must show a governed write.
    """
    governed = [r for r in _census() if r[0] == module and r[3].split("+")[0] in _GOVERNED]
    assert governed, f"{module} performs no governed tag write; a deploy through it stamps no governance tag"
