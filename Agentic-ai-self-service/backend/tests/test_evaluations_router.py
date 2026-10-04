"""Phase 1 Gap 1C — evaluation router unit tests.

These tests use FastAPI's TestClient against a small app that mounts the
evaluations router, with the agent_versions_store + boto3 clients mocked.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

from app.routers.evaluations import (  # noqa: E402
    _default_evaluation_config_name,
)
from app.routers.evaluations import (
    router as evaluations_router,
)
from app.services.auth import _LOCAL_DEV_SUB, get_caller_sub  # noqa: E402
from app.services.runtime_target_context import OwnedRuntimeTarget  # noqa: E402


def _aws_error(code: str, operation: str, message: str = "sensitive internal detail") -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"RequestId": "sensitive-request-id"},
        },
        operation,
    )


def _target(
    runtime_id: str,
    *,
    ctrl=None,
    logs=None,
    cloudwatch=None,
) -> OwnedRuntimeTarget:
    clients = {
        "bedrock-agentcore-control": ctrl or MagicMock(name="target_ctrl"),
        "logs": logs or MagicMock(name="target_logs"),
        "cloudwatch": cloudwatch or MagicMock(name="target_cloudwatch"),
    }
    session = MagicMock(name="target_session")
    session.client.side_effect = lambda service, **_kwargs: clients[service]
    return OwnedRuntimeTarget(
        runtime_id=runtime_id,
        version_id="v1",
        deployment_id="d1",
        region="eu-west-1",
        account_id="222222222222",
        role_arn="arn:aws:iam::222222222222:role/DeploymentRole",
        session=session,
    )


@pytest.fixture
def app_with_router() -> FastAPI:
    app = FastAPI()
    app.include_router(evaluations_router)
    app.dependency_overrides[get_caller_sub] = lambda: _LOCAL_DEV_SUB
    return app


@pytest.fixture
def client(app_with_router: FastAPI) -> TestClient:
    return TestClient(app_with_router)


def test_evaluation_config_404_when_no_slot(client: TestClient):
    with patch(
        "app.routers.evaluations.resolve_owned_runtime_target",
        side_effect=HTTPException(status_code=404, detail="Not found"),
    ):
        resp = client.get("/api/runtimes/myagent/evaluation-config")
    assert resp.status_code == 404


def test_evaluation_config_cross_tenant_returns_404(client: TestClient):
    """Different owner_sub on the slot row → 404 (existence non-disclosure)."""
    with patch(
        "app.routers.evaluations.resolve_owned_runtime_target",
        side_effect=HTTPException(status_code=404, detail="Not found"),
    ):
        resp = client.get("/api/runtimes/myagent/evaluation-config")
    assert resp.status_code == 404


def test_evaluation_config_matches_an_exact_runtime_target(client: TestClient):
    """A generated name is only accepted after its data source binds the runtime."""
    runtime_id = "myagent_abcd1234-runtime-abcd1234"
    ctrl_client = MagicMock()
    with patch(
        "app.routers.evaluations.resolve_owned_runtime_target",
        return_value=_target(runtime_id, ctrl=ctrl_client),
    ):
        ctrl_client.list_online_evaluation_configs.return_value = {
            "onlineEvaluationConfigs": [
                {
                    "onlineEvaluationConfigName": f"eval_{runtime_id[:32]}",
                    "onlineEvaluationConfigId": "ec-1",
                }
            ]
        }
        ctrl_client.get_online_evaluation_config.return_value = {
            "onlineEvaluationConfigName": f"eval_{runtime_id[:32]}",
            "dataSourceConfig": {
                "cloudWatchLogs": {
                    "serviceNames": [runtime_id],
                    "logGroupNames": [],
                }
            },
            "evaluators": [
                {"evaluatorId": "Builtin.GoalSuccessRate"},
                {"evaluatorId": "Builtin.Correctness"},
            ],
            "rule": {"samplingConfig": {"samplingPercentage": 50}},
            "status": "ENABLED",
        }

        resp = client.get("/api/runtimes/myagent/evaluation-config")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["evaluators"] == [
        "Builtin.GoalSuccessRate",
        "Builtin.Correctness",
    ]
    assert body["sampling_rate"] == 50
    assert body["status"] == "ENABLED"
    assert body["config_id"] == "ec-1"


def test_evaluation_config_fallback_resolves_custom_named_config(client: TestClient):
    """A config created with a custom name (evaluationConfig.configName on
    deploy) never matches the `eval_<agent_id>` heuristic. The endpoint must
    fall back to describing each config and matching on the runtime the config
    targets (dataSourceConfig.cloudWatchLogs serviceNames / logGroupNames) —
    live-verified 404 without this."""
    runtime_id = "myagent_abcd1234-runtime-abcd1234"
    ctrl_client = MagicMock()
    with patch(
        "app.routers.evaluations.resolve_owned_runtime_target",
        return_value=_target(runtime_id, ctrl=ctrl_client),
    ):
        # Two configs: one unrelated, one custom-named targeting OUR runtime.
        ctrl_client.list_online_evaluation_configs.return_value = {
            "onlineEvaluationConfigs": [
                {
                    "onlineEvaluationConfigName": "someone_elses_evals",
                    "onlineEvaluationConfigId": "ec-other",
                },
                {
                    "onlineEvaluationConfigName": "my_custom_eval_name",
                    "onlineEvaluationConfigId": "ec-custom",
                },
            ]
        }

        def _get_cfg(onlineEvaluationConfigId):  # noqa: N803
            if onlineEvaluationConfigId == "ec-custom":
                return {
                    "onlineEvaluationConfigName": "my_custom_eval_name",
                    "onlineEvaluationConfigId": "ec-custom",
                    "dataSourceConfig": {
                        "cloudWatchLogs": {
                            "logGroupNames": [f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"],
                            "serviceNames": [runtime_id],
                        }
                    },
                    "evaluators": [{"evaluatorId": "Builtin.Correctness"}],
                    "rule": {"samplingConfig": {"samplingPercentage": 25}},
                    "status": "ACTIVE",
                }
            return {
                "onlineEvaluationConfigName": "someone_elses_evals",
                "onlineEvaluationConfigId": "ec-other",
                "dataSourceConfig": {
                    "cloudWatchLogs": {
                        "logGroupNames": ["/aws/bedrock-agentcore/runtimes/other_rt-DEFAULT"],
                        "serviceNames": ["other_rt"],
                    }
                },
                "evaluators": [],
                "rule": {},
                "status": "ACTIVE",
            }

        ctrl_client.get_online_evaluation_config.side_effect = _get_cfg

        resp = client.get("/api/runtimes/myagent/evaluation-config")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["config_id"] == "ec-custom"
    assert body["config_name"] == "my_custom_eval_name"
    assert body["evaluators"] == ["Builtin.Correctness"]
    assert body["sampling_rate"] == 25
    assert body["status"] == "ACTIVE"


def test_evaluation_config_accepts_the_exact_default_name_after_target_verification(
    client: TestClient,
):
    """The default-named config wins only after every candidate is target-bound."""
    runtime_id = "myagent_abcd1234-runtime-abcd1234"
    default_name = _default_evaluation_config_name(runtime_id)
    ctrl_client = MagicMock()
    with patch(
        "app.routers.evaluations.resolve_owned_runtime_target",
        return_value=_target(runtime_id, ctrl=ctrl_client),
    ):
        ctrl_client.list_online_evaluation_configs.return_value = {
            "onlineEvaluationConfigs": [
                {
                    "onlineEvaluationConfigName": "my_custom_eval_name",
                    "onlineEvaluationConfigId": "ec-custom",
                },
                {
                    "onlineEvaluationConfigName": default_name,
                    "onlineEvaluationConfigId": "ec-heuristic",
                },
            ]
        }

        def _get_cfg(onlineEvaluationConfigId):  # noqa: N803
            if onlineEvaluationConfigId == "ec-heuristic":
                return {
                    "onlineEvaluationConfigName": default_name,
                    "dataSourceConfig": {
                        "cloudWatchLogs": {
                            "serviceNames": [runtime_id],
                            "logGroupNames": [],
                        }
                    },
                    "evaluators": [{"evaluatorId": "Builtin.GoalSuccessRate"}],
                    "rule": {"samplingConfig": {"samplingPercentage": 100}},
                    "status": "ACTIVE",
                }
            return {
                "onlineEvaluationConfigName": "my_custom_eval_name",
                "dataSourceConfig": {
                    "cloudWatchLogs": {
                        "serviceNames": ["unrelated-runtime"],
                        "logGroupNames": [],
                    }
                },
            }

        ctrl_client.get_online_evaluation_config.side_effect = _get_cfg

        resp = client.get("/api/runtimes/myagent/evaluation-config")

    assert resp.status_code == 200, resp.text
    assert resp.json()["config_id"] == "ec-heuristic"
    assert [
        call.kwargs["onlineEvaluationConfigId"] for call in ctrl_client.get_online_evaluation_config.call_args_list
    ] == ["ec-heuristic", "ec-custom"]


def test_evaluation_config_name_prefix_cannot_select_another_runtime(client: TestClient):
    runtime_id = "runtime_with_a_shared_prefix_1234567890_ours"
    other_runtime = "runtime_with_a_shared_prefix_1234567890_other"
    ctrl_client = MagicMock()
    ctrl_client.list_online_evaluation_configs.return_value = {
        "onlineEvaluationConfigs": [
            {
                "onlineEvaluationConfigName": f"eval_{runtime_id[:32]}",
                "onlineEvaluationConfigId": "ec-other",
            }
        ]
    }
    ctrl_client.get_online_evaluation_config.return_value = {
        "onlineEvaluationConfigName": f"eval_{runtime_id[:32]}",
        "onlineEvaluationConfigId": "ec-other",
        "dataSourceConfig": {
            "cloudWatchLogs": {
                "serviceNames": [other_runtime],
                "logGroupNames": [
                    f"/aws/bedrock-agentcore/runtimes/{other_runtime}-DEFAULT",
                ],
            }
        },
    }
    with patch(
        "app.routers.evaluations.resolve_owned_runtime_target",
        return_value=_target(runtime_id, ctrl=ctrl_client),
    ):
        resp = client.get("/api/runtimes/myagent/evaluation-config")

    assert resp.status_code == 404


def test_evaluation_config_log_group_match_is_exact_not_a_substring(client: TestClient):
    runtime_id = "runtime-one"
    ctrl_client = MagicMock()
    ctrl_client.list_online_evaluation_configs.return_value = {
        "onlineEvaluationConfigs": [
            {
                "onlineEvaluationConfigName": "custom",
                "onlineEvaluationConfigId": "ec-other",
            }
        ]
    }
    ctrl_client.get_online_evaluation_config.return_value = {
        "onlineEvaluationConfigName": "custom",
        "onlineEvaluationConfigId": "ec-other",
        "dataSourceConfig": {
            "cloudWatchLogs": {
                "serviceNames": [],
                "logGroupNames": [
                    f"/aws/bedrock-agentcore/runtimes/{runtime_id}-suffix-DEFAULT",
                ],
            }
        },
    }
    with patch(
        "app.routers.evaluations.resolve_owned_runtime_target",
        return_value=_target(runtime_id, ctrl=ctrl_client),
    ):
        resp = client.get("/api/runtimes/myagent/evaluation-config")

    assert resp.status_code == 404


def test_evaluation_config_rejects_string_shaped_target_lists(client: TestClient):
    runtime_id = "runtime-one"
    ctrl_client = MagicMock()
    ctrl_client.list_online_evaluation_configs.return_value = {
        "onlineEvaluationConfigs": [
            {
                "onlineEvaluationConfigName": "custom",
                "onlineEvaluationConfigId": "ec-other",
            }
        ]
    }
    ctrl_client.get_online_evaluation_config.return_value = {
        "onlineEvaluationConfigName": "custom",
        "onlineEvaluationConfigId": "ec-other",
        "dataSourceConfig": {
            "cloudWatchLogs": {
                # A string would turn ``runtime_id in service_names`` back into
                # substring matching if response shapes were not validated.
                "serviceNames": f"prefix-{runtime_id}-suffix",
                "logGroupNames": [],
            }
        },
    }
    with patch(
        "app.routers.evaluations.resolve_owned_runtime_target",
        return_value=_target(runtime_id, ctrl=ctrl_client),
    ):
        resp = client.get("/api/runtimes/myagent/evaluation-config")

    assert resp.status_code == 404


def test_malformed_evaluation_config_detail_fails_sanitized(client: TestClient):
    ctrl_client = MagicMock()
    ctrl_client.list_online_evaluation_configs.return_value = {
        "onlineEvaluationConfigs": [
            {
                "onlineEvaluationConfigName": "custom",
                "onlineEvaluationConfigId": "ec-bad",
            }
        ]
    }
    ctrl_client.get_online_evaluation_config.return_value = ["not", "an", "object"]
    with patch(
        "app.routers.evaluations.resolve_owned_runtime_target",
        return_value=_target("runtime-one", ctrl=ctrl_client),
    ):
        resp = client.get("/api/runtimes/myagent/evaluation-config")

    assert resp.status_code == 503
    assert resp.json()["detail"] == "Runtime observability is temporarily unavailable. Try again shortly."


def test_multiple_custom_configs_for_one_runtime_fail_closed(client: TestClient):
    runtime_id = "runtime-one"
    ctrl_client = MagicMock()
    ctrl_client.list_online_evaluation_configs.return_value = {
        "onlineEvaluationConfigs": [
            {
                "onlineEvaluationConfigName": "custom-one",
                "onlineEvaluationConfigId": "ec-1",
            },
            {
                "onlineEvaluationConfigName": "custom-two",
                "onlineEvaluationConfigId": "ec-2",
            },
        ]
    }
    ctrl_client.get_online_evaluation_config.side_effect = [
        {
            "onlineEvaluationConfigName": "custom-one",
            "dataSourceConfig": {
                "cloudWatchLogs": {
                    "serviceNames": [runtime_id],
                }
            },
        },
        {
            "onlineEvaluationConfigName": "custom-two",
            "dataSourceConfig": {
                "cloudWatchLogs": {
                    "serviceNames": [runtime_id],
                }
            },
        },
    ]
    with patch(
        "app.routers.evaluations.resolve_owned_runtime_target",
        return_value=_target(runtime_id, ctrl=ctrl_client),
    ):
        resp = client.get("/api/runtimes/myagent/evaluation-config")

    assert resp.status_code == 503
    assert resp.json()["detail"] == "Runtime observability is temporarily unavailable. Try again shortly."


def test_evaluation_config_404_when_no_match(client: TestClient):
    """No matching config → 404 (not 500)."""
    runtime_id = "myagent_abcd1234-rt-xyz"
    ctrl_client = MagicMock()
    with patch(
        "app.routers.evaluations.resolve_owned_runtime_target",
        return_value=_target(runtime_id, ctrl=ctrl_client),
    ):
        ctrl_client.list_online_evaluation_configs.return_value = {
            "onlineEvaluationConfigs": [
                {
                    "onlineEvaluationConfigName": "eval_unrelated_other_agent",
                    "onlineEvaluationConfigId": "ec-other",
                }
            ]
        }
        ctrl_client.get_online_evaluation_config.return_value = {
            "onlineEvaluationConfigName": "eval_unrelated_other_agent",
            "dataSourceConfig": {
                "cloudWatchLogs": {
                    "serviceNames": ["unrelated-runtime"],
                    "logGroupNames": [],
                }
            },
        }
        resp = client.get("/api/runtimes/myagent/evaluation-config")
    assert resp.status_code == 404


def test_invalid_runtime_name_rejected(client: TestClient):
    """Names that don't match the AgentCore regex are rejected at 400."""
    resp = client.get("/api/runtimes/has-hyphens/evaluation-config")
    assert resp.status_code == 400


