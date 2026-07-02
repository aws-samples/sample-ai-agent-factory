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

from unittest.mock import MagicMock, patch

import pytest

from app.services import harness_deployer
from app.services.harness_deployer import (
    build_harness_tools,
    create_harness,
    destroy_harness,
    invoke_harness,
    pad_session_id,
    sanitize_harness_name,
)


# ---------------------------------------------------------------------------
# sanitize_harness_name — regex [a-zA-Z][a-zA-Z0-9_]{0,39}
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "my-agent-bot",          # hyphens -> underscores
        "weather agent!",        # spaces + punctuation
        "123leadingdigit",       # leading digit -> prefixed
        "a" * 80,                # over-length
        "Agent.With.Dots",       # dots
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
    result = create_harness(ctrl, "dup_harness", "role-arn")
    assert result["harness_id"] == "harness-existing"


# ---------------------------------------------------------------------------
# destroy_harness — idempotent on NotFound
# ---------------------------------------------------------------------------


def test_destroy_harness_idempotent_on_notfound():
    ctrl = MagicMock()
    # _resolve_harness_identifier: get_harness succeeds so the id is used as-is.
    ctrl.get_harness.return_value = {"harness": {"harnessId": "h-gone", "status": "READY"}}
    ctrl.delete_harness.side_effect = Exception("ResourceNotFoundException: not found")

    with patch.object(
        harness_deployer, "_create_agentcore_control_client", return_value=ctrl
    ):
        result = destroy_harness("h-gone", "us-west-2")

    assert result["success"] is True
    assert result.get("note") == "already gone"


def test_destroy_harness_success():
    ctrl = MagicMock()
    ctrl.get_harness.return_value = {"harness": {"harnessId": "h-1", "status": "READY"}}
    ctrl.delete_harness.return_value = {}

    with patch.object(
        harness_deployer, "_create_agentcore_control_client", return_value=ctrl
    ):
        result = destroy_harness("h-1", "us-west-2")

    assert result["success"] is True
    assert result["harness_id"] == "h-1"
    ctrl.delete_harness.assert_called_once_with(harnessId="h-1")
    # destroy_harness also best-effort deletes the harness->gateway outbound
    # OAuth provider (named harness-gw-<name>) so it never orphans.
    ctrl.delete_oauth2_credential_provider.assert_called_once()


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
    assert kwargs["messages"] == [
        {"role": "user", "content": [{"text": "What is the answer?"}]}
    ]


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
    ctrl.get_harness.return_value = {"harness": {"status": "READY"}}
    with patch.object(hd, "_create_agentcore_control_client", return_value=ctrl):
        res = hd.destroy_harness("cust_harness_conn_50919ca4-md7qbmyArB", "us-east-1")
    ctrl.delete_harness.assert_called_once()
    ctrl.delete_oauth2_credential_provider.assert_called_once_with(
        name="harness-gw-cust_harness_conn_50919ca4")
    assert res["success"]


def test_destroy_harness_does_not_delete_managed_backing_runtime():
    """Bug 188 (corrected): the backing ``harness_*`` runtime is HARNESS-MANAGED
    and delete_harness cascade-deletes it. destroy_harness must NOT call
    delete_agent_runtime on it (that raises 'managed by harness ... Use
    DeleteHarness')."""
    from unittest.mock import MagicMock, patch
    from app.services import harness_deployer as hd
    ctrl = MagicMock()
    ctrl.get_harness.return_value = {"harness": {"status": "READY"}}
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
        iam, "AgentCoreHarness-x",
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
    assert any(
        r.startswith("arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet")
        for r in model_res
    )
    assert any(
        "inference-profile/us.anthropic.claude-sonnet-5" in r
        for r in model_res
    )
    # Memory/gateway statement scoped to the connected ARNs, not "*"
    res = stmts["AgentCoreMemoryAndGateway"]["Resource"]
    assert "arn:aws:bedrock-agentcore:us-east-1:1:memory/m-1" in res
    assert "arn:aws:bedrock-agentcore:us-east-1:1:gateway/g-1" in res
    assert res != "*"
    # Token-vault fetches remain account-level (no resource ARN form)
    assert stmts["AgentCoreAccountLevel"]["Resource"] == "*"
    assert "bedrock-agentcore:GetResourceOauth2Token" in stmts["AgentCoreAccountLevel"]["Action"]


def test_harness_role_falls_back_to_wildcard_without_arns():
    """When no model/memory/gateway is known the role still works (Resource:*)."""
    import json
    from unittest.mock import MagicMock
    from app.services import harness_deployer as hd
    captured = {}
    iam = MagicMock()
    iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::1:role/AgentCoreHarness-y"}}
    iam.put_role_policy.side_effect = lambda **kw: captured.update(policy=json.loads(kw["PolicyDocument"]))
    hd.create_harness_iam_role(iam, "AgentCoreHarness-y")
    stmts = {s["Sid"]: s for s in captured["policy"]["Statement"]}
    assert stmts["BedrockModelAccess"]["Resource"] == "*"
    assert stmts["AgentCoreMemoryAndGateway"]["Resource"] == "*"
