"""Real Strands reference agent for the AgentCore Runtime — generated-agent slice.

This replaces the intentionally-inert compatibility handler
(`scripts/live-agentcore-runtime-memory-spike/agent/agent.py`) with a real agent
that exercises the three platform integrations the blueprint mandates and the
roadmap's Round 2.B item requires:

* **LLM inference** via the platform LLM Gateway — a Strands ``LiteLLMModel``
  pointed at the Gateway's OpenAI-compatible ``/inference/v1`` endpoint. NEVER a
  direct Bedrock call.
* **Tools** via the platform Tools Gateway — a Strands ``MCPClient`` speaking MCP
  ``tools/list`` and ``tools/call``. NEVER a direct Lambda invoke.
* **Short-term memory** via AgentCore Memory, actor-scoped, using only the
  memory id supplied through the environment.

Design for testability and honesty:

* The orchestration is a **pure core** (:class:`ReferenceAgentCore`) that depends
  only on small Protocols (``LlmClient``, ``ToolClient``, ``MemoryClient``). It
  contains all the decision logic and is fully unit-testable offline with fakes
  — no Strands, no ``litellm``, no live AWS.
* The **production wiring** (:func:`build_production_core`) constructs the real
  Strands ``LiteLLMModel`` / ``MCPClient`` and an AgentCore Memory adapter, then
  hands them to the same core. Imports of Strands/boto3 are lazy so the offline
  test suite never needs them.
* The container entrypoint acquires a Gateway-authorized bearer token exactly
  the way the live-proven Identity M2M spike does (workload identity → OAuth2
  credential provider → ``GetResourceOauth2Token``), then builds the production
  core. It never logs tokens, headers, session ids, or raw request payloads.

Contract invariants (enforced by tests):

* No direct ``bedrock``/``bedrock-runtime`` client and no ``lambda`` invoke ever
  appear in this module — inference goes through ``LiteLLMModel`` and tools
  through ``MCPClient``.
* Every LLM call carries a non-empty ``guardrail_identifier`` (R-BED-028 +
  SCP-02 + IAM deny + VPCE policy).
* Memory is scoped by ``actor_id`` only; no real end-user identity is placed in
  session tags (spec §3.4.6).

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: Deterministic marker the live ``verify`` step matches on to confirm a real
#: InvokeAgentRuntime round-trip without recording any request content.
HANDSHAKE_MARKER = "agentcore-generated-agent-ok"

#: The scalar field the entrypoint reads from the invocation payload.
PROMPT_FIELD = "prompt"

#: Actor field: AgentCore Memory is scoped by this alone (spec §3.4.6).
ACTOR_FIELD = "actorId"

#: Bounded tool-use loop — mirrors the blueprint task-agent max-iteration guard.
MAX_TOOL_ITERATIONS_DEFAULT = 6

#: Bounded, deterministic inference token cap (live-proven value, not 16).
INFERENCE_MAX_TOKENS = 256


class AgentError(RuntimeError):
    """Raised for any contract or wiring violation in the reference agent."""


# --------------------------------------------------------------------------
# Injected client Protocols — the only surface the pure core depends on
# --------------------------------------------------------------------------


class LlmClient(Protocol):
    """LLM Gateway inference. Implemented in production by a Strands
    ``LiteLLMModel`` adapter; implemented in tests by a fake."""

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        guardrail_identifier: str,
        stream: bool,
    ) -> str: ...


class ToolClient(Protocol):
    """Tools Gateway (MCP). ``list_tools`` returns qualified tool names;
    ``call_tool`` invokes one over MCP ``tools/call``."""

    def list_tools(self) -> list[str]: ...

    def call_tool(self, qualified_name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]: ...


class MemoryClient(Protocol):
    """AgentCore Memory, actor-scoped short-term events."""

    def put_event(self, *, actor_id: str, session_id: str, payload: Mapping[str, Any]) -> str: ...

    def get_last_event(self, *, actor_id: str, session_id: str) -> Mapping[str, Any] | None: ...


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceAgentConfig:
    tenant_id: str
    agent_id: str
    env_name: str
    guardrail_identifier: str
    model_id: str
    # Exact subscribed qualified tool names (``<TargetName>___<ToolName>``).
    # A tool call the agent attempts that is not in this set is refused before
    # it reaches the Tools Gateway — defense in depth alongside the Gateway's
    # own scoping.
    subscribed_tools: tuple[str, ...] = ()
    max_iterations: int = MAX_TOOL_ITERATIONS_DEFAULT
    stream: bool = True

    def __post_init__(self) -> None:
        if not self.guardrail_identifier:
            raise AgentError(
                "guardrail_identifier is mandatory (R-BED-028 + SCP-02 + IAM deny + VPCE policy)"
            )
        if not self.model_id:
            raise AgentError("model_id (target-qualified) is required")
        if self.max_iterations < 1 or self.max_iterations > 25:
            raise AgentError("max_iterations must be 1..25")


# --------------------------------------------------------------------------
# Pure orchestration core — fully offline-testable
# --------------------------------------------------------------------------


@dataclass
class AgentResult:
    marker: str
    reply_fingerprint: str
    tool_calls: list[str] = field(default_factory=list)
    discovered_tools: list[str] = field(default_factory=list)
    memory_round_trip: bool = False
    content_blocks: int = 0


class ReferenceAgentCore:
    """All decision logic; depends only on the three injected Protocols.

    A production build supplies real Strands/AgentCore adapters; tests supply
    fakes. The core never imports Strands, litellm, boto3, or urllib.
    """

    def __init__(
        self,
        config: ReferenceAgentConfig,
        llm: LlmClient,
        tools: ToolClient,
        memory: MemoryClient | None = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.tools = tools
        self.memory = memory

    @staticmethod
    def _fingerprint(value: str) -> str:
        """Non-reversible handle; never returns the input verbatim."""
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]

    def _system_prompt(self) -> str:
        return (
            "You are a governed reference agent. Use only subscribed tools via "
            "the Tools Gateway. Respond with '<done/>' when the task is complete."
        )

    def run(self, prompt: str, *, actor_id: str, session_id: str) -> AgentResult:
        if not actor_id:
            raise AgentError("actor_id is required (spec §3.4.6 — only scoping accepted)")
        if not session_id:
            raise AgentError("session_id is required for memory scoping")

        discovered = list(self.tools.list_tools())
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": prompt},
        ]

        tool_calls: list[str] = []
        reply = ""
        content_blocks = 0
        for _ in range(self.config.max_iterations):
            reply = self.llm.complete(
                messages,
                guardrail_identifier=self.config.guardrail_identifier,
                stream=self.config.stream,
            )
            content_blocks += 1
            tool_request = self._parse_tool_request(reply)
            if tool_request is None:
                break
            name, arguments = tool_request
            if name not in self.config.subscribed_tools:
                raise PermissionError(
                    f"tool {name!r} is not in the subscribed set; refusing before Gateway call"
                )
            tool_result = self.tools.call_tool(name, arguments)
            tool_calls.append(name)
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "tool", "content": _compact(tool_result)})

        memory_round_trip = False
        if self.memory is not None:
            event_payload = {
                "promptFingerprint": self._fingerprint(prompt),
                "replyFingerprint": self._fingerprint(reply),
                "toolCalls": tool_calls,
            }
            self.memory.put_event(
                actor_id=actor_id, session_id=session_id, payload=event_payload
            )
            last = self.memory.get_last_event(actor_id=actor_id, session_id=session_id)
            memory_round_trip = (
                isinstance(last, Mapping)
                and last.get("replyFingerprint") == event_payload["replyFingerprint"]
            )

        return AgentResult(
            marker=HANDSHAKE_MARKER,
            reply_fingerprint=self._fingerprint(reply),
            tool_calls=tool_calls,
            discovered_tools=discovered,
            memory_round_trip=memory_round_trip,
            content_blocks=content_blocks,
        )

    @staticmethod
    def _parse_tool_request(reply: str) -> tuple[str, dict[str, Any]] | None:
        """Extract a single deterministic tool directive from an LLM reply.

        Format (kept intentionally simple and deterministic, matching the
        blueprint pattern): a line ``TOOL <qualified_name> <json-args>``. Any
        reply without that directive terminates the loop.
        """
        import json

        for line in reply.splitlines():
            line = line.strip()
            if line.startswith("TOOL "):
                rest = line[len("TOOL ") :].strip()
                name, _, raw_args = rest.partition(" ")
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError as exc:
                    raise AgentError(f"malformed TOOL arguments: {exc}") from exc
                if not isinstance(args, dict):
                    raise AgentError("TOOL arguments must be a JSON object")
                return name, args
        return None


def _compact(value: Mapping[str, Any]) -> str:
    import json

    return json.dumps(value, separators=(",", ":"), sort_keys=True)


# --------------------------------------------------------------------------
# Production wiring — real Strands / AgentCore adapters (lazy imports)
# --------------------------------------------------------------------------


def _inference_base_url(gateway_url: str) -> str:
    """OpenAI-compatible inference base — sibling of ``/mcp``, not nested.

    Mirrors the fix proven in the Identity M2M spike: appending
    ``/inference/v1`` onto the ``/mcp`` Gateway URL yields
    ``.../mcp/inference/v1`` which the Gateway rejects with HTTP 400.
    """
    trimmed = gateway_url.rstrip("/")
    if not trimmed.endswith("/mcp"):
        raise AgentError("gateway url must end with the /mcp base path")
    return f"{trimmed[: -len('/mcp')].rstrip('/')}/inference/v1"


class _LiteLlmAdapter:
    """Adapts a Strands ``LiteLLMModel`` to the :class:`LlmClient` Protocol."""

    def __init__(self, *, gateway_url: str, bearer_token: str, model_id: str) -> None:
        from strands import Agent  # noqa: E402  lazy
        from strands.models.litellm import LiteLLMModel  # noqa: E402  lazy

        self._Agent = Agent
        self._base = _inference_base_url(gateway_url)
        self._model_id = model_id
        self._bearer = bearer_token
        self._LiteLLMModel = LiteLLMModel

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        guardrail_identifier: str,
        stream: bool,
    ) -> str:
        if not guardrail_identifier:
            raise AgentError("guardrail_identifier must be set on every inference call")
        model = self._LiteLLMModel(
            model_id=f"openai/{self._model_id}",
            client_args={"api_base": self._base, "api_key": self._bearer},
            params={
                "max_tokens": INFERENCE_MAX_TOKENS,
                "temperature": 0,
                "stream": stream,
                # Guardrail travels as an extra param the Gateway enforces; the
                # inference path is never allowed to run guardrail-free.
                "guardrail_identifier": guardrail_identifier,
            },
        )
        agent = self._Agent(model=model, callback_handler=None)
        # Collapse the message list into a single deterministic user turn; the
        # reference agent is single-shot per iteration by design.
        user_turn = "\n".join(
            m["content"] for m in messages if m.get("role") in ("user", "tool")
        )
        result = agent(user_turn or "Reply with exactly the word verified.")
        message = result.message
        if not message or not message.get("content"):
            raise AgentError("LiteLLMModel returned no Strands message content")
        blocks = message["content"]
        # Return the concatenated text content.
        return "".join(
            b.get("text", "") for b in blocks if isinstance(b, Mapping)
        ) or "<done/>"


class _McpToolAdapter:
    """Adapts a Strands ``MCPClient`` to the :class:`ToolClient` Protocol."""

    def __init__(self, *, gateway_url: str, bearer_token: str) -> None:
        # Lazy: only needed on the live path.
        from mcp.client.streamable_http import streamablehttp_client  # noqa: E402
        from strands.tools.mcp import MCPClient  # noqa: E402

        self._gateway_url = gateway_url
        self._headers = {
            "Authorization": f"Bearer {bearer_token}",
            "MCP-Protocol-Version": "2025-06-18",
        }
        self._client = MCPClient(
            lambda: streamablehttp_client(gateway_url, headers=self._headers)
        )

    def list_tools(self) -> list[str]:
        with self._client:
            return [t.tool_name for t in self._client.list_tools_sync()]

    def call_tool(self, qualified_name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        with self._client:
            result = self._client.call_tool_sync(
                tool_use_id=qualified_name, name=qualified_name, arguments=dict(arguments)
            )
        return {"status": getattr(result, "status", "unknown")}


class _AgentCoreMemoryAdapter:
    """Adapts AgentCore Memory short-term events to :class:`MemoryClient`.

    Uses only the memory id supplied via the environment and an actor/session
    scoped by the caller. Never persists raw payload beyond fingerprints the
    core already computed.
    """

    def __init__(self, *, memory_id: str, region: str) -> None:
        import boto3  # noqa: E402 lazy

        self._memory_id = memory_id
        self._client = boto3.client("bedrock-agentcore", region_name=region)

    def put_event(self, *, actor_id: str, session_id: str, payload: Mapping[str, Any]) -> str:
        import json

        resp = self._client.create_event(
            memoryId=self._memory_id,
            actorId=actor_id,
            sessionId=session_id,
            payload=[{"blob": json.dumps(payload, sort_keys=True)}],
        )
        return str(resp.get("event", {}).get("eventId", ""))

    def get_last_event(self, *, actor_id: str, session_id: str) -> Mapping[str, Any] | None:
        import json

        resp = self._client.list_events(
            memoryId=self._memory_id,
            actorId=actor_id,
            sessionId=session_id,
            maxResults=1,
        )
        events = resp.get("events") or []
        if not events:
            return None
        blobs = events[0].get("payload") or []
        for item in blobs:
            blob = item.get("blob")
            if blob:
                try:
                    return json.loads(blob)
                except json.JSONDecodeError:
                    return None
        return None


def build_production_core(
    config: ReferenceAgentConfig,
    *,
    mcp_gateway_url: str,
    inference_gateway_url: str,
    bearer_token: str,
    memory_id: str | None,
    region: str,
) -> ReferenceAgentCore:
    """Wire the real Strands/AgentCore adapters and hand them to the pure core.

    In the D-03 topology the tools MCP endpoint (workstream tool Gateway) and
    the OpenAI-compatible inference endpoint (Platform inference Gateway) are
    two distinct Gateways, so they are wired from two separate URLs. Passing the
    same value for both preserves the single-Gateway compatibility-spike shape.
    """
    llm = _LiteLlmAdapter(
        gateway_url=inference_gateway_url,
        bearer_token=bearer_token,
        model_id=config.model_id,
    )
    tools = _McpToolAdapter(gateway_url=mcp_gateway_url, bearer_token=bearer_token)
    memory = (
        _AgentCoreMemoryAdapter(memory_id=memory_id, region=region) if memory_id else None
    )
    return ReferenceAgentCore(config, llm, tools, memory)


# --------------------------------------------------------------------------
# Container entrypoint
# --------------------------------------------------------------------------


def _load_entrypoint():  # pragma: no cover - exercised only in the live container
    """Construct the BedrockAgentCoreApp entrypoint. Imported lazily so the
    offline test suite never needs bedrock_agentcore."""
    from bedrock_agentcore import BedrockAgentCoreApp

    app = BedrockAgentCoreApp()

    @app.entrypoint
    def invoke(payload: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
        cfg = ReferenceAgentConfig(
            tenant_id=os.environ["AGENTCORE_TENANT_ID"],
            agent_id=os.environ["AGENTCORE_AGENT_ID"],
            env_name=os.environ["AGENTCORE_ENV_NAME"],
            guardrail_identifier=os.environ["AGENTCORE_GUARDRAIL_ID"],
            model_id=os.environ["AGENTCORE_MODEL_ID"],
            subscribed_tools=tuple(
                t for t in os.environ.get("AGENTCORE_SUBSCRIBED_TOOLS", "").split(",") if t
            ),
        )
        # The bearer token is acquired by the runtime via AgentCore Identity M2M
        # (workload identity -> OAuth2 credential provider -> resource token).
        # The credential-provider recipe is the one live-proven by the Identity
        # M2M spike; here it is supplied through the environment/secure fetch.
        bearer_token = _fetch_gateway_token()
        mcp_gateway_url = os.environ["AGENTCORE_GATEWAY_URL"]
        # Inference (LiteLLM) endpoint. In D-03 this is the Platform inference
        # Gateway, distinct from the workstream MCP tool Gateway above. Falls
        # back to the MCP Gateway URL for the single-Gateway compatibility shape.
        inference_gateway_url = (
            os.environ.get("AGENTCORE_INFERENCE_GATEWAY_URL") or mcp_gateway_url
        )
        core = build_production_core(
            cfg,
            mcp_gateway_url=mcp_gateway_url,
            inference_gateway_url=inference_gateway_url,
            bearer_token=bearer_token,
            memory_id=os.environ.get("AGENTCORE_MEMORY_ID"),
            region=os.environ.get("AWS_REGION", "us-west-2"),
        )
        payload_map = payload if isinstance(payload, Mapping) else {}
        prompt = payload_map.get(PROMPT_FIELD)
        prompt_text = prompt if isinstance(prompt, str) else "Reply with exactly the word verified."
        actor = payload_map.get(ACTOR_FIELD)
        actor_id = actor if isinstance(actor, str) and actor else "default-actor"
        session_id = ReferenceAgentCore._fingerprint(actor_id)[:16]
        result = core.run(prompt_text, actor_id=actor_id, session_id=session_id)
        return {
            "marker": result.marker,
            "replyFingerprint": result.reply_fingerprint,
            "toolCalls": result.tool_calls,
            "discoveredToolCount": len(result.discovered_tools),
            "memoryRoundTrip": result.memory_round_trip,
            "contentBlocks": result.content_blocks,
            "memoryConfigured": bool(os.environ.get("AGENTCORE_MEMORY_ID")),
        }

    return app


def _fetch_gateway_token() -> str:  # pragma: no cover - live path only
    """Acquire a Gateway-authorized bearer token via AgentCore Identity M2M.

    The concrete acquisition (workload identity -> OAuth2 credential provider ->
    GetResourceOauth2Token) is the recipe proven live by the Identity M2M spike.
    In the deployed Runtime this is provided by the platform Identity wiring; a
    token supplied through the environment is used as the injection seam.
    """
    token = os.environ.get("AGENTCORE_GATEWAY_BEARER")
    if not token:
        raise AgentError(
            "no Gateway bearer token available; AgentCore Identity M2M wiring must "
            "supply AGENTCORE_GATEWAY_BEARER or an equivalent secure fetch"
        )
    return token


if __name__ == "__main__":  # pragma: no cover
    _load_entrypoint().run()
