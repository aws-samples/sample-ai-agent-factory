"""YAML is parsed only by safe loaders driven directly, never through ``yaml.load`` (AppSec ACAT UnsafeYAMLLoad).

An AppSec scan of the repository flagged four ``yaml.load(...)`` calls, one in the gateway
deployer's static-schema parser and three in export tests. Every one already passed a safe
loader (a ``SafeLoader`` subclass or ``BaseLoader``), so none could instantiate arbitrary
objects. The finding is about the entry point itself: ``yaml.load`` is the function that
WILL run ``!!python/object/apply`` payloads when handed ``yaml.Loader``/``UnsafeLoader``,
and a safe call site is one refactor away from an unsafe one. The fix drives each safe
loader directly (construct, ``get_single_data()``, ``dispose()``, which is exactly what
``yaml.load`` does) so the unsafe entry point is no longer referenced anywhere.

This module pins both halves:

  * a lint over the backend source and test trees: no module references the unsafe PyYAML
    entry points (``load``, ``load_all``, ``full_load``, ``unsafe_load`` and their
    ``_all`` forms), the loaders that make them unsafe (``Loader``, ``FullLoader``,
    ``UnsafeLoader`` and their ``C`` variants), or the constructors a custom loader would
    compose them from. It works on the AST, so comments and docstrings cannot trip it, and
    a direct reference through any of the import forms it resolves (``import yaml as y``,
    ``from yaml import load as x``, ``import yaml.loader as yl``, ``from yaml import loader
    as yl``) is caught, as is a ``getattr`` on the yaml module with a literal or computed
    name. Names are resolved against the bindings in effect where they are used: imports
    bind in source order; a non-yaml import, assignment, ``del``, ``except ... as``, ``with
    ... as``, loop target or parameter of the same name unbinds, while a bare annotation
    (``yaml: ModuleType``) only declares and keeps the binding; across alternative paths
    (``if``/``else``, ``try``/``except``, loops, ``match``) a name keeps every binding it may
    have, so ``try: import yaml`` / ``except ImportError: yaml = None`` still resolves to
    PyYAML afterwards; a function's own bindings win inside that function; a class body's
    bindings are not visible to its methods; and a definition's header (decorators, bases,
    keywords, defaults, annotations) is checked in the defining scope so a safe loader
    subclass cannot be quietly rebased onto an unsafe one. Only absolute imports count, so a
    sibling module that happens to be called ``yaml`` is not reported. It is a lint, not a
    proof: code that reaches PyYAML through ``importlib``, a variable or attribute holding
    the module, ``global``/``nonlocal`` rebinding, a lambda or comprehension parameter that
    shadows a module alias, or an indirection it cannot see is for review to catch;
  * the production parser still behaves as a safe loader: a ``!!python/object/apply``
    payload is refused as invalid YAML and never reaches ``os.system`` (a tripwire replaces
    it for the duration of the test, so even an unsafe loader could execute nothing),
    duplicate keys are still refused, and a plain OpenAPI document still parses.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest
import yaml
from app.services.gateway_deployer import _load_static_schema_document

BACKEND = Path(__file__).resolve().parents[1]

# Every name that, imported from the module on the left, hands code an unsafe way to turn
# YAML into Python objects. ``safe_load``, ``SafeLoader``, ``CSafeLoader`` and ``BaseLoader``
# are deliberately absent.
UNSAFE_NAMES_BY_MODULE: dict[str, frozenset[str]] = {
    "yaml": frozenset(
        {
            "load",
            "load_all",
            "full_load",
            "full_load_all",
            "unsafe_load",
            "unsafe_load_all",
            "Loader",
            "FullLoader",
            "UnsafeLoader",
            "CLoader",
            "CFullLoader",
            "CUnsafeLoader",
        }
    ),
    "yaml.loader": frozenset({"Loader", "FullLoader", "UnsafeLoader"}),
    "yaml.cyaml": frozenset({"CLoader", "CFullLoader", "CUnsafeLoader"}),
    "yaml.constructor": frozenset({"Constructor", "FullConstructor", "UnsafeConstructor"}),
}

_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_TRY_NODES = (ast.Try, getattr(ast, "TryStar", ast.Try))

Bindings = dict[str, frozenset[str]]  # local name -> every watched yaml module it may be bound to
Offender = tuple[int, str]  # (line, description)
FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def _python_sources() -> list[Path]:
    files = [p for root in ("src", "tests") for p in (BACKEND / root).rglob("*.py")]
    # .venv / lib never sit under src or tests, but build helpers may symlink; be explicit.
    return sorted(p for p in files if ".venv" not in p.parts and "lib" not in p.parts)


def _merge(*paths: Bindings) -> Bindings:
    """The bindings a name may have after any one of several alternative paths ran."""
    merged: dict[str, set[str]] = {}
    for path in paths:
        for name, modules in path.items():
            merged.setdefault(name, set()).update(modules)
    return {name: frozenset(modules) for name, modules in merged.items()}


def _parameter_names(function: FunctionNode) -> set[str]:
    a = function.args
    names = {arg.arg for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs]}
    names |= {extra.arg for extra in (a.vararg, a.kwarg) if extra is not None}
    return names


def _unbind_targets(target: ast.AST, bindings: Bindings) -> None:
    for node in ast.walk(target):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bindings.pop(node.id, None)


def _bind_import(node: ast.Import, bindings: Bindings) -> None:
    for alias in node.names:
        if alias.name == "yaml" or alias.name.startswith("yaml."):
            if alias.asname:
                bindings[alias.asname] = frozenset({alias.name})
            else:
                bindings["yaml"] = frozenset({"yaml"})  # ``import yaml.loader`` binds the top-level name
        else:
            bindings.pop(alias.asname or alias.name.split(".")[0], None)


def _bind_import_from(node: ast.ImportFrom, bindings: Bindings) -> None:
    for alias in node.names:
        if alias.name == "*":
            continue
        submodule = f"{node.module}.{alias.name}" if node.level == 0 and node.module else None
        if submodule in UNSAFE_NAMES_BY_MODULE:
            bindings[alias.asname or alias.name] = frozenset({submodule})
        else:
            bindings.pop(alias.asname or alias.name, None)


def _dotted(node: ast.AST) -> list[str] | None:
    """``a.b.c`` as ``["a", "b", "c"]``; None for anything that is not a plain name chain."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return parts[::-1]


