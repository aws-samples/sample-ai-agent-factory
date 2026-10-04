"""Four routes carried an ``agent:read``/``agent:write`` scope and no per-record check.

A scope answers "may this caller read workflows". It never answers "may they read
THIS one". ``export``, ``validate`` and ``deploy`` each had the scope and nothing
else, and ``import`` took the record's authority fields straight from the request
body. Measured against the live stack ``acfe2e-p0920`` before the fix:

===========================================  =====================  ======================
call                                         before                 after
===========================================  =====================  ======================
``GET  /{other-tenant-id}/export``           200 + whole canvas     404
``POST /{other-tenant-id}/validate``         200 + node problems    404
``POST /{other-tenant-id}/deploy``           200 ``status:failed``  501 (retired)
``POST /import`` with a forged ``owner_sub`` row owned by the forgee row owned by the caller
``POST /{own-id}/deploy`` on the platform    200 ``status:failed``  501
===========================================  =====================  ======================

Two directions matter for the active CRUD/export/validate routes. The legacy
in-process deploy route is the deliberate exception: it is retired for everyone
because it could create resources without a durable teardown manifest. The
supported ``POST /api/deploy`` endpoint performs deployment.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

from app.models import WorkflowDefinition  # noqa: E402
from app.services.auth import get_caller_sub  # noqa: E402

OWNER = "owner-sub-aaaa"
STRANGER = "stranger-sub-bbbb"
VIEWER = "viewer-sub-cccc"

# Planted in the stored record. A cross-tenant read is proven by this string
# reaching a caller who is not the owner, not by a status code alone.
CANVAS_SECRET = "zzcanvassecretzz"


def _metadata() -> dict:
    return {"author": "tester", "tags": [], "awsRegion": "us-east-1"}


def _valid_node(node_id: str = "n1", prompt: str = CANVAS_SECRET) -> dict:
    return {
        "id": node_id,
        "type": "runtime",
        "position": {"x": 0, "y": 0},
        "data": {
            "component_type": "runtime",
            "name": "agent",
            "model": {"model_id": "anthropic.claude-sonnet-4-5-20250929-v1:0"},
            "system_prompt": prompt,
        },
    }


def _app(caller_sub: str) -> TestClient:
    from app.routers import workflows as workflows_router

    app = FastAPI()
    app.include_router(workflows_router.router)
    app.dependency_overrides[get_caller_sub] = lambda: caller_sub
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def storage():
    from app.services.storage import WorkflowStorage, get_workflow_storage, set_workflow_storage

    original = get_workflow_storage()
    store = WorkflowStorage()
    set_workflow_storage(store)
    yield store
    set_workflow_storage(original)


def _store(store, workflow_id: str, owner_sub: str | None, acl: dict | None = None) -> WorkflowDefinition:
    """Put a real, valid ``WorkflowDefinition`` in storage under a given owner."""
    now = datetime.now(timezone.utc)
    data = {
        "id": workflow_id,
        "name": "wf",
        "version": "1.0.0",
        "nodes": [_valid_node()],
        "edges": [],
        "metadata": _metadata(),
        "owner_sub": owner_sub,
        "created_at": now,
        "updated_at": now,
    }
    if acl is not None:
        data["acl"] = acl
    workflow = WorkflowDefinition.model_validate(data)
    store._workflows[workflow_id] = workflow
    return workflow


# ===========================================================================
# export
# ===========================================================================


class TestExportIsNotReadableByAnyoneWithTheScope:
    def test_a_stranger_gets_404_and_no_canvas(self, storage):
        _store(storage, "wf-1", OWNER)
        r = _app(STRANGER).get("/api/workflows/wf-1/export")
        assert r.status_code == 404
        assert CANVAS_SECRET not in r.text

    def test_the_owner_still_gets_the_export(self, storage):
        """The vacuity direction: 404-for-everyone would satisfy the test above."""
        _store(storage, "wf-1", OWNER)
        r = _app(OWNER).get("/api/workflows/wf-1/export")
        assert r.status_code == 200
        assert r.json()["workflow_json"]["nodes"][0]["data"]["system_prompt"] == CANVAS_SECRET

    def test_a_shared_viewer_gets_the_export(self, storage):
        _store(storage, "wf-1", OWNER, acl={"owner_sub": OWNER, "viewers": [VIEWER]})
        r = _app(VIEWER).get("/api/workflows/wf-1/export")
        assert r.status_code == 200

    def test_the_denial_is_indistinguishable_from_a_missing_id(self, storage):
        """Otherwise the route is an existence oracle over every tenant's ids."""
        _store(storage, "wf-1", OWNER)
        client = _app(STRANGER)
        denied = client.get("/api/workflows/wf-1/export")
        missing = client.get("/api/workflows/wf-absent/export")
        assert denied.status_code == missing.status_code == 404
        assert denied.json()["detail"].replace("wf-1", "X") == missing.json()["detail"].replace("wf-absent", "X")


