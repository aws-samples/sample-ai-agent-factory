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

All endpoints resolve an owner-checked slot, version, and deployment record.
Cross-tenant requests return 404 (existence-non-disclosure).
"""

from __future__ import annotations

import logging
import re
import time

from fastapi import APIRouter, Depends, HTTPException

from app.services.auth import get_caller_sub
from app.services.aws_errors import error_code, is_error
from app.services.aws_pagination import list_all
from app.services.rbac import require_scopes
from app.services.runtime_target_context import resolve_owned_runtime_target

logger = logging.getLogger(__name__)
_OBSERVABILITY_UNAVAILABLE = "Runtime observability is temporarily unavailable. Try again shortly."
_NOT_FOUND_CODES = ("DashboardNotFoundError", "ResourceNotFound", "ResourceNotFoundException")


def _validate_runtime_name(name: str) -> str:
    if not name or len(name) > 64:
        raise HTTPException(status_code=400, detail="Invalid runtime_name")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", name):
        raise HTTPException(status_code=400, detail="Invalid runtime_name format")
    return name


router = APIRouter(prefix="/api/runtimes", tags=["evaluations"])


def _list_all_evaluation_configs(ctrl) -> list[dict]:
    return list_all(
        ctrl,
        "list_online_evaluation_configs",
        item_keys=("onlineEvaluationConfigs", "items"),
        request={"maxResults": 50},
    )


def _runtime_log_group(runtime_id: str) -> str:
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"


def _default_evaluation_config_name(runtime_id: str) -> str:
    """Mirror evaluation_step's default name without treating it as authority."""

    config_name = re.sub(r"[^a-zA-Z0-9_]", "_", f"eval_{runtime_id}")[:48]
    if not config_name or not config_name[0].isalpha():
        config_name = f"e{config_name}"[:48]
    return config_name


def _config_targets_runtime(detail: dict, runtime_id: str) -> bool:
    """Bind an evaluation config to a runtime by exact data-source values."""

    if not isinstance(detail, dict):
        return False
    data_source = detail.get("dataSourceConfig") or {}
    if not isinstance(data_source, dict):
        return False
    cloudwatch_logs = data_source.get("cloudWatchLogs") or {}
    if not isinstance(cloudwatch_logs, dict):
        return False
    service_names = cloudwatch_logs.get("serviceNames") or []
    log_groups = cloudwatch_logs.get("logGroupNames") or []
    if not isinstance(service_names, list) or not isinstance(log_groups, list):
        return False
    return runtime_id in service_names or _runtime_log_group(runtime_id) in log_groups


def _service_unavailable(operation: str, exc: Exception) -> HTTPException:
    """Log a diagnosable, non-sensitive AWS failure and return a stable API error."""

    code = error_code(exc)
    logger.warning(
        "%s failed: %s%s",
        operation,
        type(exc).__name__,
        f" {code}" if code else "",
    )
    return HTTPException(status_code=503, detail=_OBSERVABILITY_UNAVAILABLE)


def _ambiguous_evaluation_config(runtime_id: str) -> HTTPException:
    logger.warning(
        "Multiple online evaluation configs target runtime %s; refusing an arbitrary selection",
        runtime_id,
    )
    return HTTPException(status_code=503, detail=_OBSERVABILITY_UNAVAILABLE)


def _query_status_unavailable(operation: str, status: str) -> HTTPException:
    logger.warning("%s ended with status %s", operation, status)
    return HTTPException(status_code=503, detail=_OBSERVABILITY_UNAVAILABLE)


def _is_resource_not_found(exc: Exception, client=None) -> bool:
    """Match botocore errors and lightweight test/client exception classes safely."""

    if is_error(exc, *_NOT_FOUND_CODES):
        return True
    exception_type = getattr(getattr(client, "exceptions", None), "ResourceNotFoundException", None)
    return (
        isinstance(exception_type, type)
        and issubclass(exception_type, BaseException)
        and isinstance(exc, exception_type)
    )


def _find_evaluation_config_for_runtime(ctrl, runtime_id: str) -> tuple[dict | None, dict | None]:
    """Return one config proven to target ``runtime_id``.

    Config names are only an ordering hint.  AgentCore truncates names, and
    custom names are supported, so a prefix/substring match cannot establish
    which runtime a config belongs to.  Every candidate is described and bound
    through its exact service name or exact runtime log-group name.
    """

    try:
        configs = _list_all_evaluation_configs(ctrl)
    except Exception as exc:
        raise _service_unavailable("list evaluation configs", exc) from exc

    expected_name = _default_evaluation_config_name(runtime_id)
    ordered = sorted(
        enumerate(configs),
        key=lambda item: (
            item[1].get("onlineEvaluationConfigName") != expected_name,
            item[0],
        ),
    )
    matches: list[tuple[dict, dict]] = []
    seen_ids: set[str] = set()
    for _, config in ordered:
        if not isinstance(config, dict):
            raise _service_unavailable(
                "parse evaluation config listing",
                TypeError("evaluation config summary is not an object"),
            )
        config_id = str(config.get("onlineEvaluationConfigId") or "").strip()
        if not config_id or config_id in seen_ids:
            continue
        seen_ids.add(config_id)
        try:
            detail = ctrl.get_online_evaluation_config(
                onlineEvaluationConfigId=config_id,
            )
        except Exception as exc:
            if _is_resource_not_found(exc, ctrl):
                logger.debug(
                    "evaluation config disappeared during lookup: %s",
                    error_code(exc),
                )
                continue
            raise _service_unavailable("describe evaluation config", exc) from exc
        if not isinstance(detail, dict):
            raise _service_unavailable(
                "parse evaluation config",
                TypeError("evaluation config detail is not an object"),
            )
        if _config_targets_runtime(detail, runtime_id):
            matches.append((config, detail))

    if len(matches) > 1:
        raise _ambiguous_evaluation_config(runtime_id)
    if matches:
        return matches[0]
    return None, None