def _resolve(parts: list[str], bindings: Bindings) -> list[tuple[str, str]]:
    """Every (watched yaml module, member) a dotted reference may denote under ``bindings``."""
    resolved: list[tuple[str, str]] = []
    for alias_target in bindings.get(parts[0], ()):
        full = ".".join([alias_target, *parts[1:]])
        for module in sorted(UNSAFE_NAMES_BY_MODULE, key=len, reverse=True):
            if full.startswith(module + ".") and "." not in full[len(module) + 1 :]:
                resolved.append((module, full[len(module) + 1 :]))
                break
    return resolved


def _getattr_offender(node: ast.Call, bindings: Bindings) -> Offender | None:
    if not (isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2):
        return None
    parts = _dotted(node.args[0])
    if not parts:
        return None
    modules = {".".join([alias_target, *parts[1:]]) for alias_target in bindings.get(parts[0], ())}
    modules &= set(UNSAFE_NAMES_BY_MODULE)
    if not modules:
        return None
    name = node.args[1]
    if isinstance(name, ast.Constant) and isinstance(name.value, str):
        if any(name.value in UNSAFE_NAMES_BY_MODULE[module] for module in modules):
            return node.lineno, f'getattr({".".join(parts)}, "{name.value}")'
        return None
    return node.lineno, f"getattr({'.'.join(parts)}, <computed name>)"


