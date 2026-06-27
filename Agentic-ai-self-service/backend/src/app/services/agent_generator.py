"""NL agent generator — Phase 1 Gap 1E.

Takes a natural-language description and returns a canvas spec
(``{nodes, edges}``) ready for the frontend's ``instantiateTemplate``
helper. Mirrors the ``services.tool_generator`` two-turn pattern:
the first turn yields a ``clarification`` response with 2-4 questions,
subsequent turns use the conversation history to emit a generated
spec via Bedrock tool-use (function calling).

The output schema is a small subset of the frontend's
``WorkflowTemplate`` type — just enough to feed
``instantiateTemplate``. The generator avoids exposing every
component-config field; it picks safe defaults and the user
opens config modals to refine them per node.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import boto3

logger = logging.getLogger(__name__)


AGENT_GENERATOR_MODEL_ID = os.environ.get(
    "AGENT_GENERATOR_MODEL_ID",
    os.environ.get(
        "TOOL_GENERATOR_MODEL_ID",
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    ),
)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

CLARIFICATION_PROMPT = """You help users design AgentCore agents. The user has described an agent in natural language. \
Ask 2-4 clarifying questions about their request. Focus on:
- What knowledge sources should the agent search (S3 docs, web, KB)?
- Should the agent have memory across sessions?
- What tools (search, custom Lambdas, MCP servers) should it call?
- Should it have safety guardrails (PII filters, prompt-injection defense)?
- Should it run on a schedule, or only when invoked?

Return ONLY: {"responseType": "clarification", "message": "your questions here"}
No markdown. No text outside JSON."""


GENERATION_PROMPT = """Generate an AgentCore canvas spec from the conversation. Return ONLY a tool-use call to `submit_canvas`.

# CANVAS COMPONENT TYPES (use these `type` values exactly)

- `runtime` (REQUIRED, exactly one): the agent itself. Configuration:
    {"name": "<snake_case>", "framework": "strands_agents", "modelProvider": "bedrock",
     "model": {"modelId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0"},
     "systemPrompt": "<the agent's role and instructions>",
     "protocol": "HTTP", "pythonRuntime": "PYTHON_3_13", "enableOtel": false}
- `gateway` (OPTIONAL): MCP gateway with predefined tools. Configuration:
    {"name": "Gateway", "tools": [], "auth": "cognito"}
- `memory` (OPTIONAL): persistent memory. Configuration:
    {"name": "AgentMemory", "enabled": true, "eventExpiryDuration": 90,
     "strategies": [{"type": "semantic", "name": "semantic_strategy"}]}
