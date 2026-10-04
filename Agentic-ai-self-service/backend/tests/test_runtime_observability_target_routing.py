"""Every runtime-name observability surface must use the runtime's AWS target."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from app.routers import cost, evaluations
from app.services.cost_tracking import summarize_from_logs
from app.services.runtime_target_context import OwnedRuntimeTarget
from app.services.trace_query import fetch_trace_waterfall
from fastapi import HTTPException

REGION = "eu-west-1"
ACCOUNT = "222222222222"
RUNTIME_ID = "orders-runtime-id"
TRACE_ID = "0123456789abcdef0123456789abcdef"


def _target(*, ctrl=None, logs=None, cloudwatch=None) -> OwnedRuntimeTarget:
    clients = {
        "bedrock-agentcore-control": ctrl or MagicMock(name="target_ctrl"),
        "logs": logs or MagicMock(name="target_logs"),
        "cloudwatch": cloudwatch or MagicMock(name="target_cloudwatch"),
    }
    session = MagicMock(name="account_distinct_target_session")
    session.client.side_effect = lambda service, **_kwargs: clients[service]
    return OwnedRuntimeTarget(
        runtime_id=RUNTIME_ID,
        version_id="v1",
        deployment_id="dep-1",
        region=REGION,
        account_id=ACCOUNT,
        role_arn=f"arn:aws:iam::{ACCOUNT}:role/DeploymentRole",
        session=session,
    )


def test_evaluation_config_uses_target_control_plane_client():
    ctrl = MagicMock()
    ctrl.list_online_evaluation_configs.return_value = {
        "onlineEvaluationConfigs": [
            {
                "onlineEvaluationConfigName": f"eval_{RUNTIME_ID}",
                "onlineEvaluationConfigId": "eval-1",
            }
        ]
    }
    ctrl.get_online_evaluation_config.return_value = {
        "onlineEvaluationConfigName": f"eval_{RUNTIME_ID}",
        "dataSourceConfig": {
            "cloudWatchLogs": {
                "serviceNames": [RUNTIME_ID],
                "logGroupNames": [],
            }
        },
        "evaluators": [],
        "rule": {"samplingConfig": {"samplingPercentage": 10}},
        "status": "ACTIVE",
    }
    target = _target(ctrl=ctrl)

    with patch.object(evaluations, "resolve_owned_runtime_target", return_value=target):
        result = asyncio.run(
            evaluations.get_evaluation_config(
                "orders",
                caller_sub="owner",
            )
        )

    assert result["config_id"] == "eval-1"
    target.session.client.assert_called_once_with(
        "bedrock-agentcore-control",
        region_name=REGION,
    )


def test_evaluation_results_uses_target_control_and_logs_clients():
    ctrl = MagicMock()
    ctrl.list_online_evaluation_configs.return_value = {"onlineEvaluationConfigs": []}
    logs = MagicMock()

    class _Missing(Exception):
        pass

    logs.exceptions.ResourceNotFoundException = _Missing
    logs.start_query.side_effect = _Missing()
    target = _target(ctrl=ctrl, logs=logs)

    with patch.object(evaluations, "resolve_owned_runtime_target", return_value=target):
        result = asyncio.run(
            evaluations.list_evaluation_results(
                "orders",
                hours=1,
                caller_sub="owner",
            )
        )

    assert result["results"] == []
    assert target.session.client.call_args_list == [
        (("logs",), {"region_name": REGION}),
        (("bedrock-agentcore-control",), {"region_name": REGION}),
    ]


def test_dashboard_probe_and_console_url_use_the_target_account_region():
    cloudwatch = MagicMock()
    target = _target(cloudwatch=cloudwatch)

    with patch.object(evaluations, "resolve_owned_runtime_target", return_value=target):
        result = asyncio.run(
            evaluations.get_dashboard_url(
                "orders",
                caller_sub="owner",
            )
        )

    assert result["exists"] is True
    assert f"https://{REGION}.console.aws.amazon.com/" in result["dashboard_url"]
    assert f"region={REGION}" in result["dashboard_url"]
    target.session.client.assert_called_once_with("cloudwatch", region_name=REGION)


def test_cost_route_passes_the_target_logs_client_to_the_query_helper():
    logs = MagicMock(name="target_logs")
    target = _target(logs=logs)
    summary = {
        "total_cost": 0.0,
        "total_in": 0,
        "total_out": 0,
        "by_model": {},
    }
    with (
        patch.object(cost, "resolve_owned_runtime_target", return_value=target),
        patch.object(cost, "summarize_from_logs", return_value=summary) as summarize,
        patch("app.services.budget_store.get_budget_store") as budget_store,
    ):
        budget_store.return_value.get.return_value = None
        result = asyncio.run(
            cost.get_runtime_cost(
                "orders",
                from_=100,
                to=200,
                caller_sub="owner",
            )
        )

    assert result["runtime_id"] == RUNTIME_ID
    summarize.assert_called_once_with(
        RUNTIME_ID,
        100,
        200,
        REGION,
        logs_client=logs,
    )


def test_trace_route_passes_the_target_logs_client_to_the_query_helper():
    logs = MagicMock(name="target_logs")
    target = _target(logs=logs)
    with (
        patch.object(cost, "resolve_owned_runtime_target", return_value=target),
        patch(
            "app.services.trace_query.fetch_trace_waterfall",
            return_value={"spans": []},
        ) as fetch,
    ):
        result = asyncio.run(
            cost.get_runtime_traces(
                "orders",
                from_=100,
                to=200,
                trace_id=TRACE_ID,
                caller_sub="owner",
            )
        )

    assert result["version_id"] == "v1"
    fetch.assert_called_once_with(
        RUNTIME_ID,
        100,
        200,
        REGION,
        logs_client=logs,
        trace_id=TRACE_ID,
    )


def test_trace_route_rejects_query_injection_before_target_resolution():
    with patch.object(cost, "resolve_owned_runtime_target") as resolve:
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                cost.get_runtime_traces(
                    "orders",
                    from_=100,
                    to=200,
                    trace_id='abc" | limit 1',
                    caller_sub="owner",
                )
            )

    assert exc_info.value.status_code == 400
    resolve.assert_not_called()


def test_cost_helper_never_rebuilds_ambient_logs_when_one_is_supplied():
    logs = MagicMock()
    logs.describe_log_groups.return_value = {"logGroups": []}

    with patch(
        "app.services.cost_tracking.boto3.client",
        side_effect=AssertionError("ambient AWS session used"),
    ):
        result = summarize_from_logs(
            RUNTIME_ID,
            100,
            200,
            REGION,
            logs_client=logs,
        )

    assert result["log_group_names"] == []
    logs.describe_log_groups.assert_called_once()


def test_trace_helper_never_rebuilds_ambient_logs_when_one_is_supplied():
    logs = MagicMock()
    logs.describe_log_groups.return_value = {"logGroups": []}

    with patch(
        "app.services.trace_query.boto3.client",
        side_effect=AssertionError("ambient AWS session used"),
    ):
        result = fetch_trace_waterfall(
            RUNTIME_ID,
            100,
            200,
            REGION,
            logs_client=logs,
        )

    assert result["query_status"] == "Empty"
    logs.describe_log_groups.assert_called_once()


@pytest.mark.parametrize(
    ("route", "operation"),
    [
        (cost.get_runtime_cost, "cost"),
        (cost.get_runtime_traces, "traces"),
    ],
)
def test_runtime_observability_query_failure_is_sanitized_not_an_empty_success(
    route,
    operation,
):
    logs = MagicMock()
    logs.describe_log_groups.side_effect = RuntimeError(
        "Access denied to arn:aws:logs:eu-west-1:222222222222:log-group:private"
    )
    target = _target(logs=logs)

    with patch.object(cost, "resolve_owned_runtime_target", return_value=target):
        with pytest.raises(HTTPException) as exc_info:
            kwargs = {
                "runtime_name": "orders",
                "from_": 100,
                "to": 200,
                "caller_sub": "owner",
            }
            if operation == "traces":
                kwargs["trace_id"] = None
            asyncio.run(route(**kwargs))

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Runtime observability is temporarily unavailable. Try again shortly."
    assert "222222222222" not in exc_info.value.detail


@pytest.mark.parametrize(
    ("helper", "expected_status"),
    [
        (summarize_from_logs, "runtime cost query ended with status Failed"),
        (fetch_trace_waterfall, "runtime trace query ended with status Failed"),
    ],
)
def test_runtime_observability_helper_rejects_partial_failed_queries(
    helper,
    expected_status,
):
    logs = MagicMock()
    logs.describe_log_groups.return_value = {
        "logGroups": [{"logGroupName": (f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT")}]
    }
    logs.start_query.return_value = {}

    with pytest.raises(RuntimeError, match=expected_status):
        helper(
            RUNTIME_ID,
            100,
            200,
            REGION,
            logs_client=logs,
            poll_seconds=1,
        )