def _scan_expression(expr: ast.AST | None, bindings: Bindings, found: list[Offender]) -> None:
    """Report unsafe references inside an expression; a walrus target is unbound afterwards."""
    if expr is None:
        return
    walrus_targets: list[ast.AST] = []
    for node in ast.walk(expr):
        if isinstance(node, ast.Attribute):
            parts = _dotted(node)
            if parts and any(member in UNSAFE_NAMES_BY_MODULE[module] for module, member in _resolve(parts, bindings)):
                found.append((node.lineno, ".".join(parts)))
        elif isinstance(node, ast.Call):
            offender = _getattr_offender(node, bindings)
            if offender:
                found.append(offender)
        elif isinstance(node, ast.NamedExpr):
            walrus_targets.append(node.target)
    for target in walrus_targets:
        _unbind_targets(target, bindings)


def _scan_block(
    stmts: list[ast.stmt], bindings: Bindings, found: list[Offender], deferred: list[FunctionNode]
) -> Bindings:
    """Scan statements in order and return the bindings in effect afterwards (``bindings`` is left intact)."""
    current = dict(bindings)
    for stmt in stmts:
        current = _scan_statement(stmt, current, found, deferred)
    return current


def _scan_statement(stmt: ast.stmt, current: Bindings, found: list[Offender], deferred: list[FunctionNode]) -> Bindings:
    if isinstance(stmt, ast.Import):
        _bind_import(stmt, current)
    elif isinstance(stmt, ast.ImportFrom):
        if stmt.level == 0 and stmt.module in UNSAFE_NAMES_BY_MODULE:
            for alias in stmt.names:
                if alias.name == "*" or alias.name in UNSAFE_NAMES_BY_MODULE[stmt.module]:
                    found.append((stmt.lineno, f"from {stmt.module} import {alias.name}"))
        _bind_import_from(stmt, current)
    elif isinstance(stmt, _FUNCTION_NODES):
        # The header is evaluated now, in this scope; the body runs later and is scanned with
        # the bindings in effect at the end of this scope.
        for decorator in stmt.decorator_list:
            _scan_expression(decorator, current, found)
        _scan_expression(stmt.args, current, found)
        _scan_expression(stmt.returns, current, found)
        current.pop(stmt.name, None)
        deferred.append(stmt)
    elif isinstance(stmt, ast.ClassDef):
        for decorator in stmt.decorator_list:
            _scan_expression(decorator, current, found)
        for base in stmt.bases:
            _scan_expression(base, current, found)
        for keyword in stmt.keywords:
            _scan_expression(keyword.value, current, found)
        # The class body runs now and sees the enclosing bindings plus its own; its methods
        # run later and see only the enclosing scope, so they are deferred to it.
        _scan_block(stmt.body, current, found, deferred)
        current.pop(stmt.name, None)
    elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        if isinstance(stmt, ast.AnnAssign):
            _scan_expression(stmt.annotation, current, found)
            if stmt.value is None:
                return current  # a bare annotation (``yaml: ModuleType``) declares; it binds nothing
        _scan_expression(stmt.value, current, found)
        for target in stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]:
            _scan_expression(target, current, found)
            _unbind_targets(target, current)
    elif isinstance(stmt, ast.Delete):
        for target in stmt.targets:
            _unbind_targets(target, current)
    elif isinstance(stmt, ast.If):
        _scan_expression(stmt.test, current, found)
        return _merge(
            _scan_block(stmt.body, current, found, deferred), _scan_block(stmt.orelse, current, found, deferred)
        )
    elif isinstance(stmt, ast.While):
        _scan_expression(stmt.test, current, found)
        after_body = _scan_block(stmt.body, current, found, deferred)
        after_body = _scan_block(stmt.body, _merge(current, after_body), found, deferred)  # loop-carried bindings
        return _scan_block(stmt.orelse, _merge(current, after_body), found, deferred)
    elif isinstance(stmt, (ast.For, ast.AsyncFor)):
        _scan_expression(stmt.iter, current, found)
        entry = dict(current)
        _unbind_targets(stmt.target, entry)
        after_body = _scan_block(stmt.body, entry, found, deferred)
        looped = _merge(entry, after_body)
        _unbind_targets(stmt.target, looped)
        after_body = _scan_block(stmt.body, looped, found, deferred)  # loop-carried bindings
        return _scan_block(stmt.orelse, _merge(current, after_body), found, deferred)  # zero iterations possible
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        for item in stmt.items:
            _scan_expression(item.context_expr, current, found)
            if item.optional_vars is not None:
                _unbind_targets(item.optional_vars, current)
        return _scan_block(stmt.body, current, found, deferred)
    elif isinstance(stmt, _TRY_NODES):
        after_body = _scan_block(stmt.body, current, found, deferred)
        paths = [_scan_block(stmt.orelse, after_body, found, deferred)]
        for handler in stmt.handlers:
            entry = _merge(current, after_body)  # the body may have run any prefix before raising
            _scan_expression(handler.type, entry, found)
            if handler.name:
                entry.pop(handler.name, None)
            paths.append(_scan_block(handler.body, entry, found, deferred))
        return _scan_block(stmt.finalbody, _merge(*paths), found, deferred)
    elif isinstance(stmt, ast.Match):
        _scan_expression(stmt.subject, current, found)
        paths = [current]  # no case may match
        for case in stmt.cases:
            entry = dict(current)
            for node in ast.walk(case.pattern):
                for bound in (getattr(node, "name", None), getattr(node, "rest", None)):
                    if isinstance(bound, str):
                        entry.pop(bound, None)
            _scan_expression(case.guard, entry, found)
            paths.append(_scan_block(case.body, entry, found, deferred))
        return _merge(*paths)
    else:  # Expr, Return, Raise, Assert, Pass, Break, Continue, Global, Nonlocal, TypeAlias ...
        for child in ast.iter_child_nodes(stmt):
            _scan_expression(child, current, found)
    return current


