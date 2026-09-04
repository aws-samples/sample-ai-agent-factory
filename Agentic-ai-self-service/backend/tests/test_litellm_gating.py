"""The deploy-time governance gate under the LiteLLM registry backend.

`test_gating_unknown_status.py` pins the gate's 503/403 triad for the AWS Agent
Registry path. This pins that the LiteLLM backend inherits it *exactly*, because
the gate is provider-dispatched inside `unapproved_integrations` and the call site
in `deployment_handler` is unchanged — which means one specific mistake in the new
backend fails the gate OPEN:

    try:
        blocked = unapproved_integrations(idents)
    except RegistryQueryFailed:   # <- from aws_agent_registry
        ... 503
    ...
    except Exception:             # <- swallows anything else
        logger.warning("integration gating skipped")   # DEPLOY PROCEEDS

A `RegistryQueryFailed` defined in the LiteLLM module rather than imported would
be a *different class object*, miss that `except`, hit the outer `except
Exception`, and turn an unreachable governance backend into a silently ungoverned
deploy. `test_the_wrong_exception_class_would_fail_the_gate_open` demonstrates that
on a deliberately-wrong stand-in so the hazard is proven, not asserted.

These tests drive the real `ar.unapproved_integrations` — only the HTTP layer is
substituted — so the dispatch itself is under test, not a replica of it.
"""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, "src")

from app.services import aws_agent_registry as ar  # noqa: E402
from app.services.registry_providers import litellm as lgl  # noqa: E402
from app.services.registry_providers import unapproved_integrations_for_provider  # noqa: E402

from tests.test_gating_unknown_status import _Req, _run_gating  # noqa: E402

_BACKEND = pathlib.Path(__file__).resolve().parents[1]

APPROVED_URL = "https://mcp.example.com/github/mcp"
_SERVERS = [
    {"server_id": "s1", "alias": "GitHub MCP", "url": APPROVED_URL, "tools": [{"name": "list_issues"}]},
    {"server_id": "s2", "alias": "retired", "url": "https://mcp.example.com/old/mcp", "enabled": False},
]


@pytest.fixture
def litellm_active(monkeypatch):
    """LiteLLM is the active registry backend and its catalog is readable."""
    monkeypatch.setenv("REGISTRY_PROVIDER", "litellm")
    monkeypatch.setattr(
        lgl,
        "get_litellm_registry_config",
        lambda: {"base_url": "https://litellm.example.com", "api_key_ref": "arn:x", "verified": True},
    )
    monkeypatch.setattr(lgl, "_read_api_key", lambda ref: "sk-test")
    monkeypatch.setattr(lgl, "_get_json", lambda url, key, servers=None: {"data": _SERVERS})


# ---------------------------------------------------------------------------
# The triad, preserved
# ---------------------------------------------------------------------------


def test_an_enabled_server_approves_the_integration(litellm_active):
    _run_gating(_Req(mcp={"endpoint": APPROVED_URL}))  # no raise


def test_an_unknown_endpoint_is_403(litellm_active):
    with pytest.raises(HTTPException) as ei:
        _run_gating(_Req(mcp={"endpoint": "https://mcp.example.com/not-in-the-catalog/mcp"}))
    assert ei.value.status_code == 403
    # The 403 must name what it blocked. An operator who sees only "unapproved
    # integration" cannot tell which endpoint to go publish or correct.
    # Matched on the path segment rather than the host deliberately: a
    # host-substring assertion reads as an incomplete-URL-sanitization check to
    # static analysis, and matching a full authority here proves nothing extra.
    assert "not-in-the-catalog" in str(ei.value.detail)


def test_a_disabled_server_is_not_approval(litellm_active):
    """Presence in the catalog is not the signal — presence *and enablement* is.
    Disabling a compromised MCP server in LiteLLM has to stop new deploys from
    wiring it, or the disable button is decorative."""
    with pytest.raises(HTTPException) as ei:
        _run_gating(_Req(mcp={"endpoint": "https://mcp.example.com/old/mcp"}))
    assert ei.value.status_code == 403


