"""A legacy fallback does not re-decide a resource the manifest loop already decided.

Measured 2026-10-02 on the matrix account: four failed mcp-server-gateway-target deploys (no
runtime of their own, unsealed manifests) all listed one shared MCP server runtime. Each delete
handed it off in the manifest loop. Then the legacy MCP-server step, which an unsealed manifest
keeps enabled, retained the same runtime because another of the four still referenced it. That
retention counts as a consumer that may still be running, so every hand-off was kept as a
retention and the teardown ended delete_retained. A delete_retained tombstone is a live
reference, so the last two protected the runtime, gateway, provider and roles for each other.
Retrying either repeated that for ever.

F-66c (test_teardown_handoff_f66c.py) fixed the same loop for sealed manifests, which never
reach the legacy steps. These tests run unsealed records through the real teardown.
"""

from unittest.mock import MagicMock

from app import deployment_handler

from tests.test_teardown_handoff_f66c import GW, GW_ROLE, MCP_SHARED, _delete_as_the_product_does, _Table, _wire

MEMORY = {"type": "memory", "id": "mem-shared", "region": "us-east-1"}
MEMORY_ROLE = {"type": "iam_role", "id": "AgentCoreMemory-shared", "name": "AgentCoreMemory-shared"}


def _failed_mcp_deploy(rid: str, *, mcp_row: bool = True) -> dict:
    """A failed mcp-server-gateway-target deploy as the matrix account held them."""
    rows = [{**GW, "created_by_deployment": False}, {**GW_ROLE, "created_by_deployment": False}]
    if mcp_row:
        rows.insert(0, {**MCP_SHARED, "created_by_deployment": False})
    return {
        "deployment_id": rid,
        "user_id": "owner-1",
        "target_region": "us-east-1",
        "status": "failed",
        "resource_manifest_complete": False,
        "mcp_server_runtime_id": MCP_SHARED["id"],
        "created_resources": rows,
    }


def test_two_failed_deploys_sharing_an_mcp_runtime_are_deleted_in_turn(monkeypatch):
    table = _Table({"f-1": _failed_mcp_deploy("f-1"), "f-2": _failed_mcp_deploy("f-2")})
    deleted = _wire(monkeypatch, table)  # destroy_runtime raises: the legacy step must not run

    first = _delete_as_the_product_does("f-1", table)
    assert first["status"] == "deleted", first["message"]
    assert "[manifest] handed off agent_runtime mcp-server-shared" in first["message"]
    assert "left in place (protected)" not in first["message"]
    assert deleted == []

    second = _delete_as_the_product_does("f-2", table)
    assert second["status"] == "deleted", second["message"]
    assert ("agent_runtime", "mcp-server-shared") in deleted
    assert ("gateway", "gw-shared") in deleted
    assert ("iam_role", "AgentCoreGateway-shared") in deleted


def test_the_live_tombstones_are_recovered_by_deleting_them_again(monkeypatch):
    """The exact state the matrix account was left in: both records delete_retained."""
    table = _Table({"f-1": _failed_mcp_deploy("f-1"), "f-2": _failed_mcp_deploy("f-2")})
    for item in table.items.values():
        item["delete_status"] = "delete_retained"
    deleted = _wire(monkeypatch, table)

    assert _delete_as_the_product_does("f-1", table)["status"] == "deleted"
    assert ("agent_runtime", "mcp-server-shared") not in deleted
    assert _delete_as_the_product_does("f-2", table)["status"] == "deleted"
    assert ("agent_runtime", "mcp-server-shared") in deleted


def test_an_mcp_runtime_no_row_records_still_goes_through_the_legacy_gate(monkeypatch):
    """What the fallback is for: a runtime the manifest lost. Another live deployment still
    lists it, so it stays, and the teardown says so instead of claiming a clean delete."""
    table = _Table(
        {
            "f-1": _failed_mcp_deploy("f-1", mcp_row=False),
            "live": {**_failed_mcp_deploy("live"), "status": "succeeded"},
        }
    )
    _wire(monkeypatch, table)

    out = _delete_as_the_product_does("f-1", table)

    assert out["status"] == "delete_retained", out["message"]
    assert "MCP server runtime mcp-server-shared left in place (protected)" in out["message"]


