"""F-15: a flow save is a compare-and-set, not a last-writer-wins overwrite.

``DynamoDBFlowStorage.update`` used to ``_get_item`` and then ``_put_item`` the whole row with no
condition, and ``PUT /flows/{id}`` accepted the whole document with no version. Two tabs on one flow:
the stale tab's move deleted the other tab's gateway and neither was told.

Now every ``Flow`` carries an integer ``version``. A save names the version it was built on
(``expectedVersion``); the store refuses the write when the row has moved (409 from the router,
carrying the server's current version) and the row itself is guarded by a DynamoDB
``ConditionExpression`` so two requests that both passed the in-Python check cannot both land.
Rows written before this change have no ``version`` attribute and read as version 0; the first save
against them succeeds and stamps version 1.

ARCC was queried (arcc CLI fallback, 2026-09-28) for optimistic concurrency / conditional writes and
returned nothing; this is standard DynamoDB practice.
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

import app.services.flow_storage  # noqa: E402, F401
from app.models import Flow  # noqa: E402
from app.routers import flows as flows_router  # noqa: E402
from app.services.auth import get_caller_sub  # noqa: E402

# ``app.services`` re-exports an INSTANCE named ``flow_storage`` that shadows the submodule, so
# ``from app.services import flow_storage`` binds the instance. Take the real module.
fs = sys.modules["app.services.flow_storage"]

TABLE = "test-flows-cas"
REGION = "us-east-1"
OWNER = "owner-sub-1"


def _workflow(marker: str) -> dict:
    return {
        "id": "wf-1",
        "name": marker,
        "version": "1.0.0",
        "nodes": [
            {"id": marker, "type": "runtime", "position": {"x": 0, "y": 0}, "data": {"component_type": "runtime"}}
        ],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }


def _node_ids(flow: Flow) -> list[str]:
    return [n["id"] for n in flow.workflow["nodes"]]


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
            KeySchema=[{"AttributeName": "flow_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "flow_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield fs.DynamoDBFlowStorage(table_name=TABLE, region=REGION)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def test_a_row_without_a_version_attribute_reads_as_version_zero():
    flow = Flow.model_validate(
        {
            "id": "legacy",
            "name": "n",
            "workflow": {},
            "created_at": "2026-09-25T00:00:00Z",
            "updated_at": "2026-09-25T00:00:00Z",
        }
    )
    assert flow.version == 0
    # and it is published to the client under the camelCase alias the store reads
    assert Flow.model_validate(flow.model_dump(mode="json")).model_dump(mode="json", by_alias=True)["version"] == 0


# ---------------------------------------------------------------------------
# DynamoDB store
# ---------------------------------------------------------------------------


def test_create_stamps_version_zero_and_each_save_advances_it(ddb_store):
    created = ddb_store.create("n", owner_sub=OWNER)
    assert created.version == 0
    first = ddb_store.update(created.id, workflow=_workflow("a"), expected_version=0)
    assert first is not None and first.version == 1
    second = ddb_store.update(created.id, workflow=_workflow("b"), expected_version=1)
    assert second is not None and second.version == 2
    assert ddb_store.get(created.id).version == 2


def test_a_stale_tab_cannot_overwrite_a_newer_save(ddb_store):
    created = ddb_store.create("n", owner_sub=OWNER)
    # Tab A and tab B both loaded version 0.
    ddb_store.update(created.id, workflow=_workflow("tab-a-added-gateway"), expected_version=0)

    with pytest.raises(fs.FlowVersionConflict) as excinfo:
        ddb_store.update(created.id, workflow=_workflow("tab-b-stale"), expected_version=0)

    assert excinfo.value.flow_id == created.id
    assert excinfo.value.current.version == 1
    stored = ddb_store.get(created.id)
    assert _node_ids(stored) == ["tab-a-added-gateway"], "the stale write landed"
    assert stored.version == 1


def test_a_legacy_row_with_no_version_attribute_saves_once_then_fences(ddb_store):
    now = datetime.now(timezone.utc)
    legacy = Flow(
        id="legacy-1", name="old", workflow=_workflow("legacy"), created_at=now, updated_at=now, owner_sub=OWNER
    )
    item = fs._serialize_flow(legacy)
    del item["version"]  # exactly what a row written before this change looks like
    ddb_store._table.put_item(Item=item)  # noqa: SLF001 - seeding the legacy shape raw
    assert "version" not in ddb_store._table.get_item(Key={"flow_id": "legacy-1"})["Item"]  # noqa: SLF001

    assert ddb_store.get("legacy-1").version == 0
    saved = ddb_store.update("legacy-1", workflow=_workflow("first-save"), expected_version=0)
    assert saved is not None and saved.version == 1

    with pytest.raises(fs.FlowVersionConflict):
        ddb_store.update("legacy-1", workflow=_workflow("second-stale"), expected_version=0)
    assert _node_ids(ddb_store.get("legacy-1")) == ["first-save"]


def test_the_row_itself_is_fenced_between_the_read_and_the_put(ddb_store, monkeypatch):
    """Two requests that both read version 0 must not both land.

    The in-Python ``expected_version`` check cannot see a write that happens after its read. Freeze
    the read at version 0, move the row underneath, and the put must be refused by DynamoDB itself.
    """
    created = ddb_store.create("n", owner_sub=OWNER)
    frozen = fs._get_item(ddb_store._table, {"flow_id": created.id})  # noqa: SLF001
    real_get = fs._get_item
    reads: list[int] = []

    def stale_first_read(table, key):
        reads.append(1)
        return frozen if len(reads) == 1 else real_get(table, key)

    # The other request lands first (through the real path).
    ddb_store.update(created.id, workflow=_workflow("winner"), expected_version=0)

    monkeypatch.setattr(fs, "_get_item", stale_first_read)
    with pytest.raises(fs.FlowVersionConflict) as excinfo:
        ddb_store.update(created.id, workflow=_workflow("loser"), expected_version=0)

    assert excinfo.value.current.version == 1, "the conflict must carry the row as it is now, not as it was read"
    assert _node_ids(ddb_store.get(created.id)) == ["winner"]


def test_a_save_that_names_no_version_is_still_fenced_on_the_row_it_read(ddb_store, monkeypatch):
    """A legacy client (no expectedVersion) keeps last-writer-wins across its session, but two
    concurrent server requests still cannot both land."""
    created = ddb_store.create("n", owner_sub=OWNER)
    frozen = fs._get_item(ddb_store._table, {"flow_id": created.id})  # noqa: SLF001
    ddb_store.update(created.id, workflow=_workflow("winner"))
    real_get = fs._get_item
    reads: list[int] = []

    def stale_first_read(table, key):
        reads.append(1)
        return frozen if len(reads) == 1 else real_get(table, key)

    monkeypatch.setattr(fs, "_get_item", stale_first_read)
    with pytest.raises(fs.FlowVersionConflict):
        ddb_store.update(created.id, workflow=_workflow("loser"))
    assert _node_ids(ddb_store.get(created.id)) == ["winner"]


def test_create_refuses_to_overwrite_an_existing_row(ddb_store, monkeypatch):
    created = ddb_store.create("first", owner_sub=OWNER)
    monkeypatch.setattr(fs.uuid, "uuid4", lambda: created.id)
    with pytest.raises(ClientError) as excinfo:
        ddb_store.create("second", owner_sub="someone-else")
    assert excinfo.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
    assert ddb_store.get(created.id).name == "first"


def test_a_rename_is_fenced_too_because_it_rewrites_the_whole_row(ddb_store):
    created = ddb_store.create("n", owner_sub=OWNER)
    ddb_store.update(created.id, workflow=_workflow("canvas-edit"), expected_version=0)
    with pytest.raises(fs.FlowVersionConflict):
        ddb_store.update(created.id, name="renamed-from-stale-list", expected_version=0)
    stored = ddb_store.get(created.id)
    assert stored.name == "n"
    assert _node_ids(stored) == ["canvas-edit"]


# ---------------------------------------------------------------------------
# In-memory store (what the router tests and local dev run against)
# ---------------------------------------------------------------------------


def test_the_in_memory_store_has_the_same_fence():
    store = fs.FlowStorage()
    created = store.create("n", owner_sub=OWNER)
    assert created.version == 0
    assert store.update(created.id, workflow=_workflow("a"), expected_version=0).version == 1
    with pytest.raises(fs.FlowVersionConflict) as excinfo:
        store.update(created.id, workflow=_workflow("stale"), expected_version=0)
    assert excinfo.value.current.version == 1
    assert _node_ids(store.get(created.id)) == ["a"]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def _client(store, monkeypatch, caller_sub: str = OWNER) -> TestClient:
    monkeypatch.setattr(flows_router, "_get_flow_storage", lambda: store)
    app = FastAPI()
    app.include_router(flows_router.router)
    app.dependency_overrides[get_caller_sub] = lambda: caller_sub
    return TestClient(app, raise_server_exceptions=False)


def test_put_with_a_stale_expected_version_is_a_409_carrying_the_current_version(monkeypatch):
    store = fs.FlowStorage()
    flow = store.create("n", owner_sub=OWNER)
    client = _client(store, monkeypatch)

    first = client.put(f"/flows/{flow.id}", json={"workflow": _workflow("tab-a"), "expectedVersion": 0})
    assert first.status_code == 200, first.text
    assert first.json()["flow"]["version"] == 1

    stale = client.put(f"/flows/{flow.id}", json={"workflow": _workflow("tab-b"), "expectedVersion": 0})
    assert stale.status_code == 409, stale.text
    detail = stale.json()["detail"]
    assert detail["code"] == "flow_version_conflict"
    assert detail["currentVersion"] == 1
    assert isinstance(detail["updatedAt"], str) and detail["updatedAt"]
    # neither side was lost: the server still holds tab A's canvas
    assert _node_ids(store.get(flow.id)) == ["tab-a"]

    # and the row moved on, so a retry that adopts the server's version succeeds
    retry = client.put(f"/flows/{flow.id}", json={"workflow": _workflow("tab-b-retry"), "expectedVersion": 1})
    assert retry.status_code == 200, retry.text
    assert retry.json()["flow"]["version"] == 2


def test_the_read_and_list_routes_publish_the_version(monkeypatch):
    store = fs.FlowStorage()
    flow = store.create("n", owner_sub=OWNER)
    client = _client(store, monkeypatch)
    client.put(f"/flows/{flow.id}", json={"workflow": _workflow("x"), "expectedVersion": 0})

    assert client.get(f"/flows/{flow.id}").json()["version"] == 1
    assert client.get("/flows").json()["flows"][0]["version"] == 1


def test_a_put_without_expected_version_still_saves_a_legacy_client(monkeypatch):
    store = fs.FlowStorage()
    flow = store.create("n", owner_sub=OWNER)
    client = _client(store, monkeypatch)
    response = client.put(f"/flows/{flow.id}", json={"workflow": _workflow("legacy-client")})
    assert response.status_code == 200, response.text
    assert response.json()["flow"]["version"] == 1


def test_a_negative_expected_version_is_rejected_as_a_bad_request(monkeypatch):
    store = fs.FlowStorage()
    flow = store.create("n", owner_sub=OWNER)
    client = _client(store, monkeypatch)
    response = client.put(f"/flows/{flow.id}", json={"workflow": _workflow("x"), "expectedVersion": -1})
    assert response.status_code == 422


def test_a_stranger_gets_404_not_409_for_a_stale_version(monkeypatch):
    """The ownership check runs first: a conflict body must not leak the row's state to a non-owner."""
    store = fs.FlowStorage()
    flow = store.create("n", owner_sub=OWNER)
    store.update(flow.id, workflow=_workflow("owner-edit"), expected_version=0)
    client = _client(store, monkeypatch, caller_sub="stranger")
    response = client.put(f"/flows/{flow.id}", json={"workflow": _workflow("x"), "expectedVersion": 0})
    assert response.status_code == 404
