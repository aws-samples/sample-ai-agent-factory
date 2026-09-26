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
* Every LLM call carries a non-empty ``guardrail_identifier`` — a CLIENT-SIDE
  hygiene invariant. Live 2026-09-24: the AgentCore inference Gateway forwards
  the request to Bedrock Mantle under its own role and ignores this parameter.
  Server-side enforcement is therefore the Gateway REQUEST interceptor
  (``packages/platform-inference-gateway/lambda/guardrail-interceptor``), which
  applies the stage baseline Guardrail to every request body and fails closed;
  a blocked request surfaces here as HTTP 403 from the inference call. Original
  rationale for the client-side invariant: R-BED-028 + SCP-02 + IAM deny + VPCE
  policy on the D-01 direct-Bedrock path.
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
#: Semantic version of this agent revision, reported in every response as
#: ``agentVersion`` so an upgrade campaign (and fleet inventory) can confirm
#: which revision is serving without reading container digests. Bump it on
#: every behaviour-changing agent release; the deployment-continuity probe
#: gates on the observed transition.
AGENT_VERSION = "1.2.2"
#: Environment variables the container reads its AWS Region from, in order.
#: The Runtime stack sets ``AGENTCORE_REGION`` to the Region it deploys into;
#: the other two are the ambient AWS variables boto3 itself honours.
REGION_ENV_VARS = ("AGENTCORE_REGION", "AWS_REGION", "AWS_DEFAULT_REGION")
# Protocol terminator the model emits when the task is complete.
DONE_MARKER = "<done/>"
#: Corrective turns the loop may spend on replies that carry neither a TOOL
#: directive nor the done marker (they count toward ``max_iterations`` too).
#: Live 2026-09-25: the rated reasoning model occasionally leaks a reasoning
#: fragment glued in front of the directive ('We need to output the TOOL
#: line.TOOL <name> {...}'), which the line-start grammar correctly refuses to
#: execute; the loop used to treat that reply as task completion. One notice
#: repaired every measured case (10/10); two bound the cost.
MAX_PROTOCOL_REPAIRS = 2
#: Corrective user-role turn. Plain and non-imperative on purpose: it is an
#: untrusted turn to the Gateway's guardrail interceptor, and this wording
#: scored clean (10/10 allowed, 10/10 repaired, live 2026-09-25).
PROTOCOL_NOTICE = (
    "Agent runtime notice: the previous reply had no tool request and no "
    f"{DONE_MARKER} marker, so nothing was executed."
)
#: Why the bounded loop stopped, reported as ``stopReason``.
STOP_DONE = "done"
STOP_PROTOCOL_VIOLATION = "protocol_violation"
STOP_MAX_ITERATIONS = "max_iterations"

#: The scalar field the entrypoint reads from the invocation payload.
PROMPT_FIELD = "prompt"

#: Actor field: AgentCore Memory is scoped by this alone (spec §3.4.6).
ACTOR_FIELD = "actorId"

#: Bounded tool-use loop — mirrors the blueprint task-agent max-iteration guard.
MAX_TOOL_ITERATIONS_DEFAULT = 6

