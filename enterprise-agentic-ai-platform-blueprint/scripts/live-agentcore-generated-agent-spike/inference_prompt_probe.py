"""Reproduce the generated agent's inference turn standalone (no redeploy).

Why: the fourth live invoke (2026-09-23) returned HTTP 200 with exact marker,
``discoveredToolCount=3`` and ``contentBlocks=1`` but ``toolCalls=[]`` -- the
rated ``openai.gpt-oss-120b`` did not emit the ``TOOL <name> <json>`` directive.
Runtime logs carry fingerprints only, so this probe drives the SAME
``_LiteLlmAdapter`` (Strands ``LiteLLMModel`` against the Platform inference
Gateway) with candidate system prompts and reports, per candidate, whether the
FIRST reply parses as a TOOL directive for the subscribed echo tool.

Auth: the Platform-published cross-account M2M secret (the sanctioned seed for
the workstream credential provider) is read inside this process to mint a
Cognito client_credentials token; nothing secret is ever printed. Output is
classification + fingerprints + a bounded, redacted preview of the reply.

Usage (Workstream account creds; read-only apart from the token mint):
  python inference_prompt_probe.py --secret-arn <platform m2m secret arn> \
      --inference-gateway-url <url> --model-id <id> --guardrail-id <arn>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import boto3
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent / "agent"))
import agent as agent_mod  # noqa: E402

ECHO_TOOL = "target-tool-echo___tool-echo"
SUBSCRIBED = ("target-tool-echo___tool-echo", "target-tool-ping___tool-ping")
# Plain-language user turn: the protocol lives in the system prompt, and the
# imperative "reply with exactly ... nothing else" wording scores as a prompt
# attack under the baseline guardrail now enforced by the Gateway (2026-09-24).
USER_PROMPT = (
    f'Could you use the {ECHO_TOOL} tool with the arguments {{"message":"probe"}}? '
    "When its result is back, finish the task."
)


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def redact(text: str, limit: int = 160) -> str:
    """Bounded preview: the reply is model output about a public probe, but keep
    it short and strip anything that looks like a bearer or ARN."""
    flat = " ".join(text.split())
    for marker in ("Bearer ", "arn:aws", "eyJ"):
        if marker in flat:
            flat = flat.split(marker)[0] + "<redacted>"
    return flat[:limit]


def mint_bearer(secret_arn: str, region: str) -> str:
    sm = boto3.client("secretsmanager", region_name=region)
    data = json.loads(sm.get_secret_value(SecretId=secret_arn)["SecretString"])
    token_endpoint = data["tokenEndpoint"]
    scope = data.get("scope") or data.get("inferenceScope")
    resp = httpx.post(
        token_endpoint,
        data={"grant_type": "client_credentials", "scope": scope},
        auth=(data["clientId"], data["clientSecret"]),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def candidates(model_id: str, guardrail_id: str) -> dict[str, str]:
    config = agent_mod.ReferenceAgentConfig(
        tenant_id="demo",
        agent_id="primary",
        env_name="nonprod",
        guardrail_identifier=guardrail_id,
        model_id=model_id,
        subscribed_tools=SUBSCRIBED,
    )
    core = agent_mod.ReferenceAgentCore(config, None, None)  # type: ignore[arg-type]
    current = core._system_prompt()
    minimal = (
        "You are a governed reference agent. Use only subscribed tools via "
        "the Tools Gateway. Respond with '<done/>' when the task is complete."
    )
    return {"current": current, "minimal-legacy": minimal}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--secret-arn", required=True)
    ap.add_argument("--inference-gateway-url", required=True)
    ap.add_argument("--model-id", required=True)
    ap.add_argument("--guardrail-id", required=True)
    ap.add_argument("--region", required=True)
    ap.add_argument("--repeats", type=int, default=2)
    args = ap.parse_args()

    bearer = mint_bearer(args.secret_arn, args.region)
    adapter = agent_mod._LiteLlmAdapter(
        gateway_url=args.inference_gateway_url,
        bearer_token=bearer,
        model_id=args.model_id,
    )
    report: dict[str, Any] = {"probe": "inference-prompt-directive", "candidates": {}}
    tool_result = agent_mod._compact({"ok": True, "tool": ECHO_TOOL, "echo": {"message": "probe"}})
    for name, system_prompt in candidates(args.model_id, args.guardrail_id).items():
        outcomes = []
        for _ in range(args.repeats):
            reply = adapter.complete(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": USER_PROMPT},
                ],
                guardrail_identifier=args.guardrail_id,
                stream=False,
            )
            parsed = agent_mod.ReferenceAgentCore._parse_tool_request(reply)
            # Second turn mirrors the core's loop: assistant directive + tool result.
            second = adapter.complete(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": USER_PROMPT},
                    {"role": "assistant", "content": reply},
                    {"role": "tool", "content": tool_result},
                ],
                guardrail_identifier=args.guardrail_id,
                stream=False,
            )
            outcomes.append(
                {
                    "directiveParsed": parsed is not None,
                    "targetsEcho": bool(parsed and parsed[0] == ECHO_TOOL),
                    "replyFingerprint": fingerprint(reply),
                    "replyPreview": redact(reply),
                    "secondTurnDone": second.strip() == "<done/>",
                    "secondTurnPreview": redact(second),
                }
            )
        report["candidates"][name] = outcomes
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
