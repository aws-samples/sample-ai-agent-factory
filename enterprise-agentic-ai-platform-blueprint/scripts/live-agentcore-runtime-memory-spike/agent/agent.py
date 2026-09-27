"""Deterministic AgentCore Runtime entrypoint for the compatibility probe.

This agent is intentionally inert. Its only job is to prove that a container
built from ``Dockerfile`` satisfies the AgentCore Runtime contract:

* it wraps its handler with :class:`BedrockAgentCoreApp` (which serves the
  ``/invocations`` and ``/ping`` HTTP contract on port 8080), and
* it answers one deterministic handshake so the ``verify`` step can confirm an
  ``InvokeAgentRuntime`` round-trip without depending on any model, tool, or
  memory call.

It does NOT call Bedrock, MCP tools, or Memory. Those are the real platform
integrations a production agent would use, and this file marks exactly where
they would attach -- but leaves them unwired on purpose. Faking them would make
a probe report success for behaviour it never exercised, which is the opposite
of what a compatibility probe is for.

Security posture:

* Never log, echo, or persist the raw request payload -- it can carry caller
  data. The handler reads a single scalar field and returns a fixed marker plus
  a non-reversible fingerprint of the correlation id.
* Never log tokens, headers, or session identifiers.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Mapping

from bedrock_agentcore import BedrockAgentCoreApp

#: Deterministic marker the ``verify`` step matches on. Its presence in the
#: response is how a live invocation is confirmed without recording any request
#: content. Keep it in sync with ``runtime_memory_model.HANDSHAKE_MARKER``.
HANDSHAKE_MARKER = "agentcore-runtime-memory-spike-ok"

#: The one scalar field the handler reads from the payload. Anything else in the
#: payload is ignored and never surfaced.
PING_FIELD = "ping"

app = BedrockAgentCoreApp()


def _fingerprint(value: str) -> str:
    """Non-reversible correlation handle. Never returns the input verbatim."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _handshake(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Pure handshake: fixed marker + fingerprint of a single scalar field.

    Separated from the entrypoint so it can be reasoned about in isolation and
    is trivially safe to unit test offline. It touches no AWS service.
    """
    correlation = payload.get(PING_FIELD)
    correlation_text = correlation if isinstance(correlation, str) else "default"
    return {
        "marker": HANDSHAKE_MARKER,
        "echoFingerprint": _fingerprint(correlation_text),
        "runtimeReady": True,
    }


@app.entrypoint
def invoke(payload: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """AgentCore Runtime entrypoint. Returns a deterministic handshake only.

    Optional, deliberately-unwired future integration points (a production agent
    would attach these; this probe does not, so it never reports success for an
    integration it did not exercise):

    * LLM inference via the platform LLM Gateway -- ``LiteLLMModel`` pointed at
      the gateway's OpenAI-compatible endpoint. Never a direct Bedrock call.
    * Tools via the platform Tools Gateway -- ``MCPClient``. Never a direct
      Lambda invoke.
    * Short/long-term memory via AgentCore Memory, using only the memory id
      supplied through the environment and an actor/session scoped by the
      caller. This handler does not read or write memory.

    The environment may advertise a memory id (``AGENTCORE_MEMORY_ID``) so a
    later, explicitly-wired revision can pick it up; this handler only reports
    whether it was provided, never its value.
    """
    result = _handshake(payload if isinstance(payload, Mapping) else {})
    result["memoryConfigured"] = bool(os.environ.get("AGENTCORE_MEMORY_ID"))
    return result


if __name__ == "__main__":
    app.run()
