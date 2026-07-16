"""Tests for the scheduled Cedar-ENFORCE promotion sweep (Loom-study 0.6).

The sweep self-drives pending ENFORCE promotions without a user touchpoint. These
tests exercise the handler's orchestration (scan → promote each → count) with the
store + promoter stubbed, and the store's scan filter shape.
"""

from __future__ import annotations

import sys
import types

sys.path.insert(0, "src")


def _install_stub_deployment_handler(monkeypatch, pending, promote_results):
    """Stub app.deployment_handler with a fake store + _maybe_promote_policy."""
    calls = {"promoted": []}

    class _Dep:
        def __init__(self, did):
            self._did = did
        def model_dump(self, mode="json"):  # noqa: ARG002
            return {"deployment_id": self._did, "policy_result": {"enforce_pending": {"x": 1}}}

    class _Store:
        def scan_pending_enforce(self, max_items=200):  # noqa: ARG002
            return [_Dep(d) for d in pending]

    def _maybe_promote_policy(state, region):  # noqa: ARG001
        did = state.get("deployment_id")
        calls["promoted"].append(did)
        return promote_results.get(did, False)

    fake = types.ModuleType("app.deployment_handler")
    fake._get_state_store = lambda: _Store()
    fake._maybe_promote_policy = _maybe_promote_policy
    monkeypatch.setitem(sys.modules, "app.deployment_handler", fake)
    return calls


def test_sweep_promotes_each_pending(monkeypatch):
    calls = _install_stub_deployment_handler(
        monkeypatch, pending=["d1", "d2", "d3"],
        promote_results={"d1": True, "d2": False, "d3": True},
    )
    from app.step_handlers.policy_sweep_step import handler
    out = handler({"policy_sweep": True}, None)
    assert out == {"swept": 3, "promoted": 2, "failed": 0}
    assert calls["promoted"] == ["d1", "d2", "d3"]


def test_sweep_counts_failures_and_continues(monkeypatch):
    def _boom_store(monkeypatch):
        import types as _t

        class _Dep:
            def __init__(self, did):
                self._did = did
            def model_dump(self, mode="json"):  # noqa: ARG002
                return {"deployment_id": self._did}

        class _Store:
            def scan_pending_enforce(self, max_items=200):  # noqa: ARG002
                return [_Dep("d1"), _Dep("d2")]

        def _maybe(state, region):  # noqa: ARG001
            if state.get("deployment_id") == "d1":
                raise RuntimeError("promote blew up")
            return True

        fake = _t.ModuleType("app.deployment_handler")
        fake._get_state_store = lambda: _Store()
        fake._maybe_promote_policy = _maybe
        monkeypatch.setitem(sys.modules, "app.deployment_handler", fake)

    _boom_store(monkeypatch)
    from app.step_handlers.policy_sweep_step import handler
    out = handler({"policy_sweep": True}, None)
    # d1 raised (failed), d2 promoted — the sweep does not abort on one failure.
    assert out["swept"] == 2
    assert out["promoted"] == 1
    assert out["failed"] == 1


def test_sweep_handles_scan_failure(monkeypatch):
    import types as _t

    class _Store:
        def scan_pending_enforce(self, max_items=200):  # noqa: ARG002
            raise RuntimeError("ddb down")

    fake = _t.ModuleType("app.deployment_handler")
    fake._get_state_store = lambda: _Store()
    fake._maybe_promote_policy = lambda *a, **k: True
    monkeypatch.setitem(sys.modules, "app.deployment_handler", fake)

    from app.step_handlers.policy_sweep_step import handler
    out = handler({"policy_sweep": True}, None)
    assert out["error"] == "scan_failed"
    assert out["promoted"] == 0