def test_evaluation_results_handles_missing_log_group(client: TestClient):
    """If the runtime hasn't received traffic yet, the log group doesn't exist
    yet — treat that as "no results", not a 500."""
    logs_client = MagicMock()
    ctrl_client = MagicMock()
    ctrl_client.list_online_evaluation_configs.return_value = {"onlineEvaluationConfigs": []}
    with patch(
        "app.routers.evaluations.resolve_owned_runtime_target",
        return_value=_target("rt-xyz", ctrl=ctrl_client, logs=logs_client),
    ):

        class _RNF(Exception):
            pass

        logs_client.exceptions.ResourceNotFoundException = _RNF
        logs_client.start_query.side_effect = _RNF()

        resp = client.get("/api/runtimes/myagent/evaluations")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"] == []
    assert "No evaluation log group" in (body.get("message") or "")


def test_evaluation_results_use_a_custom_configs_exact_result_group(client: TestClient):
    runtime_id = "runtime-one"
    ctrl_client = MagicMock()
    ctrl_client.list_online_evaluation_configs.return_value = {
        "onlineEvaluationConfigs": [
            {
                "onlineEvaluationConfigName": "custom",
                "onlineEvaluationConfigId": "ec-custom",
            }
        ]
    }
    ctrl_client.get_online_evaluation_config.return_value = {
        "onlineEvaluationConfigName": "custom",
        "dataSourceConfig": {
            "cloudWatchLogs": {
                "serviceNames": [runtime_id],
            }
        },
    }
    logs_client = MagicMock()
    logs_client.start_query.return_value = {"queryId": "query-1"}
    logs_client.get_query_results.return_value = {
        "status": "Complete",
        "results": [],
    }
    with patch(
        "app.routers.evaluations.resolve_owned_runtime_target",
        return_value=_target(runtime_id, ctrl=ctrl_client, logs=logs_client),
    ):
        resp = client.get("/api/runtimes/myagent/evaluations")

    assert resp.status_code == 200
    assert resp.json()["log_group_name"] == ("/aws/bedrock-agentcore/evaluations/results/ec-custom")
    assert logs_client.start_query.call_args.kwargs["logGroupName"] == (
        "/aws/bedrock-agentcore/evaluations/results/ec-custom"
    )


