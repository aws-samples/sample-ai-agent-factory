"""Offline tests for the generated-agent reference core.

Exercises the pure orchestration core with fakes — no Strands, no litellm, no
boto3, no live AWS — and enforces the contract invariants:

* inference goes through the injected LlmClient (a LiteLLMModel adapter in
  production), never a direct Bedrock client;
* tools go through the injected ToolClient (an MCPClient adapter in
  production), never a direct Lambda invoke;
* every LLM call carries a non-empty guardrail identifier;
* memory is actor/session scoped;
* a tool the model requests that is not subscribed is refused before any
  Gateway call.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import pathlib
import re

import pytest

import agent as agent_mod
from agent import (
    AgentError,
    ReferenceAgentConfig,
    ReferenceAgentCore,
    HANDSHAKE_MARKER,
)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeLlm:
    """Scripted LLM. Records every guardrail id it was called with."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.guardrails: list[str] = []
        self.calls = 0

    def complete(self, messages, *, guardrail_identifier, stream):
        self.guardrails.append(guardrail_identifier)
        self.calls += 1
        return self._replies[min(self.calls - 1, len(self._replies) - 1)]


class FakeTools:
    def __init__(self, tools):
        self._tools = list(tools)
        self.called: list[tuple[str, dict]] = []

    def list_tools(self):
        return list(self._tools)

    def call_tool(self, qualified_name, arguments):
        self.called.append((qualified_name, dict(arguments)))
        return {"ok": True, "tool": qualified_name}


class FakeMemory:
    def __init__(self):
        self.events: dict[tuple[str, str], list[dict]] = {}

    def put_event(self, *, actor_id, session_id, payload):
        self.events.setdefault((actor_id, session_id), []).append(dict(payload))
        return f"event-{len(self.events[(actor_id, session_id)])}"

    def get_last_event(self, *, actor_id, session_id):
        seq = self.events.get((actor_id, session_id))
        return seq[-1] if seq else None


def _cfg(**overrides):
    base = dict(
        tenant_id="demo",
        agent_id="primary",
        env_name="nonprod",
        guardrail_identifier="gd-123",
        model_id="target-demo/openai.gpt-oss-120b",
        subscribed_tools=("target-demo___tool-echo",),
    )
    base.update(overrides)
    return ReferenceAgentConfig(**base)


# --------------------------------------------------------------------------
# Config validation
# --------------------------------------------------------------------------


def test_config_requires_guardrail():
    with pytest.raises(AgentError):
        _cfg(guardrail_identifier="")


def test_config_requires_model_id():
    with pytest.raises(AgentError):
        _cfg(model_id="")


def test_config_rejects_out_of_range_iterations():
    with pytest.raises(AgentError):
        _cfg(max_iterations=0)
    with pytest.raises(AgentError):
        _cfg(max_iterations=26)


# --------------------------------------------------------------------------
# Happy path: single-shot, no tool
# --------------------------------------------------------------------------


def test_single_shot_reply_sets_guardrail_and_marker():
    llm = FakeLlm(["done, here is the answer <done/>"])
    tools = FakeTools(["target-demo___tool-echo"])
    core = ReferenceAgentCore(_cfg(), llm, tools)
    out = core.run("hello", actor_id="actor-1", session_id="sess-1")
    assert out.marker == HANDSHAKE_MARKER
    assert out.content_blocks == 1
    assert out.tool_calls == []
    assert out.discovered_tools == ["target-demo___tool-echo"]
    # Guardrail was set on the inference call.
    assert llm.guardrails == ["gd-123"]
    # Reply is fingerprinted, never returned verbatim.
    assert re.fullmatch(r"[0-9a-f]{32}", out.reply_fingerprint)


# --------------------------------------------------------------------------
# Tool-use path
# --------------------------------------------------------------------------


def test_tool_call_routes_through_tool_client():
    llm = FakeLlm(
        [
            'TOOL target-demo___tool-echo {"message": "hi"}',
            "final answer <done/>",
        ]
    )
    tools = FakeTools(["target-demo___tool-echo"])
    core = ReferenceAgentCore(_cfg(), llm, tools)
    out = core.run("echo hi", actor_id="a", session_id="s")
    assert out.tool_calls == ["target-demo___tool-echo"]
    assert tools.called == [("target-demo___tool-echo", {"message": "hi"})]
    # Two inference turns: the tool request and the final answer.
    assert out.content_blocks == 2
    assert llm.guardrails == ["gd-123", "gd-123"]


def test_unsubscribed_tool_is_refused_before_gateway_call():
    llm = FakeLlm(['TOOL target-demo___tool-danger {}'])
    tools = FakeTools(["target-demo___tool-echo"])
    core = ReferenceAgentCore(_cfg(), llm, tools)
    with pytest.raises(PermissionError):
        core.run("do bad", actor_id="a", session_id="s")
    # The Tools Gateway was never called for the unsubscribed tool.
    assert tools.called == []


def test_malformed_tool_arguments_fail_closed():
    llm = FakeLlm(["TOOL target-demo___tool-echo {not-json"])
    tools = FakeTools(["target-demo___tool-echo"])
    core = ReferenceAgentCore(_cfg(), llm, tools)
    with pytest.raises(AgentError):
        core.run("x", actor_id="a", session_id="s")