#: Bounded, deterministic inference token cap. Live-proven 2026-09-23 against
#: the rated ``openai.gpt-oss-120b`` (a reasoning model whose hidden reasoning
#: counts toward ``max_tokens``): the spike's one-word prompt fit in 256, but
#: the reference agent's tool-selection turn exhausted 256 before any visible
#: output and Strands raised ``MaxTokensReachedException``. 2048 leaves
#: headroom for reasoning plus a one-line ``TOOL`` directive while staying a
#: hard, small bound per turn (the loop is additionally iteration-capped).
INFERENCE_MAX_TOKENS = 2048


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
    """AgentCore Memory, actor-scoped short-term events.

    ``put_event`` returns the service-minted ``eventId``; ``get_event`` reads
    that exact record back. ``ListEvents`` ordering is unspecified by the API
    reference, so a "latest event" read is not a valid round-trip proof.
    """

    def put_event(self, *, actor_id: str, session_id: str, payload: Mapping[str, Any]) -> str: ...

    def get_event(
        self, *, actor_id: str, session_id: str, event_id: str
    ) -> Mapping[str, Any] | None: ...


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
    stop_reason: str = STOP_DONE
    protocol_repairs: int = 0


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
        # Live-proven 2026-09-23 (inference_prompt_probe.py, 4/4 identical
        # replies at temperature 0): once the system turn actually reaches the
        # model it emits the exact TOOL directive. State the protocol and the
        # subscribed names explicitly so compliance never depends on the user
        # prompt alone; the core still refuses any name outside the allowlist.
        tools = ", ".join(self.config.subscribed_tools) or "(none subscribed)"
        return (
            "You are a governed reference agent. You can call tools ONLY through "
            "the Tools Gateway using this exact protocol: reply with a single line "
            "'TOOL <qualified_tool_name> <json_object_arguments>' and nothing else. "
            f"Subscribed tools: {tools}. When you call a tool, the TOOL line must "
            "be your entire reply -- do not describe, explain, quote or wrap it. "
            f"Respond with '{DONE_MARKER}' when the task is complete. If you "
            f"decline or cannot complete the task, say so briefly and end that "
            f"reply with '{DONE_MARKER}'."
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
        repairs = 0
        stop_reason = STOP_MAX_ITERATIONS
        for _ in range(self.config.max_iterations):
            reply = self.llm.complete(
                messages,
                guardrail_identifier=self.config.guardrail_identifier,
                stream=self.config.stream,
            )
            content_blocks += 1
            tool_request = self._parse_tool_request(reply)
            if tool_request is None:
                if DONE_MARKER in reply:
                    stop_reason = STOP_DONE
                    break
                # Neither a directive nor the terminator: the task is NOT
                # complete. Never execute a directive buried mid-line (it may
                # be quoted reasoning); ask once more within the budget.
                if repairs >= MAX_PROTOCOL_REPAIRS:
                    stop_reason = STOP_PROTOCOL_VIOLATION
                    break
                repairs += 1
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": PROTOCOL_NOTICE})
                continue
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
            event_id = self.memory.put_event(
                actor_id=actor_id, session_id=session_id, payload=event_payload
            )
            if not event_id:
                raise AgentError("Memory put_event returned no eventId")
            last = self.memory.get_event(
                actor_id=actor_id, session_id=session_id, event_id=event_id
            )
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
            stop_reason=stop_reason,
            protocol_repairs=repairs,
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
                raw_args = raw_args.strip()
                if not raw_args:
                    return name, {}
                # Decode exactly one JSON value from the start of the arguments.
                # The rated reasoning model appends the done marker to the same
                # line in about two of three runs (live 2026-09-24:
                # 'TOOL <name> {"message":"probe"}<done/>'); the marker is part
                # of the protocol, so it is the only trailing text tolerated.
                # Anything else after the object still fails closed.
                try:
                    args, end = json.JSONDecoder().raw_decode(raw_args)
                except json.JSONDecodeError as exc:
                    raise AgentError(f"malformed TOOL arguments: {exc}") from exc
                trailing = raw_args[end:].strip()
                if trailing and trailing != DONE_MARKER:
                    raise AgentError(
                        f"malformed TOOL arguments: unexpected trailing text {trailing[:40]!r}"
                    )
                if not isinstance(args, dict):
                    raise AgentError("TOOL arguments must be a JSON object")
                return name, args
        return None


def _compact(value: Mapping[str, Any]) -> str:
    import json

    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _split_history(
    messages: Sequence[Mapping[str, str]],
) -> tuple[list[dict[str, Any]], str]:
    """Split the agent's message list into Strands history and the prompt.

    Returns ``(history, prompt)`` where ``history`` holds every earlier
    non-system turn in Strands/Bedrock message form (``user``/``assistant``
    roles with text content blocks; a ``tool`` result is a user-role turn in
    this text protocol) and ``prompt`` is the text of the newest user or tool
    turn. Each untrusted turn therefore reaches the Gateway as its own message
    and is guardrail-scored on its own.
    """
    turns = [m for m in messages if m.get("role") in ("user", "assistant", "tool")]
    if not turns or turns[-1].get("role") == "assistant":
        history_turns, prompt = turns, ""
    else:
        history_turns, prompt = turns[:-1], turns[-1]["content"]
    history: list[dict[str, Any]] = []
    for turn in history_turns:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        history.append({"role": role, "content": [{"text": turn["content"]}]})
    return history, prompt


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
        # The system turn was previously dropped on the floor (only user/tool
        # roles were joined), so the model never saw the TOOL protocol. Hand it
        # to Strands as the agent's system prompt.
        system_prompt = "\n".join(
            m["content"] for m in messages if m.get("role") == "system"
        ) or None
        # Keep the turn structure: prior user/assistant/tool turns become
        # Strands conversation history and only the newest untrusted turn is
        # the prompt. The Gateway guardrail interceptor scores every untrusted
        # turn on its own; the earlier collapse of user+tool turns into one
        # user turn made the prompt-attack classifier score the concatenation
        # (live 2026-09-24: a benign request plus a benign tool result tripped
        # PROMPT_ATTACK LOW only when joined). Tool results are user-role input
        # in this text protocol, which also keeps user/assistant alternation.
        history, prompt = _split_history(messages)
        agent = self._Agent(
            model=model,
            callback_handler=None,
            system_prompt=system_prompt,
            messages=history,
        )
        result = agent(prompt or "Reply with exactly the word verified.")
        message = result.message
        if not message or not message.get("content"):
            raise AgentError("LiteLLMModel returned no Strands message content")
        blocks = message["content"]
        # Return the concatenated text content. An empty reply stays empty: the
        # core treats it as a protocol violation instead of a completed task
        # (it used to be replaced by the done marker, which hid the failure).
        return "".join(
            b.get("text", "") for b in blocks if isinstance(b, Mapping)
        )


class _McpToolAdapter:
    """Adapts a Strands ``MCPClient`` to the :class:`ToolClient` Protocol.

    Supports two Gateway auth models:

    * ``auth_mode="sigv4"`` — the workstream tool Gateway is ``AWS_IAM``; every
      MCP request is SigV4-signed with the container's ambient AWS credentials
      for service ``bedrock-agentcore``. No bearer token is used.
    * ``auth_mode="bearer"`` — the single-Gateway ``CUSTOM_JWT`` compatibility
      shape; a static ``Authorization: Bearer`` header is sent.
    """

    def __init__(
        self,
        *,
        gateway_url: str,
        auth_mode: str = "sigv4",
        bearer_token: str | None = None,
        region: str,
    ) -> None:
        # Lazy: only needed on the live path.
        from mcp.client.streamable_http import streamablehttp_client  # noqa: E402
        from strands.tools.mcp import MCPClient  # noqa: E402

        self._gateway_url = gateway_url
        self._auth_mode = auth_mode
        base_headers = {"MCP-Protocol-Version": "2025-06-18"}

        if auth_mode == "bearer":
            if not bearer_token:
                raise AgentError("bearer auth_mode requires a bearer token")
            headers = {**base_headers, "Authorization": f"Bearer {bearer_token}"}
            self._client = MCPClient(
                lambda: streamablehttp_client(gateway_url, headers=headers)
            )
        elif auth_mode == "sigv4":
            auth = _SigV4HttpxAuth(service="bedrock-agentcore", region=region)
            self._client = MCPClient(
                lambda: streamablehttp_client(
                    gateway_url, headers=base_headers, auth=auth
                )
            )
        else:
            raise AgentError(f"unsupported MCP auth_mode: {auth_mode!r}")

    def list_tools(self) -> list[str]:
        with self._client:
            return [t.tool_name for t in self._client.list_tools_sync()]

    def call_tool(self, qualified_name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        with self._client:
            result = self._client.call_tool_sync(
                tool_use_id=qualified_name, name=qualified_name, arguments=dict(arguments)
            )
        return {"status": getattr(result, "status", "unknown")}


class _SigV4HttpxAuth:
    """Callable httpx auth function that SigV4-signs each request.

    ``httpx`` wraps callable auth objects in its sync/async-compatible
    ``FunctionAuth`` adapter. The signer uses botocore over the default
    credential chain (the AgentCore-vended Runtime execution role in the
    container). It never logs credentials or signed headers.
    """

    def __init__(self, *, service: str, region: str) -> None:
        import boto3  # noqa: E402 lazy

        self._service = service
        self._region = region
        self._session = boto3.Session()

    def __call__(self, request):
        from botocore.auth import SigV4Auth  # noqa: E402 lazy
        from botocore.awsrequest import AWSRequest  # noqa: E402 lazy

        credentials = self._session.get_credentials()
        if credentials is None:
            raise AgentError("no AWS credentials available for SigV4 MCP signing")
        frozen = credentials.get_frozen_credentials()
        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers={
                k: v
                for k, v in request.headers.items()
                # botocore recomputes these; passing them in breaks the signature.
                if k.lower() not in ("authorization", "x-amz-date", "x-amz-security-token")
            },
        )
        SigV4Auth(frozen, self._service, self._region).add_auth(aws_request)
        for key, value in aws_request.headers.items():
            request.headers[key] = value
        return request


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
        from datetime import datetime, timezone

        # Shape pinned against the botocore model and LIVE behaviour
        # (2026-09-23): ``eventTimestamp`` is REQUIRED. The ``blob`` union
        # member accepts a document but the service returns it as a lossy
        # ``{k=v, ...}`` rendering, so it cannot round-trip structured data;
        # the ``conversational`` text member round-trips byte-exact (the same
        # member the live-proven runtime-memory spike uses). Carry the record
        # as canonical JSON text in an ASSISTANT turn.
        resp = self._client.create_event(
            memoryId=self._memory_id,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=datetime.now(timezone.utc),
            payload=[
                {
                    "conversational": {
                        "role": "ASSISTANT",
                        "content": {"text": json.dumps(payload, sort_keys=True)},
                    }
                }
            ],
        )
        return str(resp.get("event", {}).get("eventId", ""))

    def get_event(
        self, *, actor_id: str, session_id: str, event_id: str
    ) -> Mapping[str, Any] | None:
        import json

        from botocore.exceptions import ClientError

        # Read back the exact record by id (same call the live-proven
        # runtime-memory spike uses); ListEvents ordering is unspecified.
        try:
            resp = self._client.get_event(
                memoryId=self._memory_id,
                actorId=actor_id,
                sessionId=session_id,
                eventId=event_id,
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return None
            raise
        blocks = (resp.get("event") or {}).get("payload") or []
        for item in blocks:
            conversational = item.get("conversational")
            if not isinstance(conversational, Mapping):
                continue
            content = conversational.get("content")
            text = content.get("text") if isinstance(content, Mapping) else None
            if not isinstance(text, str):
                continue
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                return None
            return decoded if isinstance(decoded, Mapping) else None
        return None


def build_production_core(
    config: ReferenceAgentConfig,
    *,
    mcp_gateway_url: str,
    inference_gateway_url: str,
    inference_bearer_token: str,
    memory_id: str | None,
    region: str,
    mcp_auth: str = "sigv4",
    mcp_bearer_token: str | None = None,
) -> ReferenceAgentCore:
    """Wire the real Strands/AgentCore adapters and hand them to the pure core.

    D-03 uses two distinct Gateways with two auth models:

    * Inference (Platform inference Gateway, ``CUSTOM_JWT``): the LiteLLM adapter
      calls ``<inference_gateway_url>/inference/v1`` with a Cognito M2M bearer
      token (``inference_bearer_token``).
    * Tools (workstream tool Gateway, ``AWS_IAM``): the MCP adapter SigV4-signs
      each request with the container's ambient AWS credentials
      (``mcp_auth="sigv4"``).

    Passing ``mcp_auth="bearer"`` with ``mcp_bearer_token`` and the same URL for
    both preserves the single-Gateway ``CUSTOM_JWT`` compatibility-spike shape.
    """
    llm = _LiteLlmAdapter(
        gateway_url=inference_gateway_url,
        bearer_token=inference_bearer_token,
        model_id=config.model_id,
    )
    tools = _McpToolAdapter(
        gateway_url=mcp_gateway_url,
        auth_mode=mcp_auth,
        bearer_token=mcp_bearer_token,
        region=region,
    )
    memory = (
        _AgentCoreMemoryAdapter(memory_id=memory_id, region=region) if memory_id else None
    )
    return ReferenceAgentCore(config, llm, tools, memory)


# --------------------------------------------------------------------------
# Container entrypoint
# --------------------------------------------------------------------------


def _resolve_region() -> str:
    """Return the AWS Region for the SigV4 tool client, Memory and Identity.

    There is deliberately no hard-coded default. A wrong Region signs tool
    calls for the wrong endpoint and looks up Memory and the workload identity
    where they do not exist, and a default equal to the reference Region hides
    that on every run made there. A missing Region therefore fails closed.
    """
    for name in REGION_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise AgentError(
        "no AWS Region configured: set AGENTCORE_REGION (the Runtime stack does) "
        "or AWS_REGION"
    )


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
        # Two auth models (D-03): the inference bearer is a Cognito M2M token
        # for the Platform inference Gateway; MCP tool calls SigV4-sign against
        # the AWS_IAM workstream tool Gateway with the container's ambient creds.
        inference_bearer = _fetch_inference_bearer()
        mcp_gateway_url = os.environ["AGENTCORE_GATEWAY_URL"]
        # Inference (LiteLLM) endpoint. In D-03 this is the Platform inference
        # Gateway, distinct from the workstream MCP tool Gateway above. Falls
        # back to the MCP Gateway URL for the single-Gateway compatibility shape.
        inference_gateway_url = (
            os.environ.get("AGENTCORE_INFERENCE_GATEWAY_URL") or mcp_gateway_url
        )
        # MCP auth mode: default SigV4 (AWS_IAM Gateway). Set "bearer" only for
        # the single-Gateway CUSTOM_JWT compatibility shape.
        mcp_auth = os.environ.get("AGENTCORE_MCP_AUTH", "sigv4")
        core = build_production_core(
            cfg,
            mcp_gateway_url=mcp_gateway_url,
            inference_gateway_url=inference_gateway_url,
            inference_bearer_token=inference_bearer,
            memory_id=os.environ.get("AGENTCORE_MEMORY_ID"),
            region=_resolve_region(),
            mcp_auth=mcp_auth,
            mcp_bearer_token=os.environ.get("AGENTCORE_GATEWAY_BEARER"),
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
            "agentVersion": AGENT_VERSION,
            "replyFingerprint": result.reply_fingerprint,
            "toolCalls": result.tool_calls,
            "discoveredToolCount": len(result.discovered_tools),
            "memoryRoundTrip": result.memory_round_trip,
            "contentBlocks": result.content_blocks,
            "stopReason": result.stop_reason,
            "protocolRepairs": result.protocol_repairs,
            "memoryConfigured": bool(os.environ.get("AGENTCORE_MEMORY_ID")),
        }

    return app


def _fetch_inference_bearer() -> str:  # pragma: no cover - live path only
    """Acquire a Cognito M2M bearer for the Platform inference Gateway.

    Sanctioned path (live-proven by the Identity M2M spike): AgentCore Identity.
    The Runtime mints a workload-identity token by name via
    ``GetWorkloadAccessToken``; a pre-created ``CognitoOauth2`` credential
    provider (seeded once with the inference Gateway's Cognito client id +
    secret) exchanges it for a resource token at the Gateway scope via
    ``GetResourceOauth2Token``. No cross-account Cognito describe and no raw
    Secrets Manager read is required — the provider holds the secret.

    ``AGENTCORE_INFERENCE_BEARER`` is an explicit injection seam (tests /
    pre-fetched token) that short-circuits the fetch. Never logs the token.
    """
    seam = os.environ.get("AGENTCORE_INFERENCE_BEARER")
    if seam:
        return seam

    provider_name = os.environ["AGENTCORE_INFERENCE_CREDENTIAL_PROVIDER"]
    workload_name = os.environ["AGENTCORE_WORKLOAD_IDENTITY_NAME"]
    scope = os.environ["AGENTCORE_INFERENCE_SCOPE"]
    region = _resolve_region()

    import boto3  # noqa: E402 lazy

    identity = boto3.client("bedrock-agentcore", region_name=region)
    workload_resp = identity.get_workload_access_token(workloadName=workload_name)
    workload_token = workload_resp.get("workloadAccessToken")
    if not workload_token:
        raise AgentError("GetWorkloadAccessToken did not return a workload token")
    try:
        resp = identity.get_resource_oauth2_token(
            workloadIdentityToken=workload_token,
            resourceCredentialProviderName=provider_name,
            scopes=[scope],
            oauth2Flow="M2M",
        )
    finally:
        del workload_token
    token = resp.get("accessToken") or resp.get("access_token")
    if not token:
        raise AgentError("GetResourceOauth2Token did not return an access token")
    return str(token)


if __name__ == "__main__":  # pragma: no cover
    _load_entrypoint().run()