def test_evaluation_config_list_failure_is_sanitized_and_fails_closed(client: TestClient):
    ctrl_client = MagicMock()
    ctrl_client.list_online_evaluation_configs.side_effect = _aws_error(
        "AccessDeniedException",
        "ListOnlineEvaluationConfigs",
        "role arn:aws:iam::222222222222:role/private denied; request sensitive-request-id",
    )
    with patch(
        "app.routers.evaluations.resolve_owned_runtime_target",
        return_value=_target("rt-xyz", ctrl=ctrl_client),
    ):
        resp = client.get("/api/runtimes/myagent/evaluation-config")

    assert resp.status_code == 503
    assert resp.json()["detail"] == "Runtime observability is temporarily unavailable. Try again shortly."
    assert "222222222222" not in resp.text
    assert "sensitive-request-id" not in resp.text


def test_evaluation_config_fallback_does_not_turn_access_denied_into_404(client: TestClient):
    runtime_id = "myagent_abcd1234-runtime-abcd1234"
    ctrl_client = MagicMock()
    ctrl_client.list_online_evaluation_configs.return_value = {
        "onlineEvaluationConfigs": [
            {
                "onlineEvaluationConfigName": "custom-name",
                "onlineEvaluationConfigId": "ec-private",
            }
        ]
    }
    ctrl_client.get_online_evaluation_config.side_effect = _aws_error(
        "AccessDeniedException",
        "GetOnlineEvaluationConfig",
    )
    with patch(
        "app.routers.evaluations.resolve_owned_runtime_target",
        return_value=_target(runtime_id, ctrl=ctrl_client),
    ):
        resp = client.get("/api/runtimes/myagent/evaluation-config")

    assert resp.status_code == 503
    assert "sensitive" not in resp.text


