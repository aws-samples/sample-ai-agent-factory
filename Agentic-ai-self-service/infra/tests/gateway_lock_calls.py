"""Follow a call through ``GatewayLock`` to the control-plane call it makes (F-66e).

``GatewayLock.update`` and ``GatewayLock.delete`` are the only places the platform
calls UpdateGateway and DeleteGateway, so a call site reads ``gw_lock.delete(...)``
and never ``ctrl.delete_gateway(...)``. An IAM oracle that scans a function for calls
on its control client then no longer sees DeleteGateway, although the step's role
still needs it. These helpers map each lock method to the ``self._ctrl`` calls it
makes, transitively through the class's own helpers (``_poll``), and find the
``with gateway_mutation_lock(<client>, ...) as <lock>:`` bindings in a function, so
an oracle can count ``<lock>.delete()`` as the call on ``<client>`` it really is.
"""

from __future__ import annotations

import ast
import pathlib

LOCK_SRC = (
    pathlib.Path(__file__).resolve().parents[2] / "backend" / "src" / "app" / "services" / "gateway_mutation_lock.py"
)
LOCK_CLASS = "GatewayLock"
LOCK_CONTEXT = "gateway_mutation_lock"


def _is_self_attr(node: ast.AST, attr: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def lock_method_ctrl_calls(source: pathlib.Path = LOCK_SRC) -> dict[str, set[str]]:
    """``{method: {ctrl method, ...}}`` for every public ``GatewayLock`` method."""
    tree = ast.parse(source.read_text())
    cls = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == LOCK_CLASS),
        None,
    )
    assert cls is not None, f"{LOCK_CLASS} not found in {source.name}; the lock oracle measures nothing"
    methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}

    direct: dict[str, set[str]] = {}
    helpers: dict[str, set[str]] = {}
    for name, fn in methods.items():
        direct[name] = set()
        helpers[name] = set()
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if _is_self_attr(node.func.value, "_ctrl"):
                direct[name].add(node.func.attr)
            elif isinstance(node.func.value, ast.Name) and node.func.value.id == "self" and node.func.attr in methods:
                helpers[name].add(node.func.attr)

    def _closure(name: str, seen: frozenset[str] = frozenset()) -> set[str]:
        out = set(direct[name])
        for h in helpers[name] - seen:
            out |= _closure(h, seen | {name})
        return out

    return {name: _closure(name) for name in methods if not name.startswith("_")}


def lock_bindings(fn: ast.AST) -> dict[str, str]:
    """``{lock variable: client variable}`` for each ``with gateway_mutation_lock(c, ...) as v``."""
    out: dict[str, str] = {}
    for node in ast.walk(fn):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            call = item.context_expr
            if not (isinstance(call, ast.Call) and call.args and isinstance(item.optional_vars, ast.Name)):
                continue
            f = call.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name == LOCK_CONTEXT and isinstance(call.args[0], ast.Name):
                out[item.optional_vars.id] = call.args[0].id
    return out


def calls_through_locks(fn: ast.AST, lock_calls: dict[str, set[str]] | None = None) -> set[tuple[str, str]]:
    """``(client variable, ctrl method)`` for every lock-method call in *fn*."""
    lock_calls = lock_method_ctrl_calls() if lock_calls is None else lock_calls
    bindings = lock_bindings(fn)
    out: set[tuple[str, str]] = set()
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Name) and receiver.id in bindings:
            assert node.func.attr in lock_calls, (
                f"{receiver.id}.{node.func.attr}() is not a {LOCK_CLASS} method this oracle knows"
            )
            out |= {(bindings[receiver.id], m) for m in lock_calls[node.func.attr]}
    return out