@router.get("/{runtime_name}/evaluation-config", dependencies=[Depends(require_scopes("eval:read"))])
async def get_evaluation_config(
    runtime_name: str,
    caller_sub: str = Depends(get_caller_sub),
) -> dict:
    runtime_name = _validate_runtime_name(runtime_name)
    target = resolve_owned_runtime_target(runtime_name, caller_sub)
    runtime_id, version_id = target.runtime_id, target.version_id

    try:
        ctrl = target.client("bedrock-agentcore-control")
    except Exception as exc:
        raise _service_unavailable("create evaluation control client", exc) from exc
    matched, detail = _find_evaluation_config_for_runtime(ctrl, runtime_id)

    if matched is None:
        raise HTTPException(
            status_code=404,
            detail="No evaluation config found for this runtime",
        )

    cfg_id = matched.get("onlineEvaluationConfigId")
    assert detail is not None
    return {
        "runtime_name": runtime_name,
        "version_id": version_id,
        "runtime_id": runtime_id,
        "config_id": cfg_id,
        "config_name": detail.get("onlineEvaluationConfigName"),
        "evaluators": [ev.get("evaluatorId") for ev in detail.get("evaluators", [])],
        "sampling_rate": (detail.get("rule", {}).get("samplingConfig", {}).get("samplingPercentage")),
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
    target = resolve_owned_runtime_target(runtime_name, caller_sub)
    runtime_id, version_id = target.runtime_id, target.version_id

    # AgentCore Online Evaluation writes scores to a dedicated log group per
    # config: /aws/bedrock-agentcore/evaluations/results/{config_id}. We
    # locate the config first (matching the config_name we minted in
    # evaluation_step.py — `eval_<sanitized_runtime_id>`) and then query
    # against that log group. Falls back to the runtime log group only if
    # no eval config exists. Verified live 2026-05-28; lessons.md Bug 120.
    try:
        logs_client = target.client("logs")
        ctrl = target.client("bedrock-agentcore-control")
    except Exception as exc:
        raise _service_unavailable("create evaluation service clients", exc) from exc

    log_group = ""
    matched, _ = _find_evaluation_config_for_runtime(ctrl, runtime_id)
    if matched is not None:
        cfg_id = matched.get("onlineEvaluationConfigId", "")
        if cfg_id:
            log_group = f"/aws/bedrock-agentcore/evaluations/results/{cfg_id}"

    if not log_group:
        # Bug 139: runtime invocation logs land in the "-DEFAULT" endpoint group
        # (same group cost + dashboard read); without the suffix this queried an
        # empty group and the panel showed no scores even with live traffic.
        log_group = _runtime_log_group(runtime_id)

    end_ts = int(time.time())
    start_ts = end_ts - hours * 3600

    # Logs Insights query — filter to evaluator score events, group by evaluator id.
    # AgentCore's eval log shape (from samples lab-05) has fields:
    #   evaluatorId, score, timestamp
    query_string = (
        "fields @timestamp, @message"
        "\n| filter @message like /evaluatorId/"
        '\n| parse @message /"evaluatorId":"(?<eid>[^"]+)".*"score":(?<score>[0-9.]+)/'
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
    except Exception as exc:
        if _is_resource_not_found(exc, logs_client):
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
        raise _service_unavailable("start evaluation Logs Insights query", exc) from exc

    # Poll until the query finishes (Logs Insights is async). We block up to
    # ~10s to keep the API call within API GW's 29s ceiling with margin.
    deadline = time.time() + 10
    results: list[dict] = []
    status = "Running"
    while time.time() < deadline:
        try:
            get_resp = logs_client.get_query_results(queryId=query_id)
        except Exception as exc:
            raise _service_unavailable("poll evaluation Logs Insights query", exc) from exc
        status = get_resp.get("status", "Running")
        if status in ("Complete", "Failed", "Cancelled"):
            for row in get_resp.get("results", []):
                row_dict = {field["field"]: field["value"] for field in row}
                results.append(row_dict)
            break
        time.sleep(0.5)

    if status == "Running":
        # Query still running — cancel it rather than presenting a timeout as
        # a successful empty result. A later API call can start a fresh query.
        try:
            logs_client.stop_query(queryId=query_id)
        except Exception:  # noqa: BLE001 — best-effort cancel; partial results are still returned
            logger.debug("stop_query %s failed", query_id, exc_info=True)
        raise _query_status_unavailable("evaluation Logs Insights query", status)
    if status in {"Failed", "Cancelled"}:
        raise _query_status_unavailable("evaluation Logs Insights query", status)

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
    target = resolve_owned_runtime_target(runtime_name, caller_sub)
    runtime_id, version_id = target.runtime_id, target.version_id

    name = dashboard_name_for_runtime(runtime_id)
    region = target.region

    # Optionally probe the dashboard exists; we don't need this for the URL
    # itself, but the response surfaces a clearer "exists/missing" flag the
    # frontend can use to disable the button on failed deploys.
    try:
        cw = target.client("cloudwatch")
    except Exception as exc:
        raise _service_unavailable("create runtime dashboard client", exc) from exc
    exists = True
    try:
        cw.get_dashboard(DashboardName=name)
    except Exception as exc:
        if is_error(exc, *_NOT_FOUND_CODES):
            exists = False
        else:
            raise _service_unavailable("get runtime dashboard", exc) from exc

    return {
        "runtime_name": runtime_name,
        "version_id": version_id,
        "runtime_id": runtime_id,
        "dashboard_name": name,
        "dashboard_url": dashboard_console_url(region, name),
        "exists": exists,
    }