def test_evaluation_results_config_lookup_failure_does_not_query_a_fallback_group(client: TestClient):
    ctrl_client = MagicMock()
    logs_client = MagicMock()
    ctrl_client.list_online_evaluation_configs.side_effect = _aws_error(
        "AccessDeniedException",
        "ListOnlineEvaluationConfigs",
    )
    with patch(
        "app.routers.evaluations.resolve_owned_runtime_target",
        return_value=_target("rt-xyz", ctrl=ctrl_client, logs=logs_client),
    ):
        resp = client.get("/api/runtimes/myagent/evaluations")

    assert resp.status_code == 503
    logs_client.start_query.assert_not_called()


def test_evaluation_query_failure_never_returns_the_aws_exception(client: TestClient):
    ctrl_client = MagicMock()
    ctrl_client.list_online_evaluation_configs.return_value = {"onlineEvaluationConfigs": []}
    logs_client = MagicMock()
    logs_client.start_query.side_effect = _aws_error(
        "AccessDeniedException",
        "StartQuery",
        "log-group arn:aws:logs:eu-west-1:222222222222:log-group:private request sensitive-request-id",
    )
    with patch(
        "app.routers.evaluations.resolve_owned_runtime_target",
        return_value=_target("rt-xyz", ctrl=ctrl_client, logs=logs_client),
    ):
        resp = client.get("/api/runtimes/myagent/evaluations")

    assert resp.status_code == 503
    assert "222222222222" not in resp.text
    assert "sensitive-request-id" not in resp.text


