"""Each read the gateway and teardown code makes is granted to exactly the roles that run it.

Three actions, three silent failure modes. None of them surfaces as a 403 anyone reads:

* ``secretsmanager:ListSecrets``: the teardown's tag discovery for a secret no manifest
  row names (a lost CreateSecret response, a step killed before its row). It was granted
  to the gateway and mcp_server step roles, which never enumerate, and NOT to
  status_update, which does. There the discovery 403s and every failed deploy's cleanup
  reports ``delete_retained``: an honest result, but a permanent one.
* ``cognito-idp:DescribeResourceServer``: the shared-pool reuse proof. Granted nowhere,
  while ``_resource_server_has_scope`` swallows the denial and answers False. Every
  redeploy of a gateway name then fails with the create's AlreadyExists, and the
  message says nothing about IAM.
* ``cognito-idp:ListUserPoolClients``: the co-residency check that keeps a resource
  server another deploy still uses. It fails closed, so a missing grant leaks the
  resource server rather than revoking a scope. Quiet either way.

The required set is DERIVED, not listed. The Lambda entry points come from the
synthesized template (Handler -> Role), and each entry's reach comes from a name-based
call graph over backend/src/app, seeded at the boto3 call IAM actually checks. See
``test_every_step_that_reads_the_client_secret_is_allowed_to`` for why a hand-written
list restates the belief that caused the bug. Name-based propagation over-approximates,
which is the safe direction: a false positive demands a grant and gets reviewed, while
a false negative IS the bug.

``_REVIEWED`` pins the derived answer, so a change to it becomes a decision rather than
a side effect. ARCC cnt_SFJJhkOueCPRkd: a `*` resource is kept only where the action has
no resource-level scoping (ListSecrets), and nowhere it is not used.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

from tests.iam_attachment import statements_for_role

_REPO = pathlib.Path(__file__).resolve().parents[2]
# A worktree that symlinks infra/ reads another tree's backend; point it at its own.
_BACKEND_SRC = pathlib.Path(os.environ.get("CALL_GRAPH_BACKEND_SRC") or _REPO / "backend" / "src" / "app")
_TARGET_ROLE_DOC = _REPO / "docs" / "cross-account-deploy-role.json"

#: IAM action -> the boto3 operation it authorizes.
SEEDS = {
    "secretsmanager:ListSecrets": "list_secrets",
    "cognito-idp:DescribeResourceServer": "describe_resource_server",
    "cognito-idp:ListUserPoolClients": "list_user_pool_clients",
}

#: Handler module (relative to backend/src/app) of each Lambda that must hold the action.
_REVIEWED = {
    "secretsmanager:ListSecrets": {
        # _run_delete_cleanup -> unrecorded_deployment_secret_rows (the user DELETE).
        "deployment_handler.py",
        # _auto_cleanup_on_failure -> unrecorded_deployment_secret_rows.
        "step_handlers/status_update_step.py",
    },
    "cognito-idp:DescribeResourceServer": {
        # deploy_gateway -> _create_cognito_oauth -> the shared-pool reuse proof.
        "step_handlers/gateway_step.py",
    },
    "cognito-idp:ListUserPoolClients": {
        # _delete_managed_resource -> resource_server_is_unused (teardown).
        "deployment_handler.py",
        # deploy_gateway -> cleanup_gateway_resources -> resource_server_is_unused (a
        # failed deploy's own rollback of the resource server it created).
        "step_handlers/gateway_step.py",
        # _cleanup_resource -> resource_server_is_unused.
        "step_handlers/status_update_step.py",
    },
}

_ROUTE_VERBS = {"get", "post", "put", "patch", "delete"}


# --------------------------------------------------------------------------------
# The backend call graph.
# --------------------------------------------------------------------------------


def _references_in(node) -> tuple[set[tuple[bool, str]], set[str]]:
    """``({(is_bare_name, name)}, string constants passed to a call)`` in *node*.

    A function passed as a call argument counts as called (callbacks, executors), and
    a string argument is how ``list_all(client, "list_user_pool_clients", ...)`` names
    its operation; a docstring is not a call argument, so it never seeds.
    """
    refs: set[tuple[bool, str]] = set()
    strings: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        for ref in [child.func, *child.args, *(kw.value for kw in child.keywords)]:
            if isinstance(ref, ast.Name):
                refs.add((True, ref.id))
            elif isinstance(ref, ast.Attribute):
                refs.add((False, ref.attr))
            elif ref is not child.func and isinstance(ref, ast.Constant) and isinstance(ref.value, str):
                strings.add(ref.value)
    return refs, strings


def _is_route(node) -> bool:
    for deco in node.decorator_list:
        if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute) and deco.func.attr in _ROUTE_VERBS:
            return True
    return False


def _module_path(module: str | None, level: int, rel: str) -> str | None:
    """``app.routers.admin`` (or ``.admin`` inside routers/) -> ``routers/admin.py``."""
    if level:
        base = pathlib.PurePosixPath(rel).parents[level - 1]
        parts = [*base.parts, *(module or "").split(".")]
    elif module and module.startswith("app."):
        parts = module.split(".")[1:]
    else:
        return None
    return "/".join(p for p in parts if p) + ".py"


class _Graph:
    def __init__(self) -> None:
        self.calls: dict[tuple[str, str], set[tuple[bool, str]]] = {}
        self.strings: dict[tuple[str, str], set[str]] = {}
        self.routes: set[tuple[str, str]] = set()
        # Module -> the app modules it imports (``from app.X import ...``).
        self.imports: dict[str, set[str]] = {}
        for path in sorted(_BACKEND_SRC.rglob("*.py")):
            rel = str(path.relative_to(_BACKEND_SRC))
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    target = _module_path(node.module, node.level, rel)
                    if target:
                        self.imports.setdefault(rel, set()).add(target)
                        # ``from app.routers import workflows_router``: the package
                        # re-exports; its __init__ says from where.
                        self.imports[rel].add(target[: -len(".py")] + "/__init__.py")
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    key = (rel, node.name)
                    names, strings = _references_in(node)
                    self.calls.setdefault(key, set()).update(names)
                    self.strings.setdefault(key, set()).update(strings)
                    if _is_route(node):
                        self.routes.add(key)
        self.by_name: dict[str, set[tuple[str, str]]] = {}
        for key in self.calls:
            self.by_name.setdefault(key[1], set()).add(key)

    def seeds(self, op: str) -> set[tuple[str, str]]:
        return {key for key in self.calls if op in {n for _b, n in self.calls[key]} or op in self.strings[key]}

    def callees(self, key: tuple[str, str]) -> set[tuple[str, str]]:
        """A bare name defined in the caller's own module is that definition (Python's
        lexical scope); anything else is every function of that name. The latter
        over-approximates, the safe direction."""
        out: set[tuple[str, str]] = set()
        for bare, name in self.calls[key]:
            local = (key[0], name)
            out |= {local} if bare and local in self.calls else self.by_name.get(name, set())
        return out

    def served_modules(self, rel: str) -> set[str]:
        """The entry module, plus every module whose routes its app can serve: the
        ones it imports, transitively through ``app.main`` and ``app.routers``."""
        out, todo = set(), [rel]
        while todo:
            mod = todo.pop()
            if mod in out:
                continue
            out.add(mod)
            todo.extend(m for m in self.imports.get(mod, ()) if m in {"main.py"} or m.startswith("routers/"))
        return out

    def reach(self, rel: str, entry: str) -> set[tuple[str, str]]:
        # FastAPI dispatches to a route by decorator, not by call, so each route an
        # entry's app mounts is a root. Only ITS app's: the deployment Lambda's routes
        # are not the workflow Lambda's.
        served = self.served_modules(rel)
        roots = {(rel, entry)} | {r for r in self.routes if r[0] in served}
        seen: set[tuple[str, str]] = set()
        todo = [r for r in roots if r in self.calls]
        while todo:
            key = todo.pop()
            if key in seen:
                continue
            seen.add(key)
            todo.extend(self.callees(key))
        return seen


@pytest.fixture(scope="module")
def graph() -> _Graph:
    return _Graph()


# --------------------------------------------------------------------------------
# The template.
# --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def template_json():
    app = cdk.App()
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name="test",
        project_name="agentcore-workflow",
        env=cdk.Environment(region="us-east-1"),
    )
    return Template.from_stack(stack).to_json()


@pytest.fixture(scope="module")
def entries(template_json) -> dict[str, str]:
    """``{handler module relpath: role logical id}`` for every backend Lambda."""
    out: dict[str, str] = {}
    for resource in template_json["Resources"].values():
        if resource["Type"] != "AWS::Lambda::Function":
            continue
        handler = resource["Properties"].get("Handler") or ""
        if not handler.startswith("src/app/"):
            continue
        module, _, _fn = handler.removeprefix("src/app/").rpartition(".")
        out[f"{module}.py"] = resource["Properties"]["Role"]["Fn::GetAtt"][0]
    return out


def _entry_function(template_json, rel: str) -> str:
    for resource in template_json["Resources"].values():
        handler = (resource.get("Properties") or {}).get("Handler") or ""
        if resource["Type"] == "AWS::Lambda::Function" and handler.startswith(f"src/app/{rel[:-3]}."):
            return handler.rpartition(".")[2]
    raise AssertionError(rel)


def _derived(graph: _Graph, template_json, entries, op: str) -> set[str]:
    seeds = graph.seeds(op)
    return {rel for rel in entries if graph.reach(rel, _entry_function(template_json, rel)) & seeds}


def _allows(statements, action: str) -> list[dict]:
    out = []
    for _src, st in statements:
        acts = st.get("Action")
        acts = [acts] if isinstance(acts, str) else (acts or [])
        if st.get("Effect") == "Allow" and action in acts:
            out.append(st)
    return out


# --------------------------------------------------------------------------------
# Tests.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("action", sorted(SEEDS))
def test_the_seed_is_a_real_call_site(graph, action):
    """Vacuity guard: an empty seed makes every grant assertion below pass for free."""
    assert graph.seeds(SEEDS[action]), f"nothing in backend/src/app calls {SEEDS[action]}"


def test_every_backend_lambda_was_found(entries):
    """Vacuity guard on the entry side: the template is the source of the entries."""
    assert {"deployment_handler.py", "step_handlers/status_update_step.py", "step_handlers/gateway_step.py"} <= set(
        entries
    )
    assert len(entries) >= 15


@pytest.mark.parametrize("action", sorted(SEEDS))
def test_the_derived_callers_are_the_reviewed_ones(graph, template_json, entries, action):
    derived = _derived(graph, template_json, entries, SEEDS[action])
    assert derived == _REVIEWED[action], (
        f"the Lambdas reaching {SEEDS[action]}() changed.\n"
        f"  derived:  {sorted(derived)}\n  reviewed: {sorted(_REVIEWED[action])}\n"
        f"Grant or revoke {action} to match, then update _REVIEWED naming the call path."
    )


@pytest.mark.parametrize("action", sorted(SEEDS))
def test_every_caller_holds_the_grant(graph, template_json, entries, action):
    missing = [
        f"{rel} ({entries[rel]})"
        for rel in sorted(_derived(graph, template_json, entries, SEEDS[action]))
        if not _allows(statements_for_role(template_json, entries[rel]), action)
    ]
    assert not missing, f"these Lambdas call {SEEDS[action]}() but their role cannot: {missing}"


@pytest.mark.parametrize("action", sorted(SEEDS))
def test_no_other_backend_lambda_holds_the_grant(graph, template_json, entries, action):
    """The least-privilege half, and a second vacuity guard: if every role held it,
    the test above would pass no matter what."""
    needed = _derived(graph, template_json, entries, SEEDS[action])
    over = [
        rel
        for rel, role in sorted(entries.items())
        if rel not in needed and _allows(statements_for_role(template_json, role), action)
    ]
    assert not over, f"granted {action} but never call {SEEDS[action]}(): {over}"


@pytest.mark.parametrize("action", ["cognito-idp:DescribeResourceServer", "cognito-idp:ListUserPoolClients"])
def test_the_cognito_reads_are_scoped_to_user_pools(template_json, entries, action):
    for rel in _REVIEWED[action]:
        for st in _allows(statements_for_role(template_json, entries[rel]), action):
            resources = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
            for res in resources:
                assert res != "*", f"{rel}'s {action} is on '*'"
                if isinstance(res, str):
                    assert ":userpool/" in res, res


def test_the_target_account_role_holds_every_action():
    """A cross-account deploy and its teardown run the same code under the assumed
    target role, so it needs the union."""
    doc = json.loads(_TARGET_ROLE_DOC.read_text())
    actions: set[str] = set()
    for key, policy in doc.items():
        if key.startswith("permissions-policy") and key.endswith(".json"):
            for st in policy["Statement"]:
                if st.get("Effect") == "Allow":
                    value = st.get("Action") or []
                    actions.update([value] if isinstance(value, str) else value)
    assert set(SEEDS) <= actions, f"missing from the target role: {sorted(set(SEEDS) - actions)}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
