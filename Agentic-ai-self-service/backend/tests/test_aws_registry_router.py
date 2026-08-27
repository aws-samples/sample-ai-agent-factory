"""AWS Agent Registry federation endpoints (/api/registry/aws-*).

FastAPI TestClient with the caller/admin dependencies overridden — no Cognito, no
AWS. The adapter itself is monkeypatched, so these tests are about the ROUTER's
contract: what it reports, and which failure it blames.

The GA migration made one distinction load-bearing: "unreachable because the
registryId/IAM is wrong" vs "unreachable because this bundle's boto3 has no
agent-registry service models at all". The first is fixed in the console, the
second only by redeploying. Conflating them sends admins after the wrong bug, so
both the GET status payload and the POST error assert on it here.
"""

from __future__ import annotations

import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

from app.routers import registry as registry_router_mod  # noqa: E402
from app.routers.registry import caller_is_admin  # noqa: E402
from app.services import aws_agent_registry as ar  # noqa: E402
from app.services.auth import get_caller_sub  # noqa: E402


def _client(admin: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(registry_router_mod.router)
    app.dependency_overrides[get_caller_sub] = lambda: "alice"
    app.dependency_overrides[caller_is_admin] = lambda: admin
    return TestClient(app)


_RAISES = object()

# Default control-plane view: agrees with what search() returns.
_TRUTH_AGREES = [{"recordId": "aaaaaaaaaaaa", "name": "found", "status": "APPROVED"}]


class _FakeReg:
    """`status` is what the router actually reads. `ok=False` with status=None models
    the unreadable case (bad id / old SDK); pass an explicit status to model a
    registry that answers but is not READY.

    `truth` is the CONTROL-plane record listing, kept separate from what search()
    returns so a test can model the live-verified case where the two planes
    disagree. Pass `_RAISES` to model the control plane being unqueryable."""

    def __init__(self, ok: bool = True, status: str | None = "__default__", truth=_TRUTH_AGREES):
        self._ok = ok
        self._status = ("READY" if ok else None) if status == "__default__" else status
        self.truth = truth

    def registry_status(self) -> str | None:
        return self._status

    def available(self) -> bool:
        return self._status == "READY"

    def search(self, q, **kw):
        # The data plane's own view. `truth` below is the control plane's; when they
        # disagree the router must serve the control plane's (see the drift tests).
        return [{"recordId": "aaaaaaaaaaaa", "name": "found", "recordType": "AGENT", "status": "APPROVED"}]

    def list_records_strict(self, **kw):
        if self.truth is _RAISES:
            raise ar.RegistryQueryFailed("control plane unreachable")
        return self.truth


# -- GET /aws-config ---------------------------------------------------------


def test_config_reports_not_configured(monkeypatch):
    monkeypatch.setattr(ar, "get_configured_registry_id", lambda: None)
    monkeypatch.setattr(ar, "agent_registry_supported", lambda: True)
    body = _client().get("/api/registry/aws-config").json()
    assert body == {
        "enabled": False,
        "registry_id": None,
        "available": False,
        "sdk_supported": True,
        "status": None,
    }


def test_config_reports_connected(monkeypatch):
    monkeypatch.setattr(ar, "get_configured_registry_id", lambda: "reg1")
    monkeypatch.setattr(ar, "get_registry", lambda: _FakeReg(True))
    monkeypatch.setattr(ar, "agent_registry_supported", lambda: True)
    body = _client().get("/api/registry/aws-config").json()
    assert body["enabled"] is True and body["available"] is True
    assert body["registry_id"] == "reg1"


def test_config_flags_an_sdk_too_old_for_ga(monkeypatch):
    """The clients can't be built at all, so available() is False for a VALID id —
    sdk_supported is what tells the UI not to blame the registryId."""
    monkeypatch.setattr(ar, "get_configured_registry_id", lambda: "reg1")
    monkeypatch.setattr(ar, "get_registry", lambda: _FakeReg(False))
    monkeypatch.setattr(ar, "agent_registry_supported", lambda: False)
    body = _client().get("/api/registry/aws-config").json()
    assert body["enabled"] is True
    assert body["available"] is False
    assert body["sdk_supported"] is False


def test_config_never_500s_when_the_adapter_is_unavailable(monkeypatch):
    """get_registry() returning None (no boto3 models) must degrade, not raise."""
    monkeypatch.setattr(ar, "get_configured_registry_id", lambda: "reg1")
    monkeypatch.setattr(ar, "get_registry", lambda: None)
    monkeypatch.setattr(ar, "agent_registry_supported", lambda: False)
    resp = _client().get("/api/registry/aws-config")
    assert resp.status_code == 200
    assert resp.json()["available"] is False


# -- POST /aws-config --------------------------------------------------------


def test_enable_rejects_non_admin(monkeypatch):
    monkeypatch.setattr(ar, "agent_registry_supported", lambda: True)
    resp = _client(admin=False).post("/api/registry/aws-config", json={"registry_id": "reg1"})
    assert resp.status_code == 403


def test_enable_blames_the_sdk_not_the_registry_id(monkeypatch):
    """An old bundle must not produce "check the registryId" — that's misleading
    and the admin cannot act on it."""
    monkeypatch.setattr(ar, "agent_registry_supported", lambda: False)
    resp = _client().post("/api/registry/aws-config", json={"registry_id": "reg1"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "SDK" in detail and "1.43.66" in detail
    assert "registryId" not in detail


def test_enable_blames_the_registry_id_when_the_sdk_is_fine(monkeypatch):
    monkeypatch.setattr(ar, "agent_registry_supported", lambda: True)
    monkeypatch.setattr(ar, "AwsAgentRegistry", lambda rid: _FakeReg(False))
    resp = _client().post("/api/registry/aws-config", json={"registry_id": "bogus"})
    assert resp.status_code == 400
    assert "registryId" in resp.json()["detail"]


def test_enable_persists_a_reachable_registry(monkeypatch):
    saved = {}
    monkeypatch.setattr(ar, "agent_registry_supported", lambda: True)
    monkeypatch.setattr(ar, "AwsAgentRegistry", lambda rid: _FakeReg(True))
    monkeypatch.setattr(ar, "set_configured_registry_id", lambda rid: saved.setdefault("id", rid))
    resp = _client().post("/api/registry/aws-config", json={"registry_id": "reg1"})
    assert resp.status_code == 200
    assert saved["id"] == "reg1"


# -- "not READY yet" is a third, distinct state -------------------------------


def test_enable_says_still_provisioning_rather_than_blaming_the_id(monkeypatch):
    """A registry takes tens of seconds to reach READY, and enabling federation
    right after creating one in the console is the normal sequence. Reporting that
    as "check the registryId" sends the admin to re-verify something correct; the
    real instruction is "retry in a moment"."""
    saved = {}
    monkeypatch.setattr(ar, "agent_registry_supported", lambda: True)
    monkeypatch.setattr(ar, "AwsAgentRegistry", lambda rid: _FakeReg(status="CREATING"))
    monkeypatch.setattr(ar, "set_configured_registry_id", lambda rid: saved.setdefault("id", rid))
    resp = _client().post("/api/registry/aws-config", json={"registry_id": "reg1"})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "CREATING" in detail and "READY" in detail
    assert "registryId" not in detail
    # and it must NOT be persisted — records cannot be written yet
    assert saved == {}


def test_config_surfaces_a_non_ready_status(monkeypatch):
    monkeypatch.setattr(ar, "get_configured_registry_id", lambda: "reg1")
    monkeypatch.setattr(ar, "get_registry", lambda: _FakeReg(status="UPDATING"))
    monkeypatch.setattr(ar, "agent_registry_supported", lambda: True)
    body = _client().get("/api/registry/aws-config").json()
    assert body["available"] is False
    assert body["status"] == "UPDATING"
    assert body["sdk_supported"] is True


def test_config_status_is_none_when_the_registry_cannot_be_read(monkeypatch):
    """None distinguishes "we could not ask" from any real lifecycle state, so the
    UI can keep blaming the registryId only in the case that warrants it."""
    monkeypatch.setattr(ar, "get_configured_registry_id", lambda: "reg1")
    monkeypatch.setattr(ar, "get_registry", lambda: _FakeReg(False))
    monkeypatch.setattr(ar, "agent_registry_supported", lambda: True)
    body = _client().get("/api/registry/aws-config").json()
    assert body["available"] is False
    assert body["status"] is None


# -- GET /aws-search ---------------------------------------------------------


def test_search_returns_disabled_when_unconfigured(monkeypatch):
    monkeypatch.setattr(ar, "get_registry", lambda: None)
    body = _client().get("/api/registry/aws-search?q=bot").json()
    assert body == {"enabled": False, "results": []}


def test_search_passes_ga_record_fields_through(monkeypatch):
    """recordType/status are GA-only response fields the panel renders as chips."""
    monkeypatch.setattr(ar, "get_registry", lambda: _FakeReg(True))
    body = _client().get("/api/registry/aws-search?q=bot").json()
    assert body["enabled"] is True
    assert body["results"][0]["recordType"] == "AGENT"
    assert body["results"][0]["status"] == "APPROVED"
    assert body["status_authoritative"] is True


def test_search_status_comes_from_the_control_plane_not_the_search_index(monkeypatch):
    """Live-verified GA drift: the data plane keeps serving a demoted record as
    APPROVED for minutes after the control plane says DRAFT.

    Reachable on the ordinary path — register() upserts on redeploy and an update
    demotes the record — so a redeployed integration would show an APPROVED badge
    on a governance screen while it is actually waiting on re-review."""
    monkeypatch.setattr(
        ar,
        "get_registry",
        lambda: _FakeReg(True, truth=[{"recordId": "aaaaaaaaaaaa", "name": "found", "status": "DRAFT"}]),
    )
    body = _client().get("/api/registry/aws-search?q=bot").json()
    assert body["results"][0]["status"] == "DRAFT", "served the stale index status"
    assert body["status_authoritative"] is True


def test_search_reports_a_deleted_record_as_deleted_not_as_its_last_status(monkeypatch):
    """A hit the control plane no longer has was deleted but not yet de-indexed."""
    monkeypatch.setattr(ar, "get_registry", lambda: _FakeReg(True, truth=[]))
    body = _client().get("/api/registry/aws-search?q=bot").json()
    assert body["results"][0]["status"] == "DELETED"


def test_search_drops_status_when_it_cannot_be_reconciled(monkeypatch):
    """If the control plane can't be read we have no trustworthy status. Dropping the
    field is honest; passing the index's version through would be misleading and
    failing the whole request would break browse over a cosmetic concern."""
    monkeypatch.setattr(ar, "get_registry", lambda: _FakeReg(True, truth=_RAISES))
    body = _client().get("/api/registry/aws-search?q=bot").json()
    assert body["enabled"] is True
    assert body["status_authoritative"] is False
    assert "status" not in body["results"][0]
    assert body["results"][0]["name"] == "found", "results must still be served"
