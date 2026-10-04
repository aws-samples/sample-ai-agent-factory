"""F-15 (WorkflowDefinition half): a workflow save is a compare-and-set, not last-writer-wins.

``DynamoDBWorkflowStorage.update`` did ``_get_item`` then an unconditional ``_put_item`` of the
whole row, ``PUT /api/workflows/{id}`` accepted the whole document with no fence, and
``routers/workspaces.py`` rewrote the whole row from its own read to change one ACL field: a share
issued while an editor was saving silently dropped the editor's canvas (or the share).

Mirrors the flows fix exactly (``test_flow_save_is_a_compare_and_set``), with one naming
difference: ``WorkflowDefinition.version`` is the user-facing semver string, so the fence counter
is ``revision`` (wire: ``expectedRevision``). The 409 body carries ``code``, ``currentRevision``
and, for parity with the flows body a client already understands, ``currentVersion`` with the
same integer. Rows written before this change have no ``revision`` attribute, read as 0, and
their first save succeeds.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import boto3
import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

sys.path.insert(0, "src")

from app.models import WorkflowDefinition  # noqa: E402
from app.routers import workflows as workflows_router  # noqa: E402
from app.routers import workspaces as workspaces_router  # noqa: E402
from app.services import dynamodb_storage as dds  # noqa: E402
from app.services import storage as mem  # noqa: E402
from app.services.auth import get_caller_sub  # noqa: E402
from app.services.storage import WorkflowRevisionConflict, WorkflowStorage  # noqa: E402

TABLE = "test-workflows-cas"
REGION = "us-east-1"
OWNER = "owner-sub-1"
EDITOR = "editor-sub-2"


def _definition(marker: str, **overrides) -> WorkflowDefinition:
    """A minimal valid row whose ``name`` is the marker (the content the other writer must keep)."""
    now = datetime.now(timezone.utc)
    data = {
        "id": "wf-1",
        "name": marker,
        "version": "1.0.0",
        "nodes": [],
        "edges": [],
        "metadata": {"author": "probe", "awsRegion": REGION},
        "created_at": now,
        "updated_at": now,
        "owner_sub": OWNER,
    }
    data.update(overrides)
    return WorkflowDefinition.model_validate(data)


def _marks(wf: WorkflowDefinition) -> list[str]:
    return [wf.name]


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    os.environ.pop("AWS_PROFILE", None)


@pytest.fixture
def ddb_store(aws_env):
    with mock_aws():
        boto3.resource("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "workflow_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "workflow_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield dds.DynamoDBWorkflowStorage(table_name=TABLE, region=REGION)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def test_a_row_without_a_revision_attribute_reads_as_revision_zero():
    wf = _definition("legacy")
    assert wf.revision == 0
    assert "revision" in wf.model_dump(mode="json")


def test_a_negative_revision_is_not_a_valid_row():
    with pytest.raises(ValueError):
        _definition("x", revision=-1)


# ---------------------------------------------------------------------------
# DynamoDB store
# ---------------------------------------------------------------------------


def test_create_stamps_revision_zero_and_each_save_advances_it(ddb_store):
    created = ddb_store.create(_definition("a"))
    assert created.revision == 0
    first = ddb_store.update(created.id, created.model_copy(update={"name": "b"}), expected_revision=0)
    assert first is not None and first.revision == 1
    second = ddb_store.update(created.id, first.model_copy(update={"name": "c"}), expected_revision=1)
    assert second is not None and second.revision == 2
    assert ddb_store.get(created.id).revision == 2


def test_a_stale_tab_cannot_overwrite_a_newer_save(ddb_store):
    created = ddb_store.create(_definition("start"))
    ddb_store.update(created.id, _definition("tab-a-added-gateway"), expected_revision=0)

    with pytest.raises(WorkflowRevisionConflict) as excinfo:
        ddb_store.update(created.id, _definition("tab-b-stale"), expected_revision=0)

    assert excinfo.value.workflow_id == created.id
    assert excinfo.value.expected == 0
    assert excinfo.value.current.revision == 1
    stored = ddb_store.get(created.id)
    assert _marks(stored) == ["tab-a-added-gateway"], "the stale write landed"
    assert stored.revision == 1


def test_a_legacy_row_with_no_revision_attribute_saves_once_then_fences(ddb_store):
    item = dds._serialize_workflow(_definition("legacy", id="legacy-1"))
    del item["revision"]  # exactly what a row written before this change looks like
    ddb_store._table.put_item(Item=item)  # noqa: SLF001 - seeding the legacy shape raw
    assert "revision" not in ddb_store._table.get_item(Key={"workflow_id": "legacy-1"})["Item"]  # noqa: SLF001

    assert ddb_store.get("legacy-1").revision == 0
    saved = ddb_store.update("legacy-1", _definition("first-save", id="legacy-1"), expected_revision=0)
    assert saved is not None and saved.revision == 1

    with pytest.raises(WorkflowRevisionConflict):
        ddb_store.update("legacy-1", _definition("second-stale", id="legacy-1"), expected_revision=0)
    assert _marks(ddb_store.get("legacy-1")) == ["first-save"]


def test_the_row_itself_is_fenced_between_the_read_and_the_put(ddb_store, monkeypatch):
    """Two requests that both read revision 0 must not both land: freeze one request's read,
    move the row underneath it, and DynamoDB itself must refuse the put."""
    created = ddb_store.create(_definition("start"))
    frozen = dds._get_item(ddb_store._table, {"workflow_id": created.id})  # noqa: SLF001
    real_get = dds._get_item
    reads: list[int] = []

    def stale_first_read(table, key):
        reads.append(1)
        return frozen if len(reads) == 1 else real_get(table, key)

    ddb_store.update(created.id, _definition("winner"), expected_revision=0)

    monkeypatch.setattr(dds, "_get_item", stale_first_read)
    with pytest.raises(WorkflowRevisionConflict) as excinfo:
        ddb_store.update(created.id, _definition("loser"), expected_revision=0)

    assert excinfo.value.current.revision == 1, "the conflict must carry the row as it is now"
    assert _marks(ddb_store.get(created.id)) == ["winner"]


def test_a_save_that_names_no_revision_is_still_fenced_on_the_row_it_read(ddb_store, monkeypatch):
    created = ddb_store.create(_definition("start"))
    frozen = dds._get_item(ddb_store._table, {"workflow_id": created.id})  # noqa: SLF001
    ddb_store.update(created.id, _definition("winner"))
    real_get = dds._get_item
    reads: list[int] = []

    def stale_first_read(table, key):
        reads.append(1)
        return frozen if len(reads) == 1 else real_get(table, key)

    monkeypatch.setattr(dds, "_get_item", stale_first_read)
    with pytest.raises(WorkflowRevisionConflict):
        ddb_store.update(created.id, _definition("loser"))
    assert _marks(ddb_store.get(created.id)) == ["winner"]


def test_create_refuses_to_overwrite_an_existing_row_atomically(ddb_store, monkeypatch):
    created = ddb_store.create(_definition("first"))
    # Defeat the pre-read so only the conditional put stands between two creates of one id.
    monkeypatch.setattr(dds, "_get_item", lambda table, key: None)
    with pytest.raises(ValueError, match="already exists"):
        ddb_store.create(_definition("second", owner_sub="someone-else"))
    monkeypatch.undo()
    assert ddb_store.get(created.id).name == "first"


def test_a_conditional_put_error_that_is_not_the_fence_propagates(ddb_store, monkeypatch):
    created = ddb_store.create(_definition("start"))

    def boom(*a, **k):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}}, "PutItem")

    monkeypatch.setattr(dds, "_conditional_put_item", boom)
    with pytest.raises(ClientError):
        ddb_store.update(created.id, _definition("x"), expected_revision=0)


# ---------------------------------------------------------------------------
# In-memory store (what the router tests and local dev run against)
# ---------------------------------------------------------------------------


def test_the_in_memory_store_has_the_same_fence():
    store = WorkflowStorage()
    created = store.create(_definition("start"))
    assert created.revision == 0
    assert store.update(created.id, _definition("a"), expected_revision=0).revision == 1
    with pytest.raises(WorkflowRevisionConflict) as excinfo:
        store.update(created.id, _definition("stale"), expected_revision=0)
    assert excinfo.value.current.revision == 1
    assert _marks(store.get(created.id)) == ["a"]
    assert store.update(created.id, _definition("b")).revision == 2, "no expected revision still saves"


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------


@pytest.fixture
def store():
    original = mem.get_workflow_storage()
    fresh = WorkflowStorage()
    mem.set_workflow_storage(fresh)
    yield fresh
    mem.set_workflow_storage(original)


def _client(caller_sub: str = OWNER) -> TestClient:
    app = FastAPI()
    app.include_router(workflows_router.router)
    app.include_router(workspaces_router.router)
    app.dependency_overrides[get_caller_sub] = lambda: caller_sub
    return TestClient(app, raise_server_exceptions=False)


def _put_body(marker: str, expected: int | None = None) -> dict:
    body = {"name": marker}
    if expected is not None:
        body["expectedRevision"] = expected
    return body


def test_put_with_a_stale_expected_revision_is_a_409_carrying_the_current_revision(store):
    wf = store.create(_definition("start"))
    client = _client()

    first = client.put(f"/api/workflows/{wf.id}", json=_put_body("tab-a", 0))
    assert first.status_code == 200, first.text
    assert first.json()["workflow"]["revision"] == 1

    stale = client.put(f"/api/workflows/{wf.id}", json=_put_body("tab-b", 0))
    assert stale.status_code == 409, stale.text
    detail = stale.json()["detail"]
    assert detail["code"] == "workflow_revision_conflict"
    assert detail["currentRevision"] == 1
    assert detail["currentVersion"] == 1
    assert isinstance(detail["updatedAt"], str) and detail["updatedAt"]
    assert _marks(store.get(wf.id)) == ["tab-a"], "neither side may be lost: the server keeps tab A"

    retry = client.put(f"/api/workflows/{wf.id}", json=_put_body("tab-b-retry", 1))
    assert retry.status_code == 200, retry.text
    assert retry.json()["workflow"]["revision"] == 2


def test_the_read_and_list_routes_publish_the_revision(store):
    wf = store.create(_definition("start"))
    client = _client()
    client.put(f"/api/workflows/{wf.id}", json=_put_body("x", 0))
    assert client.get(f"/api/workflows/{wf.id}").json()["revision"] == 1
    assert client.get("/api/workflows").json()[0]["revision"] == 1


def test_a_put_without_expected_revision_still_saves_a_legacy_client(store):
    wf = store.create(_definition("start"))
    response = _client().put(f"/api/workflows/{wf.id}", json=_put_body("legacy-client"))
    assert response.status_code == 200, response.text
    assert response.json()["workflow"]["revision"] == 1


def test_a_negative_expected_revision_is_rejected_as_a_bad_request(store):
    wf = store.create(_definition("start"))
    assert _client().put(f"/api/workflows/{wf.id}", json=_put_body("x", -1)).status_code == 422


def test_a_stranger_gets_404_not_409_for_a_stale_revision(store):
    """Ownership runs first: a conflict body must not describe the row to a non-owner."""
    wf = store.create(_definition("start"))
    store.update(wf.id, _definition("owner-edit"), expected_revision=0)
    response = _client("stranger").put(f"/api/workflows/{wf.id}", json=_put_body("x", 0))
    assert response.status_code == 404


def test_a_shared_editor_is_fenced_like_the_owner(store):
    wf = store.create(_definition("start", acl={"owner_sub": OWNER, "editors": [EDITOR], "viewers": []}))
    store.update(wf.id, _definition("owner-edit", acl=wf.acl), expected_revision=0)
    response = _client(EDITOR).put(f"/api/workflows/{wf.id}", json=_put_body("editor-stale", 0))
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["currentRevision"] == 1


# ---------------------------------------------------------------------------
# The ACL writes in routers/workspaces.py rewrite the whole row: they must be fenced too
# ---------------------------------------------------------------------------


def test_a_share_advances_the_revision_and_keeps_the_canvas(store):
    wf = store.create(_definition("canvas"))
    response = _client().post(f"/api/workflows/{wf.id}/share", json={"sub": EDITOR, "role": "editor"})
    assert response.status_code == 200, response.text
    stored = store.get(wf.id)
    assert EDITOR in stored.acl["editors"]
    assert stored.revision == 1
    assert _marks(stored) == ["canvas"]


def test_a_share_built_on_a_row_that_moved_is_a_409_and_writes_nothing(store, monkeypatch):
    """The share handler reads the row, mutates the acl, and writes the WHOLE row back. If an
    editor's save lands between that read and that write, the share used to erase the save."""
    wf = store.create(_definition("before"))
    stale_copy = store.get(wf.id)
    real_get = store.get

    def read_then_someone_saves(workflow_id):
        row = real_get(workflow_id)
        # the editor's save lands after the share handler's read
        store.update(workflow_id, _definition("editor-save"), expected_revision=row.revision)
        return stale_copy

    monkeypatch.setattr(store, "get", read_then_someone_saves)
    response = _client().post(f"/api/workflows/{wf.id}/share", json={"sub": EDITOR, "role": "editor"})
    monkeypatch.undo()
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "workflow_revision_conflict"
    stored = store.get(wf.id)
    assert _marks(stored) == ["editor-save"], "the share must not have rewritten the canvas from its stale read"
    assert not (stored.acl or {}).get("editors")


def test_an_unshare_is_fenced_the_same_way(store, monkeypatch):
    wf = store.create(_definition("before", acl={"owner_sub": OWNER, "editors": [EDITOR], "viewers": []}))
    stale_copy = store.get(wf.id)
    real_get = store.get

    def read_then_someone_saves(workflow_id):
        row = real_get(workflow_id)
        store.update(workflow_id, _definition("editor-save", acl=row.acl), expected_revision=row.revision)
        return stale_copy

    monkeypatch.setattr(store, "get", read_then_someone_saves)
    response = _client().delete(f"/api/workflows/{wf.id}/share/{EDITOR}")
    monkeypatch.undo()
    assert response.status_code == 409, response.text
    assert _marks(store.get(wf.id)) == ["editor-save"]
    assert EDITOR in store.get(wf.id).acl["editors"], "the stale unshare must not have landed"
