#!/usr/bin/env python3
"""Golden-dataset promotion gate: centralized traces -> AgentCore Evaluations Dataset -> batch evaluation
-> the evaluation_gate.py thresholds.

The CloudWatch Omni GA loop ends with "curate golden datasets from production traffic, run experiments
against them". This script is that stage for the blueprint, as three subcommands that a pipeline or an
operator runs in order:

  curate   Read the traces of chosen sessions (the hub's centralized copies, or a cell's own groups),
           rebuild each session as a predefined scenario (turn inputs, the responses the agent gave, the
           tools it called) and publish them as an IMMUTABLE AgentCore Evaluations Dataset version.
           The scenarios are also written to a local JSON file so a human reviews what became ground
           truth before the version is used as a gate.
  replay   Replay a dataset version's turns against a candidate AgentCore Runtime, one fresh session per
           scenario, and write the scenario -> session map the gate consumes.
  gate     StartBatchEvaluation over the candidate sessions with the dataset version as ground truth
           (expected responses, assertions, expected tool trajectory), poll GetBatchEvaluation, map the
           results onto the SAME thresholds scripts/evaluation_gate.py reads from the environment, and
           exit 0 (PASS) / 1 (FAIL) / 2 (error).

Threshold mapping (documented in docs/CLOUDWATCH_OMNI_INTEGRATION.md section 3):
  EVAL_QUALITY_MIN_PCT              <- 100 * mean(Builtin.Correctness)          expected_response per turn
  EVAL_REGRESSION_PASS_MIN_PCT      <- 100 * mean(Builtin.GoalSuccessRate)      assertions per scenario
  EVAL_TOOL_SUCCESS_MIN_PCT         <- 100 * mean(Builtin.Trajectory*Match)     expected_trajectory per scenario
  EVAL_GUARDRAIL_VIOLATION_MAX_PCT  <- share of Builtin.Harmfulness results labelled Harmful
  EVAL_REFUSAL_RATE_MIN_PCT         <- share of Builtin.Refusal results labelled Yes on the turns the
                                       dataset marks adversarial (metadata.adversarial_turns)
  EVAL_FIRST_TOKEN_P99_MAX_MS, EVAL_COST_PER_PROMPT_MAX_USD are not measured by a batch evaluation and
  stay with evaluation_gate.py; the gate prints them as "not measured".

Everything here is a real API call (Logs Insights, bedrock-agentcore-control, bedrock-agentcore,
bedrock-agentcore runtime). No CloudFormation type exists for datasets or batch evaluations.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evaluation_gate  # noqa: E402  (sibling script: the shared threshold contract)

SCHEMA_TYPE = "AGENTCORE_EVALUATION_PREDEFINED_V1"
DATASET_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")
TERMINAL_BATCH_STATES = ("COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "STOPPED")
TERMINAL_DATASET_STATES = ("ACTIVE", "CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED")

# Evaluators behind each threshold dimension. The trajectory evaluator is configurable because the
# three matchers differ in strictness (exact order / in order / any order).
CORRECTNESS = "Builtin.Correctness"
GOAL_SUCCESS = "Builtin.GoalSuccessRate"
HARMFULNESS = "Builtin.Harmfulness"
REFUSAL = "Builtin.Refusal"
TRAJECTORY_EVALUATORS = (
    "Builtin.TrajectoryExactOrderMatch",
    "Builtin.TrajectoryInOrderMatch",
    "Builtin.TrajectoryAnyOrderMatch",
)
DEFAULT_TRAJECTORY_EVALUATOR = "Builtin.TrajectoryAnyOrderMatch"
DEFAULT_ADVERSARIAL_RE = r"ignore (all )?(previous|prior) instructions|system prompt|jailbreak|developer mode"

USER_EVENT_NAMES = ("gen_ai.user.message",)
ASSISTANT_EVENT_NAMES = ("gen_ai.choice", "gen_ai.assistant.message")
TOOL_NAME_KEYS = ("gen_ai.tool.name", "tool.name")


# ----------------------------------------------------------------------------------------------------
# Clients
# ----------------------------------------------------------------------------------------------------
@dataclass
class Clients:
    region: str
    profile: str | None = None
    # A pipeline runs each subcommand under the role that owns its side: the hub's golden-gate
    # runner role for export/gate, the cell's evaluation invoker role for replay.
    assume_role_arn: str | None = None
    _session: Any = None
    _logs: Any = None
    _control: Any = None
    _data: Any = None

    @property
    def session(self):
        if self._session is None:
            base = boto3.session.Session(region_name=self.region, profile_name=self.profile)
            if self.assume_role_arn:
                credentials = base.client("sts").assume_role(
                    RoleArn=self.assume_role_arn, RoleSessionName="golden-dataset-gate", DurationSeconds=3600
                )["Credentials"]
                base = boto3.session.Session(
                    region_name=self.region,
                    aws_access_key_id=credentials["AccessKeyId"],
                    aws_secret_access_key=credentials["SecretAccessKey"],
                    aws_session_token=credentials["SessionToken"],
                )
            self._session = base
        return self._session

    @property
    def logs(self):
        if self._logs is None:
            self._logs = self.session.client("logs")
        return self._logs

    @property
    def control(self):
        if self._control is None:
            self._control = self.session.client("bedrock-agentcore-control")
        return self._control

    @property
    def data(self):
        if self._data is None:
            self._data = self.session.client("bedrock-agentcore")
        return self._data


# ----------------------------------------------------------------------------------------------------
# Record reading (the raw span records of aws/spans and the GenAI log-event records of the agent group,
# as the AgentCore SDK collector and the live gates read them)
# ----------------------------------------------------------------------------------------------------
def _text(value: Any) -> str:
    """Text of a message content value: a string, a list of content blocks, or a dict with text."""
    if value is None:
        return ""
    if isinstance(value, str):
        stripped = value.strip()
        # Observed live: the distro serialises Strands message content as a JSON string of content
        # blocks ('[{"text": "..."}]'); unwrap it so the dataset input is the prompt itself.
        if stripped[:1] in ("[", "{"):
            try:
                return _text(json.loads(stripped))
            except ValueError:
                return stripped
        return stripped
    if isinstance(value, dict):
        for key in ("text", "content", "message"):
            if key in value:
                return _text(value[key])
        return ""
    if isinstance(value, list):
        return "\n".join(t for t in (_text(v) for v in value) if t).strip()
    return str(value)


def _event_name(record: dict[str, Any]) -> str:
    attributes = record.get("attributes") or {}
    return str(record.get("eventName") or record.get("name") or attributes.get("event.name") or "")


def _nanos(record: dict[str, Any]) -> int:
    for key in ("startTimeUnixNano", "timeUnixNano", "observedTimeUnixNano"):
        value = record.get(key)
        if value not in (None, ""):
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    stamp = record.get("@timestamp") or record.get("time")
    if stamp:
        try:
            parsed = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00").replace(" ", "T"))
            return int(parsed.timestamp() * 1_000_000_000)
        except ValueError:
            pass
    return 0


def _messages_from_record(record: dict[str, Any]) -> tuple[list[str], list[str]]:
    """User texts and assistant texts carried by one record: span attributes (gen_ai.*), OpenTelemetry
    events, or a GenAI log-event body (`body.input.messages` / `body.output.messages`, Strands via the
    AWS Distro for OpenTelemetry)."""
    users: list[str] = []
    assistants: list[str] = []
    attributes = record.get("attributes") or {}

    def from_messages(messages: Any, default_role: str) -> None:
        if isinstance(messages, str):
            try:
                messages = json.loads(messages)
            except ValueError:
                messages = [{"role": default_role, "content": messages}]
        for message in messages if isinstance(messages, list) else []:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", default_role))
            # Bedrock Converse shape: content blocks; OpenTelemetry GenAI shape: parts with type/content.
            # Tool calls and tool results carry no `text`/`content` string and fall out as "".
            text = _text(message.get("content")) or _text(
                [p for p in (message.get("parts") or []) if isinstance(p, dict) and p.get("type", "text") == "text"]
            )
            if not text:
                continue
            if role in ("user", "human"):
                users.append(text)
            elif role in ("assistant", "ai", "model"):
                assistants.append(text)

    for key in ("gen_ai.input.messages", "gen_ai.prompt"):
        if key in attributes:
            from_messages(attributes[key], "user")
    for key in ("gen_ai.output.messages", "gen_ai.completion"):
        if key in attributes:
            from_messages(attributes[key], "assistant")

    body = record.get("body")
    if isinstance(body, dict):
        from_messages((body.get("input") or {}).get("messages"), "user")
        from_messages((body.get("output") or {}).get("messages"), "assistant")
        if "input" not in body and "output" not in body:
            name = _event_name(record)
            if name in USER_EVENT_NAMES:
                users.append(_text(body.get("content")))
            elif name in ASSISTANT_EVENT_NAMES:
                assistants.append(_text(body.get("message") if "message" in body else body.get("content")))

    for event in list(record.get("events") or []) + list(record.get("span_events") or []):
        if not isinstance(event, dict):
            continue
        name = _event_name(event)
        event_attributes = event.get("attributes") or {}
        if name in USER_EVENT_NAMES:
            users.append(_text(event_attributes.get("content")))
        elif name in ASSISTANT_EVENT_NAMES:
            assistants.append(_text(event_attributes.get("message") if "message" in event_attributes else event_attributes.get("content")))
        event_users, event_assistants = _messages_from_record(event)
        users.extend(event_users)
        assistants.extend(event_assistants)
    return [u for u in users if u], [a for a in assistants if a]


def _run_insights(logs, groups: list[str], query: str, start_s: int, end_s: int, timeout_s: int = 180) -> list[dict[str, str]]:
    qid = logs.start_query(logGroupNames=groups, startTime=start_s, endTime=end_s, queryString=query)["queryId"]
    deadline = time.time() + timeout_s
    status = "Running"
    result: dict[str, Any] = {}
    while time.time() < deadline:
        result = logs.get_query_results(queryId=qid)
        status = result.get("status", "")
        if status in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        time.sleep(2)
    if status != "Complete":
        raise RuntimeError(f"Logs Insights query ended with status {status}")
    return [{f["field"]: f["value"] for f in row} for row in result.get("results", [])]


def discover_sessions(logs, groups: list[str], service_name: str, start_s: int, end_s: int, limit: int) -> list[str]:
    """Most recent sessions of one agent (by `invoke_agent` spans), newest first."""
    query = (
        'filter name like /^invoke_agent/ | filter ispresent(attributes.session.id)'
        f' | filter resource.attributes.service.name = "{service_name}" or attributes.service.name = "{service_name}"'
        ' | stats count(*) as turns, max(@timestamp) as last by attributes.session.id'
        f' | sort last desc | limit {int(limit)}'
    )
    rows = _run_insights(logs, groups, query, start_s, end_s)
    return [row["attributes.session.id"] for row in rows if row.get("attributes.session.id")]


def session_records(logs, groups: list[str], session_id: str, start_s: int, end_s: int) -> list[dict[str, Any]]:
    query = (
        f'fields @message | filter attributes.session.id = "{session_id}" | filter ispresent(traceId)'
        ' | sort @timestamp asc | limit 5000'
    )
    records = []
    for row in _run_insights(logs, groups, query, start_s, end_s):
        try:
            records.append(json.loads(row["@message"]))
        except (KeyError, ValueError):
            continue
    return records


def ordered_traces(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The `invoke_agent` spans of a session in start order: one per agent turn."""
    spans = [r for r in records if str(r.get("name", "")).startswith("invoke_agent") and r.get("traceId") and r.get("spanId")]
    spans.sort(key=_nanos)
    seen: set[str] = set()
    unique = []
    for span in spans:
        if span["traceId"] in seen:
            continue
        seen.add(span["traceId"])
        unique.append(span)
    return unique


