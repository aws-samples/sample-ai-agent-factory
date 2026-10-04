"""The unjournaled in-process deploy implementation cannot become public again."""

from inspect import getsource
from unittest.mock import MagicMock, call

import app.services as services
from app.services import deployment


def test_workflow_executor_is_not_a_package_level_service():
    assert not hasattr(services, "WorkflowExecutor")
    assert "WorkflowExecutor" not in services.__all__
    assert "Legacy, unsupported" in deployment.WorkflowExecutor.__doc__


def test_legacy_executor_has_no_unpaginated_agentcore_list_calls():
    source = getsource(deployment)
    for operation in (
        "list_policies",
        "list_policy_engines",
        "list_memories",
        "list_agent_runtime_endpoints",
        "list_gateways",
        "list_gateway_targets",
    ):
        assert f".{operation}(" not in source


def test_legacy_policy_wait_finds_an_active_policy_on_page_two():
    client = MagicMock()
    client.list_policies.side_effect = [
        {
            "policies": [{"name": "other", "status": "ACTIVE"}],
            "nextToken": "page-2",
        },
        {
            "policySummaries": [
                {"name": "wanted", "status": "ACTIVE"},
            ]
        },
    ]

    deployment._await_policies_active(client, "engine-1", ["wanted"])

    assert client.list_policies.call_args_list == [
        call(policyEngineId="engine-1", maxResults=100),
        call(
            policyEngineId="engine-1",
            maxResults=100,
            nextToken="page-2",
        ),
    ]
