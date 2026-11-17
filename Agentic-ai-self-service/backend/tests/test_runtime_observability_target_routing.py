"""Every runtime-name observability surface must use the runtime's AWS target."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
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


def test_cost_route_labels_authorization_units_as_non_billing_capacity():
    target = _target(logs=MagicMock(name="target_logs"))
    summary = {
        "total_cost": 1.0,
        "total_in": 10,
        "total_out": 5,
        "by_model": {},
    }
    budget = MagicMock(limit_usd=10.0, warn_pct=80)
    budget_store = MagicMock()
    budget_store.get.return_value = budget
    budget_store.counter_snapshot.return_value = {
        "authorized_microusd": 4_000_000,
        "reserved_microusd": 1_000_000,
        "committed_microusd": 3_000_000,
    }
    with (
        patch.object(cost, "resolve_owned_runtime_target", return_value=target),
        patch.object(cost, "summarize_from_logs", return_value=summary),
        patch("app.services.budget_store.get_budget_store", return_value=budget_store),
        patch("app.services.budget_store.invocation_reservation_usd", return_value=1.0),
    ):
        result = asyncio.run(
            cost.get_runtime_cost(
                "orders",
                from_=100,
                to=200,
                caller_sub="owner",
            )
        )

    owner_budget = result["owner_budget"]
    assert owner_budget["authorization_scope"] == "platform-mediated-invocation-operation"
    assert owner_budget["is_actual_cost_cap"] is False
    assert owner_budget["authorized"] == 4.0
    assert owner_budget["remaining_authorization"] == 6.0


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


def test_logs_route_uses_target_logs_client_group_and_sanitizes():
    """The in-app logs panel reads the runtime's own log group through the
    owner-checked target client, in a bounded ms window, and redacts secrets."""
    logs = MagicMock(name="target_logs")
    logs.filter_log_events.return_value = {
        "events": [
            {"timestamp": 1700000000000, "message": "ERROR boom token=AKIAIOSFODNN7EXAMPLE tail"},
            {"timestamp": 1700000001000, "message": "ok"},
        ]
    }
    target = _target(logs=logs)
    with patch.object(cost, "resolve_owned_runtime_observability_target", return_value=target):
        result = asyncio.run(
            cost.get_runtime_logs("orders", from_=100, to=200, errors_only=False, limit=100, caller_sub="owner")
        )

    expected_group = f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT"
    assert result["log_group"] == expected_group
    assert result["version_id"] == "v1"
    logs.filter_log_events.assert_called_once()
    _, kw = logs.filter_log_events.call_args
    assert kw["logGroupName"] == expected_group
    assert kw["startTime"] == 100_000 and kw["endTime"] == 200_000  # seconds -> ms
    assert "filterPattern" not in kw
    assert "AKIAIOSFODNN7EXAMPLE" not in result["events"][0]["message"]
    assert result["events"][1]["message"] == "ok"
    target.session.client.assert_called_once_with("logs", region_name=REGION)


def test_logs_route_missing_group_reports_no_logs_not_error():
    logs = MagicMock(name="target_logs")
    logs.filter_log_events.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "no group"}},
        "FilterLogEvents",
    )
    target = _target(logs=logs)
    with patch.object(cost, "resolve_owned_runtime_observability_target", return_value=target):
        result = asyncio.run(
            cost.get_runtime_logs("orders", from_=100, to=200, errors_only=False, limit=100, caller_sub="owner")
        )
    assert result["events"] == []
    assert "No runtime logs" in result["note"]


def test_logs_route_errors_only_passes_a_filter_pattern():
    logs = MagicMock(name="target_logs")
    logs.filter_log_events.return_value = {"events": []}
    target = _target(logs=logs)
    with patch.object(cost, "resolve_owned_runtime_observability_target", return_value=target):
        asyncio.run(
            cost.get_runtime_logs("orders", from_=100, to=200, errors_only=True, limit=100, caller_sub="owner")
        )
    _, kw = logs.filter_log_events.call_args
    assert kw.get("filterPattern")


def test_logs_route_query_failure_is_sanitized_503():
    logs = MagicMock(name="target_logs")
    logs.filter_log_events.side_effect = RuntimeError(
        "Access denied to arn:aws:logs:eu-west-1:222222222222:log-group:private"
    )
    target = _target(logs=logs)
    with patch.object(cost, "resolve_owned_runtime_observability_target", return_value=target):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                cost.get_runtime_logs("orders", from_=100, to=200, errors_only=False, limit=100, caller_sub="owner")
            )
    assert exc_info.value.status_code == 503
    assert "222222222222" not in exc_info.value.detail


def test_observability_target_prefers_production_slot():
    """In-app log reads resolve the production slot when one is promoted."""
    from app.services import runtime_target_context as rtc

    target = _target()
    with patch.object(rtc, "resolve_owned_runtime_slot_target", return_value=target) as resolve:
        result = rtc.resolve_owned_runtime_observability_target("orders", "owner")
    assert result is target
    resolve.assert_called_once()
    assert resolve.call_args.args[1] == "production"


def test_observability_target_falls_back_to_staging_when_no_production():
    """A just-deployed (staging-only) runtime stays diagnosable in-app: a 404 on
    the production slot falls back to the owner's staging slot."""
    from app.services import runtime_target_context as rtc

    staging_target = _target()
    seen: list = []

    def fake_slot(runtime_name, slot, caller_sub, **kw):
        seen.append(slot)
        if slot == "production":
            raise HTTPException(status_code=404, detail="Not found")
        return staging_target

    with patch.object(rtc, "resolve_owned_runtime_slot_target", side_effect=fake_slot):
        result = rtc.resolve_owned_runtime_observability_target("orders", "owner")
    assert result is staging_target
    assert seen == ["production", "staging"]


def test_observability_target_propagates_non_404_authorization_failures():
    """Only a missing-slot 404 triggers the fallback; an ownership/binding failure
    (403/409/503) must surface unchanged, never masked by trying staging."""
    from app.services import runtime_target_context as rtc

    with patch.object(
        rtc,
        "resolve_owned_runtime_slot_target",
        side_effect=HTTPException(status_code=403, detail="nope"),
    ):
        with pytest.raises(HTTPException) as exc_info:
            rtc.resolve_owned_runtime_observability_target("orders", "owner")
    assert exc_info.value.status_code == 403
