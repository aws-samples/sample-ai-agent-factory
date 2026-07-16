"""Evaluation results + observability dashboard API — Phase 1 Gap 1C/1D.

Surfaces AgentCore Online Evaluation configs, their recent scores, and the
auto-generated CloudWatch dashboard URL so the frontend can show per-runtime
observability without forcing the user to open the AWS console manually.

Endpoints:

* ``GET /api/runtimes/{runtime_name}/evaluation-config`` — returns the
  evaluator IDs + sampling rate currently registered against the production
  version's runtime, or 404 if no eval config exists.
* ``GET /api/runtimes/{runtime_name}/evaluations`` — returns the most recent
  per-evaluator scores aggregated from the runtime's CloudWatch Logs.
* ``GET /api/runtimes/{runtime_name}/dashboard-url`` — Phase 1 Gap 1D:
  returns the CloudWatch console URL for the runtime's auto-generated
  dashboard (created by ``runtime_launch_step.py`` on every successful
  deploy).

All endpoints require ownership of the runtime via the ``RuntimeSlots``
table. Cross-tenant requests return 404 (existence-non-disclosure).
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

import boto3
from fastapi import APIRouter, Depends, HTTPException

from app.services.agent_versions_store import (
    get_slots_store,
    get_versions_store,
)
from app.services.auth import assert_owner, get_caller_sub
from app.services.rbac import require_scopes

logger = logging.getLogger(__name__)


def _validate_runtime_name(name: str) -> str:
    if not name or len(name) > 64:
        raise HTTPException(status_code=400, detail="Invalid runtime_name")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", name):
        raise HTTPException(status_code=400, detail="Invalid runtime_name format")
    return name


def _region() -> str:
    return os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))


router = APIRouter(prefix="/api/runtimes", tags=["evaluations"])


def _resolve_owned_runtime_id(
    runtime_name: str, caller_sub: str
) -> tuple[str, str]:
    """Return (runtime_id, version_id) for the production version owned by
    *caller_sub*, or 404 if either the runtime or the slot is missing.
    """
    slots = get_slots_store().get(runtime_name)
    if slots is None or not slots.production_version_id:
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(slots.owner_sub, caller_sub)
    version = get_versions_store().get(runtime_name, slots.production_version_id)
    if version is None or not version.runtime_id:
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(version.owner_sub, caller_sub)
    return version.runtime_id, version.version_id


@router.get("/{runtime_name}/evaluation-config", dependencies=[Depends(require_scopes("eval:read"))])
async def get_evaluation_config(
    runtime_name: str,
    caller_sub: str = Depends(get_caller_sub),
) -> dict:
    runtime_name = _validate_runtime_name(runtime_name)
    runtime_id, version_id = _resolve_owned_runtime_id(runtime_name, caller_sub)

    ctrl = boto3.client("bedrock-agentcore-control", region_name=_region())
    # AgentCore's ListOnlineEvaluationConfigs has no direct filter for runtime,
    # but configs are named after the agent_id during create. Match by name
    # prefix as a heuristic; fall back to scanning the cloudWatchLogs serviceNames.
    configs: list[dict] = []
    next_token: Optional[str] = None
    for _ in range(20):  # cap pagination
        kwargs: dict = {"maxResults": 50}
        if next_token:
            kwargs["nextToken"] = next_token
        resp = ctrl.list_online_evaluation_configs(**kwargs)
        configs.extend(resp.get("onlineEvaluationConfigs", []))
        next_token = resp.get("nextToken")
        if not next_token:
            break

    matched = None
    for cfg in configs:
        cfg_name = cfg.get("onlineEvaluationConfigName", "")
        # evaluation_step.py names configs `eval_<agent_id sanitized>` (re-sub
        # of non-alnum to _), so match by runtime_id substring.
        normalised_runtime = re.sub(r"[^a-zA-Z0-9_]", "_", runtime_id)
        if normalised_runtime[:32] in cfg_name or runtime_id[:32] in cfg_name:
            matched = cfg
            break

    if matched is None:
        raise HTTPException(
            status_code=404,
            detail="No evaluation config found for this runtime",
        )

    cfg_id = matched.get("onlineEvaluationConfigId")
    detail = ctrl.get_online_evaluation_config(onlineEvaluationConfigId=cfg_id)
    return {
        "runtime_name": runtime_name,
        "version_id": version_id,
        "runtime_id": runtime_id,
        "config_id": cfg_id,
        "config_name": detail.get("onlineEvaluationConfigName"),
        "evaluators": [
            ev.get("evaluatorId") for ev in detail.get("evaluators", [])
        ],
        "sampling_rate": (
            detail.get("rule", {}).get("samplingConfig", {}).get("samplingPercentage")
        ),
        "status": detail.get("status"),
    }


@router.get("/{runtime_name}/evaluations", dependencies=[Depends(require_scopes("eval:read"))])
async def list_evaluation_results(
    runtime_name: str,
    hours: int = 24,
    caller_sub: str = Depends(get_caller_sub),
) -> dict:
    """Aggregate the most recent per-evaluator scores from CloudWatch Logs.

    AgentCore's online evaluation writes one log event per evaluated
    invocation to the runtime's log group. Each event includes the
    evaluator id and a numeric score in the message body. We run a
    Logs Insights query that buckets by evaluator and returns the
    average + count + most recent score.
    """
    runtime_name = _validate_runtime_name(runtime_name)
    if hours < 1 or hours > 168:
        raise HTTPException(status_code=400, detail="hours must be 1-168")
    runtime_id, version_id = _resolve_owned_runtime_id(runtime_name, caller_sub)

    # AgentCore Online Evaluation writes scores to a dedicated log group per
    # config: /aws/bedrock-agentcore/evaluations/results/{config_id}. We
    # locate the config first (matching the config_name we minted in
    # evaluation_step.py — `eval_<sanitized_runtime_id>`) and then query
    # against that log group. Falls back to the runtime log group only if
    # no eval config exists. Verified live 2026-05-28; lessons.md Bug 120.
    logs_client = boto3.client("logs", region_name=_region())
    ctrl = boto3.client("bedrock-agentcore-control", region_name=_region())

    log_group = ""
    try:
        next_token: Optional[str] = None
        for _ in range(20):
            kw: dict = {"maxResults": 50}
            if next_token:
                kw["nextToken"] = next_token
            resp = ctrl.list_online_evaluation_configs(**kw)
            normalised_runtime = re.sub(r"[^a-zA-Z0-9_]", "_", runtime_id)
            for cfg in resp.get("onlineEvaluationConfigs", []):
                cfg_name = cfg.get("onlineEvaluationConfigName", "")
                if normalised_runtime[:32] in cfg_name or runtime_id[:32] in cfg_name:
                    cfg_id = cfg.get("onlineEvaluationConfigId", "")
                    if cfg_id:
                        log_group = (
                            f"/aws/bedrock-agentcore/evaluations/results/{cfg_id}"
                        )
                    break
            if log_group:
                break
            next_token = resp.get("nextToken")
            if not next_token:
                break
    except Exception:
        logger.exception("Failed to list eval configs while resolving log group")

    if not log_group:
        # Bug 139: runtime invocation logs land in the "-DEFAULT" endpoint group
        # (same group cost + dashboard read); without the suffix this queried an
        # empty group and the panel showed no scores even with live traffic.
        log_group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"

    end_ts = int(time.time())
    start_ts = end_ts - hours * 3600

    # Logs Insights query — filter to evaluator score events, group by evaluator id.
    # AgentCore's eval log shape (from samples lab-05) has fields:
    #   evaluatorId, score, timestamp
    query_string = (
        "fields @timestamp, @message"
        "\n| filter @message like /evaluatorId/"
        "\n| parse @message /\"evaluatorId\":\"(?<eid>[^\"]+)\".*\"score\":(?<score>[0-9.]+)/"
        "\n| stats count(*) as runs, avg(score) as avg_score, latest(score) as latest_score by eid"
        "\n| sort by avg_score desc"
        "\n| limit 50"
    )

    try:
        start_resp = logs_client.start_query(
            logGroupName=log_group,
            startTime=start_ts,
            endTime=end_ts,
            queryString=query_string,
        )
        query_id = start_resp.get("queryId")
        if not query_id:
            raise HTTPException(status_code=500, detail="Failed to start CloudWatch query")
    except logs_client.exceptions.ResourceNotFoundException:
        # Log group not created yet — runtime hasn't received traffic with eval enabled.
        return {
            "runtime_name": runtime_name,
            "version_id": version_id,
            "runtime_id": runtime_id,
            "log_group_name": log_group,
            "from_ts": start_ts,
            "to_ts": end_ts,
            "results": [],
            "message": "No evaluation log group yet. Invoke the runtime first.",
        }
    except Exception as exc:
        logger.exception("Failed to start CloudWatch Insights query")
        raise HTTPException(status_code=500, detail=str(exc))

    # Poll until the query finishes (Logs Insights is async). We block up to
    # ~10s to keep the API call within API GW's 29s ceiling with margin.
    deadline = time.time() + 10
    results: list[dict] = []
    status = "Running"
    while time.time() < deadline:
        get_resp = logs_client.get_query_results(queryId=query_id)
        status = get_resp.get("status", "Running")
        if status in ("Complete", "Failed", "Cancelled"):
            for row in get_resp.get("results", []):
                row_dict = {field["field"]: field["value"] for field in row}
                results.append(row_dict)
            break
        time.sleep(0.5)

    if status == "Running":
        # Query still running — cancel + return partial. Caller can re-poll.
        try:
            logs_client.stop_query(queryId=query_id)
        except Exception:
            pass

    return {
        "runtime_name": runtime_name,
        "version_id": version_id,
        "runtime_id": runtime_id,
        "log_group_name": log_group,
        "from_ts": start_ts,
        "to_ts": end_ts,
        "query_status": status,
        "results": results,
    }


@router.get("/{runtime_name}/dashboard-url", dependencies=[Depends(require_scopes("eval:read"))])
async def get_dashboard_url(
    runtime_name: str,
    caller_sub: str = Depends(get_caller_sub),
) -> dict:
    """Phase 1 Gap 1D — return the CloudWatch dashboard URL for *runtime_name*.

    The dashboard is created by ``runtime_launch_step.py`` on every
    successful deploy (per AgentCore runtime ID). This endpoint resolves
    the production version, computes the dashboard name, and returns the
    deep link to the CloudWatch console.

    The dashboard exists for the lifetime of the runtime: it is
    upserted on every redeploy of the same version and deleted when
    ``destroy_runtime`` (DELETE /api/runtime/{id}) is called.
    """
    from app.services.observability_dashboard import (
        dashboard_console_url,
        dashboard_name_for_runtime,
    )

    runtime_name = _validate_runtime_name(runtime_name)
    runtime_id, version_id = _resolve_owned_runtime_id(runtime_name, caller_sub)

    name = dashboard_name_for_runtime(runtime_id)
    region = _region()

    # Optionally probe the dashboard exists; we don't need this for the URL
    # itself, but the response surfaces a clearer "exists/missing" flag the
    # frontend can use to disable the button on failed deploys.
    cw = boto3.client("cloudwatch", region_name=region)
    exists = True
    try:
        cw.get_dashboard(DashboardName=name)
    except Exception as e:
        msg = str(e)
        if "DashboardNotFoundError" in msg or "ResourceNotFound" in msg:
            exists = False
        else:
            logger.warning("get_dashboard probe failed for %s: %s", name, msg)
            exists = False

    return {
        "runtime_name": runtime_name,
        "version_id": version_id,
        "runtime_id": runtime_id,
        "dashboard_name": name,
        "dashboard_url": dashboard_console_url(region, name),
        "exists": exists,
    }