def test_an_unreadable_catalog_is_503_and_blocks_the_deploy(monkeypatch):
    monkeypatch.setenv("REGISTRY_PROVIDER", "litellm")
    monkeypatch.setattr(lgl, "get_litellm_registry_config", lambda: None)

    with pytest.raises(HTTPException) as ei:
        _run_gating(_Req(mcp={"endpoint": APPROVED_URL}))

    assert ei.value.status_code == 503, "unknown approval status must not read as denied"
    detail = str(ei.value.detail)
    assert "unknown" in detail.lower()
    assert "not APPROVED" not in detail, "must not blame the customer's integrations"


def test_a2a_peers_are_gated_too(litellm_active):
    with pytest.raises(HTTPException) as ei:
        _run_gating(_Req(a2a={"peer_allowlist": ["https://peer.example.com/a2a"]}))
    assert ei.value.status_code == 403


def test_no_integrations_never_touches_the_backend(monkeypatch):
    monkeypatch.setenv("REGISTRY_PROVIDER", "litellm")

    def _must_not_be_called(*a, **kw):
        raise AssertionError("the gate queried LiteLLM with no integrations to check")

    monkeypatch.setattr(lgl, "get_litellm_registry_config", _must_not_be_called)
    _run_gating(_Req())


# ---------------------------------------------------------------------------
# Fail-open hazards
# ---------------------------------------------------------------------------


def test_the_dispatch_returns_none_not_empty_for_the_default_backend(monkeypatch):
    """[] from the dispatch would mean "everything is approved" and would skip the
    AWS federation path entirely — silently disabling gating for every existing
    customer. None means "not my gate, keep going"."""
    monkeypatch.delenv("REGISTRY_PROVIDER", raising=False)
    assert unapproved_integrations_for_provider(["https://x.example.com/mcp"]) is None


def test_the_default_backend_still_reaches_the_aws_path(monkeypatch):
    """The dispatch is a pass-through when inactive: whatever the AWS federation
    path decides is still what the gate returns."""
    monkeypatch.delenv("REGISTRY_PROVIDER", raising=False)
    monkeypatch.setattr(ar, "get_registry", lambda: None)
    assert ar.unapproved_integrations(["https://x.example.com/mcp"]) == []

    class _Reg:
        def list_records_strict(self, filters=None):
            return [{"name": "ok", "status": "APPROVED"}]

    monkeypatch.setattr(ar, "get_registry", lambda: _Reg())
    assert ar.unapproved_integrations(["nope"]) == ["nope"]


def test_the_wrong_exception_class_would_fail_the_gate_open(monkeypatch):
    """Proof of the hazard, not a test of production code.

    A same-named RegistryQueryFailed declared in the LiteLLM module escapes the
    `except RegistryQueryFailed` at the call site and lands in the outer `except
    Exception`, which logs "gating skipped" and LETS THE DEPLOY THROUGH. That is
    why litellm.py imports the class instead of defining one — and why
    `test_registry_providers.py` asserts the class identity directly."""

    class RegistryQueryFailed(RuntimeError):  # deliberately NOT ar.RegistryQueryFailed
        pass

    def _boom(_idents):
        raise RegistryQueryFailed("litellm unreachable")

    monkeypatch.setattr(ar, "unapproved_integrations", _boom)

    # _run_gating replicates the call site's inner try/except only. The impostor
    # sails straight through it — no 503 — which in handle_deploy means the outer
    # `except Exception` swallows it and the deploy proceeds ungoverned.
    with pytest.raises(RegistryQueryFailed):
        _run_gating(_Req(mcp={"endpoint": APPROVED_URL}))


