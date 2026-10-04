"""Unit tests for services/memory_trace_lookup.py — Memory log -> span -> trace join.

Fixtures mirror LIVE-VERIFIED shapes (2026-09-14, us-west-2):
  * Memory APPLICATION_LOGS extraction record: session_id/actor_id at top level,
    body.currentConversations[].eventId = "<epoch-ms>#<hash>", and NO trace field.
  * aws/spans Memory span row: traceId + attributes.event.id (CreateEvent only,
    "" on ListEvents) + attributes.session.id.
No AWS credentials needed — the logs client is a MagicMock.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from app.services import memory_trace_lookup as mtl

MEM = "trcverify_mem-fE6ah3CPFU"
SESSION = "harnessC-554990d0334a4093a86705c10220354a"
TRACE = "6aa7d9a03b8deec427907e895d0223aa"
EID_1 = "0000001789385121339#a124a6bd"
EID_2 = "0000001789385121452#69724a53"


def _extraction_record(event_ids: list[str], log: str = "Starting to process Summary strategies.") -> dict:
    return {
        "resource_arn": f"arn:aws:bedrock-agentcore:us-west-2:111122223333:memory/{MEM}",
        "event_timestamp": 1789385186608,
        "memory_strategy_id": "TrcSummarizer-At3PP0274h",
        "namespace": f"/summaries/default/{SESSION}/",
        "actor_id": "default",
        "session_id": SESSION,
        "body": {
            "log": log,
            "requestId": "f90f1d70-fc07-45d8-9486-e1c8e97fe9ac",
            "isError": False,
            "currentConversations": [
                {"role": "USER", "content": {"text": "hi"}, "eventId": eid, "eventTimestamp": 1789385121339}
                for eid in event_ids
            ],
        },
        "attributes": {
            "aws.resource.type": "AWS::BedrockAgentCore::Memory",
            "session.id": SESSION,
            "actor.id": "default",
        },
        "severityText": "INFO",
    }


def _consolidation_record() -> dict:
    # Consolidation logs carry session_id but NO event ids (documented field set).
    rec = _extraction_record([], log="Processing consolidation input")
    rec["body"].pop("currentConversations")
    rec.pop("actor_id")
    return rec


def _span(name: str, event_id: str, trace_id: str = TRACE, session: str = SESSION) -> dict:
    return {
        "@timestamp": "2026-09-14 11:25:21.437",
        "traceId": trace_id,
        "spanId": "266d18a4a70d2a58",
        "parentSpanId": "3519a838ae8c0372",
        "name": name,
        "attributes.event.id": event_id,
        "attributes.session.id": session,
        "attributes.actor.id": "default",
        "attributes.harness.id": "trcverify_harness-wSksv7Qy0F",
    }


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_parse_record_accepts_json_and_dict_and_rejects_garbage():
    rec = _extraction_record([EID_1])
    assert mtl.parse_record(json.dumps(rec)) == rec
    assert mtl.parse_record(rec) is rec
    assert mtl.parse_record("not json") == {}
    assert mtl.parse_record("[1,2]") == {}


def test_event_ids_from_record_collects_conversation_event_ids_in_order():
    assert mtl.event_ids_from_record(_extraction_record([EID_1, EID_2, EID_1])) == [EID_1, EID_2]
    assert mtl.event_ids_from_record(_consolidation_record()) == []
    rec = _extraction_record([EID_2])
    rec["event_id"] = EID_1  # documented top-level field wins first
    assert mtl.event_ids_from_record(rec) == [EID_1, EID_2]


def test_event_id_timestamp_ms_parses_prefix():
    assert mtl.event_id_timestamp_ms(EID_1) == 1789385121339
    assert mtl.event_id_timestamp_ms("garbage") is None


def test_build_spans_query_scopes_to_memory_and_quotes_ids():
    q = mtl.build_spans_query(MEM, [EID_1], [SESSION])
    assert f'attributes.memory.id = "{MEM}"' in q
    assert f'attributes.event.id in ["{EID_1}"]' in q
    assert f'attributes.session.id in ["{SESSION}"]' in q
    assert "traceId" in q and "| limit 1000" in q
    # No ids -> memory-only filter, no dangling "and ()".
    q2 = mtl.build_spans_query(MEM, [], [])
    assert "and (" not in q2 and f'attributes.memory.id = "{MEM}"' in q2


def test_join_prefers_exact_event_match():
    other_trace = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    spans = [
        _span("ListEvents", ""),  # ListEvents has no event.id — must not pollute by_event_id
        _span("CreateEvent", EID_1),
        _span("CreateEvent", EID_2, trace_id=other_trace),
    ]
    out = mtl.join_records_to_spans([_extraction_record([EID_1])], spans)
    assert out["by_event_id"] == {EID_1: TRACE, EID_2: other_trace}
    assert out["by_session_id"] == {SESSION: [TRACE, other_trace]}
    rec = out["records"][0]
    assert rec["trace_id"] == TRACE and rec["resolution"] == "event" and rec["trace_ids"] == [TRACE]
    assert rec["event_ids"] == [EID_1] and rec["request_id"] == "f90f1d70-fc07-45d8-9486-e1c8e97fe9ac"


def test_join_falls_back_to_session_and_is_exact_when_single_trace():
    out = mtl.join_records_to_spans([_consolidation_record()], [_span("CreateEvent", EID_1)])
    rec = out["records"][0]
    assert rec["resolution"] == "session" and rec["trace_id"] == TRACE and rec["trace_ids"] == [TRACE]


def test_join_propagates_event_resolution_to_same_request_id_siblings():
    """Consolidation records have no event ids but share the extraction job's
    body.requestId (verified live) — they inherit the exact trace, even when the
    session has several turns (which would make the session fallback ambiguous)."""
    other_trace = "cccccccccccccccccccccccccccccccc"
    spans = [_span("CreateEvent", EID_1), _span("CreateEvent", EID_2, trace_id=other_trace)]
    extraction = _extraction_record([EID_1])  # requestId f90f1d70-...
    sibling = _consolidation_record()  # same requestId, no event ids
    stranger = _consolidation_record()
    stranger["body"]["requestId"] = "another-job"
    out = mtl.join_records_to_spans([sibling, extraction, stranger], spans)
    by_log = {r["request_id"]: r for r in out["records"] if r["request_id"] != "f90f1d70-fc07-45d8-9486-e1c8e97fe9ac"}
    same_job = [r for r in out["records"] if r["request_id"] == "f90f1d70-fc07-45d8-9486-e1c8e97fe9ac"]
    assert {r["resolution"] for r in same_job} == {"event", "request"}
    assert all(r["trace_id"] == TRACE for r in same_job)
    # A different job in a multi-turn session stays ambiguous (session fallback).
    assert by_log["another-job"]["resolution"] == "session" and by_log["another-job"]["trace_id"] is None


def test_join_session_fallback_is_ambiguous_across_turns():
    other_trace = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    spans = [_span("CreateEvent", EID_1), _span("CreateEvent", EID_2, trace_id=other_trace)]
    rec = mtl.join_records_to_spans([_consolidation_record()], spans)["records"][0]
    assert rec["resolution"] == "session"
    assert rec["trace_id"] is None  # two turns in the session -> "one of", not exact
    assert rec["trace_ids"] == [TRACE, other_trace]


def test_join_unresolved_when_no_spans():
    rec = mtl.join_records_to_spans([_extraction_record([EID_1])], [])["records"][0]
    assert rec["trace_id"] is None and rec["resolution"] is None and rec["trace_ids"] == []


# ---------------------------------------------------------------------------
# Logs Insights wiring (mocked client)
# ---------------------------------------------------------------------------


def _insights_client(rows: list[dict], status: str = "Complete") -> MagicMock:
    client = MagicMock()
    client.exceptions.ResourceNotFoundException = type("RNF", (Exception,), {})
    client.start_query.return_value = {"queryId": "q-1"}
    client.get_query_results.return_value = {
        "status": status,
        "results": [
            [{"field": k, "value": v} for k, v in row.items()] + [{"field": "@ptr", "value": "x"}] for row in rows
        ],
    }
    return client


def test_resolve_queries_aws_spans_with_event_and_session_filters_and_joins():
    client = _insights_client([_span("CreateEvent", EID_1)])
    out = mtl.resolve_memory_log_traces(
        MEM, [_extraction_record([EID_1])], "us-west-2", logs_client=client, poll_seconds=1
    )

    assert out["query_status"] == "Complete" and out["spans_seen"] == 1
    assert out["records"][0]["trace_id"] == TRACE and out["records"][0]["resolution"] == "event"
    _, kwargs = client.start_query.call_args
    assert kwargs["logGroupName"] == "aws/spans"
    assert EID_1 in kwargs["queryString"] and SESSION in kwargs["queryString"]
    # Window derived from the event-id timestamp (epoch ms -> s, minus 1h slack).
    assert kwargs["startTime"] == 1789385121339 // 1000 - 3600
    assert kwargs["endTime"] > kwargs["startTime"]


def test_resolve_with_no_records_does_not_query():
    client = _insights_client([])
    out = mtl.resolve_memory_log_traces(MEM, [], "us-west-2", logs_client=client)
    assert out["records"] == [] and out["query_status"] == "Empty"
    client.start_query.assert_not_called()


def test_resolve_handles_missing_spans_log_group():
    client = _insights_client([])
    client.start_query.side_effect = client.exceptions.ResourceNotFoundException()
    out = mtl.resolve_memory_log_traces(MEM, [_extraction_record([EID_1])], "us-west-2", logs_client=client)
    assert out["query_status"] == "Empty" and out["records"][0]["trace_id"] is None


def test_fetch_memory_log_records_parses_and_paginates():
    client = MagicMock()
    client.exceptions.ResourceNotFoundException = type("RNF", (Exception,), {})
    rec = _extraction_record([EID_1])
    client.filter_log_events.side_effect = [
        {"events": [{"message": json.dumps(rec)}, {"message": "junk"}], "nextToken": "t"},
        {"events": [{"message": json.dumps(_consolidation_record())}]},
    ]
    out = mtl.fetch_memory_log_records(MEM, "us-west-2", 1, 2, logs_client=client)
    assert len(out) == 2 and out[0] == rec
    assert client.filter_log_events.call_args_list[0].kwargs["logGroupName"] == mtl.memory_log_group(MEM)
