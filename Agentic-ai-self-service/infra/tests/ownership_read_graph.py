"""Which AgentCore resource types each step handler can READ OWNERSHIP TAGS from.

The companion to ``handler_call_graph``, for the read half of the tagging model rather than
the write half, and it exists because transcribing this by hand got it wrong. A ``grep`` for
``assert_agentcore_resource_owned`` limited to ``step_handlers/`` finds three steps. The real
answer is six, because nine more call sites live in ``runtime_deployer``, ``gateway_deployer``
and ``harness_deployer``, reached from the steps that import them. A read the role cannot
perform fails CLOSED -- ``ResourceDeletionRefused`` / adoption refused -- so a missing grant
does not degrade a redeploy, it blocks it, and leaves the resource running.

Two deliberate differences from ``handler_call_graph._Graph``:

1. It follows ``module.function()`` calls, not only bare-``Name`` calls. That is the exact
   blind spot ``_GRAPH_BLIND_SPOTS`` documents over there, and here it is not survivable: the
   harness step's ONLY route to its ownership reads is
   ``harness_step.py`` -> ``harness_deployer.ensure_gateway_outbound_provider(...)``, an
   attribute call. A Name-only walker reports the harness step as reading nothing.
2. It records the resource-type argument, not just the fact of the call, because the grant is
   per resource type. Only string-literal types are recorded; a computed one is returned
   separately as an unresolved site so it can never be silently dropped.

Over-estimating by design, in the same direction and for the same reason as its companion:
the cost of an extra type in a grant is a narrow unused read, and the cost of a missing one
is a blocked deploy.
"""

from __future__ import annotations

import ast
import pathlib

BACKEND_SRC = pathlib.Path(__file__).resolve().parents[2] / "backend" / "src"

OWNERSHIP_FUNC = "assert_agentcore_resource_owned"

#: resource_ownership's own type label -> the key in
#: ``step_lambdas.AGENTCORE_TYPE_ARN_TAIL``. The two vocabularies differ and neither is
#: wrong: the labels are what the service's client methods are named after
#: (``get_agent_runtime``, ``get_policy_engine``), the ARN keys are what the ARN segment is
#: called. Mapped explicitly rather than normalized with ``replace("_", "-")``, because that
#: would turn ``agent_runtime`` into ``agent-runtime``, which is not a resource shape, and an
#: unmatched key is a silently missing grant.
OWNERSHIP_LABEL_TO_ARN_KEY = {
    "agent_runtime": "runtime",
    "gateway": "gateway",
    "memory": "memory",
    "policy_engine": "policy-engine",
    "harness": "harness",
    "oauth2_credential_provider": "oauth2credentialprovider",
    "api_key_credential_provider": "apikeycredentialprovider",
}


def _module_path(dotted: str) -> pathlib.Path | None:
    p = BACKEND_SRC / (dotted.replace(".", "/") + ".py")
    if p.is_file():
        return p
    p = BACKEND_SRC / dotted.replace(".", "/") / "__init__.py"
    return p if p.is_file() else None