def test_a_readable_catalog_of_zero_servers_blocks_rather_than_approves(monkeypatch):
    """An empty catalog is a legitimate 'nothing is approved yet', not a free pass."""
    monkeypatch.setenv("REGISTRY_PROVIDER", "litellm")
    monkeypatch.setattr(
        lgl,
        "get_litellm_registry_config",
        lambda: {"base_url": "https://litellm.example.com", "api_key_ref": "arn:x", "verified": True},
    )
    monkeypatch.setattr(lgl, "_read_api_key", lambda ref: "sk-test")
    monkeypatch.setattr(lgl, "_get_json", lambda url, key, servers=None: {"data": []})

    with pytest.raises(HTTPException) as ei:
        _run_gating(_Req(mcp={"endpoint": APPROVED_URL}))
    assert ei.value.status_code == 403


# ---------------------------------------------------------------------------
# The gate reads, and only reads
# ---------------------------------------------------------------------------


def test_the_gate_only_reads_the_server_listing(litellm_active, monkeypatch):
    """A governance check must not be able to invoke a tool as a side effect of
    deciding whether that tool is allowed."""
    seen: list[str] = []

    def _record(url, key, servers=None):
        seen.append(url)
        return {"data": _SERVERS}

    monkeypatch.setattr(lgl, "_get_json", _record)
    lgl.litellm_unapproved_integrations([APPROVED_URL])

    assert seen, "the gate must actually consult LiteLLM"
    for url in seen:
        assert lgl._SERVERS_PATH in url
        assert "tools/call" not in url


def test_the_gate_calls_nothing_but_read_only_helpers():
    """AST guard: the runtime check above only covers the paths it happens to
    exercise. The analogue of test_gating_never_reads_the_data_plane — pin the
    gate's call graph so no write or invoke helper can be added to it later
    without this failing.

    Docstrings are excluded deliberately: litellm.py's docstrings *mention*
    /mcp-rest/tools/call in order to explain why the gate never calls it, so a
    naive source grep asserts the opposite of what it means to."""
    src = (_BACKEND / "src" / "app" / "services" / "registry_providers" / "litellm.py").read_text()
    fn = next(
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "litellm_unapproved_integrations"
    )

    called = {
        n.func.id if isinstance(n.func, ast.Name) else ast.unparse(n.func)
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
    }
    assert called <= {
        "list_litellm_servers",
        "_server_is_enabled",
        "_identifier_matches",
        "json.dumps",
        "any",
    }, f"the gate grew a new call: {called}"

    # No string literal in the gate names a tool-invocation path (the docstring is
    # the function's first statement and is skipped).
    literals = [n.value for n in ast.walk(ast.Module(body=fn.body[1:], type_ignores=[])) if isinstance(n, ast.Constant)]
    assert not [s for s in literals if isinstance(s, str) and "tools/call" in s]


def test_a_bare_name_cannot_borrow_approval_from_an_unrelated_field(litellm_active):
    """The AWS path substring-matches every identifier against its record blob. Here
    the blob is a whole server object, tool names included, so 'list_issues' or
    'mcp' would match something. URL identifiers still match by substring; names
    must match the server name exactly or by slug."""
    blocked = lgl.litellm_unapproved_integrations(["list_issues", "mcp", "s1"])
    assert blocked == ["list_issues", "mcp", "s1"]
    # …while the real name, and its slug, are approved.
    assert lgl.litellm_unapproved_integrations(["GitHub MCP", "github-mcp"]) == []


def test_the_call_site_triad_is_unchanged_by_this_workstream():
    """The whole design rests on deployment_handler being untouched. If someone
    moves the dispatch out of unapproved_integrations and into the handler, this
    fails and points at the reason."""
    import inspect

    from app import deployment_handler as dh

    src = inspect.getsource(dh.handle_deploy)
    assert "except RegistryQueryFailed" in src
    assert "status_code=503" in src
    assert "not APPROVED in the Agent Registry" in src
    assert "registry_provider" not in src, "the gate dispatch belongs in aws_agent_registry, not here"
