"""Every production path that can create a deployment-bound secret runs inside a journal.

``_put_connector_secret`` journals the secret's name before CreateSecret only when a
journal is bound, and a hard kill between the create and its manifest row leaves that
journal row as the only record teardown can find. Binding is the caller's job, so this
pins it structurally: each step or handler call to an entry point that reaches the
create sits lexically inside ``with secret_intent_journal(...)``, and the service
modules that call an entry point themselves are exactly the reviewed set.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "src" / "app"

# Entry points that can reach _put_connector_secret -> CreateSecret.
ENTRY_POINTS = {
    "bind_connector_secret_for_deployment",
    "stage_customer_secret_for_deployment",
    "stage_runtime_secret_for_deployment",
    "deploy_gateway",
    "deploy_litellm_gateway",
}

# Service-level callers, each reached only from a checked call site. The LiteLLM
# deployer reuses the key gateway_step bound and is itself called inside a journal.
REVIEWED_SERVICE_CALLERS = {
    ("services/gateway_deployer.py", "bind_connector_secret_for_deployment"),
    ("services/gateway_deployer.py", "deploy_gateway"),
    ("services/litellm_gateway_deployer.py", "bind_connector_secret_for_deployment"),
}


def _called_name(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _journaled_withs(tree: ast.AST) -> list[ast.With]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.With) and any(
            isinstance(item.context_expr, ast.Call) and _called_name(item.context_expr) == "secret_intent_journal"
            for item in node.items
        ):
            out.append(node)
    return out


def _entry_calls(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _called_name(n) in ENTRY_POINTS]


def _inside(call: ast.Call, withs: list[ast.With]) -> bool:
    return any(any(call is n for stmt in w.body for n in ast.walk(stmt)) for w in withs)


def _checked_files() -> list[Path]:
    return sorted((APP / "step_handlers").glob("*.py")) + [APP / "deployment_handler.py"]


def test_every_step_and_handler_call_is_inside_a_journal():
    unjournaled = []
    seen = 0
    for path in _checked_files():
        tree = ast.parse(path.read_text())
        withs = _journaled_withs(tree)
        for call in _entry_calls(tree):
            seen += 1
            if not _inside(call, withs):
                unjournaled.append(f"{path.relative_to(APP)}:{call.lineno} {_called_name(call)}")
    assert unjournaled == []
    # gateway_step (bind, deploy_gateway, deploy_litellm_gateway), mcp_server_step and
    # the handler's three staging sites: a walk that finds none is a broken walk.
    assert seen >= 7


def test_the_service_level_callers_are_the_reviewed_set():
    found = set()
    for path in sorted((APP / "services").rglob("*.py")):
        rel = str(path.relative_to(APP))
        for call in _entry_calls(ast.parse(path.read_text())):
            found.add((rel, _called_name(call)))
    assert found == REVIEWED_SERVICE_CALLERS
