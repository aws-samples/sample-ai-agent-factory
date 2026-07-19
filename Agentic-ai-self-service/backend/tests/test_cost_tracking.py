"""Phase 2 Gap 2B — cost analytics + FinOps unit tests.

Covers:
  * price math (known models, eu./ap. prefix normalization, unknown-model
    fallback, zero tokens)
  * extract_usage_from_otel_span (present / missing / string-typed attrs)
  * UsageEventsStore round-trip under moto (put/get Decimal fidelity),
    query_for_runtime SK time-range filtering, query_for_owner via the
    owner_sub-event_id-index GSI
  * summarize() aggregation by_model
  * the cost endpoint via FastAPI TestClient + dependency_overrides:
    cross-tenant 404, no-production-slot 404, invalid-name 400,
    empty-summary (not 500) on ResourceNotFoundException, and a happy path
    where the boto3 logs client returns gen_ai.usage rows and the response
    cost matches the price table.

Runs standalone with no real AWS (moto) and no applied shared edits.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

moto = pytest.importorskip("moto")
from app.routers.cost import router as cost_router  # noqa: E402
from app.services.agent_versions_store import AgentVersion, RuntimeSlots  # noqa: E402
from app.services.auth import _LOCAL_DEV_SUB, get_caller_sub  # noqa: E402
from app.services.cost_tracking import (  # noqa: E402
    UsageEvent,
    UsageEventsStore,
    compute_cost,
    extract_usage_from_otel_span,
    new_event_id,
    normalize_model_id,
    summarize,
)
from moto import mock_aws  # noqa: E402  — import after the importorskip

# ---------------------------------------------------------------------------
# compute_cost / price table
# ---------------------------------------------------------------------------

_SONNET = "us.anthropic.claude-sonnet-5"
_HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_compute_cost_known_sonnet_matches_hand_math():
    # sonnet: input 0.003/1k, output 0.015/1k
    cost = compute_cost(_SONNET, 1000, 1000)
    assert cost == pytest.approx(0.003 + 0.015)


def test_compute_cost_haiku_differs_from_sonnet():
    haiku = compute_cost(_HAIKU, 1000, 1000)
    sonnet = compute_cost(_SONNET, 1000, 1000)
    # haiku: 0.001 + 0.005 = 0.006
    assert haiku == pytest.approx(0.006)
    assert haiku < sonnet


def test_compute_cost_eu_and_ap_prefixes_normalize_to_us_rate():
    base = compute_cost(_SONNET, 5000, 2000)
    eu = compute_cost("eu.anthropic.claude-sonnet-5", 5000, 2000)
    ap = compute_cost("ap.anthropic.claude-sonnet-5", 5000, 2000)
    assert eu == pytest.approx(base)
    assert ap == pytest.approx(base)


def test_compute_cost_bare_model_id_form_resolves():
    bare = compute_cost("anthropic.claude-sonnet-5", 1000, 0)
    assert bare == pytest.approx(0.003)


def test_compute_cost_unknown_model_falls_back_nonnegative():
    cost = compute_cost("anthropic.some-future-model-v9:0", 1000, 1000)
    # Falls back to the default rate (0.003 + 0.015), and is never negative.
    assert cost >= 0.0
    assert cost == pytest.approx(0.018)


def test_compute_cost_zero_tokens_is_zero():
    assert compute_cost(_SONNET, 0, 0) == 0.0


def test_compute_cost_negative_tokens_clamped():
    assert compute_cost(_SONNET, -100, -100) == 0.0


def test_normalize_model_id_strips_prefix():
    assert normalize_model_id(_SONNET) == "anthropic.claude-sonnet-5"
    assert normalize_model_id("anthropic.foo") == "anthropic.foo"
    assert normalize_model_id(None) == ""


# ---------------------------------------------------------------------------
# extract_usage_from_otel_span
# ---------------------------------------------------------------------------


def test_extract_usage_present_int_attrs():
    usage = extract_usage_from_otel_span(
        {
            "gen_ai.usage.input_tokens": 1234,
            "gen_ai.usage.output_tokens": 567,
            "gen_ai.request.model": _SONNET,
        }
    )
    assert usage == {
        "input_tokens": 1234,
        "output_tokens": 567,
        "model_id": _SONNET,
    }


def test_extract_usage_tolerates_string_typed_attrs():
    usage = extract_usage_from_otel_span(
        {
            "gen_ai.usage.input_tokens": "100",
            "gen_ai.usage.output_tokens": "50.0",
            "gen_ai.request.model": _HAIKU,
        }
    )
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 50
    assert usage["model_id"] == _HAIKU


def test_extract_usage_missing_attrs_degrades_gracefully():
    usage = extract_usage_from_otel_span({})
    assert usage == {"input_tokens": 0, "output_tokens": 0, "model_id": None}
    none_usage = extract_usage_from_otel_span(None)
    assert none_usage == {"input_tokens": 0, "output_tokens": 0, "model_id": None}


def test_extract_usage_falls_back_to_response_model():
    usage = extract_usage_from_otel_span(
        {
            "gen_ai.usage.input_tokens": 10,
            "gen_ai.usage.output_tokens": 20,
            "gen_ai.response.model": _SONNET,
        }
    )
    assert usage["model_id"] == _SONNET


# ---------------------------------------------------------------------------
# summarize()
# ---------------------------------------------------------------------------


def _ev(model_id: str, in_tok: int, out_tok: int, cost: float, owner="alice") -> UsageEvent:
    return UsageEvent(
        runtime_id="rt-1",
        event_id=new_event_id(),
        owner_sub=owner,
        model_id=model_id,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=cost,
        ts="2026-05-28T00:00:00+00:00",
    )


def test_summarize_aggregates_by_model():
    events = [
        _ev(_SONNET, 1000, 500, 0.0105),
        _ev(_SONNET, 2000, 1000, 0.021),
        _ev(_HAIKU, 1000, 1000, 0.006),
    ]
    summary = summarize(events)
    assert summary["total_in"] == 4000
    assert summary["total_out"] == 2500
    assert summary["total_cost"] == pytest.approx(0.0105 + 0.021 + 0.006)
    assert set(summary["by_model"].keys()) == {_SONNET, _HAIKU}
    assert summary["by_model"][_SONNET]["count"] == 2
    assert summary["by_model"][_SONNET]["input_tokens"] == 3000
    assert summary["by_model"][_HAIKU]["count"] == 1


def test_summarize_empty_is_zeroed():
    summary = summarize([])
    assert summary == {
        "total_cost": 0.0,
        "total_in": 0,
        "total_out": 0,
        "by_model": {},
    }


# ---------------------------------------------------------------------------
# UsageEventsStore (moto)
# ---------------------------------------------------------------------------


@pytest.fixture
def aws() -> Iterator[None]:
    with mock_aws():
        yield


@pytest.fixture
def events_store(aws: None) -> UsageEventsStore:
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="UsageEvents",
        KeySchema=[
            {"AttributeName": "runtime_id", "KeyType": "HASH"},
            {"AttributeName": "event_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "runtime_id", "AttributeType": "S"},
            {"AttributeName": "event_id", "AttributeType": "S"},
            {"AttributeName": "owner_sub", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "owner_sub-event_id-index",
                "KeySchema": [
                    {"AttributeName": "owner_sub", "KeyType": "HASH"},
                    {"AttributeName": "event_id", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return UsageEventsStore(table_name="UsageEvents", region="us-east-1")


def test_usage_event_put_get_round_trip(events_store: UsageEventsStore):
    ev = UsageEvent(
        runtime_id="rt-1",
        event_id=new_event_id(),
        owner_sub="alice",
        model_id=_SONNET,
        input_tokens=1234,
        output_tokens=567,
        cost_usd=0.012105,
        ts="2026-05-28T00:00:00+00:00",
        version_id="v1",
    )
    events_store.put(ev)
    loaded = events_store.get("rt-1", ev.event_id)
    assert loaded is not None
    assert loaded.owner_sub == "alice"
    assert loaded.model_id == _SONNET
    assert loaded.input_tokens == 1234
    assert loaded.output_tokens == 567
    # Decimal <-> float fidelity on cost_usd
    assert loaded.cost_usd == pytest.approx(0.012105)
    assert loaded.version_id == "v1"
    # TTL stamped automatically.
    assert loaded.ttl is not None and loaded.ttl > int(time.time())


def test_query_for_runtime_time_range_filters_on_sk(events_store: UsageEventsStore):
    # Build events with explicit time-prefixed event_ids spanning a window.
    def _eid(epoch_ms: int) -> str:
        return f"{epoch_ms:012x}" + "0" * 20

    base = int(time.time())
    inside_ids = []
    for offset in (10, 20, 30):  # seconds inside [base, base+60]
        ev = UsageEvent(
            runtime_id="rt-1",
            event_id=_eid((base + offset) * 1000),
            owner_sub="alice",
            model_id=_SONNET,
            input_tokens=10,
            output_tokens=10,
            cost_usd=0.0,
            ts="2026-05-28T00:00:00+00:00",
        )
        events_store.put(ev)
        inside_ids.append(ev.event_id)
    # One well before the window.
    events_store.put(
        UsageEvent(
            runtime_id="rt-1",
            event_id=_eid((base - 3600) * 1000),
            owner_sub="alice",
            model_id=_SONNET,
            input_tokens=99,
            output_tokens=99,
            cost_usd=0.0,
            ts="2026-05-28T00:00:00+00:00",
        )
    )

    results = events_store.query_for_runtime("rt-1", from_ts=base, to_ts=base + 60)
    got_ids = [e.event_id for e in results]
    assert set(got_ids) == set(inside_ids)
    # newest-first
    assert got_ids == sorted(inside_ids, reverse=True)


def test_query_for_owner_uses_gsi(events_store: UsageEventsStore):
    events_store.put(_ev(_SONNET, 1, 1, 0.0, owner="alice"))
    events_store.put(_ev(_HAIKU, 1, 1, 0.0, owner="alice"))
    events_store.put(_ev(_SONNET, 1, 1, 0.0, owner="bob"))
    alice = events_store.query_for_owner("alice")
    bob = events_store.query_for_owner("bob")
    assert len(alice) == 2
    assert all(e.owner_sub == "alice" for e in alice)
    assert len(bob) == 1
    assert bob[0].owner_sub == "bob"


# ---------------------------------------------------------------------------
# Cost endpoint (FastAPI TestClient + dependency_overrides)
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_router() -> FastAPI:
    app = FastAPI()
    app.include_router(cost_router)
    app.dependency_overrides[get_caller_sub] = lambda: _LOCAL_DEV_SUB
    return app


@pytest.fixture
def client(app_with_router: FastAPI) -> TestClient:
    return TestClient(app_with_router)


def test_cost_endpoint_invalid_runtime_name_400(client: TestClient):
    resp = client.get("/api/runtimes/has-hyphens/cost")
    assert resp.status_code == 400


def test_cost_endpoint_404_when_no_production_slot(client: TestClient):
    with patch("app.routers.cost.get_slots_store") as slots_store_mock:
        slots_store_mock.return_value.get.return_value = None
        resp = client.get("/api/runtimes/myagent/cost")
    assert resp.status_code == 404


def test_cost_endpoint_cross_tenant_404(client: TestClient):
    """Slot owned by someone else → 404 (existence non-disclosure)."""
    with (
        patch("app.routers.cost.get_slots_store") as slots_store_mock,
        patch("app.routers.cost.get_versions_store") as versions_store_mock,
    ):
        slots_store_mock.return_value.get.return_value = RuntimeSlots(
            runtime_name="myagent",
            owner_sub="someone-else",
            production_version_id="v1",
        )
        # Set up the version return too so a bug that bypasses the slot
        # assert_owner can't accidentally pass.
        versions_store_mock.return_value.get.return_value = AgentVersion(
            runtime_name="myagent",
            version_id="v1",
            owner_sub="someone-else",
            created_at="2026-05-28T00:00:00+00:00",
            deployment_id="d1",
            agentcore_runtime_name="myagent_xyz",
            runtime_id="rt-xyz",
        )
        resp = client.get("/api/runtimes/myagent/cost")
    assert resp.status_code == 404


def test_cost_endpoint_empty_summary_when_log_group_missing(client: TestClient):
    """ResourceNotFoundException (log group not created yet) → empty, not 500."""
    with (
        patch("app.routers.cost.get_slots_store") as slots_store_mock,
        patch("app.routers.cost.get_versions_store") as versions_store_mock,
        patch("boto3.client") as boto_mock,
    ):
        slots_store_mock.return_value.get.return_value = RuntimeSlots(
            runtime_name="myagent",
            owner_sub=_LOCAL_DEV_SUB,
            production_version_id="v1",
        )
        versions_store_mock.return_value.get.return_value = AgentVersion(
            runtime_name="myagent",
            version_id="v1",
            owner_sub=_LOCAL_DEV_SUB,
            created_at="2026-05-28T00:00:00+00:00",
            deployment_id="d1",
            agentcore_runtime_name="myagent_abcd1234",
            runtime_id="rt-xyz",
        )
        logs_client = MagicMock()

        class _RNF(Exception):
            pass

        logs_client.exceptions.ResourceNotFoundException = _RNF
        logs_client.start_query.side_effect = _RNF()
        boto_mock.return_value = logs_client

        resp = client.get("/api/runtimes/myagent/cost")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_cost"] == 0.0
    assert body["by_model"] == {}
    assert body["runtime_id"] == "rt-xyz"


def test_cost_endpoint_happy_path_cost_matches_price_table(client: TestClient):
    """Mocked logs client returns gen_ai.usage rows; response cost matches the
    price table applied to the mocked token sums."""
    with (
        patch("app.routers.cost.get_slots_store") as slots_store_mock,
        patch("app.routers.cost.get_versions_store") as versions_store_mock,
        patch("boto3.client") as boto_mock,
    ):
        slots_store_mock.return_value.get.return_value = RuntimeSlots(
            runtime_name="myagent",
            owner_sub=_LOCAL_DEV_SUB,
            production_version_id="v1",
        )
        versions_store_mock.return_value.get.return_value = AgentVersion(
            runtime_name="myagent",
            version_id="v1",
            owner_sub=_LOCAL_DEV_SUB,
            created_at="2026-05-28T00:00:00+00:00",
            deployment_id="d1",
            agentcore_runtime_name="myagent_abcd1234",
            runtime_id="rt-xyz",
        )
        logs_client = MagicMock()

        class _RNF(Exception):
            pass

        logs_client.exceptions.ResourceNotFoundException = _RNF
        logs_client.start_query.return_value = {"queryId": "q-1"}
        logs_client.get_query_results.return_value = {
            "status": "Complete",
            "results": [
                [
                    {"field": "model", "value": _SONNET},
                    {"field": "input_tokens", "value": "10000"},
                    {"field": "output_tokens", "value": "2000"},
                    {"field": "invocations", "value": "5"},
                ],
                [
                    {"field": "model", "value": _HAIKU},
                    {"field": "input_tokens", "value": "4000"},
                    {"field": "output_tokens", "value": "1000"},
                    {"field": "invocations", "value": "3"},
                ],
            ],
        }
        boto_mock.return_value = logs_client

        resp = client.get("/api/runtimes/myagent/cost")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # sonnet: 10k in * 0.003 + 2k out * 0.015 = 0.03 + 0.03 = 0.06
    # haiku:  4k in * 0.001 + 1k out * 0.005 = 0.004 + 0.005 = 0.009
    expected_sonnet = compute_cost(_SONNET, 10000, 2000)
    expected_haiku = compute_cost(_HAIKU, 4000, 1000)
    assert body["total_in"] == 14000
    assert body["total_out"] == 3000
    assert body["total_cost"] == pytest.approx(expected_sonnet + expected_haiku)
    assert body["by_model"][_SONNET]["cost"] == pytest.approx(expected_sonnet)
    assert body["by_model"][_SONNET]["count"] == 5
    assert body["by_model"][_HAIKU]["count"] == 3
    assert body["query_status"] == "Complete"


def test_cost_endpoint_invalid_window_400(client: TestClient):
    """from >= to → 400."""
    with patch("app.routers.cost.get_slots_store") as slots_store_mock:
        slots_store_mock.return_value.get.return_value = RuntimeSlots(
            runtime_name="myagent",
            owner_sub=_LOCAL_DEV_SUB,
            production_version_id="v1",
        )
        resp = client.get("/api/runtimes/myagent/cost?from=2000&to=1000")
    assert resp.status_code == 400