def _scan_scope(stmts: list[ast.stmt], inherited: Bindings, found: list[Offender]) -> None:
    deferred: list[FunctionNode] = []
    final = _scan_block(stmts, inherited, found, deferred)
    for function in deferred:
        parameters = _parameter_names(function)
        _scan_scope(function.body, {name: m for name, m in final.items() if name not in parameters}, found)


def _unsafe_references(path: Path) -> list[Offender]:
    """Every (line, description) in ``path`` that may reach an unsafe PyYAML name."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[Offender] = []
    _scan_scope(tree.body, {}, found)
    return sorted(set(found))


def test_no_backend_module_references_the_yaml_load_entry_point_or_an_unsafe_loader():
    sources = _python_sources()
    assert sources, "no backend sources found; the guard is scanning the wrong tree"
    offenders = [
        f"{path.relative_to(BACKEND)}:{line} {what}" for path in sources for line, what in _unsafe_references(path)
    ]
    assert not offenders, "\n".join(["drive a SafeLoader/BaseLoader directly instead:", *offenders])


def _offenders_of(tmp_path: Path, source: str) -> set[Offender]:
    sample = tmp_path / "sample.py"
    sample.write_text(source, encoding="utf-8")
    return set(_unsafe_references(sample))


GUARD_SELF_TEST_SOURCE = """\
import yaml as y
import yaml.loader as yl
from yaml import FullLoader, safe_load
from yaml import load as l
from yaml.constructor import UnsafeConstructor
a = y.load("x", Loader=y.Loader)
b = y.safe_load("x")
c = yl.Loader
d = getattr(y, "unsafe_load")
e = getattr(y, NAME)
f = y.CSafeLoader
g = yaml.load("x")  # 'yaml' is not bound in this module, so this is some other object
from yaml import loader as yl2
h = yl2.UnsafeLoader("x").get_single_data()
from yaml import cyaml
i = cyaml.CSafeLoader  # the safe C loader through a submodule alias must not count
# yaml.load in a comment must not count
'''yaml.unsafe_load in a docstring must not count'''
"""

GUARD_SELF_TEST_EXPECTED = {
    (3, "from yaml import FullLoader"),
    (4, "from yaml import load"),
    (5, "from yaml.constructor import UnsafeConstructor"),
    (6, "y.load"),
    (6, "y.Loader"),
    (8, "yl.Loader"),
    (9, 'getattr(y, "unsafe_load")'),
    (10, "getattr(y, <computed name>)"),
    (14, "yl2.UnsafeLoader"),
}


def test_the_guard_reports_exactly_the_unsafe_references_and_nothing_else(tmp_path):
    assert _offenders_of(tmp_path, GUARD_SELF_TEST_SOURCE) == GUARD_SELF_TEST_EXPECTED


GUARD_SCOPE_SELF_TEST_SOURCE = """\
from .yaml import load  # a relative import of a sibling module called yaml is not PyYAML
import yaml.constructor as y