class TestExportStripsTheAuthorityFields:
    """These four fields say who may touch the record, not what the canvas is.

    The export is the input to ``import``, so carrying them across is exactly how a
    caller would try to hand a record to someone else — or point its git sync at a
    secret in another tenant's namespace.
    """

    @pytest.mark.parametrize("field", ["owner_sub", "acl", "workspace_id", "git_source"])
    def test_the_field_is_absent_from_the_export(self, storage, field):
        _store(storage, "wf-1", OWNER, acl={"owner_sub": OWNER, "viewers": [VIEWER]})
        r = _app(OWNER).get("/api/workflows/wf-1/export")
        assert r.status_code == 200
        assert field not in r.json()["workflow_json"]

    def test_the_stranger_sub_does_not_appear_anywhere_in_the_export(self, storage):
        """``acl`` carries other users' Cognito subs, which are not the exporter's to take."""
        _store(storage, "wf-1", OWNER, acl={"owner_sub": OWNER, "viewers": [VIEWER]})
        r = _app(OWNER).get("/api/workflows/wf-1/export")
        assert VIEWER not in r.text

    def test_the_canvas_itself_survives_stripping(self, storage):
        """Stripping must remove authority, not content."""
        _store(storage, "wf-1", OWNER)
        exported = _app(OWNER).get("/api/workflows/wf-1/export").json()["workflow_json"]
        assert exported["id"] == "wf-1"
        assert exported["name"] == "wf"
        assert len(exported["nodes"]) == 1
        # ``model_dump`` emits field names, not the camelCase aliases the UI sends.
        assert exported["metadata"]["aws_region"] == "us-east-1"


# ===========================================================================
# import
# ===========================================================================


class TestImportTakesOwnershipFromTheTokenNotTheBody:
    def _payload(self, **extra) -> dict:
        body = {
            "name": "imported",
            "version": "1.0.0",
            "nodes": [_valid_node()],
            "edges": [],
            "metadata": _metadata(),
        }
        body.update(extra)
        return body

    def test_a_forged_owner_sub_does_not_take_effect(self, storage):
        r = _app(STRANGER).post("/api/workflows/import", json={"workflow_json": self._payload(owner_sub=OWNER)})
        assert r.status_code == 200
        created = storage._workflows[r.json()["workflow"]["id"]]
        assert created.owner_sub == STRANGER

    def test_a_forged_acl_does_not_take_effect(self, storage):
        """An ACL naming the importer an editor of someone else's workspace."""
        r = _app(STRANGER).post(
            "/api/workflows/import",
            json={"workflow_json": self._payload(owner_sub=OWNER, acl={"owner_sub": OWNER, "editors": [STRANGER]})},
        )
        assert r.status_code == 200
        created = storage._workflows[r.json()["workflow"]["id"]]
        assert created.owner_sub == STRANGER
        acl = getattr(created, "acl", None)
        assert not acl or acl.get("owner_sub") in (None, STRANGER)

    def test_a_forged_git_source_does_not_take_effect(self, storage):
        """``git_source.token_ref`` is a Secrets Manager reference the sync job reads."""
        r = _app(STRANGER).post(
            "/api/workflows/import",
            json={
                "workflow_json": self._payload(
                    git_source={
                        "repo_url": "https://example.invalid/x.git",
                        "branch": "main",
                        "token_ref": "someone-elses/secret",
                    }
                )
            },
        )
        assert r.status_code == 200
        created = storage._workflows[r.json()["workflow"]["id"]]
        assert getattr(created, "git_source", None) is None

    def test_omitting_owner_sub_does_not_create_an_invisible_row(self, storage):
        """``owner_sub=None`` is treated as a pre-tenancy row and hidden from everyone,
        so an import that stored it produced a record nobody could read or delete."""
        client = _app(STRANGER)
        r = client.post("/api/workflows/import", json={"workflow_json": self._payload()})
        assert r.status_code == 200
        workflow_id = r.json()["workflow"]["id"]
        assert storage._workflows[workflow_id].owner_sub == STRANGER
        assert client.get(f"/api/workflows/{workflow_id}").status_code == 200
        assert client.delete(f"/api/workflows/{workflow_id}").status_code == 200

    def test_the_imported_canvas_is_still_the_one_that_was_sent(self, storage):
        """The vacuity direction: dropping the whole body would pass the tests above."""
        r = _app(STRANGER).post("/api/workflows/import", json={"workflow_json": self._payload()})
        assert r.status_code == 200
        created = storage._workflows[r.json()["workflow"]["id"]]
        assert created.name == "imported"
        assert len(created.nodes) == 1
        assert created.nodes[0].data.system_prompt == CANVAS_SECRET

    def test_an_export_round_trips_into_a_record_owned_by_the_importer(self, storage):
        """The end-to-end shape of the fix: A exports, B imports, B owns the copy and
        A's ownership is untouched."""
        _store(storage, "wf-1", OWNER, acl={"owner_sub": OWNER, "viewers": [VIEWER]})
        exported = _app(OWNER).get("/api/workflows/wf-1/export").json()["workflow_json"]
        r = _app(VIEWER).post("/api/workflows/import", json={"workflow_json": exported})
        assert r.status_code == 200
        new_id = r.json()["workflow"]["id"]
        assert new_id != "wf-1", "the id collided and should have been regenerated"
        assert storage._workflows[new_id].owner_sub == VIEWER
        assert storage._workflows["wf-1"].owner_sub == OWNER


