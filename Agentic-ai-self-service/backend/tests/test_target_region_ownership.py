"""Cross-account steps must journal at home and tag resources for the target."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from app.services.resource_ownership import (
    OWNER_TAG_KEY,
    ForeignResourceError,
    owner_tag_list,
    stack_id,
)

HOME_REGION = "us-east-1"
TARGET_REGION = "eu-west-1"


class _AlreadyExists(Exception):
    pass


class _IamExceptions:
    EntityAlreadyExistsException = _AlreadyExists
    NoSuchEntityException = type("NoSuchEntityException", (Exception,), {})


@pytest.fixture(autouse=True)
def _stack(monkeypatch):
    monkeypatch.setenv("PROJECT_NAME", "target-region-tests")
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("APP_AWS_REGION", HOME_REGION)


def _tag_map(call) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in call.kwargs["Tags"]}


def test_knowledge_base_role_is_tagged_for_the_target_region(monkeypatch):
    from app.step_handlers import knowledge_base_step

    iam = MagicMock()
    iam.exceptions = _IamExceptions()
    iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::123456789012:role/AgentCoreKBRole-audit"}}
    monkeypatch.setattr(knowledge_base_step.time, "sleep", lambda _seconds: None)

    knowledge_base_step._create_kb_role(
        iam,
        "AgentCoreKBRole-audit",
        {},
        TARGET_REGION,
        account_id="123456789012",
    )

    tags = _tag_map(iam.create_role.call_args)
    assert tags[OWNER_TAG_KEY] == stack_id(TARGET_REGION)
    assert tags[OWNER_TAG_KEY] != stack_id(HOME_REGION)


def test_knowledge_base_role_collision_is_checked_against_target_region(monkeypatch):
    from app.step_handlers import knowledge_base_step

    iam = MagicMock()
    iam.exceptions = _IamExceptions()
    iam.create_role.side_effect = _AlreadyExists("exists")
    iam.get_role.return_value = {
        "Role": {
            "Arn": "arn:aws:iam::123456789012:role/AgentCoreKBRole-audit",
            "Tags": owner_tag_list(HOME_REGION),
        }
    }
    monkeypatch.setattr(knowledge_base_step.time, "sleep", lambda _seconds: None)

    with pytest.raises(ForeignResourceError):
        knowledge_base_step._create_kb_role(
            iam,
            "AgentCoreKBRole-audit",
            {},
            TARGET_REGION,
            account_id="123456789012",
        )

    iam.put_role_policy.assert_not_called()


def _evaluation_clients(iam, control):
    def _client(_event, service, **_kwargs):
        if service == "iam":
            return iam
        if service == "bedrock-agentcore-control":
            return control
        raise AssertionError(f"unexpected service: {service}")

    return _client


def _evaluation_event() -> dict:
    return {
        "deployment_id": "dep-target-region",
        "target_region": TARGET_REGION,
        "runtime_id": "runtime-123456789012",
        "runtime_arn": ("arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/runtime-123456789012"),
        "evaluation_config": {"enabled": True},
    }


def test_evaluation_role_is_tagged_for_the_target_region(monkeypatch):
    from app.step_handlers import evaluation_step

    store = MagicMock()
    iam = MagicMock()
    iam.exceptions = _IamExceptions()
    iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::123456789012:role/AgentCoreEval-runtime"}}
    control = MagicMock()
    control.create_online_evaluation_config.return_value = {"onlineEvaluationConfigId": "eval-123"}
    monkeypatch.setattr(evaluation_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(
        evaluation_step.step_clients,
        "client",
        _evaluation_clients(iam, control),
    )
    monkeypatch.setattr(evaluation_step.time, "sleep", lambda _seconds: None)

    result = evaluation_step.handler(_evaluation_event(), None)

    tags = _tag_map(iam.create_role.call_args)
    assert tags[OWNER_TAG_KEY] == stack_id(TARGET_REGION)
    assert result["evaluation_result"]["config_id"] == "eval-123"


def test_evaluation_conflict_recovers_the_config_from_page_two(monkeypatch):
    from app.step_handlers import evaluation_step

    store = MagicMock()
    iam = MagicMock()
    iam.exceptions = _IamExceptions()
    iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::123456789012:role/AgentCoreEval-runtime"}}
    control = MagicMock()
    control.create_online_evaluation_config.side_effect = Exception(
        "ConflictException: online evaluation config already exists"
    )
    control.list_online_evaluation_configs.side_effect = [
        {
            "onlineEvaluationConfigs": [
                {
                    "onlineEvaluationConfigName": "other",
                    "onlineEvaluationConfigId": "eval-other",
                }
            ],
            "nextToken": "page-2",
        },
        {
            "onlineEvaluationConfigs": [
                {
                    "onlineEvaluationConfigName": "paged_eval",
                    "onlineEvaluationConfigId": "eval-paged",
                }
            ]
        },
    ]
    monkeypatch.setattr(evaluation_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(
        evaluation_step.step_clients,
        "client",
        _evaluation_clients(iam, control),
    )
    monkeypatch.setattr(evaluation_step.time, "sleep", lambda _seconds: None)
    event = _evaluation_event()
    event["evaluation_config"] = {
        "enabled": True,
        "name": "paged_eval",
    }

    result = evaluation_step.handler(event, None)

    assert result["evaluation_result"]["config_id"] == "eval-paged"
    assert [invocation.kwargs for invocation in control.list_online_evaluation_configs.call_args_list] == [
        {"maxResults": 50},
        {"maxResults": 50, "nextToken": "page-2"},
    ]
    assert all(
        "agentId" not in invocation.kwargs for invocation in control.list_online_evaluation_configs.call_args_list
    )


def test_evaluation_refuses_to_pass_a_foreign_role_to_agentcore(monkeypatch):
    from app.step_handlers import evaluation_step

    store = MagicMock()
    iam = MagicMock()
    iam.exceptions = _IamExceptions()
    iam.create_role.side_effect = _AlreadyExists("exists")
    iam.get_role.return_value = {
        "Role": {
            "Arn": "arn:aws:iam::123456789012:role/AgentCoreEval-runtime",
            "Tags": owner_tag_list(HOME_REGION),
        }
    }
    control = MagicMock()
    monkeypatch.setattr(evaluation_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(
        evaluation_step.step_clients,
        "client",
        _evaluation_clients(iam, control),
    )

    with pytest.raises(ForeignResourceError):
        evaluation_step.handler(_evaluation_event(), None)

    iam.put_role_policy.assert_not_called()
    control.create_online_evaluation_config.assert_not_called()


def test_memory_uses_target_region_for_tags_manifest_and_home_journal(monkeypatch):
    from app.step_handlers import memory_step

    store = MagicMock()
    iam = MagicMock()
    iam.exceptions = _IamExceptions()
    iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::123456789012:role/AgentCoreMemory-audit"}}
    control = MagicMock()
    control.list_memories.return_value = {"memories": []}
    control.create_memory.return_value = {"memoryId": "memory-123456789012"}
    control.get_memory.return_value = {"status": "ACTIVE"}

    requested_services: list[str] = []

    def _client(_event, service, **_kwargs):
        requested_services.append(service)
        if service == "iam":
            return iam
        if service == "bedrock-agentcore-control":
            return control
        raise AssertionError(f"state must not be written through target service {service}")

    monkeypatch.setattr(memory_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(memory_step.step_clients, "client", _client)
    monkeypatch.setattr(memory_step.time, "sleep", lambda _seconds: None)

    result = memory_step.handler(
        {
            "deployment_id": "dep-target-region",
            "owner_sub": "owner-target-region",
            "target_account_id": "123456789012",
            "target_region": TARGET_REGION,
            "memory_config": {"name": "audit"},
        },
        None,
    )

    tags = _tag_map(iam.create_role.call_args)
    assert tags[OWNER_TAG_KEY] == stack_id(TARGET_REGION)
    assert "dynamodb" not in requested_services
    assert result["memory_result"]["memory_id"] == "memory-123456789012"
    assert any(call.args[1].get("region") == TARGET_REGION for call in store.record_resource.call_args_list)
    store.update_status.assert_called_once()
    assert store.update_status.call_args.kwargs["memory_result"] == result["memory_result"]


def test_runtime_launch_uses_target_region_for_manifest_and_dashboard_client(monkeypatch):
    from app.step_handlers import runtime_launch_step

    store = MagicMock()
    control = MagicMock()
    cloudwatch = MagicMock()
    dashboard_call: dict = {}

    def _client(_event, service, **kwargs):
        if service == "bedrock-agentcore-control":
            return control
        if service == "cloudwatch":
            assert kwargs["region_name"] == TARGET_REGION
            return cloudwatch
        raise AssertionError(f"unexpected service: {service}")

    def _put_dashboard(**kwargs):
        dashboard_call.update(kwargs)
        return "dashboard", "console-url"

    monkeypatch.setattr(runtime_launch_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(runtime_launch_step.step_clients, "client", _client)
    monkeypatch.setattr(
        runtime_launch_step,
        "wait_for_runtime_ready",
        lambda *_args, **_kwargs: {"success": True, "arn": "runtime-arn"},
    )
    monkeypatch.setattr(
        runtime_launch_step,
        "wait_for_default_endpoint_ready",
        lambda *_args, **_kwargs: {"success": True, "endpoint_arn": "endpoint-arn"},
    )
    monkeypatch.setattr(runtime_launch_step, "put_dashboard_for_runtime", _put_dashboard)

    result = runtime_launch_step.handler(
        {
            "deployment_id": "dep-target-region",
            "target_region": TARGET_REGION,
            "runtime_id": "runtime-123",
            "friendly_runtime_name": "audit",
        },
        None,
    )

    assert store.record_resource.call_args.args[1]["region"] == TARGET_REGION
    assert dashboard_call["region"] == TARGET_REGION
    assert dashboard_call["cloudwatch_client"] is cloudwatch
    assert result["runtime_endpoint"] == "endpoint-arn"


def test_runtime_configure_uses_target_region_for_model_and_manifest(monkeypatch):
    from app.step_handlers import runtime_configure_step

    store = MagicMock()
    control = MagicMock()
    logs = MagicMock()
    create_call: dict = {}
    governance_call: dict = {}
    order: list[str] = []

    def _create_runtime(**kwargs):
        create_call.update(kwargs)
        return {"runtime_id": "runtime-123", "arn": "runtime-arn"}

    def _client(_event, service, **kwargs):
        if service == "bedrock-agentcore-control":
            return control
        if service == "logs":
            assert kwargs["region_name"] == TARGET_REGION
            return logs
        pytest.fail(service)

    def _record_resource(*_args, **_kwargs):
        order.append("record")

    def _govern(logs_client, runtime_id):
        order.append("govern")
        governance_call.update(
            {
                "logs_client": logs_client,
                "runtime_id": runtime_id,
            }
        )

    store.record_resource.side_effect = _record_resource
    monkeypatch.setattr(runtime_configure_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(runtime_configure_step.step_clients, "client", _client)
    monkeypatch.setattr(runtime_configure_step, "create_agent_runtime", _create_runtime)
    monkeypatch.setattr(runtime_configure_step, "govern_default_runtime_log_group", _govern)
    monkeypatch.setattr(runtime_configure_step, "get_platform_observability_defaults", lambda: None)

    result = runtime_configure_step.handler(
        {
            "deployment_id": "dep-target-region",
            "target_region": TARGET_REGION,
            "config": {
                "name": "audit",
                "model": {"modelId": "us.anthropic.claude-sonnet-5"},
            },
            "role_arn": "arn:aws:iam::123456789012:role/AgentCoreRuntime-audit",
            "s3_bucket": "artifact-bucket",
            "s3_key": "code.zip",
        },
        None,
    )

    assert create_call["env_vars"]["MODEL_ID"].startswith("eu.")
    assert store.record_resource.call_args_list[0].args[1]["region"] == TARGET_REGION
    assert governance_call == {
        "logs_client": logs,
        "runtime_id": "runtime-123",
    }
    assert order[:2] == ["record", "govern"]
    assert result["runtime_id"] == "runtime-123"


def test_runtime_configure_keeps_the_teardown_handle_when_log_governance_fails(monkeypatch):
    from app.step_handlers import runtime_configure_step

    store = MagicMock()
    control = MagicMock()
    logs = MagicMock()
    failure = RuntimeError("retention could not be applied")

    def _client(_event, service, **kwargs):
        if service == "bedrock-agentcore-control":
            return control
        if service == "logs":
            assert kwargs["region_name"] == TARGET_REGION
            return logs
        pytest.fail(service)

    monkeypatch.setattr(runtime_configure_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(runtime_configure_step.step_clients, "client", _client)
    monkeypatch.setattr(
        runtime_configure_step,
        "create_agent_runtime",
        lambda **_kwargs: {
            "runtime_id": "runtime-governance-failure",
            "arn": "runtime-arn",
            "created_by_deployment": True,
        },
    )
    monkeypatch.setattr(
        runtime_configure_step,
        "govern_default_runtime_log_group",
        MagicMock(side_effect=failure),
    )
    monkeypatch.setattr(runtime_configure_step, "get_platform_observability_defaults", lambda: None)

    with pytest.raises(RuntimeError) as raised:
        runtime_configure_step.handler(
            {
                "deployment_id": "dep-governance-failure",
                "target_region": TARGET_REGION,
                "config": {
                    "name": "audit",
                    "model": {"modelId": "eu.anthropic.claude-sonnet-5"},
                },
                "role_arn": "arn:aws:iam::123456789012:role/AgentCoreRuntime-audit",
                "s3_bucket": "artifact-bucket",
                "s3_key": "code.zip",
            },
            None,
        )

    assert raised.value is failure
    runtime_row = store.record_resource.call_args_list[0].args[1]
    assert runtime_row == {
        "type": "agent_runtime",
        "id": "runtime-governance-failure",
        "name": "audit",
        "region": TARGET_REGION,
        "created_by_deployment": True,
    }


def test_iam_step_passes_target_region_to_runtime_role_creator(monkeypatch):
    from app.step_handlers import iam_step

    store = MagicMock()
    create_call: dict = {}

    def _create_role(**kwargs):
        create_call.update(kwargs)
        return "arn:aws:iam::123456789012:role/AgentCoreRuntime-audit"

    monkeypatch.delenv("SHARED_RUNTIME_ROLE_ARN", raising=False)
    monkeypatch.setattr(iam_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(iam_step, "create_runtime_iam_role", _create_role)
    monkeypatch.setattr(iam_step.step_clients, "client", lambda *_args, **_kwargs: MagicMock())
    monkeypatch.setattr(
        iam_step.step_clients,
        "account_id_for_event",
        lambda _event: "123456789012",
    )
    monkeypatch.setattr(iam_step, "get_platform_observability_defaults", lambda: None)

    result = iam_step.handler(
        {
            "deployment_id": "dep-target-region",
            "target_region": TARGET_REGION,
            "config": {
                "name": "audit",
                "model": {"modelId": "us.anthropic.claude-sonnet-5"},
            },
        },
        None,
    )

    assert create_call["region"] == TARGET_REGION
    assert result["role_arn"].endswith("/AgentCoreRuntime-audit")


def test_cross_account_iam_step_uses_the_registered_target_runtime_role(monkeypatch):
    from app.step_handlers import iam_step

    store = MagicMock()
    role_arn = "arn:aws:iam::123456789012:role/custom/StableRuntimeRole"
    monkeypatch.setattr(iam_step, "_get_deployment_store", lambda: store)
    monkeypatch.setenv(
        "SHARED_RUNTIME_ROLE_ARN",
        "arn:aws:iam::999999999999:role/HomeAccountRuntimeRole",
    )

    result = iam_step.handler(
        {
            "deployment_id": "dep-target-region",
            "target_account_id": "123456789012",
            "target_region": TARGET_REGION,
            "target_runtime_role_arn": role_arn,
            "config": {
                "name": "audit",
                "model": {"modelId": "us.anthropic.claude-sonnet-5"},
            },
        },
        None,
    )

    assert result["role_arn"] == role_arn
    assert result["role_name"] == "StableRuntimeRole"
    store.record_resource.assert_not_called()


def test_cross_account_iam_step_refuses_per_agent_role_creation(monkeypatch):
    from app.step_handlers import iam_step

    store = MagicMock()
    create_role = MagicMock()
    monkeypatch.setattr(iam_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(iam_step, "create_runtime_iam_role", create_role)

    with pytest.raises(RuntimeError, match="cannot mint.*per-agent"):
        iam_step.handler(
            {
                "deployment_id": "dep-target-region",
                "target_account_id": "123456789012",
                "target_region": TARGET_REGION,
                "identity_config": {"mode": "per_agent"},
                "config": {
                    "name": "audit",
                    "model": {"modelId": "us.anthropic.claude-sonnet-5"},
                },
            },
            None,
        )

    create_role.assert_not_called()


def test_harness_step_threads_target_region_into_role_and_manifest(monkeypatch):
    from app.step_handlers import harness_step

    store = MagicMock()
    role_call: dict = {}

    def _role(_iam, _name, **kwargs):
        role_call.update(kwargs)
        return "arn:aws:iam::123456789012:role/AgentCoreHarness-audit"

    monkeypatch.delenv("SHARED_HARNESS_ROLE_ARN", raising=False)
    monkeypatch.setattr(harness_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(harness_step.step_clients, "client", lambda *_args, **_kwargs: MagicMock())
    monkeypatch.setattr(harness_step.harness_deployer, "get_shared_or_new_harness_role", _role)
    monkeypatch.setattr(
        harness_step.harness_deployer,
        "create_harness",
        lambda *_args, **_kwargs: {"harness_id": "harness-123", "arn": "harness-arn"},
    )
    monkeypatch.setattr(
        harness_step.harness_deployer,
        "wait_for_harness_ready",
        lambda *_args, **_kwargs: {
            "success": True,
            "arn": "harness-arn",
            "backing_runtime_id": "harness_audit-AbCdEf1234",
        },
    )

    result = harness_step.handler(
        {
            "deployment_id": "dep-target-region",
            "target_region": TARGET_REGION,
            "config": {
                "name": "audit",
                "model": {"modelId": "us.anthropic.claude-sonnet-5"},
            },
        },
        None,
    )

    assert role_call["region"] == TARGET_REGION
    assert all(call.args[1]["region"] == TARGET_REGION for call in store.record_resource.call_args_list)
    assert result["harness_id"] == "harness-123"


def test_cross_account_harness_uses_target_role_not_home_shared_role(monkeypatch):
    from app.step_handlers import harness_step

    store = MagicMock()
    iam = MagicMock()
    control = MagicMock()
    secrets = MagicMock()
    cognito = MagicMock()
    home_role = "arn:aws:iam::999999999999:role/HomeAccountHarnessRole"
    target_role = "arn:aws:iam::123456789012:role/custom/StableHarnessRole"
    role_factory = MagicMock()
    ensure_provider = MagicMock(return_value=(None, []))
    create_harness = MagicMock(return_value={"harness_id": "harness-target", "arn": "harness-target-arn"})
    logs = MagicMock()
    logs_regions: list = []

    def _client(_event, service, **_kwargs):
        if service == "logs":
            logs_regions.append(_kwargs.get("region_name"))
            return logs
        if service == "iam":
            return iam
        if service == "bedrock-agentcore-control":
            return control
        if service == "secretsmanager":
            return secrets
        if service == "cognito-idp":
            return cognito
        raise AssertionError(service)

    monkeypatch.setenv("SHARED_HARNESS_ROLE_ARN", home_role)
    monkeypatch.setattr(harness_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(harness_step.step_clients, "client", _client)
    monkeypatch.setattr(
        harness_step.harness_deployer,
        "get_shared_or_new_harness_role",
        role_factory,
    )
    monkeypatch.setattr(
        harness_step.harness_deployer,
        "ensure_gateway_outbound_provider",
        ensure_provider,
    )
    monkeypatch.setattr(harness_step.harness_deployer, "create_harness", create_harness)
    monkeypatch.setattr(
        harness_step.harness_deployer,
        "wait_for_harness_ready",
        lambda *_args, **_kwargs: {
            "success": True,
            "arn": "harness-target-arn",
            "backing_runtime_id": "harness_audit-TgTgTgTgTg",
        },
    )

    result = harness_step.handler(
        {
            "deployment_id": "dep-target-region",
            "target_account_id": "123456789012",
            "target_region": TARGET_REGION,
            "target_harness_role_arn": target_role,
            "gateway_result": {
                "gateway_arn": ("arn:aws:bedrock-agentcore:eu-west-1:123456789012:gateway/gateway-target"),
                "client_info": {
                    "client_secret_ref": (
                        "arn:aws:secretsmanager:eu-west-1:123456789012:secret:agentcore-connector/gateway"
                    ),
                },
            },
            "config": {
                "name": "audit",
                "model": {"modelId": "us.anthropic.claude-sonnet-5"},
            },
        },
        None,
    )

    role_factory.assert_not_called()
    assert ensure_provider.call_args.kwargs["secrets_client"] is secrets
    assert ensure_provider.call_args.kwargs["cognito_client"] is cognito
    assert create_harness.call_args.args[2] == target_role
    assert all(call.args[1]["type"] != "iam_role" for call in store.record_resource.call_args_list)
    assert result["harness_result"]["role_arn"] == target_role


def test_failure_cleanup_falls_back_to_the_deployment_target_region(monkeypatch):
    from app.step_handlers import status_update_step

    store = MagicMock()
    state = MagicMock()
    state.model_dump.return_value = {"created_resources": [{"type": "memory", "id": "memory-123"}]}
    store.get.return_value = state
    calls: list[tuple[dict, str, dict]] = []
    monkeypatch.setattr(
        status_update_step,
        "_cleanup_resource",
        lambda resource, region, event: calls.append((resource, region, event)),
    )

    event = {
        "deployment_id": "dep-target-region",
        "target_region": TARGET_REGION,
    }
    status_update_step._auto_cleanup_on_failure(store, "dep-target-region", event)

    assert len(calls) == 1
    resource, region, cleanup_event = calls[0]
    assert resource == {"type": "memory", "id": "memory-123"}
    assert region == TARGET_REGION
    assert cleanup_event["deployment_id"] == event["deployment_id"]
    assert cleanup_event["target_region"] == event["target_region"]
    assert isinstance(cleanup_event["_cleanup_deadline_monotonic"], float)
