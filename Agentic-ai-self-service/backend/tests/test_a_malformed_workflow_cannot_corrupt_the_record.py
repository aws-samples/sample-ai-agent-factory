"""A malformed ``nodes``/``edges`` payload must be refused BEFORE it is stored.

This file exists because of a live measurement, not a review opinion. Against the
deployed stack ``acfe2e-p0920`` (us-east-1), one ``PUT /api/workflows/{id}`` from a
legitimately authenticated ``agent:write`` caller permanently bricked a workflow:

  | call                                            | before the fix        |
  |-------------------------------------------------|-----------------------|
  | ``PUT`` with ``nodes=[{... "data": {"nope":1}}]``| 500, and it PERSISTED |
  | ``GET /api/workflows/{id}``                     | 500 forever           |
  | ``DELETE /api/workflows/{id}``                  | 500 forever           |
  | ``PUT`` with valid ``{"nodes":[],"edges":[]}``  | 500 (repair refused)  |
  | ``GET /api/workflows`` (list)                   | 200 ``[]`` — invisible|

The chain, each link verified: ``nodes``/``edges`` were a bare ``list`` on the request
models, so FastAPI validated nothing about their contents → ``update_workflow`` merged
them with ``existing.model_copy(update=...)``, which does NOT validate → the row was
written to DynamoDB → building the response then raised ``ValidationError`` as an
unhandled 500 → every later read hit ``_deserialize_workflow`` → both delete and update
read-before-write, so the row was neither repairable nor removable → and ``list_all``
swallows the per-item deserialize failure, so nothing showed the damage.

The suites below are ordered by the link they pin:

  A. the boundary refuses it (the fix), with the vacuity direction
  B. the cross-field re-validation after ``model_copy`` (what typing cannot catch)
  C. an ALREADY-corrupt row — written by the old code, or by anything but this API —
     is readable-as-an-error and deletable by its owner

Part C runs against a real ``DynamoDBWorkflowStorage`` over moto with a genuinely
poisoned item put straight into the table, because that is the only way to reproduce
the state the bug leaves behind: an in-memory store holds parsed models and therefore
cannot BE corrupt.

ARCC ``cnt_VlYhNEFt6msJmr``: "Strong input validation is performed on objects before
deserialization takes place." ARCC ``cnt_94E30Xo4RZHtSJ``: fail closed.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

sys.path.insert(0, "src")

from app.services.auth import get_caller_sub  # noqa: E402

OWNER = "owner-sub-aaaa"
STRANGER = "stranger-sub-bbbb"
REGION = "us-east-1"
TABLE = "test-corruption-workflows"

# A sentinel inside the rejected payload, used to prove which error bodies quote what.
SENTINEL = "zzsentinelzz"
# A sentinel inside the STORED record. This is the one that must never come back out:
# the object handed to the post-merge re-validation is stored content plus caller input,
# and a pydantic message quotes the value it rejected.
STORED_SENTINEL = "zzstoredzz"


def _app(caller_sub: str) -> TestClient:
    from app.routers import workflows_router

    app = FastAPI()
    app.include_router(workflows_router)
    app.dependency_overrides[get_caller_sub] = lambda: caller_sub
    return TestClient(app, raise_server_exceptions=False)


def _metadata() -> dict:
    return {
        "author": "tester",
        "tags": [],
        "awsRegion": REGION,
    }


def _valid_node(node_id: str = "n1", prompt: str = "be useful") -> dict:
    """The minimal node the API accepts. Established by probing the live API: a
    ``type`` with no matching ``data.component_type`` and ``modelId`` instead of
    ``model_id`` were both 500s, not 422s, before this fix."""
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


def _edge(source: str, target: str, edge_id: str = "e1") -> dict:
    """A structurally complete edge. ``sourceHandle``/``targetHandle``/``type`` are
    required, so an edge that omits them is refused by the request model — which is a
    different (and weaker) refusal than the reference check these tests are after."""
    return {
        "id": edge_id,
        "source": source,
        "target": target,
        "sourceHandle": "out",
        "targetHandle": "in",
        "type": "data",
    }


def _malformed_node() -> dict:
    """The exact shape that corrupted the live record: a real component ``type`` with
    a ``data`` object that describes nothing."""
    return {
        "id": "bad",
        "type": "runtime",
        "position": {"x": 0, "y": 0},
        "data": {"nope": SENTINEL},
    }


@pytest.fixture
def storage():
    """In-memory storage holding real ``WorkflowDefinition`` instances."""
    from app.services.storage import WorkflowStorage, get_workflow_storage, set_workflow_storage

    original = get_workflow_storage()
    store = WorkflowStorage()
    set_workflow_storage(store)
    yield store
    set_workflow_storage(original)


def _create(client: TestClient, **overrides) -> str:
    body = {
        "name": "wf",
        "version": "1.0.0",
        "nodes": [_valid_node()],
        "edges": [],
        "metadata": _metadata(),
    }
    body.update(overrides)
    resp = client.post("/api/workflows", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["workflow"]["id"]


# ===========================================================================
# A — the boundary refuses it, and still accepts what is valid
# ===========================================================================


class TestTheBoundaryRefusesAMalformedNode:
    def test_create_with_a_malformed_node_is_a_422_not_a_500(self, storage):
        resp = _app(OWNER).post(
            "/api/workflows",
            json={
                "name": "wf",
                "version": "1.0.0",
                "nodes": [_malformed_node()],
                "edges": [],
                "metadata": _metadata(),
            },
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"], "a 422 with no detail tells the caller nothing"

    def test_create_with_a_malformed_node_stores_nothing(self, storage):
        _app(OWNER).post(
            "/api/workflows",
            json={
                "name": "wf",
                "version": "1.0.0",
                "nodes": [_malformed_node()],
                "edges": [],
                "metadata": _metadata(),
            },
        )
        assert storage.list_all() == [], "the rejected workflow was written anyway"

    def test_update_with_a_malformed_node_is_a_422_not_a_500(self, storage):
        client = _app(OWNER)
        wid = _create(client)
        resp = client.put(f"/api/workflows/{wid}", json={"nodes": [_malformed_node()]})
        assert resp.status_code == 422, resp.text

    @pytest.mark.parametrize("method", ["post", "put"])
    def test_the_refusal_names_the_offending_field(self, storage, method):
        """This is what pins the TYPING specifically, and it is worth a test of its own:
        the post-merge re-validation below would also return 422 for this payload, but
        with a generic message. Only request-model validation can tell the caller
        ``body -> nodes -> 0 -> data``, and an un-actionable 422 is how a user ends up
        filing "the canvas won't save" instead of fixing their node.
        """
        client = _app(OWNER)
        if method == "post":
            resp = client.post(
                "/api/workflows",
                json={
                    "name": "wf",
                    "version": "1.0.0",
                    "nodes": [_malformed_node()],
                    "edges": [],
                    "metadata": _metadata(),
                },
            )
        else:
            resp = client.put(f"/api/workflows/{_create(client)}", json={"nodes": [_malformed_node()]})
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert isinstance(detail, list), f"not a field-level refusal: {detail}"
        locs = [".".join(str(part) for part in err.get("loc", ())) for err in detail]
        assert any("nodes" in loc for loc in locs), f"the refusal does not name nodes: {locs}"

    def test_update_with_a_malformed_node_leaves_the_stored_row_intact(self, storage):
        """The whole point. A rejected update that still wrote is the bug; a 422 that
        wrote is the same bug with a nicer status code."""
        client = _app(OWNER)
        wid = _create(client)
        before = storage.get(wid)
        client.put(f"/api/workflows/{wid}", json={"nodes": [_malformed_node()]})
        after = storage.get(wid)
        assert after is not None, "the row vanished"
        assert [n.id for n in after.nodes] == [n.id for n in before.nodes]
        assert after.updated_at == before.updated_at, "the row was rewritten by a rejected update"

    def test_the_row_is_still_readable_after_a_rejected_update(self, storage):
        """The corruption was not that one call failed — it was that every later call
        failed. This is the assertion that would have caught it."""
        client = _app(OWNER)
        wid = _create(client)
        client.put(f"/api/workflows/{wid}", json={"nodes": [_malformed_node()]})
        assert client.get(f"/api/workflows/{wid}").status_code == 200
        assert client.delete(f"/api/workflows/{wid}").status_code == 200

    def test_no_error_body_echoes_the_stored_record(self, storage):
        """A pydantic message quotes the value it rejected, and the object handed to the
        post-merge re-validation is the STORED record with the caller's fields merged
        in. So returning that detail would hand a caller back content they did not
        send — on a shared workflow, content they may not be able to read at all.

        Note the deliberate asymmetry: FastAPI's own 422 for a request-model failure
        DOES echo the offending value, and that is fine — it is the caller's own
        submission coming straight back. Only the post-merge path is a disclosure.
        """
        client = _app(OWNER)
        wid = _create(client, nodes=[_valid_node("n1", prompt=STORED_SENTINEL)])
        # Rejected by the cross-field check, which runs on the merged object.
        update = client.put(f"/api/workflows/{wid}", json={"edges": [_edge("n1", "ghost")]})
        assert update.status_code == 422, update.text
        assert STORED_SENTINEL not in update.text, "the stored system prompt came back in the error body"


class TestTheBoundaryStillAcceptsWhatIsValid:
    """The vacuity direction. Every test above is satisfied by an endpoint that rejects
    everything, and a refusal-only suite has hidden a dead happy path in this repo
    before."""

    def test_a_valid_create_still_returns_201_and_is_readable(self, storage):
        client = _app(OWNER)
        wid = _create(client)
        got = client.get(f"/api/workflows/{wid}")
        assert got.status_code == 200, got.text
        assert [n["id"] for n in got.json()["nodes"]] == ["n1"]

    def test_a_valid_update_still_returns_200_and_is_applied(self, storage):
        client = _app(OWNER)
        wid = _create(client)
        resp = client.put(
            f"/api/workflows/{wid}",
            json={"name": "renamed", "nodes": [_valid_node("n1"), _valid_node("n2")]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["workflow"]["name"] == "renamed"
        assert sorted(n.id for n in storage.get(wid).nodes) == ["n1", "n2"]

    def test_clearing_the_canvas_is_still_allowed(self, storage):
        """``{"nodes": [], "edges": []}`` was the repair attempt that the live bug also
        refused. An empty list must not be confused with "field absent"."""
        client = _app(OWNER)
        wid = _create(client)
        resp = client.put(f"/api/workflows/{wid}", json={"nodes": [], "edges": []})
        assert resp.status_code == 200, resp.text
        assert storage.get(wid).nodes == []

    def test_a_valid_edge_between_two_real_nodes_is_accepted(self, storage):
        client = _app(OWNER)
        resp = client.post(
            "/api/workflows",
            json={
                "name": "wf",
                "version": "1.0.0",
                "nodes": [_valid_node("n1"), _valid_node("n2")],
                "edges": [_edge("n1", "n2")],
                "metadata": _metadata(),
            },
        )
        assert resp.status_code == 201, resp.text


# ===========================================================================
# B — the cross-field check that typing cannot make
# ===========================================================================


class TestEdgeReferencesAreCheckedAfterTheMerge:
    """``WorkflowDefinition.validate_edge_references`` compares edges against nodes, so
    no request model can enforce it: a PUT may send edges while keeping the stored
    nodes. ``model_copy`` runs no validators, so without the explicit re-validation
    this shape reaches DynamoDB with well-typed fields and corrupts the row exactly
    like an untyped one."""

    def test_create_with_a_dangling_edge_is_a_422_not_a_500(self, storage):
        resp = _app(OWNER).post(
            "/api/workflows",
            json={
                "name": "wf",
                "version": "1.0.0",
                "nodes": [_valid_node("n1")],
                "edges": [_edge("n1", "ghost")],
                "metadata": _metadata(),
            },
        )
        assert resp.status_code == 422, resp.text
        assert storage.list_all() == []

    def test_update_with_a_dangling_edge_is_a_422_and_does_not_write(self, storage):
        client = _app(OWNER)
        wid = _create(client)
        before = storage.get(wid)
        resp = client.put(f"/api/workflows/{wid}", json={"edges": [_edge("n1", "ghost")]})
        assert resp.status_code == 422, resp.text
        assert storage.get(wid).edges == before.edges
        assert client.get(f"/api/workflows/{wid}").status_code == 200

    def test_dropping_a_node_an_edge_still_points_at_is_refused(self, storage):
        """The asymmetric case: the edges are untouched and valid on their own, and it
        is the NODE removal that breaks the reference. Only a whole-object check sees
        it."""
        client = _app(OWNER)
        resp = client.post(
            "/api/workflows",
            json={
                "name": "wf",
                "version": "1.0.0",
                "nodes": [_valid_node("n1"), _valid_node("n2")],
                "edges": [_edge("n1", "n2")],
                "metadata": _metadata(),
            },
        )
        wid = resp.json()["workflow"]["id"]
        put = client.put(f"/api/workflows/{wid}", json={"nodes": [_valid_node("n1")]})
        assert put.status_code == 422, put.text
        assert len(storage.get(wid).nodes) == 2, "the node removal was persisted anyway"

    def test_dropping_the_node_together_with_the_edge_is_allowed(self, storage):
        """Vacuity guard for the three tests above: the re-validation must reject an
        INCONSISTENT object, not any object that shrinks."""
        client = _app(OWNER)
        resp = client.post(
            "/api/workflows",
            json={
                "name": "wf",
                "version": "1.0.0",
                "nodes": [_valid_node("n1"), _valid_node("n2")],
                "edges": [_edge("n1", "n2")],
                "metadata": _metadata(),
            },
        )
        wid = resp.json()["workflow"]["id"]
        put = client.put(f"/api/workflows/{wid}", json={"nodes": [_valid_node("n1")], "edges": []})
        assert put.status_code == 200, put.text


# ===========================================================================
# C — an already-corrupt row: readable as an error, deletable by its owner
# ===========================================================================


def _poisoned_item(workflow_id: str, owner_sub: str | None) -> dict:
    """A DynamoDB item that ``_deserialize_workflow`` cannot parse.

    Written directly to the table, which is how it really arises: by the old code
    path, by a schema change, or by anything touching the table that is not this API.
    """
    now = datetime.now(timezone.utc).isoformat()
    item = {
        "workflow_id": workflow_id,
        "name": "poisoned",
        "description": "",
        "version": "1.0.0",
        "nodes": [{"id": "bad", "type": "runtime", "position": {"x": 0, "y": 0}, "data": {"nope": SENTINEL}}],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
        "metadata": {"author": "tester", "tags": [], "aws_region": REGION},
        "created_at": now,
        "updated_at": now,
    }
    if owner_sub is not None:
        item["owner_sub"] = owner_sub
    return item


@pytest.fixture
def poisoned_table():
    """A real ``DynamoDBWorkflowStorage`` over moto, wired in as the app's storage,
    holding one unparseable row (``wf-bad``) and one good row (``wf-ok``)."""
    from app.models import Viewport, WorkflowDefinition, WorkflowMetadata
    from app.services.dynamodb_storage import DynamoDBWorkflowStorage
    from app.services.storage import get_workflow_storage, set_workflow_storage

    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION,
    }.items():
        os.environ[key] = value

    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "workflow_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "workflow_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        store = DynamoDBWorkflowStorage(table_name=TABLE, region=REGION)
        table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
        table.put_item(Item=_poisoned_item("wf-bad", OWNER))
        now = datetime.now(timezone.utc)
        store.create(
            WorkflowDefinition(
                id="wf-ok",
                name="good",
                version="1.0.0",
                nodes=[],
                edges=[],
                viewport=Viewport(x=0, y=0, zoom=1.0),
                metadata=WorkflowMetadata(author="tester", aws_region=REGION),
                created_at=now,
                updated_at=now,
                owner_sub=OWNER,
            )
        )
        original = get_workflow_storage()
        set_workflow_storage(store)
        try:
            yield store, table
        finally:
            set_workflow_storage(original)


class TestAnUnreadableRowIsStillOwnedAndStillRemovable:
    def test_the_row_really_is_unparseable(self, poisoned_table):
        """Guard on the fixture itself. If moto or a model change made this item
        parseable, every test below would pass while testing nothing."""
        from pydantic import ValidationError

        store, _ = poisoned_table
        with pytest.raises(ValidationError):
            store.get("wf-bad")

    def test_get_tells_the_owner_what_is_wrong_and_what_to_do(self, poisoned_table):
        resp = _app(OWNER).get("/api/workflows/wf-bad")
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert "DELETE" in detail, f"the owner is not told how to recover: {detail}"
        assert SENTINEL not in resp.text, "the stored content leaked into the error body"

    def test_get_does_not_confirm_the_row_exists_to_anyone_else(self, poisoned_table):
        """ "Invalid record" is an existence oracle. A stranger gets the same 404 they
        would get for an id that was never used."""
        corrupt = _app(STRANGER).get("/api/workflows/wf-bad")
        absent = _app(STRANGER).get("/api/workflows/wf-nonexistent")
        assert corrupt.status_code == absent.status_code == 404
        # Same message modulo the id each one was asked about, so the pair is not an
        # oracle for "this id exists but is broken".
        assert corrupt.json()["detail"].replace("wf-bad", "X") == absent.json()["detail"].replace("wf-nonexistent", "X")

    def test_update_refuses_rather_than_500s(self, poisoned_table):
        resp = _app(OWNER).put("/api/workflows/wf-bad", json={"nodes": [], "edges": []})
        assert resp.status_code == 422, resp.text

    def test_validate_refuses_rather_than_500s(self, poisoned_table):
        resp = _app(OWNER).post("/api/workflows/wf-bad/validate")
        assert resp.status_code == 422, resp.text

    def test_the_owner_can_delete_it(self, poisoned_table):
        """The link that made this unrecoverable: the ownership check read ``owner_sub``
        off the parsed model, so when parsing was what failed, delete failed BEFORE it
        authorized anything."""
        store, table = poisoned_table
        resp = _app(OWNER).delete("/api/workflows/wf-bad")
        assert resp.status_code == 200, resp.text
        assert "Item" not in table.get_item(Key={"workflow_id": "wf-bad"}), "the row is still in the table"

    def test_a_stranger_cannot_delete_it(self, poisoned_table):
        """Unreadable is not unowned. The escape hatch must not be a way to delete
        another tenant's row by first making it unparseable."""
        store, table = poisoned_table
        resp = _app(STRANGER).delete("/api/workflows/wf-bad")
        assert resp.status_code == 404, resp.text
        assert "Item" in table.get_item(Key={"workflow_id": "wf-bad"})

    def test_a_corrupt_row_with_no_owner_is_deletable_by_nobody(self, poisoned_table):
        """Pre-tenancy rows are invisible to every caller by policy
        (``assert_owner``), and being unreadable must not promote one to
        deletable-by-anyone."""
        _store, table = poisoned_table
        table.put_item(Item=_poisoned_item("wf-orphan", None))
        assert _app(OWNER).delete("/api/workflows/wf-orphan").status_code == 404
        assert "Item" in table.get_item(Key={"workflow_id": "wf-orphan"})

    def test_deleting_a_good_row_still_works(self, poisoned_table):
        """Vacuity guard: the unreadable-row branch must not have swallowed the normal
        delete path."""
        assert _app(OWNER).delete("/api/workflows/wf-ok").status_code == 200

    def test_the_good_row_is_still_listed_while_the_bad_one_is_hidden(self, poisoned_table):
        """Records the behaviour that hid the damage in production rather than
        asserting it is desirable: ``list_all`` swallows the per-item deserialize
        failure. The corrupt row is invisible in the list, which is exactly why GET
        and DELETE had to become the recovery path."""
        body = _app(OWNER).get("/api/workflows").json()
        assert [w["id"] for w in body] == ["wf-ok"]


