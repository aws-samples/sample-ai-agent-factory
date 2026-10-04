"""Regression: KB ingestion must report its terminal status honestly.

P-E2E matrix finding — the KB was reported ready the moment the ingestion job
STARTED. Under a combined deploy the corpus hadn't produced queryable vectors in
the old 300s window, so the KB tool returned nothing and the agent said
"technical error". The fix:
  * _start_and_wait_ingestion returns (job_id, terminal_status) — COMPLETE means
    queryable, IN_PROGRESS means still ingesting (not a silent success).
  * FAILED still raises.
  * The KB tool Lambda returns a retryable "still_ingesting" signal on zero
    citations instead of a hard error.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, call

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, "src")

from app.services.gateway_deployer import KNOWLEDGE_BASE_LAMBDA_TEMPLATE  # noqa: E402
from app.step_handlers.knowledge_base_step import (  # noqa: E402
    _list_all_data_sources,
    _list_all_ingestion_jobs,
    _list_all_vector_indexes,
    _start_and_wait_ingestion,
)


class _FakeBA:
    """Fake bedrock-agent whose ingestion job walks a scripted status sequence."""

    def __init__(self, statuses):
        self._statuses = list(statuses)
        self._i = 0

    def start_ingestion_job(self, **kw):
        return {"ingestionJob": {"ingestionJobId": "job-1"}}

    def get_ingestion_job(self, **kw):
        # Return the next status, holding on the last one.
        s = self._statuses[min(self._i, len(self._statuses) - 1)]
        self._i += 1
        return {"ingestionJob": {"status": s, "failureReasons": ["boom"]}}


def test_complete_returns_complete(monkeypatch):
    import app.step_handlers.knowledge_base_step as kb

    monkeypatch.setattr(kb.time, "sleep", lambda *_: None)
    job_id, status = _start_and_wait_ingestion(_FakeBA(["IN_PROGRESS", "COMPLETE"]), "kb", "ds", max_wait=60)
    assert job_id == "job-1"
    assert status == "COMPLETE"


def test_timeout_returns_in_progress_not_success(monkeypatch):
    import app.step_handlers.knowledge_base_step as kb

    monkeypatch.setattr(kb.time, "sleep", lambda *_: None)
    # Never completes within the (tiny) window -> IN_PROGRESS, not a silent COMPLETE.
    job_id, status = _start_and_wait_ingestion(_FakeBA(["IN_PROGRESS"]), "kb", "ds", max_wait=10)
    assert status == "IN_PROGRESS"


def test_failed_raises(monkeypatch):
    import app.step_handlers.knowledge_base_step as kb

    monkeypatch.setattr(kb.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError, match="Ingestion job failed"):
        _start_and_wait_ingestion(_FakeBA(["FAILED"]), "kb", "ds", max_wait=10)


def test_conflicting_ingestion_adopts_an_ongoing_job_on_page_two(monkeypatch):
    import app.step_handlers.knowledge_base_step as kb

    client = MagicMock()
    client.start_ingestion_job.side_effect = ClientError(
        {
            "Error": {
                "Code": "ConflictException",
                "Message": "an ingestion job is already running",
            }
        },
        "StartIngestionJob",
    )
    client.list_ingestion_jobs.side_effect = [
        {
            "ingestionJobSummaries": [
                {"ingestionJobId": "old", "status": "COMPLETE"},
            ],
            "nextToken": "page-2",
        },
        {
            "ingestionJobSummaries": [
                {"ingestionJobId": "ongoing", "status": "IN_PROGRESS"},
            ]
        },
    ]
    client.get_ingestion_job.return_value = {"ingestionJob": {"status": "COMPLETE"}}
    monkeypatch.setattr(kb.time, "sleep", lambda *_: None)

    job_id, status = _start_and_wait_ingestion(
        client,
        "kb",
        "ds",
        max_wait=10,
    )

    assert (job_id, status) == ("ongoing", "COMPLETE")
    assert client.list_ingestion_jobs.call_args_list == [
        call(knowledgeBaseId="kb", dataSourceId="ds", maxResults=50),
        call(
            knowledgeBaseId="kb",
            dataSourceId="ds",
            maxResults=50,
            nextToken="page-2",
        ),
    ]


def test_kb_resource_lookups_include_page_two():
    s3v = MagicMock()
    s3v.list_indexes.side_effect = [
        {
            "indexes": [{"indexName": "first"}],
            "nextToken": "page-2",
        },
        {"indexes": [{"indexName": "second"}]},
    ]
    assert [item["indexName"] for item in _list_all_vector_indexes(s3v, "bucket")] == [
        "first",
        "second",
    ]

    bedrock_agent = MagicMock()
    bedrock_agent.list_data_sources.side_effect = [
        {
            "dataSourceSummaries": [{"name": "first", "dataSourceId": "ds-1"}],
            "nextToken": "page-2",
        },
        {
            "dataSourceSummaries": [
                {"name": "second", "dataSourceId": "ds-2"},
            ]
        },
    ]
    assert [item["dataSourceId"] for item in _list_all_data_sources(bedrock_agent, "kb")] == ["ds-1", "ds-2"]

    bedrock_agent.list_ingestion_jobs.side_effect = [
        {
            "ingestionJobSummaries": [{"ingestionJobId": "job-1"}],
            "nextToken": "page-2",
        },
        {"ingestionJobSummaries": [{"ingestionJobId": "job-2"}]},
    ]
    assert [item["ingestionJobId"] for item in _list_all_ingestion_jobs(bedrock_agent, "kb", "ds")] == [
        "job-1",
        "job-2",
    ]


def test_kb_tool_lambda_emits_retryable_on_empty(monkeypatch):
    """The KB tool Lambda returns still_ingesting/retryable when no citations."""
    ns: dict = {}
    # Stub boto3 so the template's module-level client + retrieve_and_generate work.
    import types

    class _Resp(dict):
        pass

    class _Client:
        def retrieve_and_generate(self, **kw):
            return {"output": {"text": "I could not find that."}, "citations": []}

    fake_boto3 = types.SimpleNamespace(client=lambda *a, **k: _Client())
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("KNOWLEDGE_BASE_ID", "kb-1")
    monkeypatch.setenv("FOUNDATION_MODEL_ARN", "arn:model")
    exec(KNOWLEDGE_BASE_LAMBDA_TEMPLATE, ns)  # noqa: S102 — trusted template under test
    out = ns["lambda_handler"]({"query": "where is the canary"}, None)
    import json

    body = json.loads(out["body"])
    assert body.get("still_ingesting") is True
    assert body.get("retryable") is True