class _ReadGraph:
    """Walks a handler's transitive calls, recording every ownership read it can reach."""

    def __init__(self) -> None:
        self._trees: dict[str, ast.Module | None] = {}

    def tree(self, dotted: str) -> ast.Module | None:
        if dotted not in self._trees:
            p = _module_path(dotted)
            self._trees[dotted] = ast.parse(p.read_text()) if p else None
        return self._trees[dotted]

    @staticmethod
    def _defs(tree: ast.Module) -> dict[str, ast.AST]:
        return {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    @staticmethod
    def _name_imports(node: ast.AST) -> dict[str, tuple[str, str]]:
        """``{local name: (module, original name)}`` for ``from app.x import y``."""
        out: dict[str, tuple[str, str]] = {}
        for n in ast.walk(node):
            if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("app."):
                for a in n.names:
                    out[a.asname or a.name] = (n.module, a.name)
        return out

    @staticmethod
    def _module_imports(node: ast.AST) -> dict[str, str]:
        """``{local name: dotted module}`` for the MODULE-object import shape.

        Covers both ``from app.services import harness_deployer`` and
        ``import app.services.harness_deployer as harness_deployer``. This is the shape the
        companion walker cannot follow, and the harness step's reads are only reachable
        through it.
        """
        out: dict[str, str] = {}
        for n in ast.walk(node):
            if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("app"):
                for a in n.names:
                    # `from app.services import harness_deployer` -- only a module if a file
                    # of that name exists; otherwise it is a function and _name_imports has
                    # it already.
                    dotted = f"{n.module}.{a.name}"
                    if _module_path(dotted) is not None:
                        out[a.asname or a.name] = dotted
            elif isinstance(n, ast.Import):
                for a in n.names:
                    if a.name.startswith("app.") and _module_path(a.name) is not None:
                        out[a.asname or a.name.split(".")[-1]] = a.name
        return out

    @staticmethod
    def _loop_bound_strings(node: ast.AST, name: str) -> list[str]:
        """Literal strings *name* can take as a ``for`` target inside *node*.

        ``delete_owned_credential_provider`` is the reason this exists. It loops over a
        literal tuple of ``(resource_type, delete_method)`` pairs and calls the ownership
        assert with the loop variable, so the type is not a constant at the call site:

            for resource_type, delete_method in (
                ("oauth2_credential_provider", "delete_oauth2_credential_provider"),
                ("api_key_credential_provider", "delete_api_key_credential_provider"),
            ):

        Both namespaces are therefore read on one call, and the loop only ``continue``s when
        the exception is a MISSING resource. An AccessDeniedException is not missing, so it
        re-raises -- and because oauth2 is probed FIRST and deleted before the api-key probe
        runs, a role holding the read for only one type tears down HALF the providers and
        then fails. That is worse than failing closed, so the api-key type cannot be dropped.
        """
        out: list[str] = []
        for n in ast.walk(node):
            if not isinstance(n, ast.For):
                continue
            target = n.target
            # `for x in ...` -- the whole element is the type.
            if isinstance(target, ast.Name) and target.id == name:
                idx = None
            # `for x, y in ...` -- the type is at x's position in each tuple.
            elif isinstance(target, ast.Tuple) and any(isinstance(e, ast.Name) and e.id == name for e in target.elts):
                idx = next(i for i, e in enumerate(target.elts) if isinstance(e, ast.Name) and e.id == name)
            else:
                continue
            if not isinstance(n.iter, (ast.Tuple, ast.List, ast.Set)):
                continue
            for elt in n.iter.elts:
                if idx is None:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        out.append(elt.value)
                elif isinstance(elt, (ast.Tuple, ast.List)) and len(elt.elts) > idx:
                    e = elt.elts[idx]
                    if isinstance(e, ast.Constant) and isinstance(e.value, str):
                        out.append(e.value)
        return out

    def walk(
        self,
        dotted: str,
        func: str,
        seen: set[tuple[str, str]],
        types: dict[str, set[str]],
        unresolved: set[str],
    ) -> None:
        if (dotted, func) in seen:
            return
        seen.add((dotted, func))
        tree = self.tree(dotted)
        if tree is None:
            return
        defs = self._defs(tree)
        if func not in defs:
            return
        node = defs[func]
        names = self._name_imports(tree)
        names.update(self._name_imports(node))
        mods = self._module_imports(tree)
        mods.update(self._module_imports(node))

        for n in ast.walk(node):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            called_name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if called_name == OWNERSHIP_FUNC:
                site = f"{dotted}.{func}:{n.lineno}"
                # (client, resource_type, resource_id, region) -- the type is positional 1.
                arg = n.args[1] if len(n.args) > 1 else None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    types.setdefault(arg.value, set()).add(site)
                elif isinstance(arg, ast.Name) and (bound := self._loop_bound_strings(node, arg.id)):
                    for v in bound:
                        types.setdefault(v, set()).add(site)
                else:
                    unresolved.add(site)
            if isinstance(f, ast.Name):
                if f.id in names:
                    m, orig = names[f.id]
                    self.walk(m, orig, seen, types, unresolved)
                elif f.id in defs:
                    self.walk(dotted, f.id, seen, types, unresolved)
            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in mods:
                self.walk(mods[f.value.id], f.attr, seen, types, unresolved)


def reachable_ownership_read_types(handler_ref: str) -> tuple[set[str], int, dict[str, set[str]], set[str]]:
    """``(ARN-table keys, functions reached, {label: sites}, unresolved sites)``.

    *handler_ref* is the CDK ``handler=`` string, e.g.
    ``"src/app/step_handlers/memory_step.handler"``.

    The reached count is returned for the same reason the companion returns it: a typo'd
    handler ref would otherwise yield an empty set, which reads as "reads nothing, no grant
    needed" -- the failure shape this module exists to prevent.
    """
    path, _, func = handler_ref.rpartition(".")
    dotted = path.removeprefix("src/").replace("/", ".")
    labels: dict[str, set[str]] = {}
    unresolved: set[str] = set()
    seen: set[tuple[str, str]] = set()
    _ReadGraph().walk(dotted, func, seen, labels, unresolved)
    keys = set()
    for label in labels:
        # An unmapped label is a hard error rather than a skip: it means a new resource type
        # grew an ownership read and the grant table cannot express it yet.
        if label not in OWNERSHIP_LABEL_TO_ARN_KEY:
            raise AssertionError(
                f"{OWNERSHIP_FUNC} is called with resource type {label!r} at "
                f"{sorted(labels[label])}, which has no entry in OWNERSHIP_LABEL_TO_ARN_KEY. "
                "Add it there and to step_lambdas.AGENTCORE_TYPE_ARN_TAIL, or the role that "
                "reaches this call site cannot be granted the read and the path fails closed."
            )
        keys.add(OWNERSHIP_LABEL_TO_ARN_KEY[label])
    return keys, len(seen), labels, unresolved