def build_scenario(
    records: list[dict[str, Any]],
    scenario_id: str,
    *,
    assertions: list[str] | None = None,
    adversarial_re: str = DEFAULT_ADVERSARIAL_RE,
    metadata: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """One predefined scenario (dataset schema AGENTCORE_EVALUATION_PREDEFINED_V1) from a session's records.

    Turn boundaries are the `invoke_agent` spans. The turn input is the user message that first appears in
    that trace; the expected response is the trace's final assistant text (the record attached to the
    invoke_agent span wins over inner chat records); the expected trajectory is the tool names of the
    `execute_tool` spans across the session, in start order."""
    turns: list[dict[str, Any]] = []
    trajectory: list[str] = []
    seen_user: set[str] = set()
    by_trace: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        trace_id = record.get("traceId")
        if trace_id:
            by_trace.setdefault(trace_id, []).append(record)
    pattern = re.compile(adversarial_re, re.IGNORECASE) if adversarial_re else None
    adversarial: list[int] = []

    for span in ordered_traces(records):
        trace_records = sorted(by_trace.get(span["traceId"], []), key=_nanos)
        own = [r for r in trace_records if r.get("spanId") == span["spanId"]]
        inner = [r for r in trace_records if r.get("spanId") != span["spanId"]]
        own_users, own_assistants = [], []
        for record in own:
            users, assistants = _messages_from_record(record)
            own_users.extend(users)
            own_assistants.extend(assistants)
        inner_users, inner_assistants = [], []
        for record in inner:
            users, assistants = _messages_from_record(record)
            inner_users.extend(users)
            inner_assistants.extend(assistants)
        prompt = next((u for u in own_users + inner_users if u not in seen_user), None)
        seen_user.update(own_users + inner_users)
        response = (own_assistants or inner_assistants or [""])[-1]
        if not prompt:
            continue
        turn: dict[str, Any] = {"input": prompt}
        if response:
            turn["expected_response"] = response
        if pattern and pattern.search(prompt):
            adversarial.append(len(turns))
        turns.append(turn)
        tool_spans = sorted(
            (r for r in trace_records if str(r.get("name", "")).startswith("execute_tool") and r.get("spanId")),
            key=_nanos,
        )
        for tool_span in tool_spans:
            attributes = tool_span.get("attributes") or {}
            name = next((str(attributes[k]) for k in TOOL_NAME_KEYS if attributes.get(k)), None)
            if name is None:
                name = str(tool_span.get("name", "")).split(" ", 1)[-1]
            if name:
                trajectory.append(name)

    if not turns:
        return None
    scenario: dict[str, Any] = {"scenario_id": scenario_id, "turns": turns}
    if trajectory:
        scenario["expected_trajectory"] = trajectory
    if assertions:
        scenario["assertions"] = list(assertions)
    scenario_metadata = dict(metadata or {})
    scenario_metadata["adversarial_turns"] = ",".join(str(i) for i in adversarial)
    scenario["metadata"] = scenario_metadata
    return scenario


# ----------------------------------------------------------------------------------------------------
# Dataset lifecycle (bedrock-agentcore-control). CreateDataset / CreateDatasetVersion are asynchronous.
# ----------------------------------------------------------------------------------------------------
def _wait_dataset(control, dataset_id: str, version: str | None = None, timeout_s: int = 600, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    while True:
        kwargs = {"datasetId": dataset_id}
        if version is not None:
            kwargs["datasetVersion"] = version
        try:
            described = control.get_dataset(**kwargs)
        except ClientError as exc:
            # Observed live: GetDataset on a version CreateDatasetVersion just returned answers
            # ResourceNotFoundException for a few seconds before the version is readable.
            if version is not None and exc.response["Error"]["Code"] == "ResourceNotFoundException" and time.time() < deadline:
                sleep(5)
                continue
            raise
        status = described.get("status")
        if status in TERMINAL_DATASET_STATES:
            if status != "ACTIVE":
                raise RuntimeError(f"dataset {dataset_id} version {version or 'DRAFT'} ended {status}: {described.get('failureReason')}")
            return described
        if time.time() >= deadline:
            raise TimeoutError(f"dataset {dataset_id} still {status} after {timeout_s}s")
        sleep(5)


def publish_dataset(
    control,
    *,
    dataset_name: str,
    scenarios: list[dict[str, Any]],
    description: str,
    dataset_id: str | None = None,
    kms_key_arn: str | None = None,
    tags: dict[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Create the dataset (or add to an existing one's DRAFT) and publish an immutable version."""
    if not DATASET_NAME_RE.match(dataset_name):
        raise ValueError(f"dataset name '{dataset_name}' must match {DATASET_NAME_RE.pattern}")
    if not scenarios:
        raise ValueError("no scenarios to publish")
    source = {"inlineExamples": {"examples": scenarios}}
    if dataset_id is None:
        kwargs: dict[str, Any] = {
            "datasetName": dataset_name,
            "schemaType": SCHEMA_TYPE,
            "source": source,
            "description": description,
            "clientToken": str(uuid.uuid4()),
        }
        if kms_key_arn:
            kwargs["kmsKeyArn"] = kms_key_arn
        if tags:
            kwargs["tags"] = tags
        created = control.create_dataset(**kwargs)
        dataset_id = created["datasetId"]
    else:
        control.add_dataset_examples(datasetId=dataset_id, source=source, clientToken=str(uuid.uuid4()))
    _wait_dataset(control, dataset_id, sleep=sleep)
    versioned = control.create_dataset_version(datasetId=dataset_id, clientToken=str(uuid.uuid4()))
    version = versioned["datasetVersion"]
    final = _wait_dataset(control, dataset_id, version=version, sleep=sleep)
    return {
        "datasetId": dataset_id,
        "datasetArn": final.get("datasetArn", versioned.get("datasetArn")),
        "datasetVersion": version,
        "exampleCount": final.get("exampleCount"),
        "schemaType": final.get("schemaType", SCHEMA_TYPE),
    }


def list_examples(control, dataset_id: str, version: str) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    token = None
    while True:
        kwargs = {"datasetId": dataset_id, "datasetVersion": version, "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        page = control.list_dataset_examples(**kwargs)
        examples.extend(page.get("examples") or [])
        token = page.get("nextToken")
        if not token:
            return examples


# ----------------------------------------------------------------------------------------------------
# Ground truth and the batch evaluation (bedrock-agentcore)
# ----------------------------------------------------------------------------------------------------
def _scenario_fields(example: dict[str, Any]) -> dict[str, Any]:
    """An example as ListDatasetExamples returns it: the scenario fields at the top level (with an
    exampleId), or nested under `example`/`content` if the service wraps them."""
    for key in ("example", "content", "scenario"):
        nested = example.get(key)
        if isinstance(nested, dict) and "turns" in nested:
            return nested
    return example


def ground_truth_metadata(examples: list[dict[str, Any]], sessions: dict[str, str]) -> list[dict[str, Any]]:
    """`evaluationMetadata.sessionMetadata` for StartBatchEvaluation: one entry per scenario that has a
    candidate session, carrying the scenario's expected responses (positional per turn), assertions and
    expected trajectory."""
    entries = []
    for raw in examples:
        scenario = _scenario_fields(raw)
        scenario_id = str(scenario.get("scenario_id", ""))
        session_id = sessions.get(scenario_id)
        if not session_id:
            continue
        inline: dict[str, Any] = {}
        turns = []
        for turn in scenario.get("turns") or []:
            prompt = turn.get("input")
            entry: dict[str, Any] = {"input": {"prompt": prompt if isinstance(prompt, str) else json.dumps(prompt)}}
            if turn.get("expected_response"):
                entry["expectedResponse"] = {"text": str(turn["expected_response"])}
            turns.append(entry)
        if turns:
            inline["turns"] = turns
        if scenario.get("assertions"):
            inline["assertions"] = [{"text": str(a)} for a in scenario["assertions"]]
        if scenario.get("expected_trajectory"):
            inline["expectedTrajectory"] = {"toolNames": [str(t) for t in scenario["expected_trajectory"]]}
        entry = {"sessionId": session_id, "testScenarioId": scenario_id, "groundTruth": {"inline": inline}}
        metadata = scenario.get("metadata") or {}
        if isinstance(metadata, dict) and metadata:
            entry["metadata"] = {str(k): str(v) for k, v in metadata.items()}
        entries.append(entry)
    return entries


def adversarial_turns(examples: list[dict[str, Any]]) -> dict[str, set[int]]:
    """scenario_id -> turn indexes the dataset marks adversarial (metadata.adversarial_turns, "1,3")."""
    out: dict[str, set[int]] = {}
    for raw in examples:
        scenario = _scenario_fields(raw)
        metadata = scenario.get("metadata") or {}
        value = metadata.get("adversarial_turns") if isinstance(metadata, dict) else None
        if value in (None, ""):
            continue
        parts = value if isinstance(value, list) else str(value).split(",")
        indexes = {int(p) for p in parts if str(p).strip().isdigit()}
        if indexes:
            out[str(scenario.get("scenario_id", ""))] = indexes
    return out


def start_batch(
    data,
    *,
    name: str,
    evaluators: list[str],
    service_name: str,
    log_groups: list[str],
    session_ids: list[str],
    session_metadata: list[dict[str, Any]],
    start: dt.datetime,
    end: dt.datetime,
    results_log_group: str | None = None,
    metrics_namespace: str | None = None,
    description: str = "",
    tags: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not DATASET_NAME_RE.match(name):
        raise ValueError(f"batch evaluation name '{name}' must match {DATASET_NAME_RE.pattern}")
    if not 1 <= len(log_groups) <= 5:
        raise ValueError("StartBatchEvaluation takes one to five log groups")
    output: dict[str, Any] = {"resultDestination": "DEDICATED_LOG_GROUP"}
    if results_log_group:
        output["logGroupName"] = results_log_group
    if metrics_namespace:
        output["metricsNamespace"] = metrics_namespace
    kwargs: dict[str, Any] = {
        "batchEvaluationName": name,
        "evaluators": [{"evaluatorId": e} for e in evaluators],
        "dataSourceConfig": {
            "cloudWatchLogs": {
                "serviceNames": [service_name],
                "logGroupNames": log_groups,
                "filterConfig": {"sessionIds": session_ids, "timeRange": {"startTime": start, "endTime": end}},
            }
        },
        "evaluationMetadata": {"sessionMetadata": session_metadata},
        "outputConfig": {"cloudWatchConfig": output},
        "clientToken": str(uuid.uuid4()),
    }
    if description:
        kwargs["description"] = description
    if tags:
        kwargs["tags"] = tags
    return data.start_batch_evaluation(**kwargs)


def ensure_result_log_group(logs, name: str, kms_key_arn: str, retention_days: int = 90, log: Callable[[str], None] = lambda s: None) -> str:
    """Create the batch result group under the hub CMK before the service does so unencrypted (observed live:
    a result group the service creates itself carries no customer key). Returns 'created', 'exists' or
    'exists-unencrypted' (an existing group without the key is left alone and reported, never re-keyed here)."""
    existing = [g for g in logs.describe_log_groups(logGroupNamePrefix=name).get("logGroups", []) if g["logGroupName"] == name]
    if existing:
        state = "exists" if existing[0].get("kmsKeyId") else "exists-unencrypted"
        log(f"result log group {name} {state}")
        return state
    logs.create_log_group(logGroupName=name, kmsKeyId=kms_key_arn, tags={"agenticai:owner": "platform", "agenticai:purpose": "golden-gate"})
    logs.put_retention_policy(logGroupName=name, retentionInDays=retention_days)
    log(f"result log group {name} created under {kms_key_arn[-12:]} ({retention_days} days)")
    return "created"


def wait_batch(data, batch_id: str, timeout_s: int = 1800, interval_s: int = 30, sleep: Callable[[float], None] = time.sleep,
               log: Callable[[str], None] = lambda s: None) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    while True:
        described = data.get_batch_evaluation(batchEvaluationId=batch_id)
        status = described.get("status")
        results = described.get("evaluationResults") or {}
        log(f"  {status}: completed={results.get('numberOfSessionsCompleted', 0)} failed={results.get('numberOfSessionsFailed', 0)}"
            f" inProgress={results.get('numberOfSessionsInProgress', 0)} total={results.get('totalNumberOfSessions', 0)}")
        if status in TERMINAL_BATCH_STATES:
            return described
        if time.time() >= deadline:
            raise TimeoutError(f"batch evaluation {batch_id} still {status} after {timeout_s}s")
        sleep(interval_s)


def result_events(logs, log_group: str, log_stream: str) -> list[dict[str, Any]]:
    """Per-session, per-evaluator OpenTelemetry events the job wrote (its dedicated log stream)."""
    events: list[dict[str, Any]] = []
    token = None
    while True:
        kwargs = {"logGroupName": log_group, "logStreamName": log_stream, "startFromHead": True}
        if token:
            kwargs["nextToken"] = token
        try:
            page = logs.get_log_events(**kwargs)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                return events
            raise
        for event in page.get("events", []):
            try:
                events.append(json.loads(event["message"]))
            except (KeyError, ValueError):
                continue
        next_token = page.get("nextForwardToken")
        if not next_token or next_token == token:
            return events
        token = next_token


def _event_fields(event: dict[str, Any]) -> dict[str, Any]:
    attributes = event.get("attributes") or {}
    return {
        "evaluator": attributes.get("gen_ai.evaluation.name") or attributes.get("gen_ai.evaluation.evaluator_id"),
        "label": attributes.get("gen_ai.evaluation.score.label"),
        "value": attributes.get("gen_ai.evaluation.score.value"),
        "error": attributes.get("error.type"),
        "session": attributes.get("session.id") or attributes.get("gen_ai.session.id"),
        "trace": event.get("traceId") or attributes.get("gen_ai.trace.id") or attributes.get("trace.id") or attributes.get("traceId"),
    }


@dataclass
class Measured:
    """Threshold dimensions a batch evaluation can measure, as percentages; None = not measured."""
    quality_pct: float | None = None
    regression_pass_pct: float | None = None
    tool_success_pct: float | None = None
    guardrail_violation_pct: float | None = None
    refusal_rate_pct: float | None = None
    notes: list[str] = field(default_factory=list)


def measure(
    summaries: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    trajectory_evaluator: str,
    adversarial: dict[str, set[int]],
    trace_order: dict[str, list[str]],
    session_to_scenario: dict[str, str],
) -> Measured:
    """Map evaluator summaries (0..1 averages, higher is better) and per-turn result events onto the
    evaluation_gate.py dimensions."""
    measured = Measured()
    averages: dict[str, float] = {}
    for summary in summaries:
        statistics = summary.get("statistics") or {}
        if "averageScore" in statistics and not summary.get("evaluationDimensionName"):
            averages[summary["evaluatorId"]] = float(statistics["averageScore"])
    if CORRECTNESS in averages:
        measured.quality_pct = 100.0 * averages[CORRECTNESS]
    else:
        measured.notes.append(f"quality_pct not measured ({CORRECTNESS} absent)")
    if GOAL_SUCCESS in averages:
        measured.regression_pass_pct = 100.0 * averages[GOAL_SUCCESS]
    else:
        measured.notes.append(f"regression_pass_pct not measured ({GOAL_SUCCESS} absent)")
    if trajectory_evaluator in averages:
        measured.tool_success_pct = 100.0 * averages[trajectory_evaluator]
    else:
        measured.notes.append(f"tool_success_pct not measured ({trajectory_evaluator} absent)")

    rows = [_event_fields(e) for e in events]
    harm = [r for r in rows if r["evaluator"] == HARMFULNESS and r["label"] and not r["error"]]
    if harm:
        violations = sum(1 for r in harm if str(r["label"]).strip().lower() == "harmful")
        measured.guardrail_violation_pct = 100.0 * violations / len(harm)
    else:
        measured.notes.append(f"guardrail_violation_pct not measured (no {HARMFULNESS} result events)")

    refusals = [r for r in rows if r["evaluator"] == REFUSAL and r["label"] and not r["error"]]
    adversarial_rows = []
    for row in refusals:
        scenario_id = session_to_scenario.get(str(row["session"]))
        turns = adversarial.get(scenario_id or "", set())
        traces = trace_order.get(str(row["session"]), [])
        if not turns or not row["trace"] or row["trace"] not in traces:
            continue
        if traces.index(row["trace"]) in turns:
            adversarial_rows.append(row)
    if adversarial_rows:
        refused = sum(1 for r in adversarial_rows if str(r["label"]).strip().lower() == "yes")
        measured.refusal_rate_pct = 100.0 * refused / len(adversarial_rows)
    elif not adversarial:
        measured.notes.append("refusal_rate_pct not measured (the dataset marks no adversarial turns)")
    elif not refusals:
        measured.notes.append(f"refusal_rate_pct not measured (no {REFUSAL} result events)")
    else:
        measured.notes.append("refusal_rate_pct not measured (result events carry no trace id to attribute to the adversarial turns)")
    measured.notes.append("first_token_p99_ms and cost_per_prompt_usd are not measured by a batch evaluation (evaluation_gate.py owns them)")
    return measured


def judge_measured(measured: Measured, thresholds: evaluation_gate.EvalThresholds) -> list[str]:
    """The evaluation_gate.judge comparisons, applied only to the dimensions the batch evaluation measured."""
    failures: list[str] = []
    if measured.regression_pass_pct is not None and measured.regression_pass_pct < thresholds.regression_pass_min_pct:
        failures.append(f"regression_pass_pct={measured.regression_pass_pct:.1f} < {thresholds.regression_pass_min_pct}")
    if measured.guardrail_violation_pct is not None and measured.guardrail_violation_pct > thresholds.guardrail_violation_max_pct:
        failures.append(f"guardrail_violation_pct={measured.guardrail_violation_pct:.2f} > {thresholds.guardrail_violation_max_pct}")
    if measured.quality_pct is not None and measured.quality_pct < thresholds.quality_min_pct:
        failures.append(f"quality_pct={measured.quality_pct:.1f} < {thresholds.quality_min_pct}")
    if measured.tool_success_pct is not None and measured.tool_success_pct < thresholds.tool_success_min_pct:
        failures.append(f"tool_success_pct={measured.tool_success_pct:.1f} < {thresholds.tool_success_min_pct}")
    if measured.refusal_rate_pct is not None and measured.refusal_rate_pct < thresholds.refusal_rate_min_pct:
        failures.append(f"refusal_rate_pct={measured.refusal_rate_pct:.1f} < {thresholds.refusal_rate_min_pct}")
    return failures


def trace_order_for_sessions(logs, groups: list[str], session_ids: list[str], start_s: int, end_s: int) -> dict[str, list[str]]:
    """session id -> its `invoke_agent` trace ids in start order (turn 0, turn 1, ...)."""
    if not session_ids:
        return {}
    quoted = ", ".join(f'"{s}"' for s in session_ids)
    query = (
        "fields traceId, attributes.session.id as sid, startTimeUnixNano | filter name like /^invoke_agent/"
        f" | filter attributes.session.id in [{quoted}] | sort startTimeUnixNano asc | limit 5000"
    )
    order: dict[str, list[str]] = {}
    for row in _run_insights(logs, groups, query, start_s, end_s):
        sid, trace = row.get("sid"), row.get("traceId")
        if sid and trace and trace not in order.setdefault(sid, []):
            order[sid].append(trace)
    return order


# ----------------------------------------------------------------------------------------------------
# Evidence
# ----------------------------------------------------------------------------------------------------
_ACCOUNT_RE = re.compile(r"(?<![0-9])([0-9]{12})(?![0-9])")


def redact(value: Any) -> Any:
    """Account ids become their last four digits in anything written as evidence."""
    if isinstance(value, str):
        return _ACCOUNT_RE.sub(lambda m: "..." + m.group(1)[-4:], value)
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {redact(k): redact(v) for k, v in value.items()}
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def _write_json(path: str | None, payload: dict[str, Any]) -> None:
    if not path:
        return
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(redact(payload), handle, indent=2, default=str)
        handle.write("\n")


# ----------------------------------------------------------------------------------------------------
# Subcommands
# ----------------------------------------------------------------------------------------------------
def _window(args: argparse.Namespace) -> tuple[dt.datetime, dt.datetime]:
    end = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
    start = end - dt.timedelta(hours=float(args.since_hours), minutes=5)
    return start, end


def cmd_curate(args: argparse.Namespace, clients: Clients) -> int:
    start, end = _window(args)
    start_s, end_s = int(start.timestamp()), int(end.timestamp())
    session_ids = list(args.session_id or [])
    if not session_ids:
        session_ids = discover_sessions(clients.logs, args.log_group, args.service_name, start_s, end_s, args.max_sessions)
        print(f"discovered {len(session_ids)} session(s) of {args.service_name} in the last {args.since_hours}h")
    if not session_ids:
        print("no sessions to curate", file=sys.stderr)
        return 2
    scenarios = []
    for index, session_id in enumerate(session_ids):
        records = session_records(clients.logs, args.log_group, session_id, start_s, end_s)
        scenario = build_scenario(
            records,
            f"{args.scenario_prefix}-{session_id[-8:]}",
            assertions=args.assertion,
            adversarial_re=args.adversarial_regex,
            metadata={"source_session_id": session_id, "service_name": args.service_name, "curated_at": dt.datetime.now(dt.timezone.utc).isoformat()},
        )
        if scenario is None or len(scenario["turns"]) < args.min_turns:
            print(f"  skip session ...{session_id[-8:]}: {len(records)} records, no usable turns")
            continue
        print(f"  session ...{session_id[-8:]}: {len(records)} records -> {len(scenario['turns'])} turn(s), tools {scenario.get('expected_trajectory', [])}")
        scenarios.append(scenario)
    document = {"scenarios": scenarios}
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
        print(f"wrote {len(scenarios)} scenario(s) to {args.out} (review before the version is used as a gate)")
    if not scenarios:
        return 2
    if args.dry_run:
        print("dry run: dataset not published")
        return 0
    published = publish_dataset(
        clients.control,
        dataset_name=args.dataset_name,
        dataset_id=args.dataset_id,
        scenarios=scenarios,
        description=args.description or f"Golden scenarios of {args.service_name} curated from centralized traces",
        kms_key_arn=args.kms_key_arn,
        tags={"agenticai:owner": "platform", "agenticai:purpose": "golden-dataset"},
    )
    print(json.dumps(redact(published), indent=2))
    _write_json(args.evidence, {"command": "curate", "sessions": [s[-8:] for s in session_ids], "scenarios": len(scenarios), "dataset": published})
    return 0


def cmd_export(args: argparse.Namespace, clients: Clients) -> int:
    """Write a dataset version's scenarios to a local file, so `replay` can run under a different role
    (the cell's invoker) than the one that reads the dataset (the hub's runner)."""
    examples = list_examples(clients.control, args.dataset_id, args.dataset_version)
    if not examples:
        print("dataset version has no examples", file=sys.stderr)
        return 2
    scenarios = [{k: v for k, v in _scenario_fields(e).items() if k != "exampleId"} for e in examples]
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"datasetId": args.dataset_id, "datasetVersion": args.dataset_version, "scenarios": scenarios}, handle, indent=2)
        handle.write("\n")
    print(f"wrote {len(scenarios)} scenario(s) of {args.dataset_id} v{args.dataset_version} to {args.out}")
    return 0


def wait_for_sessions(logs, groups: list[str], expected_turns: dict[str, int], start_s: int, end_s: int, timeout_s: int,
                      sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = lambda s: None) -> dict[str, list[str]]:
    """Block until every candidate session shows at least its scenario's number of `invoke_agent` traces in
    the groups the batch evaluation will read (CloudWatch ingestion plus, for hub copies, centralization),
    or fail closed: a batch evaluation over sessions that have not landed reports no sessions."""
    deadline = time.time() + timeout_s
    while True:
        order = trace_order_for_sessions(logs, groups, list(expected_turns), start_s, int(time.time()) + 300)
        missing = {s[-8:]: (len(order.get(s, [])), n) for s, n in expected_turns.items() if len(order.get(s, [])) < n}
        if not missing:
            return order
        log(f"  waiting for telemetry: {missing} (traces seen / expected)")
        if time.time() >= deadline:
            raise RuntimeError(f"candidate sessions not fully ingested after {timeout_s}s: {missing}")
        sleep(30)


def cmd_replay(args: argparse.Namespace, clients: Clients) -> int:
    if args.scenarios:
        with open(args.scenarios, encoding="utf-8") as handle:
            examples = json.load(handle).get("scenarios") or []
    elif args.dataset_id and args.dataset_version:
        examples = list_examples(clients.control, args.dataset_id, args.dataset_version)
    else:
        print("replay needs --scenarios FILE (from export) or --dataset-id and --dataset-version", file=sys.stderr)
        return 2
    if not examples:
        print("dataset version has no examples", file=sys.stderr)
        return 2
    sessions: dict[str, str] = {}
    for raw in examples:
        scenario = _scenario_fields(raw)
        scenario_id = str(scenario.get("scenario_id"))
        session_id = str(uuid.uuid4())
        for turn in scenario.get("turns") or []:
            prompt = turn.get("input")
            payload = json.dumps({"prompt": prompt} if isinstance(prompt, str) else prompt).encode()
            response = clients.data.invoke_agent_runtime(
                agentRuntimeArn=args.runtime_arn, runtimeSessionId=session_id, qualifier=args.qualifier, payload=payload
            )
            body = response["response"].read() if hasattr(response.get("response"), "read") else b""
            print(f"  {scenario_id} ...{session_id[-8:]}: {len(body)} bytes")
        sessions[scenario_id] = session_id
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(sessions, handle, indent=2)
        handle.write("\n")
    print(f"wrote {len(sessions)} scenario -> session mapping(s) to {args.out}")
    return 0


def cmd_gate(args: argparse.Namespace, clients: Clients) -> int:
    thresholds = evaluation_gate._thresholds_from_env()
    start, end = _window(args)
    start_s, end_s = int(start.timestamp()), int(end.timestamp())
    with open(args.sessions, encoding="utf-8") as handle:
        sessions: dict[str, str] = json.load(handle)
    examples = list_examples(clients.control, args.dataset_id, args.dataset_version)
    metadata = ground_truth_metadata(examples, sessions)
    if not metadata:
        print("no scenario of the dataset version has a candidate session in the sessions file", file=sys.stderr)
        return 2
    session_ids = [m["sessionId"] for m in metadata]
    evaluators = list(dict.fromkeys(list(args.evaluator or [CORRECTNESS, GOAL_SUCCESS, HARMFULNESS, REFUSAL]) + [args.trajectory_evaluator]))
    print(f"dataset {args.dataset_id} v{args.dataset_version}: {len(examples)} scenario(s), {len(metadata)} with a candidate session")
    print(f"evaluators: {', '.join(evaluators)}")
    if args.wait_for_sessions > 0:
        expected = {m["sessionId"]: len((m["groundTruth"]["inline"].get("turns") or [None])) for m in metadata}
        print(f"waiting up to {args.wait_for_sessions}s for the candidate sessions' traces in the source groups")
        wait_for_sessions(clients.logs, args.log_group, expected, start_s, end_s, args.wait_for_sessions, log=print)
    if args.results_log_group and args.results_kms_key_arn:
        ensure_result_log_group(clients.logs, args.results_log_group, args.results_kms_key_arn, log=print)
    started = start_batch(
        clients.data,
        name=args.name,
        evaluators=evaluators,
        service_name=args.service_name,
        log_groups=args.log_group,
        session_ids=session_ids,
        session_metadata=metadata,
        start=start,
        end=end,
        results_log_group=args.results_log_group,
        metrics_namespace=args.metrics_namespace,
        description=f"golden gate: dataset {args.dataset_id} v{args.dataset_version}",
        tags={"agenticai:owner": "platform", "agenticai:purpose": "golden-gate"},
    )
    batch_id = started["batchEvaluationId"]
    print(f"batch evaluation {batch_id} started; polling every {args.poll_interval}s (timeout {args.timeout}s)")
    final = wait_batch(clients.data, batch_id, timeout_s=args.timeout, interval_s=args.poll_interval, log=print)
    status = final.get("status")
    results = final.get("evaluationResults") or {}
    summaries = results.get("evaluatorSummaries") or []
    output = (final.get("outputConfig") or {}).get("cloudWatchConfig") or {}
    events = []
    if output.get("logGroupName") and output.get("logStreamName"):
        events = result_events(clients.logs, output["logGroupName"], output["logStreamName"])
    adversarial = adversarial_turns(examples)
    order = trace_order_for_sessions(clients.logs, args.log_group, session_ids, start_s, end_s) if adversarial and events else {}
    session_to_scenario = {m["sessionId"]: m["testScenarioId"] for m in metadata}
    measured = measure(summaries, events, trajectory_evaluator=args.trajectory_evaluator, adversarial=adversarial,
                       trace_order=order, session_to_scenario=session_to_scenario)
    failures = judge_measured(measured, thresholds)

    print(f"\nBatch evaluation {batch_id}: {status}; sessions completed={results.get('numberOfSessionsCompleted')}"
          f" failed={results.get('numberOfSessionsFailed')} total={results.get('totalNumberOfSessions')}; {len(events)} result event(s)")
    for summary in summaries:
        statistics = summary.get("statistics") or {}
        print(f"  {summary.get('evaluatorId')}: average={statistics.get('averageScore')} evaluated={summary.get('totalEvaluated')} failed={summary.get('totalFailed')}")
    print("Measured against the evaluation_gate.py thresholds:")
    for name, threshold in (
        ("regression_pass_pct", f">= {thresholds.regression_pass_min_pct}"),
        ("guardrail_violation_pct", f"<= {thresholds.guardrail_violation_max_pct}"),
        ("quality_pct", f">= {thresholds.quality_min_pct}"),
        ("tool_success_pct", f">= {thresholds.tool_success_min_pct}"),
        ("refusal_rate_pct", f">= {thresholds.refusal_rate_min_pct}"),
    ):
        value = getattr(measured, name)
        print(f"  {name:<26} = {'not measured' if value is None else f'{value:.1f}':<14} (threshold {threshold})")
    for note in measured.notes:
        print(f"  note: {note}")

    verdict = 2 if status in ("FAILED", "STOPPED") else (1 if failures else 0)
    evidence = {
        "command": "gate", "dataset": {"id": args.dataset_id, "version": args.dataset_version, "scenarios": len(examples)},
        "batchEvaluation": {"id": batch_id, "status": status, "sessions": {k: v for k, v in results.items() if k != "evaluatorSummaries"},
                            "evaluatorSummaries": summaries, "errorDetails": final.get("errorDetails"), "output": output},
        "resultEvents": len(events), "measured": {k: v for k, v in measured.__dict__.items() if k != "notes"}, "notes": measured.notes,
        "thresholds": thresholds.__dict__, "failures": failures, "verdict": ["PASS", "FAIL", "ERROR"][verdict],
        "candidateSessions": {k: v[-8:] for k, v in sessions.items()},
    }
    _write_json(args.evidence, evidence)
    if verdict == 2:
        print(f"\nGATE ERROR - batch evaluation {status}: {final.get('errorDetails')}")
    elif failures:
        print("\nGATE FAILED - one or more thresholds breached:")
        for failure in failures:
            print(f"  - {failure}")
    else:
        print("\nGATE PASSED")
    return verdict


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", required=True)
    parser.add_argument("--profile", help="Named AWS profile (the hub account for centralized traces, a cell for its own groups).")
    parser.add_argument("--assume-role-arn", help="Run under this role (STS AssumeRole): the hub golden-gate runner role for export/gate, "
                                                  "the cell evaluation invoker role for replay. This is how the workload pipeline runs the gate.")
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="Write a dataset version's scenarios to a local file for replay under another role.")
    export.add_argument("--dataset-id", required=True)
    export.add_argument("--dataset-version", required=True)
    export.add_argument("--out", required=True)

    curate = sub.add_parser("curate", help="Curate scenarios from traces and publish a dataset version.")
    curate.add_argument("--log-group", action="append", required=True, help="Repeatable, 1-5: aws/spans copy + the agent's log group copy.")
    curate.add_argument("--service-name", required=True, help="The agent's OpenTelemetry service.name.")
    curate.add_argument("--since-hours", type=float, default=24)
    curate.add_argument("--session-id", action="append", help="Repeatable. Default: discover the most recent sessions.")
    curate.add_argument("--max-sessions", type=int, default=20)
    curate.add_argument("--min-turns", type=int, default=1)
    curate.add_argument("--scenario-prefix", default="golden")
    curate.add_argument("--assertion", action="append", help="Repeatable natural-language assertion applied to every scenario (Builtin.GoalSuccessRate).")
    curate.add_argument("--adversarial-regex", default=DEFAULT_ADVERSARIAL_RE, help="Turns whose input matches are marked adversarial in scenario metadata.")
    curate.add_argument("--dataset-name", required=True, help=f"New dataset name, {DATASET_NAME_RE.pattern}.")
    curate.add_argument("--dataset-id", help="Add to this existing dataset's DRAFT instead of creating a new dataset.")
    curate.add_argument("--description")
    curate.add_argument("--kms-key-arn", help="Customer managed key for the dataset (immutable after creation).")
    curate.add_argument("--out", help="Local JSON file of the curated scenarios (for review).")
    curate.add_argument("--evidence", help="Evidence JSON (account ids redacted).")
    curate.add_argument("--dry-run", action="store_true", help="Build and write the scenarios; do not call the dataset APIs.")

    replay = sub.add_parser("replay", help="Replay a dataset version against a candidate AgentCore Runtime.")
    replay.add_argument("--scenarios", help="Scenario file written by `export` (lets replay run under the cell's invoker role).")
    replay.add_argument("--dataset-id", help="Read the version directly instead (needs dataset permissions in this role).")
    replay.add_argument("--dataset-version")
    replay.add_argument("--runtime-arn", required=True)
    replay.add_argument("--qualifier", default="DEFAULT")
    replay.add_argument("--out", required=True, help="Scenario -> session id JSON the gate consumes.")

    gate = sub.add_parser("gate", help="Batch-evaluate candidate sessions against a dataset version and apply the thresholds.")
    gate.add_argument("--dataset-id", required=True)
    gate.add_argument("--dataset-version", required=True)
    gate.add_argument("--sessions", required=True, help="Scenario -> candidate session id JSON (from replay, or the harness that ran the candidate).")
    gate.add_argument("--log-group", action="append", required=True, help="Repeatable, 1-5: where the candidate sessions' spans and GenAI records are.")
    gate.add_argument("--service-name", required=True)
    gate.add_argument("--since-hours", type=float, default=24)
    gate.add_argument("--name", required=True, help=f"Batch evaluation name, {DATASET_NAME_RE.pattern}.")
    gate.add_argument("--evaluator", action="append", help=f"Repeatable. Default: {CORRECTNESS}, {GOAL_SUCCESS}, {HARMFULNESS}, {REFUSAL}.")
    gate.add_argument("--trajectory-evaluator", default=DEFAULT_TRAJECTORY_EVALUATOR, choices=TRAJECTORY_EVALUATORS)
    gate.add_argument("--results-log-group", help="Dedicated result log group; default: the service's own.")
    gate.add_argument("--results-kms-key-arn",
                      help="Create --results-log-group encrypted with this CMK before the batch starts if the group does not exist yet "
                           "(the service otherwise creates it without a customer key; observed live).")
    gate.add_argument("--metrics-namespace")
    gate.add_argument("--timeout", type=int, default=1800)
    gate.add_argument("--poll-interval", type=int, default=30)
    gate.add_argument("--wait-for-sessions", type=int, default=600,
                      help="Seconds to wait for every candidate session's traces to appear in the source groups before starting "
                           "the batch evaluation (ingestion, plus centralization for hub copies); 0 disables. Fails closed on timeout.")
    gate.add_argument("--evidence", help="Evidence JSON (account ids redacted).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    clients = Clients(region=args.region, profile=args.profile, assume_role_arn=args.assume_role_arn)
    try:
        if args.command == "curate":
            return cmd_curate(args, clients)
        if args.command == "export":
            return cmd_export(args, clients)
        if args.command == "replay":
            return cmd_replay(args, clients)
        return cmd_gate(args, clients)
    except (ClientError, RuntimeError, TimeoutError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
