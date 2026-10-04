"""Model-free contracts for both live-deploy MCP runtime shapes.

The generated FastMCP source does not call a model.  These tests drive the real
Step Functions handlers so a green control-plane deployment cannot silently
retain MODEL_ID, provider credentials, Bedrock model permissions, or the heavy
Strands fallback that previously missed the Gateway discovery deadline.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, call

import pytest

_MODEL_ROLE_ARN = "arn:aws:iam::123456789012:role/AgentCoreRuntime-model-shared"
_MCP_ROLE_ARN = "arn:aws:iam::123456789012:role/AgentCoreRuntime-mcp-shared"
_PROVIDER_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-connector/owner/mcp-AbCdEf"


def _actions(policy: dict) -> set[str]:
    result: set[str] = set()
    for statement in policy.get("Statement", []):
        value = statement.get("Action", [])
        result.update(value if isinstance(value, list) else [value])
    return result


def _capture_runtime_create(monkeypatch, *, artifact_kind: str) -> dict:
    from app.step_handlers import runtime_configure_step

    store = MagicMock()
    captured: dict = {}

    monkeypatch.setattr(runtime_configure_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(
        runtime_configure_step.step_clients,
        "client",
        lambda _event, _service, **_kwargs: MagicMock(),
    )
    monkeypatch.setattr(runtime_configure_step, "build_otel_env_vars", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        runtime_configure_step,
        "get_platform_observability_defaults",
        lambda: {},
    )
    monkeypatch.setattr(
        runtime_configure_step,
        "govern_default_runtime_log_group",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.delenv("TAG_POLICY_TABLE_NAME", raising=False)

    def _create_runtime(**kwargs):
        captured.update(kwargs)
        return {
            "runtime_id": "standalone_mcp-AbCdEf1234",
            "arn": ("arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/standalone_mcp-AbCdEf1234"),
            "created_by_deployment": True,
        }

    monkeypatch.setattr(runtime_configure_step, "create_agent_runtime", _create_runtime)
    runtime_configure_step.handler(
        {
            "deployment_id": "dep-standalone-mcp",
            "runtime_artifact_kind": artifact_kind,
            "config": {
                "name": "standalone_mcp",
                "model": {"modelId": "gpt-5"},
                "modelProvider": "openai",
                "providerApiKeyRef": _PROVIDER_SECRET_ARN,
                "protocol": "MCP" if artifact_kind == "mcp" else "HTTP",
                "entrypoint": "agent.py",
                "enableOtel": False,
            },
            "role_arn": _MCP_ROLE_ARN if artifact_kind == "mcp" else _MODEL_ROLE_ARN,
            "s3_bucket": "runtime-artifacts",
            "s3_key": "deployments/standalone/code.zip",
        },
        None,
    )
    return captured


def test_standalone_mcp_live_runtime_has_no_model_or_provider_environment(monkeypatch):
    create = _capture_runtime_create(monkeypatch, artifact_kind="mcp")
    env = create.get("env_vars") or {}

    assert create["protocol"] == "MCP"
    assert "MODEL_ID" not in env
    assert "PROVIDER_API_KEY_SECRET_ARN" not in env
    assert "PROVIDER_BASE_URL" not in env


def test_http_agent_control_keeps_its_model_environment(monkeypatch):
    create = _capture_runtime_create(monkeypatch, artifact_kind="strands")
    env = create.get("env_vars") or {}

    assert create["protocol"] == "HTTP"
    assert env["MODEL_ID"] == "gpt-5"
    assert env["PROVIDER_API_KEY_SECRET_ARN"] == _PROVIDER_SECRET_ARN


def _shared_role_for(monkeypatch, *, artifact_kind: str) -> str:
    from app.step_handlers import iam_step

    monkeypatch.setattr(iam_step, "_get_deployment_store", MagicMock)
    monkeypatch.setattr(iam_step, "get_platform_observability_defaults", lambda: {})
    monkeypatch.setenv("SHARED_RUNTIME_ROLE_ARN", _MODEL_ROLE_ARN)
    monkeypatch.setenv("SHARED_MCP_RUNTIME_ROLE_ARN", _MCP_ROLE_ARN)

    result = iam_step.handler(
        {
            "deployment_id": f"dep-{artifact_kind}",
            "runtime_artifact_kind": artifact_kind,
            "config": {
                "name": f"{artifact_kind}_runtime",
                "model": {"modelId": "us.anthropic.claude-sonnet-5"},
                "modelProvider": "bedrock",
            },
        },
        None,
    )
    return result["role_arn"]


def test_shared_role_selection_uses_the_model_free_role_for_mcp(monkeypatch):
    assert _shared_role_for(monkeypatch, artifact_kind="mcp") == _MCP_ROLE_ARN


def test_shared_role_selection_keeps_the_model_role_for_agents(monkeypatch):
    assert _shared_role_for(monkeypatch, artifact_kind="strands") == _MODEL_ROLE_ARN


@pytest.mark.parametrize(
    ("artifact_kind", "expects_model_access"),
    [("mcp", False), ("strands", True)],
)
def test_per_agent_policy_matches_the_runtime_artifact(
    monkeypatch,
    artifact_kind,
    expects_model_access,
):
    from app.step_handlers import iam_step

    store = MagicMock()
    iam = MagicMock()
    iam.exceptions.EntityAlreadyExistsException = type(
        "EntityAlreadyExistsException",
        (Exception,),
        {},
    )
    iam.get_role.return_value = {
        "Role": {"Arn": (f"arn:aws:iam::123456789012:role/AgentCoreRuntime-{artifact_kind}_runtime")}
    }

    monkeypatch.setattr(iam_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(iam_step, "get_platform_observability_defaults", lambda: {})
    monkeypatch.setattr(
        iam_step.step_clients,
        "account_id_for_event",
        lambda _event: "123456789012",
    )
    monkeypatch.setattr(
        iam_step.step_clients,
        "client",
        lambda _event, service, **_kwargs: iam if service == "iam" else pytest.fail(service),
    )
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("ARTIFACTS_BUCKET_NAME", "runtime-artifacts")

    iam_step.handler(
        {
            "deployment_id": f"dep-per-agent-{artifact_kind}",
            "runtime_artifact_kind": artifact_kind,
            "identity_config": {"mode": "per_agent"},
            "agentcore_runtime_name": f"{artifact_kind}_runtime",
            "config": {
                "name": f"{artifact_kind}_runtime",
                "model": {"modelId": "us.anthropic.claude-sonnet-5"},
                "modelProvider": "bedrock",
            },
            "connected_tools": [],
        },
        None,
    )

    policy = json.loads(iam.put_role_policy.call_args.kwargs["PolicyDocument"])
    actions = _actions(policy)
    has_model_access = {
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
    }.issubset(actions)
    assert has_model_access is expects_model_access


def test_hosted_mcp_server_role_is_model_free(monkeypatch):
    from app.step_handlers import mcp_server_step

    store = MagicMock()
    upload_s3 = MagicMock()
    dependency_s3 = MagicMock()
    dependency_s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"lean-mcp-dependencies")}
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456789012"}
    iam = MagicMock()
    iam.exceptions.EntityAlreadyExistsException = type(
        "EntityAlreadyExistsException",
        (Exception,),
        {},
    )
    iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::123456789012:role/AgentCoreMCP-order_tools"}}
    cognito = MagicMock()
    cognito.create_user_pool.side_effect = RuntimeError("stop after role")

    clients = {
        "s3": upload_s3,
        "sts": sts,
        "iam": iam,
        "cognito-idp": cognito,
    }
    monkeypatch.setattr(mcp_server_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(
        mcp_server_step.step_clients,
        "client",
        lambda _event, service, **_kwargs: clients[service],
    )
    monkeypatch.setattr(
        mcp_server_step.boto3,
        "client",
        lambda service, **_kwargs: dependency_s3 if service == "s3" else pytest.fail(service),
    )
    monkeypatch.setattr(
        mcp_server_step,
        "generate_mcp_server_code",
        lambda **_kwargs: "from mcp.server.fastmcp import FastMCP",
    )
    monkeypatch.setattr(
        mcp_server_step,
        "upload_code_to_s3",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setenv("ARTIFACTS_BUCKET_NAME", "platform-artifacts")
    monkeypatch.setenv("APP_AWS_REGION", "us-east-1")

    with pytest.raises(RuntimeError, match="stop after role"):
        mcp_server_step.handler(
            {
                "deployment_id": "dep-hosted-mcp",
                "owner_sub": "owner",
                "target_region": "us-east-1",
                "target_artifact_bucket": "runtime-artifacts",
                "agentcore_runtime_name": "client_agent",
                "mcp_server_config": {
                    "name": "order_tools",
                    "tools": [],
                },
                "gateway_config": {"name": "orders"},
            },
            None,
        )

    policies = [json.loads(item.kwargs["PolicyDocument"]) for item in iam.put_role_policy.call_args_list]
    assert policies
    actions = set().union(*(_actions(policy) for policy in policies))
    assert "bedrock:InvokeModel" not in actions
    assert "bedrock:InvokeModelWithResponseStream" not in actions


def test_hosted_mcp_server_never_falls_back_to_the_heavy_strands_bundle(
    monkeypatch,
):
    from app.step_handlers import mcp_server_step

    store = MagicMock()
    upload_s3 = MagicMock()
    dependency_s3 = MagicMock()

    def _dependency(Bucket, Key):
        if Key == "agentcore-deps/mcp-lean.zip":
            raise RuntimeError("lean bundle missing")
        if Key == "agentcore-deps/strands-mcp.zip":
            return {"Body": MagicMock(read=lambda: b"heavy-strands-bundle")}
        raise AssertionError(Key)

    dependency_s3.get_object.side_effect = _dependency
    upload = MagicMock(side_effect=AssertionError("heavy Strands fallback reached upload"))

    monkeypatch.setattr(mcp_server_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(
        mcp_server_step.step_clients,
        "client",
        lambda _event, service, **_kwargs: upload_s3 if service == "s3" else pytest.fail(service),
    )
    monkeypatch.setattr(
        mcp_server_step.boto3,
        "client",
        lambda service, **_kwargs: dependency_s3 if service == "s3" else pytest.fail(service),
    )
    monkeypatch.setattr(
        mcp_server_step,
        "generate_mcp_server_code",
        lambda **_kwargs: "from mcp.server.fastmcp import FastMCP",
    )
    monkeypatch.setattr(mcp_server_step, "upload_code_to_s3", upload)
    monkeypatch.setenv("ARTIFACTS_BUCKET_NAME", "platform-artifacts")
    monkeypatch.setenv("APP_AWS_REGION", "us-east-1")

    with pytest.raises(RuntimeError, match="mcp-lean"):
        mcp_server_step.handler(
            {
                "deployment_id": "dep-no-heavy-fallback",
                "target_region": "us-east-1",
                "target_artifact_bucket": "runtime-artifacts",
                "agentcore_runtime_name": "client_agent",
                "mcp_server_config": {"name": "order_tools", "tools": []},
            },
            None,
        )

    assert dependency_s3.get_object.call_args_list == [
        call(
            Bucket="platform-artifacts",
            Key="agentcore-deps/mcp-lean.zip",
        )
    ]
    upload.assert_not_called()