def test_an_unreferenced_mcp_runtime_no_row_records_is_destroyed_by_the_legacy_step(monkeypatch):
    table = _Table({"f-1": _failed_mcp_deploy("f-1", mcp_row=False)})
    _wire(monkeypatch, table)
    destroy = MagicMock(return_value={"success": True, "message": "destroyed"})
    monkeypatch.setattr(deployment_handler, "destroy_runtime", destroy)

    out = _delete_as_the_product_does("f-1", table)

    assert out["status"] == "deleted", out["message"]
    assert destroy.call_args.args[0] == "mcp-server-shared"


def _failed_memory_deploy(rid: str, *, role_row: bool = True) -> dict:
    rows = [{**MEMORY, "created_by_deployment": False}]
    if role_row:
        rows.append({**MEMORY_ROLE, "created_by_deployment": False})
    return {
        "deployment_id": rid,
        "user_id": "owner-1",
        "target_region": "us-east-1",
        "status": "failed",
        "resource_manifest_complete": False,
        "memory_result": {"memory_id": MEMORY["id"], "memory_role_name": MEMORY_ROLE["name"]},
        "created_resources": rows,
    }


def test_two_failed_deploys_sharing_a_memory_are_deleted_in_turn(monkeypatch):
    table = _Table({"f-1": _failed_memory_deploy("f-1"), "f-2": _failed_memory_deploy("f-2")})
    deleted = _wire(monkeypatch, table)
    monkeypatch.setattr(
        deployment_handler,
        "delete_memory_confirmed",
        MagicMock(side_effect=AssertionError("the manifest rows own the memory")),
    )

    first = _delete_as_the_product_does("f-1", table)
    assert first["status"] == "deleted", first["message"]
    assert deleted == []
    second = _delete_as_the_product_does("f-2", table)
    assert second["status"] == "deleted", second["message"]
    assert ("memory", "mem-shared") in deleted
    assert ("iam_role", "AgentCoreMemory-shared") in deleted


def test_a_memory_whose_role_row_is_missing_keeps_the_legacy_path(monkeypatch):
    """The role is the step's other subject. Without its row the manifest has not decided the
    whole step, so the conservative legacy path runs: with another live deployment on the
    memory, both stay and the teardown is a retention."""
    table = _Table({"f-1": _failed_memory_deploy("f-1", role_row=False), "f-2": _failed_memory_deploy("f-2")})
    _wire(monkeypatch, table)

    out = _delete_as_the_product_does("f-1", table)

    assert out["status"] == "delete_retained", out["message"]
    assert "Memory mem-shared left in place (protected)" in out["message"]


def test_a_guardrail_the_manifest_deleted_is_not_deleted_again(monkeypatch):
    table = _Table(
        {
            "f-1": {
                "deployment_id": "f-1",
                "user_id": "owner-1",
                "target_region": "us-east-1",
                "status": "failed",
                "resource_manifest_complete": False,
                "guardrails_result": {"created_by_flow": True, "guardrail_id": "gr-1"},
                "created_resources": [
                    {"type": "guardrail", "id": "gr-1", "region": "us-east-1", "created_by_deployment": True}
                ],
            }
        }
    )
    deleted = _wire(monkeypatch, table)
    monkeypatch.setattr(
        deployment_handler,
        "assert_guardrail_owned",
        MagicMock(side_effect=AssertionError("the manifest row owns the guardrail")),
    )

    out = _delete_as_the_product_does("f-1", table)

    assert out["status"] == "deleted", out["message"]
    assert deleted == [("guardrail", "gr-1")]