def unsafe_in_one_function(s):
    import yaml as y  # this function's own alias wins here...
    return y.load(s, Loader=y.Loader)

def safe_in_another(s):
    return y.SafeConstructor  # ...and does not leak into this one

class Holder:
    import yaml as y  # shadows the module alias in the class body only...
    doc = y.safe_load("a: 1")

    def method(self):
        return y.Constructor  # ...a method sees the module-level yaml.constructor alias
"""

GUARD_SCOPE_SELF_TEST_EXPECTED = {
    (6, "y.load"),
    (6, "y.Loader"),
    (16, "y.Constructor"),
}


def test_the_guard_resolves_aliases_per_scope_and_ignores_relative_imports(tmp_path):
    # If the class body's ``y`` leaked into ``method``, line 16 would resolve to the harmless
    # ``yaml.Constructor`` and drop out of the set, so this also proves the class rule.
    assert _offenders_of(tmp_path, GUARD_SCOPE_SELF_TEST_SOURCE) == GUARD_SCOPE_SELF_TEST_EXPECTED


GUARD_BINDING_SELF_TEST_SOURCE = """\
import yaml as y
a = y.unsafe_load("x")  # the PyYAML binding is in effect here...
import yaml.constructor as y
b = y.Constructor  # ...and the rebound one here

def json_shadows(s):
    import json as y
    return y.load(s)  # json, not PyYAML

def parameter_shadows(y):
    return y.load()  # a parameter, not PyYAML

def assignment_shadows():
    y = object()
    return y.FullLoader  # a local object, not PyYAML

def inherits_the_module_binding():
    return y.UnsafeConstructor  # the module-level yaml.constructor alias

def handler_shadows():
    try:
        pass
    except Exception as y:
        return y.Loader  # the exception, not PyYAML
"""

GUARD_BINDING_SELF_TEST_EXPECTED = {
    (2, "y.unsafe_load"),
    (4, "y.Constructor"),
    (18, "y.UnsafeConstructor"),
}


def test_the_guard_tracks_bindings_in_evaluation_order(tmp_path):
    assert _offenders_of(tmp_path, GUARD_BINDING_SELF_TEST_SOURCE) == GUARD_BINDING_SELF_TEST_EXPECTED


GUARD_CONTROL_FLOW_SELF_TEST_SOURCE = """\
try:
    import yaml
except ImportError:
    yaml = None

class U(yaml.UnsafeLoader):  # with PyYAML installed the handler never ran, so this is unsafe
    pass

if FLAG:
    import yaml.loader as yl
else:
    yl = None
w = yl.Loader  # one path binds PyYAML, so this counts

import yaml as y
if FLAG:
    y = None
v = y.Loader  # still possibly PyYAML
y = None
u = y.Loader  # rebound on every path, not PyYAML
"""

GUARD_CONTROL_FLOW_SELF_TEST_EXPECTED = {
    (6, "yaml.UnsafeLoader"),
    (13, "yl.Loader"),
    (18, "y.Loader"),
}


def test_the_guard_keeps_every_binding_a_name_may_have_across_alternative_paths(tmp_path):
    assert _offenders_of(tmp_path, GUARD_CONTROL_FLOW_SELF_TEST_SOURCE) == GUARD_CONTROL_FLOW_SELF_TEST_EXPECTED


GUARD_ANNOTATION_SELF_TEST_SOURCE = """\
from types import ModuleType
import yaml
yaml: ModuleType  # a bare annotation declares, it does not rebind
class V(yaml.UnsafeLoader):
    pass
def f():
    yaml: ModuleType  # declares a local; the possible PyYAML binding is kept
    return yaml.CLoader
def g():
    yaml: ModuleType = None  # an annotated assignment does rebind
    return yaml.Loader  # not PyYAML
