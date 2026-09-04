"""A successful teardown must not report an error it already decided to ignore.

Manifest teardown (Step 0a) deletes the ``agent_runtime`` row, and then the legacy
per-component fallback calls ``destroy_runtime`` a second time. That second call
sees the runtime mid-transition and returns
``success:false, message:"Runtime destroy error: ... Current status: DELETING"``.

Bug 159 stopped *counting* that as a failure, but the message was still appended,
so ``DELETE /api/runtime/{id}`` returned ``success:true`` with a body reading
"Runtime X deleted; Runtime destroy error: ConflictException ... DELETING".
Observed live on a clean delete of a real deployment. Customers delete often, so
this is the string they'd see every time and reasonably read as a broken teardown.

These tests pin both halves: the phantom message is suppressed when the manifest
owned the delete, and a genuine failure with NO manifest is still surfaced.
"""

import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, "src")

_RUNTIME_ID = "agent_abc-QNPVKl93O8"


def _record(*, with_manifest: bool) -> dict:
    rec = {
        "deployment_id": "dep-1",
        "user_id": "sub-1",
        "runtime_id": _RUNTIME_ID,
        "deployment_mode": "runtime",
    }
    if with_manifest:
        rec["created_resources"] = [
            {"type": "agent_runtime", "id": _RUNTIME_ID, "region": "us-east-1"},
        ]
    return rec


@pytest.fixture
def dh(monkeypatch):
    """Isolate _run_delete_cleanup: real message assembly, stubbed AWS calls."""
    import app.deployment_handler as dh

    monkeypatch.setattr(dh, "_get_state_store", lambda: MagicMock())
    # The manifest arm reports the authoritative success line.
    monkeypatch.setattr(
        dh,
        "_delete_managed_resource",
        lambda res, region: f"[manifest] runtime {res.get('id')}: Runtime {res.get('id')} deleted",
    )
    # The legacy fallback's second delete hits the mid-transition conflict.
    monkeypatch.setattr(
        dh,
        "destroy_runtime",
        lambda rid, region: {
            "success": False,
            "message": (
                "Runtime destroy error: An error occurred (ConflictException) when "
                "calling the DeleteAgentRuntime operation: The agent is currently "
                "being modified by another operation. Current status: DELETING."
            ),
        },
    )
    return dh


def _run(dh, monkeypatch, *, with_manifest: bool):
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: _record(with_manifest=with_manifest))
    return dh._run_delete_cleanup(_RUNTIME_ID, "sub-1")


def test_manifest_owned_delete_hides_the_phantom_conflict(dh, monkeypatch):
    resp = _run(dh, monkeypatch, with_manifest=True)
    msg = resp.message
    assert "deleted" in msg, msg
    assert "Runtime destroy error" not in msg, (
        f"a teardown that succeeded must not report the conflict Bug 159 already decided to ignore; got: {msg}"
    )
    assert "ConflictException" not in msg, msg
    assert resp.success is True, msg


def test_without_a_manifest_a_real_failure_is_still_reported(dh, monkeypatch):
    """No manifest means the fallback IS the delete — its error must surface."""
    resp = _run(dh, monkeypatch, with_manifest=False)
    assert "Runtime destroy error" in resp.message, resp.message
    assert resp.success is False, resp.message


def test_the_other_race_does_not_duplicate_the_deleted_line(dh, monkeypatch):
    """The second call can WIN instead of conflicting (seen live in eu-central-1).

    Then it returns success with its own "Runtime X deleted", which used to be
    concatenated onto the manifest's identical line as "... deleted; ... deleted".
    """
    monkeypatch.setattr(
        dh,
        "destroy_runtime",
        lambda rid, region: {"success": True, "message": f"Runtime {rid} deleted"},
    )
    msg = _run(dh, monkeypatch, with_manifest=True).message
    assert msg.count("deleted") == 1, f"the deleted line must appear once; got: {msg}"


def test_a_manifest_without_a_runtime_row_still_reports_the_fallback(dh, monkeypatch):
    """A deploy can fail before the runtime is recorded, leaving a manifest with
    other resources but no ``agent_runtime``. There the fallback is the only thing
    deleting the runtime, so suppressing it would hide a genuine failure."""
    rec = _record(with_manifest=True)
    rec["created_resources"] = [{"type": "secret", "id": "agentcore-connector/x", "region": "us-east-1"}]
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: rec)
    resp = dh._run_delete_cleanup(_RUNTIME_ID, "sub-1")
    assert "Runtime destroy error" in resp.message, resp.message