def test_max_iterations_bounds_the_loop():
    # Always requests a tool -> loop must stop at max_iterations.
    llm = FakeLlm(['TOOL target-demo___tool-echo {}'])
    tools = FakeTools(["target-demo___tool-echo"])
    core = ReferenceAgentCore(_cfg(max_iterations=3), llm, tools)
    out = core.run("loop", actor_id="a", session_id="s")
    assert out.content_blocks == 3
    assert out.tool_calls == ["target-demo___tool-echo"] * 3


# --------------------------------------------------------------------------
# Memory round-trip
# --------------------------------------------------------------------------


def test_memory_round_trip_recorded_when_memory_present():
    llm = FakeLlm(["answer <done/>"])
    tools = FakeTools([])
    mem = FakeMemory()
    core = ReferenceAgentCore(_cfg(), llm, tools, memory=mem)
    out = core.run("hi", actor_id="actor-9", session_id="sess-9")
    assert out.memory_round_trip is True
    # Exactly one event, scoped to the actor/session, storing only fingerprints.
    stored = mem.events[("actor-9", "sess-9")]
    assert len(stored) == 1
    assert set(stored[0]) == {"promptFingerprint", "replyFingerprint", "toolCalls"}
    assert re.fullmatch(r"[0-9a-f]{32}", stored[0]["replyFingerprint"])


def test_no_memory_means_no_round_trip():
    core = ReferenceAgentCore(_cfg(), FakeLlm(["<done/>"]), FakeTools([]))
    out = core.run("hi", actor_id="a", session_id="s")
    assert out.memory_round_trip is False


def test_missing_actor_or_session_fails_closed():
    core = ReferenceAgentCore(_cfg(), FakeLlm(["<done/>"]), FakeTools([]))
    with pytest.raises(AgentError):
        core.run("hi", actor_id="", session_id="s")
    with pytest.raises(AgentError):
        core.run("hi", actor_id="a", session_id="")


# --------------------------------------------------------------------------
# Inference base URL derivation (sibling of /mcp, not nested)
# --------------------------------------------------------------------------


def test_inference_base_url_is_sibling_of_mcp():
    gw = "https://x.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"
    assert (
        agent_mod._inference_base_url(gw)
        == "https://x.gateway.bedrock-agentcore.us-west-2.amazonaws.com/inference/v1"
    )
    assert "/mcp/inference" not in agent_mod._inference_base_url(gw)
    with pytest.raises(AgentError):
        agent_mod._inference_base_url("https://x.gateway.bedrock-agentcore.us-west-2.amazonaws.com/other")


# --------------------------------------------------------------------------
# Contract invariants — static source assertions
# --------------------------------------------------------------------------

_SRC = pathlib.Path(agent_mod.__file__).read_text(encoding="utf-8")


def test_no_direct_bedrock_or_lambda_client():
    # Inference must go through LiteLLMModel; tools through MCPClient. A direct
    # bedrock/bedrock-runtime client or a lambda invoke is a contract violation.
    assert 'client("bedrock"' not in _SRC and "client('bedrock'" not in _SRC
    assert 'client("bedrock-runtime"' not in _SRC
    assert 'client("lambda"' not in _SRC and "client('lambda'" not in _SRC
    assert ".invoke(" not in _SRC or "def invoke" in _SRC  # only the entrypoint named invoke


def test_uses_litellm_and_mcp_client():
    assert "LiteLLMModel" in _SRC
    assert "MCPClient" in _SRC


def test_sigv4_httpx_auth_is_callable():
    assert "def __call__(self, request)" in _SRC
    assert "def auth_flow(self, request)" not in _SRC


def test_mcp_protocol_version_pinned():
    assert "2025-06-18" in _SRC

# --------------------------------------------------------------------------
# AgentCore Identity M2M bearer exchange
# --------------------------------------------------------------------------


def test_inference_bearer_mints_workload_token_then_resource_token(monkeypatch):
    calls = []

    class FakeIdentity:
        def get_workload_access_token(self, **kwargs):
            calls.append(("workload", kwargs))
            return {"workloadAccessToken": "workload-token"}

        def get_resource_oauth2_token(self, **kwargs):
            calls.append(("resource", kwargs))
            return {"accessToken": "resource-token"}

    identity = FakeIdentity()

    class FakeBoto3:
        @staticmethod
        def client(service, region_name=None):
            assert service == "bedrock-agentcore"
            assert region_name == "us-west-2"
            return identity

    import sys

    monkeypatch.setitem(sys.modules, "boto3", FakeBoto3())
    monkeypatch.delenv("AGENTCORE_INFERENCE_BEARER", raising=False)
    monkeypatch.setenv(
        "AGENTCORE_INFERENCE_CREDENTIAL_PROVIDER", "provider-nonprod"
    )
    monkeypatch.setenv("AGENTCORE_WORKLOAD_IDENTITY_NAME", "workload-nonprod")
    monkeypatch.setenv("AGENTCORE_INFERENCE_SCOPE", "inference/invoke")
    monkeypatch.setenv("AWS_REGION", "us-west-2")

    assert agent_mod._fetch_inference_bearer() == "resource-token"
    assert calls == [
        ("workload", {"workloadName": "workload-nonprod"}),
        (
            "resource",
            {
                "workloadIdentityToken": "workload-token",
                "resourceCredentialProviderName": "provider-nonprod",
                "scopes": ["inference/invoke"],
                "oauth2Flow": "M2M",
            },
        ),
    ]