- `tool` (OPTIONAL, repeatable): a built-in OR custom tool. Configuration:
    {"name": "ToolName", "toolId": "snake_case_id", "description": "...",
     "enabled": true, "isCustom": false}
  TOOL SELECTION RULES (critical — a wrong toolId yields a gateway with NO
  targets and a broken agent):
  * BUILT-IN tools — set `isCustom: false` and use EXACTLY one of these toolIds:
      - `duckduckgo_search`  (web search)
      - `wikipedia_search`   (encyclopedia lookup)
      - `weather_api`        (real-time weather + geocoding via Open-Meteo)
      - `web_page_fetcher`   (fetch a URL's text)
    Do NOT invent a built-in toolId. If the user asks for weather, use
    `weather_api`. If they ask to search the web, use `duckduckgo_search`.
  * CUSTOM tools — only when the need maps to NONE of the built-ins. Set
    `isCustom: true`, pick a unique snake_case `toolId`, and ALSO include:
      "inputSchema": {"type":"object","properties":{...},"required":[...]}
    A custom tool's Lambda code is generated automatically from its description +
    inputSchema, so write a precise, implementable description.
  * NEVER emit a `tool` node with `isCustom: false` whose toolId is not in the
    built-in list above.
- `guardrails` (OPTIONAL): Bedrock Guardrails. Configuration:
    {"name": "Guardrails", "enabled": true, "mode": "create_new",
     "contentFilters": {"hate": "HIGH", "sexual": "HIGH", "violence": "HIGH",
       "insults": "MEDIUM", "misconduct": "MEDIUM", "promptAttack": "HIGH"},
     "piiTypes": [], "deniedTopics": [], "blockedWords": []}
- `evaluation` (OPTIONAL): online evaluation config. Configuration:
    {"name": "Evaluation", "enabled": true,
     "evaluators": ["Builtin.GoalSuccessRate", "Builtin.Correctness"],
     "samplingRate": 100}
- `observability` (OPTIONAL): OTEL export. Configuration:
    {"name": "Observability", "enableOtel": true, "provider": "langfuse",
     "otlpEndpoint": "https://cloud.langfuse.com/api/public/otel",
     "otlpProtocol": "http/protobuf", "sampleRate": 1.0,
     "resourceAttributes": {}, "extraHeaders": {}}

# RULES

- The runtime node MUST come first in the `nodes` array.
- Most non-runtime nodes (memory, guardrails, evaluation, observability) MUST
  have an edge from themselves to the runtime (`source` = the support node,
  `target` = the runtime). `connectionType` is "data" for tool-like nodes and
  "control" for guardrails / observability / evaluation.
- TOOL WIRING (critical — `tool` nodes may NOT connect to the runtime directly):
  tools attach to a gateway, and the gateway attaches to the runtime. So whenever
  you emit ANY `tool` node you MUST also emit exactly one `gateway` node and wire:
      * one edge `gateway -> runtime` (`connectionType` "data"), and
      * one edge per tool `tool -> gateway` (`connectionType` "data").
  The canonical shape is `tool -> gateway -> runtime`. Never emit a
  `tool -> runtime` edge (the canvas rejects it: "Cannot connect tool to runtime"),
  and never emit a `tool` node without a `gateway` node.
- Use position offsets so nodes don't overlap: runtime at (500, 300),
  support nodes laid out in a circle around it (e.g. (250, 100), (750, 100),
  (250, 500), (750, 500)).
- `idSuffix` is a short stable string (e.g. "rt", "kb", "mem", "gw"); the
  frontend rewrites these into globally unique IDs.
- Don't pick a memory strategy unless the user wants persistent memory.
- Don't add a guardrails node unless the user mentions safety / PII / refunds /
  customer-facing concerns.
- Don't pick more than 5 nodes total. Less is better.
- Pick a focused, descriptive `name` for the runtime (snake_case, ≤32 chars).
- Pick a clear, specific systemPrompt (1-3 sentences) that sets the agent's
  role and tone."""


# ---------------------------------------------------------------------------
# Tool spec for structured generation
# ---------------------------------------------------------------------------


_NODE_SCHEMA = {
    "type": "object",
    "properties": {
        "idSuffix": {"type": "string"},
        "type": {
            "type": "string",
            "enum": [
                "runtime",
                "gateway",
                "memory",
                "tool",
                "guardrails",
                "evaluation",
                "observability",
            ],
        },
        "label": {"type": "string"},
        "position": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
            },
            "required": ["x", "y"],
        },
        "configuration": {"type": "object"},
    },
    "required": ["idSuffix", "type", "label", "position", "configuration"],
}


_EDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "sourceIdSuffix": {"type": "string"},
        "targetIdSuffix": {"type": "string"},
        "connectionType": {
            "type": "string",
            "enum": ["data", "control"],
        },
    },
    "required": ["sourceIdSuffix", "targetIdSuffix", "connectionType"],
}


_SUBMIT_TOOL = {
    "toolSpec": {
        "name": "submit_canvas",
        "description": "Submit the generated AgentCore canvas spec.",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Display name (e.g. 'Stock research agent')",
                    },
                    "description": {
                        "type": "string",
                        "description": "One-paragraph summary of what the agent does.",
                    },
                    "nodes": {
                        "type": "array",
                        "items": _NODE_SCHEMA,
                        "minItems": 1,
                        "maxItems": 5,
                    },
                    "edges": {
                        "type": "array",
                        "items": _EDGE_SCHEMA,
                    },
                    "rationale": {
                        "type": "string",
                        "description": "1-2 sentence explanation of why these nodes were chosen.",
                    },
                },
                "required": ["name", "nodes", "edges"],
            }
        },
    }
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

# Built-in gateway tools the platform ships (mirrors
# gateway_deployer.GATEWAY_TOOL_SCHEMAS keys). A `tool` node with isCustom=false
# MUST use one of these ids, else the deploy creates a gateway with NO targets
# ("No predefined tool schemas matched ... skipping DynamicTools target") and the
# agent is broken. Custom tools (isCustom=true) bypass this set but must carry an
# inputSchema so the deploy can generate their Lambda. (Bug 138.)
_BUILTIN_TOOL_IDS = {
    "duckduckgo_search",
    "wikipedia_search",
    "weather_api",
    "web_page_fetcher",
}


def _validate_spec(spec: dict) -> Optional[str]:
    """Return None if the spec is valid, else an error message string.

    Validates the structural invariants documented in GENERATION_PROMPT.
    Returning a string lets the caller feed the error back into the next
    turn so the model can self-correct.
    """
    if not isinstance(spec, dict):
        return "spec is not an object"
    nodes = spec.get("nodes")
    edges = spec.get("edges", [])
    if not isinstance(nodes, list) or not nodes:
        return "spec.nodes must be a non-empty array"
    if not isinstance(edges, list):
        return "spec.edges must be an array"

    runtime_count = sum(1 for n in nodes if n.get("type") == "runtime")
    if runtime_count != 1:
        return f"spec must have exactly one runtime node (got {runtime_count})"

    suffixes = [n.get("idSuffix") for n in nodes]
    if len(set(suffixes)) != len(suffixes):
        return "node idSuffix values must be unique"

    runtime_node = next(n for n in nodes if n.get("type") == "runtime")
    runtime_suffix = runtime_node["idSuffix"]
    rt_cfg = runtime_node.get("configuration") or {}
    rt_name = rt_cfg.get("name")
    if not rt_name or not isinstance(rt_name, str) or len(rt_name) > 32:
        return "runtime configuration.name is required and must be ≤32 chars"
    rt_prompt = rt_cfg.get("systemPrompt")
    if not rt_prompt:
        return "runtime configuration.systemPrompt is required"

    valid_suffixes = set(suffixes)
    for e in edges:
        src = e.get("sourceIdSuffix")
        tgt = e.get("targetIdSuffix")
        if src not in valid_suffixes or tgt not in valid_suffixes:
            return f"edge {src}->{tgt} references unknown suffix"

    # Tool nodes wire through a gateway, NOT straight to the runtime — the canvas
    # connection matrix only permits `tool -> gateway` (a `tool -> runtime` edge
    # fails with "Cannot connect tool to runtime"). Every other support node
    # (memory, guardrails, ...) edges directly to the runtime.
    tool_suffixes = {n.get("idSuffix") for n in nodes if n.get("type") == "tool"}
    gateway_suffixes = {n.get("idSuffix") for n in nodes if n.get("type") == "gateway"}

    if tool_suffixes and not gateway_suffixes:
        return (
            "tool nodes require a gateway node — tools attach to a gateway, not the "
            "runtime. Add a 'gateway' node, wire each tool 'tool -> gateway', and "
            "wire 'gateway -> runtime'."
        )

    # No tool may connect directly to the runtime.
    for e in edges:
        if e.get("sourceIdSuffix") in tool_suffixes and e.get("targetIdSuffix") == runtime_suffix:
            return (
                f"tool node '{e.get('sourceIdSuffix')}' connects directly to the runtime "
                "(invalid: 'Cannot connect tool to runtime'). Wire it to the gateway "
                "instead ('tool -> gateway'), and wire 'gateway -> runtime'."
            )

    # Each support node must reach the runtime by the right route: tools via a
    # gateway, everything else (and the gateway itself) straight to the runtime.
    for n in nodes:
        s = n.get("idSuffix")
        if s == runtime_suffix:
            continue
        if s in tool_suffixes:
            if not any(
                e.get("sourceIdSuffix") == s and e.get("targetIdSuffix") in gateway_suffixes
                for e in edges
            ):
                return f"tool node '{s}' has no edge to a gateway ('tool -> gateway')"
            continue
        # The gateway is a non-tool support node, so this also enforces the
        # required `gateway -> runtime` edge.
        if not any(e.get("sourceIdSuffix") == s and e.get("targetIdSuffix") == runtime_suffix for e in edges):
            return f"non-runtime node '{s}' has no edge to runtime '{runtime_suffix}'"

    # Tool nodes: a built-in tool MUST use a known toolId; a custom tool MUST
    # carry an inputSchema (Bug 138 — otherwise the deploy yields 0 gateway
    # targets and a broken agent). Returning the error feeds self-correction.
    for n in nodes:
        if n.get("type") != "tool":
            continue
        cfg = n.get("configuration") or {}
        tool_id = cfg.get("toolId") or ""
        is_custom = bool(cfg.get("isCustom"))
        if not is_custom:
            if tool_id not in _BUILTIN_TOOL_IDS:
                return (
                    f"tool node '{n.get('idSuffix')}' has isCustom=false but toolId "
                    f"'{tool_id}' is not a built-in tool. Use one of "
                    f"{sorted(_BUILTIN_TOOL_IDS)}, or set isCustom=true with an "
                    "inputSchema to generate a custom tool."
                )
        else:
            schema = cfg.get("inputSchema")
            if not isinstance(schema, dict) or schema.get("type") != "object":
                return (
                    f"custom tool node '{n.get('idSuffix')}' (isCustom=true) must "
                    "include an inputSchema object (type=object with properties)."
                )

    return None


# ---------------------------------------------------------------------------
# Generation entry point
# ---------------------------------------------------------------------------


def generate_canvas(
    prompt: str,
    conversation_history: Optional[list[dict]] = None,
    region: str = "us-east-1",
    max_validation_retries: int = 2,
) -> dict:
    """Generate a canvas spec from a natural-language description.

    Returns a dict with shape:
        {
          "success": bool,
          "responseType": "clarification" | "spec",
          "message": str,                 # only for clarification
          "spec": {name, description, nodes, edges, rationale},
          "error": Optional[str],
        }
    """
    try:
        client = boto3.client("bedrock-runtime", region_name=region)

        history = (conversation_history or [])[-6:]
        messages: list[dict] = []
        for msg in history:
            role = msg.get("role")
            content = msg.get("content")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": [{"text": content}]})
        messages.append({"role": "user", "content": [{"text": prompt}]})

        # First turn (no history): clarification mode.
        if not history:
            resp = client.converse(
                modelId=AGENT_GENERATOR_MODEL_ID,
                messages=messages,
                system=[{"text": CLARIFICATION_PROMPT}],
                inferenceConfig={"maxTokens": 600, "temperature": 0.4},
            )
            text_blocks = resp.get("output", {}).get("message", {}).get("content", [])
            text = next((b.get("text", "") for b in text_blocks if "text" in b), "")
            try:
                parsed = json.loads(text)
                if parsed.get("responseType") == "clarification":
                    return {
                        "success": True,
                        "responseType": "clarification",
                        "message": parsed.get("message", ""),
                    }
            except Exception:
                pass
            # Model didn't return the clarification envelope — fall through
            # to generation. Better to attempt a spec than re-prompt.
            history = [{"role": "user", "content": prompt}]

        # Subsequent turns: tool-use generation with retry-on-validation.
        validation_error: Optional[str] = None
        for attempt in range(max_validation_retries + 1):
            attempt_messages = list(messages)
            if validation_error:
                attempt_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "text": (
                                    "The previous canvas spec was invalid: "
                                    f"{validation_error}. Re-emit the spec, fixing the issue."
                                )
                            }
                        ],
                    }
                )

            resp = client.converse(
                modelId=AGENT_GENERATOR_MODEL_ID,
                messages=attempt_messages,
                system=[{"text": GENERATION_PROMPT}],
                inferenceConfig={"maxTokens": 4000, "temperature": 0.3},
                toolConfig={
                    "tools": [_SUBMIT_TOOL],
                    "toolChoice": {"tool": {"name": "submit_canvas"}},
                },
            )
            content = resp.get("output", {}).get("message", {}).get("content", [])
            tool_use = next((b.get("toolUse") for b in content if "toolUse" in b), None)
            if not tool_use:
                logger.warning(
                    "agent_generator: no tool_use in response (attempt %d)", attempt + 1
                )
                validation_error = "model did not call submit_canvas"
                continue

            spec = tool_use.get("input") or {}
            err = _validate_spec(spec)
            if err is None:
                return {
                    "success": True,
                    "responseType": "spec",
                    "spec": spec,
                }
            logger.info(
                "agent_generator validation failed (attempt %d): %s",
                attempt + 1,
                err,
            )
            validation_error = err

        return {
            "success": False,
            "error": (
                f"Could not generate a valid canvas after {max_validation_retries + 1} "
                f"attempts. Last error: {validation_error}"
            ),
        }
    except Exception:
        # SECURITY (CodeQL py/stack-trace-exposure): the caller surfaces this
        # `error` to the client, so log the detail server-side and return a
        # generic message instead of the raw exception text.
        logger.exception("agent_generator failed")
        return {"success": False, "error": "Agent generation failed. Please try again."}