"""

GUARD_ANNOTATION_SELF_TEST_EXPECTED = {
    (4, "yaml.UnsafeLoader"),
    (8, "yaml.CLoader"),
}


def test_the_guard_treats_a_bare_annotation_as_a_declaration_not_a_rebinding(tmp_path):
    assert _offenders_of(tmp_path, GUARD_ANNOTATION_SELF_TEST_SOURCE) == GUARD_ANNOTATION_SELF_TEST_EXPECTED


GUARD_HEADER_SELF_TEST_SOURCE = """\
import yaml
class L(yaml.UnsafeLoader):  # a safe subclass rebased onto an unsafe loader
    pass
def f(x=yaml.Loader, *, z: yaml.FullLoader = None) -> yaml.CLoader:  # defaults and annotations
    return x
@yaml.load  # decorators
def g():
    pass
class M(yaml.SafeLoader):  # the safe base must not count
    pass
class N(metaclass=yaml.CUnsafeLoader):  # class keywords
    pass
"""

GUARD_HEADER_SELF_TEST_EXPECTED = {
    (2, "yaml.UnsafeLoader"),
    (4, "yaml.Loader"),
    (4, "yaml.FullLoader"),
    (4, "yaml.CLoader"),
    (6, "yaml.load"),
    (11, "yaml.CUnsafeLoader"),
}


def test_the_guard_checks_definition_headers_in_the_defining_scope(tmp_path):
    assert _offenders_of(tmp_path, GUARD_HEADER_SELF_TEST_SOURCE) == GUARD_HEADER_SELF_TEST_EXPECTED


def test_the_guard_accepts_the_safe_spellings_this_repository_uses(tmp_path):
    source = (
        "import yaml\n"
        "class _Cfn(yaml.SafeLoader):\n"
        "    pass\n"
        "loader = _Cfn('a: 1')\n"
        "doc = loader.get_single_data()\n"
        "other = yaml.BaseLoader('a: 1').get_single_data()\n"
        "fast = yaml.CSafeLoader\n"
        "plain = yaml.safe_load('a: 1')\n"
        "tag = yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG\n"
    )
    assert _offenders_of(tmp_path, source) == set()


PYTHON_OBJECT_PAYLOAD = (
    "openapi: '3.0.0'\ninfo: !!python/object/apply:os.system ['echo this line must never run']\npaths: {}\n"
)


def test_the_payload_really_carries_a_python_object_tag():
    """The oracle is not vacuous: composed (not constructed) the payload names ``os.system``."""
    composer = yaml.SafeLoader(PYTHON_OBJECT_PAYLOAD)
    try:
        root = composer.get_single_node()
    finally:
        composer.dispose()
    tags = {key.value: value.tag for key, value in root.value}
    assert tags["info"] == "tag:yaml.org,2002:python/object/apply:os.system"


def test_the_static_schema_parser_refuses_a_python_object_payload_without_reaching_os_system(monkeypatch):
    reached: list[tuple] = []

    def _tripwire(*args, **kwargs):
        reached.append(args)
        raise AssertionError("the YAML payload reached os.system")

    # Even an unsafe loader would resolve ``os.system`` through the ``os`` module object, so
    # the tripwire makes the test inert regardless of how the parser behaves.
    monkeypatch.setattr(os, "system", _tripwire)
    with pytest.raises(RuntimeError, match="not valid JSON or YAML"):
        _load_static_schema_document(PYTHON_OBJECT_PAYLOAD, "OpenAPI")
    assert reached == []


def test_the_static_schema_parser_still_refuses_duplicate_keys_and_parses_a_plain_document():
    with pytest.raises(RuntimeError, match="duplicate key"):
        _load_static_schema_document("openapi: '3.0.0'\npaths: {}\npaths: {}\n", "OpenAPI")
    document = _load_static_schema_document(
        "openapi: '3.0.0'\npaths:\n  /orders:\n    get:\n      operationId: getOrder\n", "OpenAPI"
    )
    assert document["paths"]["/orders"]["get"]["operationId"] == "getOrder"