# ===========================================================================
# validate
# ===========================================================================


class TestValidateIsNotAnOracleOverOtherTenants:
    def test_a_stranger_gets_404(self, storage):
        _store(storage, "wf-1", OWNER)
        r = _app(STRANGER).post("/api/workflows/wf-1/validate")
        assert r.status_code == 404

    def test_the_owner_still_gets_a_validation_result(self, storage):
        _store(storage, "wf-1", OWNER)
        r = _app(OWNER).post("/api/workflows/wf-1/validate")
        assert r.status_code == 200
        assert "is_valid" in r.json()

    def test_a_shared_viewer_still_gets_a_validation_result(self, storage):
        _store(storage, "wf-1", OWNER, acl={"owner_sub": OWNER, "viewers": [VIEWER]})
        assert _app(VIEWER).post("/api/workflows/wf-1/validate").status_code == 200

    def test_the_refusal_discloses_no_node_names(self, storage):
        """A ``ValidationResult`` names the offending nodes and their problems."""
        _store(storage, "wf-1", OWNER)
        r = _app(STRANGER).post("/api/workflows/wf-1/validate")
        assert CANVAS_SECRET not in r.text
        assert "n1" not in r.json().get("detail", "")


# ===========================================================================
# deploy
# ===========================================================================


class TestLegacyInProcessDeployIsRetired:
    BODY = {"aws_region": "us-east-1"}

    def test_the_route_is_refused_in_lambda(self, storage, monkeypatch):
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "agentcore-workflow-dev-workflow")
        _store(storage, "wf-1", OWNER)
        assert _app(OWNER).post("/api/workflows/wf-1/deploy", json=self.BODY).status_code == 501

    def test_the_route_is_also_refused_locally(self, storage, monkeypatch):
        monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
        _store(storage, "wf-1", OWNER)
        assert _app(OWNER).post("/api/workflows/wf-1/deploy", json=self.BODY).status_code == 501

    def test_the_refusal_names_the_supported_path(self, storage):
        _store(storage, "wf-1", OWNER)
        detail = _app(OWNER).post("/api/workflows/wf-1/deploy", json=self.BODY).json()["detail"]
        assert "/api/deploy" in detail
        assert "durable deployment state machine" in detail

    def test_it_refuses_before_reading_the_workflow(self, storage):
        storage.get = pytest.fail
        r = _app(OWNER).post("/api/workflows/wf-absent/deploy", json=self.BODY)
        assert r.status_code == 501

    def test_it_is_not_an_existence_or_acl_oracle(self, storage):
        _store(storage, "wf-1", OWNER)
        owner = _app(OWNER).post("/api/workflows/wf-1/deploy", json=self.BODY)
        stranger = _app(STRANGER).post("/api/workflows/wf-1/deploy", json=self.BODY)
        missing = _app(OWNER).post("/api/workflows/not-real/deploy", json=self.BODY)
        assert owner.status_code == stranger.status_code == missing.status_code == 501
        assert owner.json()["detail"] == stranger.json()["detail"] == missing.json()["detail"]

    def test_router_has_no_call_to_the_legacy_executor(self):
        import inspect

        from app.routers import workflows

        source = inspect.getsource(workflows)
        assert "WorkflowExecutor(" not in source
        assert "from app.services.deployment import WorkflowExecutor" not in source
