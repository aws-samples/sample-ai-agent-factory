"""Live end-to-end test of the NL agent generator (real Bedrock).

Reproduces the colleague's exact Slack->Jira prompt, runs the REAL
generate_canvas() against Bedrock, then asserts the returned spec passes
BOTH gates the bug touched:

  1. backend _validate_spec (self-correction gate)
  2. the frontend CONNECTION_COMPATIBILITY matrix (the canvas "N Errors" gate)

The frontend matrix is mirrored here verbatim from
frontend/src/types/validation.ts so we prove the canvas would show 0 errors
without standing up the browser.
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, "src")

from app.services.agent_generator import _validate_spec, generate_canvas  # noqa: E402

# Mirror of frontend/src/types/validation.ts CONNECTION_COMPATIBILITY.
CONNECTION_COMPATIBILITY = {
    "runtime": [
        "gateway",
        "memory",
        "code_interpreter",
        "browser",
        "observability",
        "identity",
        "evaluation",
        "policy",
        "guardrails",
        "a2a",
    ],
    "gateway": ["runtime", "identity", "policy", "tool"],
    "memory": ["runtime"],
    "code_interpreter": ["runtime"],
    "browser": ["runtime"],
    "observability": ["runtime"],
    "identity": ["runtime", "gateway"],
    "evaluation": ["runtime"],
    "policy": ["runtime", "gateway"],
    "guardrails": ["runtime"],
    "a2a": ["runtime"],
    "tool": ["gateway"],
}

PROMPT = (
    "create an agent that takes looks at slack messages and check for the "
    "issues. After the issue is confirmed, it creates a Jira ticket."
)

REGION = "us-east-1"


def frontend_edge_errors(spec: dict) -> list[str]:
    """Replicate the canvas validateConnection() pass over every edge."""
    type_by_suffix = {n["idSuffix"]: n["type"] for n in spec["nodes"]}
    errors = []
    for e in spec.get("edges", []):
        src = type_by_suffix.get(e["sourceIdSuffix"])
        tgt = type_by_suffix.get(e["targetIdSuffix"])
        if src is None or tgt is None:
            errors.append(f"edge {e} references unknown node")
            continue
        if tgt not in CONNECTION_COMPATIBILITY.get(src, []):
            errors.append(f"Cannot connect {src} to {tgt}")
    return errors


def main() -> int:
    print(f"=== Turn 1 (clarification) — region={REGION} ===")
    turn1 = generate_canvas(PROMPT, conversation_history=None, region=REGION)
    print(json.dumps(turn1, indent=2)[:800])
    assert turn1.get("success"), f"turn 1 failed: {turn1}"

    # Simulate the user answering, then turn 2 produces the spec.
    history = [
        {"role": "user", "content": PROMPT},
        {"role": "assistant", "content": turn1.get("message", "(questions)")},
    ]
    answer = (
        "Read from a Slack channel via the Slack API. Confirm issues with the "
        "model. Create Jira tickets via a custom tool. No persistent memory "
        "needed. Invoke on demand."
    )
    print("\n=== Turn 2 (generation) ===")
    turn2 = generate_canvas(answer, conversation_history=history, region=REGION)
    print(json.dumps(turn2, indent=2)[:2000])

    assert turn2.get("success"), f"generation failed: {turn2.get('error')}"
    assert turn2.get("responseType") == "spec", f"expected spec, got {turn2}"
    spec = turn2["spec"]

    node_types = [n["type"] for n in spec["nodes"]]
    print(f"\nGenerated node types: {node_types}")
    print(f"Generated edges: {[(e['sourceIdSuffix'], e['targetIdSuffix']) for e in spec['edges']]}")

    # Gate 1: backend validator.
    backend_err = _validate_spec(spec)
    assert backend_err is None, f"backend _validate_spec rejected: {backend_err}"
    print("\n[PASS] backend _validate_spec: spec is valid")

    # Gate 2: frontend connection matrix (the canvas "N Errors" banner).
    fe_errors = frontend_edge_errors(spec)
    assert not fe_errors, f"frontend canvas would show errors: {fe_errors}"
    print("[PASS] frontend CONNECTION_COMPATIBILITY: 0 edge errors")

    # The whole point of the bug: a tool-bearing agent must carry a gateway and
    # no tool may edge straight to the runtime.
    has_tool = "tool" in node_types
    if has_tool:
        assert "gateway" in node_types, "tool present but no gateway node"
        rt = next(n["idSuffix"] for n in spec["nodes"] if n["type"] == "runtime")
        tools = {n["idSuffix"] for n in spec["nodes"] if n["type"] == "tool"}
        bad = [e for e in spec["edges"] if e["sourceIdSuffix"] in tools and e["targetIdSuffix"] == rt]
        assert not bad, f"tool->runtime edge present: {bad}"
        print("[PASS] tool nodes route through a gateway (tool -> gateway -> runtime)")
    else:
        print("[INFO] model produced no tool nodes this run; gateway rule not exercised")

    print("\n=== LIVE E2E PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