def test_evaluation_query_poll_failure_never_returns_the_aws_exception(client: TestClient):
    ctrl_client = MagicMock()
    ctrl_client.list_online_evaluation_configs.return_value = {"onlineEvaluationConfigs": []}
    logs_client = MagicMock()
    logs_client.start_query.return_value = {"queryId": "query-1"}
    logs_client.get_query_results.side_effect = _aws_error(
        "AccessDeniedException",
        "GetQueryResults",
        "query in account 222222222222 denied; request sensitive-request-id",
    )
    with patch(
        "app.routers.evaluations.resolve_owned_runtime_target",
        return_value=_target("rt-xyz", ctrl=ctrl_client, logs=logs_client),
    ):
        resp = client.get("/api/runtimes/myagent/evaluations")

    assert resp.status_code == 503
    assert "222222222222" not in resp.text
    assert "sensitive-request-id" not in resp.text


@pytest.mark.parametrize("terminal_status", ["Failed", "Cancelled"])
def test_evaluation_query_failure_status_is_not_returned_as_http_200(
    client: TestClient,
    terminal_status: str,
):
    ctrl_client = MagicMock()
    ctrl_client.list_online_evaluation_configs.return_value = {"onlineEvaluationConfigs": []}
    logs_client = MagicMock()
    logs_client.start_query.return_value = {"queryId": "query-1"}
    logs_client.get_query_results.return_value = {
        "status": terminal_status,
        "results": [],
    }
    with patch(
        "app.routers.evaluations.resolve_owned_runtime_target",
        return_value=_target("rt-xyz", ctrl=ctrl_client, logs=logs_client),
    ):
        resp = client.get("/api/runtimes/myagent/evaluations")

    assert resp.status_code == 503
    assert resp.json()["detail"] == "Runtime observability is temporarily unavailable. Try again shortly."


def test_dashboard_not_found_is_the_only_failure_reported_as_missing(client: TestClient):
    cloudwatch = MagicMock()
    cloudwatch.get_dashboard.side_effect = _aws_error(
        "ResourceNotFound",
        "GetDashboard",
    )
    with patch(
        "app.routers.evaluations.resolve_owned_runtime_target",
        return_value=_target("rt-xyz", cloudwatch=cloudwatch),
    ):
        resp = client.get("/api/runtimes/myagent/dashboard-url")

    assert resp.status_code == 200
    assert resp.json()["exists"] is False


def test_dashboard_permission_failure_is_not_misreported_as_missing(client: TestClient):
    cloudwatch = MagicMock()
    cloudwatch.get_dashboard.side_effect = _aws_error(
        "AccessDenied",
        "GetDashboard",
        "dashboard private-dashboard in account 222222222222; request sensitive-request-id",
    )
    with patch(
        "app.routers.evaluations.resolve_owned_runtime_target",
        return_value=_target("rt-xyz", cloudwatch=cloudwatch),
    ):
        resp = client.get("/api/runtimes/myagent/dashboard-url")

    assert resp.status_code == 503
    assert "222222222222" not in resp.text
    assert "sensitive-request-id" not in resp.text
