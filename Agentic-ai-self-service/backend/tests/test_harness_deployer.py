"""Unit tests for services/harness_deployer.py (Phase B authoring path).

The AgentCore Harness control plane is fully mocked here (MagicMock control
client + patched data-plane client), so these tests need no AWS credentials and
assert against the LIVE-VERIFIED API shapes (Bug 148):

  - create/get_harness wrap the resource in a ``{"harness": {...}}`` envelope and
    the ARN field is ``arn`` (NOT ``harnessArn``).
  - harnessName must match ``[a-zA-Z][a-zA-Z0-9_]{0,39}`` (no hyphens, <=40).
  - InvokeHarness streams events (contentBlockDelta / messageStop) on the DATA
    plane and the session id must be >= 33 chars.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from app.services import harness_deployer
from app.services.harness_deployer import (
    build_harness_tools,
    create_harness,
    destroy_harness,
    ensure_gateway_outbound_provider,
    invoke_harness,
    pad_session_id,
    sanitize_harness_name,
)
from app.services.resource_ownership import owner_tags
from botocore.exceptions import ClientError


def _owned_harness(ctrl: MagicMock, harness_id: str, region: str) -> None:
    detail = {
        "harness": {
            "harnessId": harness_id,
            "arn": (f"arn:aws:bedrock-agentcore:{region}:123456789012:harness/{harness_id}"),
            "status": "READY",
        }
    }
    ctrl.get_harness.side_effect = [
        detail,
        detail,
        ClientError(
            {
                "Error": {
                    "Code": "ResourceNotFoundException",
                    "Message": "not found",
                }
            },
            "GetHarness",
        ),
    ]
    ctrl.get_oauth2_credential_provider.return_value = {
        "credentialProviderArn": (
            f"arn:aws:bedrock-agentcore:{region}:123456789012:credential-provider/harness-provider"
        )
    }
    ctrl.list_tags_for_resource.return_value = {"tags": owner_tags(region)}


# ---------------------------------------------------------------------------
# sanitize_harness_name — regex [a-zA-Z][a-zA-Z0-9_]{0,39}
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "my-agent-bot",  # hyphens -> underscores
        "weather agent!",  # spaces + punctuation
        "123leadingdigit",  # leading digit -> prefixed
        "a" * 80,  # over-length
        "Agent.With.Dots",  # dots
    ],
)
def test_sanitize_harness_name_enforces_regex(raw):
    import re

    out = sanitize_harness_name(raw)
    assert re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,39}", out), out
    assert "-" not in out
    assert len(out) <= 40


def test_sanitize_harness_name_no_hyphens_preserves_letters():
    assert sanitize_harness_name("github-connector-bot") == "github_connector_bot"


def test_sanitize_harness_name_empty_falls_back():
    out = sanitize_harness_name("")
    assert out and out[0].isalpha()


# ---------------------------------------------------------------------------
# pad_session_id — >= 33 chars
# ---------------------------------------------------------------------------


def test_pad_session_id_pads_short():
    assert len(pad_session_id("abc")) == 33


def test_pad_session_id_preserves_long():
    long_id = "x" * 50
    assert pad_session_id(long_id) == long_id


def test_pad_session_id_exactly_min():
    sid = "y" * 33
    assert pad_session_id(sid) == sid
    assert len(pad_session_id(sid)) == 33


# ---------------------------------------------------------------------------
# build_harness_tools — agentcore_gateway shape
# ---------------------------------------------------------------------------


def test_build_harness_tools_empty_without_gateway():
    assert build_harness_tools(None) == []
    assert build_harness_tools("") == []


def test_build_harness_tools_gateway_shape():
    arn = "arn:aws:bedrock-agentcore:us-west-2:111122223333:gateway/gw-abc"
    tools = build_harness_tools(arn)
    assert len(tools) == 1
    tool = tools[0]
    assert tool["type"] == "agentcore_gateway"
    assert tool["config"]["agentCoreGateway"]["gatewayArn"] == arn


def test_gateway_outbound_provider_resolves_secret_with_injected_target_client():
    ctrl = MagicMock()
    ctrl.create_oauth2_credential_provider.return_value = {
        "credentialProviderArn": "arn:aws:bedrock-agentcore:eu-west-1:1:credential-provider/provider"
    }
    secrets = MagicMock()
    secrets.get_secret_value.return_value = {"SecretString": '{"clientSecret":"target-account-secret"}'}
    cognito = MagicMock()

    provider_arn, scopes = ensure_gateway_outbound_provider(
        ctrl,
        "target_harness",
        {
            "discovery_url": (
                "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_pool/.well-known/openid-configuration"
            ),
            "client_id": "client-id",
            "client_secret_ref": ("arn:aws:secretsmanager:eu-west-1:123456789012:secret:agentcore-connector/gateway"),
            "scope": "agentcore-gateway/invoke",
        },
        secrets_client=secrets,
        cognito_client=cognito,
    )

    assert provider_arn.endswith("/provider")
    assert scopes == ["agentcore-gateway/invoke"]
    secrets.get_secret_value.assert_called_once()
    cognito.describe_user_pool_client.assert_not_called()
    create_input = ctrl.create_oauth2_credential_provider.call_args.kwargs
    assert (
        create_input["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]["clientSecret"]
        == "target-account-secret"
    )


def test_gateway_outbound_provider_legacy_client_uses_injected_target_cognito():
    ctrl = MagicMock()
    ctrl.create_oauth2_credential_provider.return_value = {
        "credentialProviderArn": "arn:aws:bedrock-agentcore:eu-west-1:1:credential-provider/provider"
    }
    secrets = MagicMock()
    cognito = MagicMock()
    cognito.describe_user_pool_client.return_value = {"UserPoolClient": {"ClientSecret": "legacy-target-secret"}}

    ensure_gateway_outbound_provider(
        ctrl,
        "target_harness",
        {
            "user_pool_id": "eu-west-1_pool",
            "client_id": "client-id",
            "scope": "agentcore-gateway/invoke",
        },
        secrets_client=secrets,
        cognito_client=cognito,
    )

    cognito.describe_user_pool_client.assert_called_once_with(
        UserPoolId="eu-west-1_pool",
        ClientId="client-id",
    )
    secrets.get_secret_value.assert_not_called()
    create_input = ctrl.create_oauth2_credential_provider.call_args.kwargs
    assert (
        create_input["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]["clientSecret"]
        == "legacy-target-secret"
    )


# ---------------------------------------------------------------------------
# create_harness — parses the {"harness": {...}} envelope + arn field
# ---------------------------------------------------------------------------


def test_create_harness_parses_envelope_and_arn():
    ctrl = MagicMock()
    ctrl.create_harness.return_value = {
        "harness": {
            "harnessId": "harness-abc123",
            "arn": "arn:aws:bedrock-agentcore:us-west-2:111122223333:harness/harness-abc123",
            "status": "CREATING",
        }
    }

    result = create_harness(
        ctrl,
        "my_harness",
        "arn:aws:iam::111122223333:role/AgentCoreHarness-my_harness",
        model_id="us.anthropic.claude-sonnet-5",
        system_prompt="You are helpful.",
        gateway_arn="arn:aws:bedrock-agentcore:us-west-2:111122223333:gateway/gw-1",
        memory_arn="arn:aws:bedrock-agentcore:us-west-2:111122223333:memory/mem-1",
    )

    assert result["harness_id"] == "harness-abc123"
    assert result["arn"].endswith("harness/harness-abc123")
    assert result["status"] == "CREATING"

    # Verify the request shape matches the verified API contract.
    _, kwargs = ctrl.create_harness.call_args
    assert kwargs["harnessName"] == "my_harness"
    assert kwargs["executionRoleArn"].endswith("AgentCoreHarness-my_harness")
    assert kwargs["model"]["bedrockModelConfig"]["modelId"].startswith("us.anthropic")
    assert kwargs["systemPrompt"] == [{"text": "You are helpful."}]
    assert kwargs["tools"][0]["type"] == "agentcore_gateway"
    assert kwargs["memory"]["agentCoreMemoryConfiguration"]["arn"].endswith("memory/mem-1")
    assert kwargs["tags"]["AgentCoreStack"].endswith("-us-east-1")


def test_create_harness_omits_model_when_not_specified():
    ctrl = MagicMock()
    ctrl.create_harness.return_value = {
        "harness": {"harnessId": "h1", "arn": "arn:...:harness/h1", "status": "CREATING"}
    }
    create_harness(ctrl, "h", "role-arn")
    _, kwargs = ctrl.create_harness.call_args
    assert "model" not in kwargs  # service defaults to Claude Sonnet 4.6
    assert "tools" not in kwargs
    assert "memory" not in kwargs


def test_create_harness_excludes_temperature_for_claude_sonnet_5():
    """Claude Sonnet 5+ rejects temperature with ValidationException."""
    ctrl = MagicMock()
    ctrl.create_harness.return_value = {
        "harness": {"harnessId": "h1", "arn": "arn:...:harness/h1", "status": "CREATING"}
    }
    create_harness(ctrl, "h", "role-arn", model_id="us.anthropic.claude-sonnet-5")
    _, kwargs = ctrl.create_harness.call_args
    model_cfg = kwargs["model"]["bedrockModelConfig"]
    assert model_cfg["modelId"] == "us.anthropic.claude-sonnet-5"
    assert "temperature" not in model_cfg  # excluded for Claude 5


def test_create_harness_includes_temperature_for_older_models():
    """Older models (e.g. Claude Sonnet 4.6) still receive temperature."""
    ctrl = MagicMock()
    ctrl.create_harness.return_value = {
        "harness": {"harnessId": "h1", "arn": "arn:...:harness/h1", "status": "CREATING"}
    }
    create_harness(ctrl, "h", "role-arn", model_id="us.anthropic.claude-sonnet-4-6-20250514-v1:0")
    _, kwargs = ctrl.create_harness.call_args
    model_cfg = kwargs["model"]["bedrockModelConfig"]
    assert model_cfg["modelId"] == "us.anthropic.claude-sonnet-4-6-20250514-v1:0"
    assert "temperature" in model_cfg  # included for older models


def test_create_harness_idempotent_on_conflict():
    ctrl = MagicMock()
    ctrl.create_harness.side_effect = Exception("ConflictException: harness already exists")
    ctrl.list_harnesses.return_value = {
        "harnesses": [
            {
                "harnessName": "dup_harness",
                "harnessId": "harness-existing",
                "arn": "arn:...:harness/harness-existing",
                "status": "READY",
            }
        ]
    }
    ctrl.get_harness.return_value = {
        "harness": {
            "harnessId": "harness-existing",
            "arn": ("arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/harness-existing"),
            "status": "READY",
        }
    }
    ctrl.list_tags_for_resource.return_value = {"tags": owner_tags("us-east-1")}

    result = create_harness(
        ctrl,
        "dup_harness",
        "role-arn",
        region="us-east-1",
    )
    assert result["harness_id"] == "harness-existing"
    assert result["created_by_deployment"] is False


def test_create_harness_conflict_lookup_finds_page_two():
    ctrl = MagicMock()
    ctrl.create_harness.side_effect = Exception("ConflictException: harness already exists")
    ctrl.list_harnesses.side_effect = [
        {"harnesses": [], "nextToken": "page-2"},
        {
            "harnesses": [
                {
                    "harnessName": "dup_harness",
                    "harnessId": "harness-existing",
                    "arn": "arn:...:harness/harness-existing",
                    "status": "READY",
                }
            ]
        },
    ]
    ctrl.get_harness.return_value = {
        "harness": {
            "harnessId": "harness-existing",
            "arn": ("arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/harness-existing"),
            "status": "READY",
        }
    }
    ctrl.list_tags_for_resource.return_value = {"tags": owner_tags("us-east-1")}

    result = create_harness(
        ctrl,
        "dup_harness",
        "role-arn",
        region="us-east-1",
    )

    assert result["harness_id"] == "harness-existing"
    assert ctrl.list_harnesses.call_args_list[1].kwargs["nextToken"] == "page-2"


def test_create_harness_refuses_a_foreign_same_named_harness():
    ctrl = MagicMock()
    ctrl.create_harness.side_effect = Exception("ConflictException: harness already exists")
    ctrl.list_harnesses.return_value = {
        "harnesses": [
            {
                "harnessName": "dup_harness",
                "harnessId": "harness-foreign",
                "arn": ("arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/harness-foreign"),
                "status": "READY",
            }
        ]
    }
    ctrl.get_harness.return_value = {
        "harness": {
            "harnessId": "harness-foreign",
            "arn": ("arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/harness-foreign"),
            "status": "READY",
        }
    }
    ctrl.list_tags_for_resource.return_value = {
        "tags": {
            "ManagedBy": "agentcore-flows",
            "AgentCoreStack": "another-stack-prod-us-east-1",
        }
    }

    with pytest.raises(Exception, match="Deletion refused"):
        create_harness(
            ctrl,
            "dup_harness",
            "role-arn",
            region="us-east-1",
        )


# ---------------------------------------------------------------------------
# destroy_harness — idempotent on NotFound
# ---------------------------------------------------------------------------


def test_destroy_harness_idempotent_on_notfound():
    ctrl = MagicMock()
    # _resolve_harness_identifier: get_harness succeeds so the id is used as-is.
    _owned_harness(ctrl, "h-gone", "us-west-2")
    ctrl.delete_harness.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}}, "DeleteHarness"
    )

    with patch.object(harness_deployer, "_create_agentcore_control_client", return_value=ctrl):
        result = destroy_harness("h-gone", "us-west-2")

    assert result["success"] is True
    assert result.get("note") == "already gone"


def test_destroy_harness_success():
    ctrl = MagicMock()
    _owned_harness(ctrl, "h-1", "us-west-2")
    ctrl.delete_harness.return_value = {}

    with patch.object(harness_deployer, "_create_agentcore_control_client", return_value=ctrl):
        result = destroy_harness("h-1", "us-west-2")

    assert result["success"] is True
    assert result["harness_id"] == "h-1"
    ctrl.delete_harness.assert_called_once_with(harnessId="h-1")
    # destroy_harness also best-effort deletes the harness->gateway outbound
    # OAuth provider (named harness-gw-<name>) so it never orphans.
    ctrl.delete_oauth2_credential_provider.assert_called_once()


def test_destroy_harness_retains_the_graph_when_delete_is_not_confirmed():
    ctrl = MagicMock()
    detail = {
        "harness": {
            "harnessId": "h-stuck",
            "arn": ("arn:aws:bedrock-agentcore:us-west-2:123456789012:harness/h-stuck"),
            "status": "DELETING",
        }
    }
    ctrl.get_harness.return_value = detail
    ctrl.list_tags_for_resource.return_value = {"tags": owner_tags("us-west-2")}

    result = destroy_harness(
        "h-stuck",
        "us-west-2",
        agentcore_ctrl=ctrl,
        confirmation_attempts=2,
        confirmation_interval=0,
    )

    assert result["success"] is False
    assert result["retained"] is True
    assert "not confirmed" in result["note"]
    ctrl.delete_oauth2_credential_provider.assert_not_called()


def test_destroy_harness_surfaces_terminal_delete_failure():
    ctrl = MagicMock()
    owned = {
        "harness": {
            "harnessId": "h-failed",
            "arn": ("arn:aws:bedrock-agentcore:us-west-2:123456789012:harness/h-failed"),
            "status": "READY",
        }
    }
    failed = {
        "harness": {
            **owned["harness"],
            "status": "DELETE_FAILED",
            "failureReason": "DeleteWorkloadIdentity denied",
        }
    }
    ctrl.get_harness.side_effect = [owned, owned, failed]
    ctrl.list_tags_for_resource.return_value = {"tags": owner_tags("us-west-2")}

    result = destroy_harness(
        "h-failed",
        "us-west-2",
        agentcore_ctrl=ctrl,
        confirmation_attempts=2,
        confirmation_interval=0,
    )

    assert result["success"] is False
    assert "DELETE_FAILED" in result["error"]
    assert "DeleteWorkloadIdentity denied" in result["error"]
    ctrl.delete_oauth2_credential_provider.assert_not_called()


# ---------------------------------------------------------------------------
# invoke_harness — collects contentBlockDelta text + messageStop stopReason
# ---------------------------------------------------------------------------


def test_invoke_harness_collects_text_and_stop_reason():
    data = MagicMock()
    data.invoke_harness.return_value = {
        "stream": [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"delta": {"text": "The answer "}}},
            {"contentBlockDelta": {"delta": {"text": "is 42."}}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {}},
        ]
    }

    with patch.object(harness_deployer, "_create_agentcore_client", return_value=data):
        result = invoke_harness(
            "us-west-2",
            "arn:aws:bedrock-agentcore:us-west-2:111122223333:harness/h-1",
            "What is the answer?",
            "short",  # forces session-id padding
        )

    assert result["success"] is True
    assert result["output"] == "The answer is 42."
    assert result["stop_reason"] == "end_turn"
    assert result["error"] == ""

    # session id was padded to >= 33 before invoke.
    _, kwargs = data.invoke_harness.call_args
    assert len(kwargs["runtimeSessionId"]) >= 33
    assert kwargs["messages"] == [{"role": "user", "content": [{"text": "What is the answer?"}]}]
    # A W3C trace id is ALWAYS sent (minted when the caller gives none) and
    # handed back, so the turn can be joined to the harness + Memory spans.
    assert re.fullmatch(r"[0-9a-f]{32}", kwargs["traceId"])
    assert kwargs["traceParent"] == f"00-{kwargs['traceId']}-{kwargs['traceParent'].split('-')[2]}-01"
    assert result["trace_id"] == kwargs["traceId"]


def test_invoke_harness_uses_the_supplied_target_account_client():
    data = MagicMock()
    data.invoke_harness.return_value = {"stream": [{"messageStop": {"stopReason": "end_turn"}}]}

    with patch.object(
        harness_deployer,
        "_create_agentcore_client",
        side_effect=AssertionError("must not create a home-account client"),
    ):
        result = invoke_harness(
            "eu-west-1",
            "arn:aws:bedrock-agentcore:eu-west-1:123456789012:harness/h-1",
            "hello",
            "s" * 40,
            agentcore_data_client=data,
        )

    assert result["success"] is True
    data.invoke_harness.assert_called_once()


def test_invoke_harness_propagates_caller_trace_id():
    data = MagicMock()
    data.invoke_harness.return_value = {"stream": [{"messageStop": {"stopReason": "end_turn"}}]}
    with patch.object(harness_deployer, "_create_agentcore_client", return_value=data):
        result = invoke_harness(
            "us-west-2", "arn:...:harness/h", "hi", "s" * 40, trace_id="2346C6EB670744B18F76FEE61AB7B540"
        )
    _, kwargs = data.invoke_harness.call_args
    assert kwargs["traceId"] == "2346c6eb670744b18f76fee61ab7b540"  # lower-cased W3C form
    assert kwargs["traceParent"].startswith("00-2346c6eb670744b18f76fee61ab7b540-")
    assert result["trace_id"] == "2346c6eb670744b18f76fee61ab7b540"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1-6aa7d83b-34e54d8ca36e19b05d4be505", "6aa7d83b34e54d8ca36e19b05d4be505"),  # X-Ray root folded
        ("6aa7d83b34e54d8ca36e19b05d4be505", "6aa7d83b34e54d8ca36e19b05d4be505"),
    ],
)
def test_normalize_trace_id_accepts_w3c_and_xray(raw, expected):
    assert harness_deployer.normalize_trace_id(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "not-a-trace", "6aa7d83b34e54d8ca36e19b05d4be50"])  # 31 hex
def test_normalize_trace_id_mints_on_missing_or_malformed(raw):
    out = harness_deployer.normalize_trace_id(raw)
    assert re.fullmatch(r"[0-9a-f]{32}", out) and out != raw


def test_invoke_harness_error_paths_still_return_trace_id():
    data = MagicMock()
    data.invoke_harness.side_effect = RuntimeError("boom")
    with patch.object(harness_deployer, "_create_agentcore_client", return_value=data):
        result = invoke_harness("us-west-2", "arn:...:harness/h", "hi", "s" * 40)
    assert result["success"] is False and re.fullmatch(r"[0-9a-f]{32}", result["trace_id"])


def test_invoke_harness_surfaces_runtime_client_error():
    data = MagicMock()
    data.invoke_harness.return_value = {
        "stream": [
            {"contentBlockDelta": {"delta": {"text": "partial"}}},
            {"runtimeClientError": {"message": "boom"}},
        ]
    }
    with patch.object(harness_deployer, "_create_agentcore_client", return_value=data):
        result = invoke_harness("us-west-2", "arn:...:harness/h", "hi", "s" * 40)

    assert result["success"] is False
    assert result["error"] == "boom"
    assert result["output"] == "partial"


def test_invoke_harness_collects_tool_calls():
    data = MagicMock()
    data.invoke_harness.return_value = {
        "stream": [
            {"contentBlockStart": {"start": {"toolUse": {"name": "github___search"}}}},
            {"contentBlockDelta": {"delta": {"text": "done"}}},
            {"messageStop": {"stopReason": "tool_use"}},
        ]
    }
    with patch.object(harness_deployer, "_create_agentcore_client", return_value=data):
        result = invoke_harness("us-west-2", "arn:...:harness/h", "hi", "s" * 40)

    assert result["tool_calls"] == ["github___search"]
    assert result["stop_reason"] == "tool_use"


def test_harness_name_from_id_recovers_name():
    from app.services.harness_deployer import _harness_name_from_id

    assert _harness_name_from_id("cust_harness_conn_50919ca4-md7qbmyArB") == "cust_harness_conn_50919ca4"
    assert _harness_name_from_id("acflows_smoke2-MGG5HVlR1U") == "acflows_smoke2"


def test_destroy_harness_deletes_outbound_provider():
    """Teardown must delete the conventionally-named harness->gateway outbound
    OAuth provider even without a persisted harness_result (live-caught orphan)."""
    from unittest.mock import MagicMock, patch

    from app.services import harness_deployer as hd

    ctrl = MagicMock()
    _owned_harness(
        ctrl,
        "cust_harness_conn_50919ca4-md7qbmyArB",
        "us-east-1",
    )
    with patch.object(hd, "_create_agentcore_control_client", return_value=ctrl):
        res = hd.destroy_harness("cust_harness_conn_50919ca4-md7qbmyArB", "us-east-1")
    ctrl.delete_harness.assert_called_once()
    ctrl.delete_oauth2_credential_provider.assert_called_once_with(name="harness-gw-cust_harness_conn_50919ca4")
    assert res["success"]


def test_destroy_harness_does_not_delete_managed_backing_runtime():
    """Bug 188 (corrected): the backing ``harness_*`` runtime is HARNESS-MANAGED
    and delete_harness cascade-deletes it. destroy_harness must NOT call
    delete_agent_runtime on it (that raises 'managed by harness ... Use
    DeleteHarness')."""
    from unittest.mock import MagicMock, patch

    from app.services import harness_deployer as hd

    ctrl = MagicMock()
    _owned_harness(ctrl, "h-1", "us-east-1")
    with patch.object(hd, "_create_agentcore_control_client", return_value=ctrl):
        res = hd.destroy_harness("h-1", "us-east-1")
    ctrl.delete_harness.assert_called_once()
    ctrl.delete_agent_runtime.assert_not_called()
    assert res["success"]


# ---------------------------------------------------------------------------
# Least-privilege harness exec role (Holmes IAM findings)
# ---------------------------------------------------------------------------


def test_harness_role_scopes_model_and_resources(monkeypatch):
    """create_harness_iam_role scopes InvokeModel to the model family and the
    memory/gateway agentcore actions to the connected ARNs (not Resource:*)."""
    import json
    from unittest.mock import MagicMock

    from app.services import harness_deployer as hd

    captured = {}
    iam = MagicMock()
    iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::1:role/AgentCoreHarness-x"}}

    def _put(**kw):
        captured["policy"] = json.loads(kw["PolicyDocument"])

    iam.put_role_policy.side_effect = _put

    hd.create_harness_iam_role(
        iam,
        "AgentCoreHarness-x",
        harness_name="x",
        model_id="us.anthropic.claude-sonnet-5",
        memory_arn="arn:aws:bedrock-agentcore:us-east-1:1:memory/m-1",
        gateway_arn="arn:aws:bedrock-agentcore:us-east-1:1:gateway/g-1",
    )
    stmts = {s["Sid"]: s for s in captured["policy"]["Statement"]}

    # InvokeModel scoped to the family ARN, not "*". For a cross-region
    # inference profile the resource list MUST include BOTH the foundation-model
    # family pattern AND the inference-profile ARN, or ConverseStream /
    # InvokeModelWithResponseStream is AccessDenied on the profile (Bug 146).
    model_res = stmts["BedrockModelAccess"]["Resource"]
    assert isinstance(model_res, list)
    assert any(r.startswith("arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet") for r in model_res)
    assert any("inference-profile/us.anthropic.claude-sonnet-5" in r for r in model_res)
    # Memory/gateway statement scoped to the connected ARNs, not "*"
    res = stmts["AgentCoreMemoryAndGateway"]["Resource"]
    assert "arn:aws:bedrock-agentcore:us-east-1:1:memory/m-1" in res
    assert "arn:aws:bedrock-agentcore:us-east-1:1:gateway/g-1" in res
    assert res != "*"
    # Token-vault fetches remain account-level (no resource ARN form)
    assert stmts["AgentCoreAccountLevel"]["Resource"] == "*"
    assert "bedrock-agentcore:GetResourceOauth2Token" in stmts["AgentCoreAccountLevel"]["Action"]


def test_harness_role_omits_scoped_statement_without_arns():
    """When no memory/gateway is connected the scoped statement is OMITTED
    (no Resource:* fallback — Holmes IAM HIGH). A bare harness still works via
    the harness-owned-memory + account-level statements. Model falls back to *
    only because the model is unknown (user-selectable at invoke time)."""
    import json
    from unittest.mock import MagicMock

    from app.services import harness_deployer as hd

    captured = {}
    iam = MagicMock()
    iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::1:role/AgentCoreHarness-y"}}
    iam.put_role_policy.side_effect = lambda **kw: captured.update(policy=json.loads(kw["PolicyDocument"]))
    hd.create_harness_iam_role(iam, "AgentCoreHarness-y", harness_name="y")
    stmts = {s["Sid"]: s for s in captured["policy"]["Statement"]}
    assert stmts["BedrockModelAccess"]["Resource"] == "*"
    assert "AgentCoreMemoryAndGateway" not in stmts
    # The bare harness keeps working through the dedicated statements -- and this
    # asserts the RESOURCE, not just the Sid. Asserting presence alone is what let a
    # ``memory/harness_*`` pattern that matched nothing ship: the statement was there,
    # this comment claimed the bare harness worked, and the first live invoke of any
    # normally-named harness was AccessDenied on ListEvents.
    owned = stmts["AgentCoreHarnessOwnedMemory"]["Resource"]
    assert owned == [
        "arn:aws:bedrock-agentcore:*:*:memory/y-*",
        "arn:aws:bedrock-agentcore:*:*:memory/y-*/*",
    ], owned
    assert stmts["AgentCoreAccountLevel"]["Resource"] == "*"


def test_harness_owned_memory_is_scoped_to_the_harness_name_not_a_harness_prefix():
    """The auto-provisioned memory is ``<harnessName>-<tail>``, never ``harness_<...>``.

    Measured live 2026-09-24 on acfe2e-p0920 through the product's own
    POST /api/test-runtime (deployment aa3f6767), with the old ``memory/harness_*``
    pattern deployed:

        assumed-role/AgentCoreHarness-p0bharn1790232124_dda45e47/BedrockAgentCore-...
        is not authorized to perform: bedrock-agentcore:ListEvents on resource:
        ...:memory/p0bharn1790232124_dda45e47-sfd0kpCXwL

    The harness name is the user's agent name through ``sanitize_harness_name``, so
    ``harness_*`` matched only a harness a user happened to name that way -- which is
    exactly what the probe behind the original "verified live" comment was called. Every
    other harness was dead on first invoke. This test fails on the specific regression of
    reintroducing a literal prefix, which no presence-only assertion can catch.

    The negative assertion is load-bearing in both directions: a pattern that is merely
    *different* from ``harness_*`` is not necessarily *right*, so the positive assertion
    pins the derived name and the negative one pins the defect.
    """
    import json
    from unittest.mock import MagicMock

    from app.services import harness_deployer as hd

    captured = {}
    iam = MagicMock()
    iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::1:role/AgentCoreHarness-q"}}
    iam.put_role_policy.side_effect = lambda **kw: captured.update(policy=json.loads(kw["PolicyDocument"]))
    hd.create_harness_iam_role(iam, "AgentCoreHarness-Support_Bot", harness_name="Support_Bot")
    stmts = {s["Sid"]: s for s in captured["policy"]["Statement"]}
    res = stmts["AgentCoreHarnessOwnedMemory"]["Resource"]

    assert res == [
        "arn:aws:bedrock-agentcore:*:*:memory/Support_Bot-*",
        "arn:aws:bedrock-agentcore:*:*:memory/Support_Bot-*/*",
    ], res
    assert not any("memory/harness_" in r for r in res), (
        f"a literal harness_ prefix denies every harness not named harness_*; got {res}"
    )
    # The name is interpolated into an IAM Resource, so it must not be able to carry a
    # wildcard that widens the pattern to another agent's memory.
    hd.create_harness_iam_role(iam, "AgentCoreHarness-evil", harness_name="ev*il")
    res2 = {s["Sid"]: s for s in captured["policy"]["Statement"]}["AgentCoreHarnessOwnedMemory"]["Resource"]
    assert not any("ev*il" in r for r in res2), res2


def test_harness_role_requires_a_harness_name_rather_than_defaulting_wide():
    """Omitting the name is a TypeError, not a silent ``memory/*`` or a silent omission.

    Both fallbacks are worse than a crash: ``memory/*`` would let one tenant's harness
    read every other agent's conversation memory in the account (ARCC cnt_L4ZLZgjrCctfxl),
    and dropping the statement reproduces the outage it exists to prevent. A required
    keyword moves that decision to the call site, where it is visible in review.
    """
    from unittest.mock import MagicMock

    import pytest
    from app.services import harness_deployer as hd

    with pytest.raises(TypeError):
        hd.create_harness_iam_role(MagicMock(), "AgentCoreHarness-n")  # type: ignore[call-arg]


def test_harness_role_logs_scoped_to_agentcore_log_groups():
    """CloudWatch Logs statement is scoped to /aws/bedrock-agentcore/* (not *)."""
    import json
    from unittest.mock import MagicMock

    from app.services import harness_deployer as hd

    captured = {}
    iam = MagicMock()
    iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::1:role/AgentCoreHarness-z"}}
    iam.put_role_policy.side_effect = lambda **kw: captured.update(policy=json.loads(kw["PolicyDocument"]))
    hd.create_harness_iam_role(iam, "AgentCoreHarness-z", harness_name="z")
    stmts = {s["Sid"]: s for s in captured["policy"]["Statement"]}
    logs_res = stmts["CloudWatchLogs"]["Resource"]
    assert logs_res != "*"
    assert any("/aws/bedrock-agentcore/" in r for r in logs_res)