class TestTheRawOwnerLookup:
    """``get_owner_sub_unvalidated`` is the only thing authorizing the delete of a row
    nobody can parse, so its failure modes are the authorization's failure modes."""

    def test_it_returns_the_owner_without_parsing(self, poisoned_table):
        store, _ = poisoned_table
        assert store.get_owner_sub_unvalidated("wf-bad") == (True, OWNER)

    def test_a_missing_row_is_reported_as_absent_not_unowned(self, poisoned_table):
        store, _ = poisoned_table
        assert store.get_owner_sub_unvalidated("wf-nope") == (False, None)

    @pytest.mark.parametrize("bad_owner", [123, {"sub": OWNER}, ["a"], True])
    def test_a_non_string_owner_is_reported_as_none(self, poisoned_table, bad_owner):
        """A dict or a number here would be compared against the caller's sub with
        ``==``. ``None`` routes it to ``assert_owner``'s legacy-row refusal instead of
        inventing an answer."""
        store, table = poisoned_table
        item = _poisoned_item("wf-weird", None)
        item["owner_sub"] = bad_owner
        table.put_item(Item=item)
        assert store.get_owner_sub_unvalidated("wf-weird") == (True, None)

    def test_the_in_memory_backend_implements_the_same_method(self):
        """The router calls this via ``getattr`` so a backend without it degrades
        rather than crashes — but both shipped backends having it is what makes the
        behaviour uniform between local dev and Lambda."""
        from app.services.storage import WorkflowStorage

        assert callable(WorkflowStorage().get_owner_sub_unvalidated)
        assert WorkflowStorage().get_owner_sub_unvalidated("nope") == (False, None)
