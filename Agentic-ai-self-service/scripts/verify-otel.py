#!/usr/bin/env python3
"""End-to-end OTLP verification for a deployed AgentCore runtime.

Runs after a deploy with the Observability node configured. Hits the agent
endpoint a few times, then polls the OTLP backend's API to verify spans
arrived with the correct GenAI semantic-convention attributes.

Currently supports:
  - Langfuse  (needs --langfuse-host, --langfuse-pk, --langfuse-sk)
  - Phoenix   (needs --phoenix-host)

Usage:
    python scripts/verify-otel.py \\
        --runtime-id arn:aws:bedrock-agentcore:us-east-1:...:runtime/foo \\
        --provider langfuse \\
        --langfuse-host https://cloud.langfuse.com \\
        --langfuse-pk pk-lf-... \\
        --langfuse-sk sk-lf-... \\
        --service-name my-agent

Exit codes:
  0  all assertions passed
  1  assertions failed (missing spans or attributes)
  2  configuration / connectivity failure
"""

import argparse
import base64
import json
import sys
import time
import urllib.parse
import urllib.request
import uuid

# ---------------------------------------------------------------------------
# Agent invocation
# ---------------------------------------------------------------------------


def _https_open(req, timeout):
    """urlopen with an https-only scheme guard (Bandit B310)."""
    if not req.full_url.startswith("https://"):
        raise ValueError("only https URLs are allowed")
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310


def invoke_agent(api_base: str, runtime_id: str, prompt: str, session_id: str) -> dict:
    """Hit the platform's /api/test-runtime endpoint."""
    body = json.dumps(
        {
            "endpoint": "",
            "input": prompt,
            "runtimeId": runtime_id,
            "sessionId": session_id,
        }
    ).encode()
    req = urllib.request.Request(
        f"{api_base}/api/test-runtime",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with _https_open(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# Langfuse polling
# ---------------------------------------------------------------------------


def fetch_langfuse_traces(host: str, pk: str, sk: str) -> list[dict]:
    """List recent traces from Langfuse (no server-side service.name filter).

    Langfuse's `?name=` query filters on the OTEL span operation name (e.g.
    `invoke_agent Strands Agents`), NOT on the resource service.name. We
    fetch a broad slice and filter client-side on resourceAttributes.
    (Verified against Langfuse API behavior, 2026-05-15.)
    """
    auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    qs = urllib.parse.urlencode({"limit": 50, "orderBy": "timestamp.desc"})
    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/public/traces?{qs}",
        headers={"Authorization": f"Basic {auth}"},
    )
    with _https_open(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())
        return body.get("data", body if isinstance(body, list) else [])


def _trace_has_service_name(trace: dict, service_name: str) -> bool:
    """Check if a trace's resource.service.name matches the expected value."""
    md = trace.get("metadata") or {}
    res = md.get("resourceAttributes") or {}
    if res.get("service.name") == service_name:
        return True
    # Fallback: some Langfuse versions surface service.name at metadata root.
    if md.get("service.name") == service_name:
        return True
    return False


def _trace_has_token_usage(trace: dict) -> bool:
    """Check if any GenAI token usage attribute is populated.

    Strands emits `gen_ai.usage.input_tokens` / `output_tokens` / `total_tokens`
    when OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental. Langfuse
    surfaces these via observation rollups and computes totalCost.
    """
    if (trace.get("totalCost") or 0) > 0:
        return True
    md = trace.get("metadata") or {}
    attrs = md.get("attributes") or {}
    for k in (
        "gen_ai.usage.total_tokens",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
    ):
        if attrs.get(k):
            return True
    for k in ("totalTokens", "promptTokens", "completionTokens"):
        if trace.get(k):
            return True
    return False


def assert_langfuse(host: str, pk: str, sk: str, service_name: str, _expected_session: str) -> None:
    print(f"[verify] polling Langfuse for traces with resource service.name={service_name}")
    deadline = time.time() + 90
    matching: list[dict] = []
    while time.time() < deadline:
        try:
            traces = fetch_langfuse_traces(host, pk, sk)
        except Exception as e:
            print(f"[verify] poll error: {e}", file=sys.stderr)
            traces = []
        matching = [t for t in traces if _trace_has_service_name(t, service_name)]
        if matching:
            break
        time.sleep(5)

    if not matching:
        print(f"[verify] FAIL: no traces with resource service.name={service_name}", file=sys.stderr)
        sys.exit(1)

    print(f"[verify] found {len(matching)} trace(s); sample id={matching[0].get('id')}")

    # GenAI attribute checks: cost-relevant rollups must be populated.
    if not any(_trace_has_token_usage(t) for t in matching):
        print("[verify] FAIL: no traces have token usage (GenAI semantic conventions not opted in?)", file=sys.stderr)
        sys.exit(1)

    print("[verify] PASS: trace present and GenAI conventions populated.")


# ---------------------------------------------------------------------------
# Phoenix polling
# ---------------------------------------------------------------------------


def assert_phoenix(host: str, service_name: str, expected_session: str) -> None:
    print(f"[verify] querying Phoenix for spans with service.name={service_name}")
    qs = urllib.parse.urlencode({"limit": 50})
    deadline = time.time() + 60
    while time.time() < deadline:
        req = urllib.request.Request(f"{host.rstrip('/')}/v1/spans?{qs}")
        try:
            with _https_open(req, timeout=15) as resp:
                spans = json.loads(resp.read().decode()).get("data", [])
        except Exception as e:
            print(f"[verify] poll error: {e}", file=sys.stderr)
            spans = []
        match = [s for s in spans if s.get("attributes", {}).get("session.id") == expected_session]
        if match:
            print(f"[verify] PASS: found {len(match)} Phoenix span(s)")
            return
        time.sleep(5)
    print("[verify] FAIL: no Phoenix spans found for session", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api-base", default="http://localhost:8000", help="AgentCore Flows API base URL")
    p.add_argument("--runtime-id", required=True)
    p.add_argument("--provider", choices=["langfuse", "phoenix"], required=True)
    p.add_argument("--service-name", required=True, help="OTEL_SERVICE_NAME used by the runtime")
    p.add_argument("--invocations", type=int, default=3)
    # Langfuse
    p.add_argument("--langfuse-host", default="https://cloud.langfuse.com")
    p.add_argument("--langfuse-pk")
    p.add_argument("--langfuse-sk")
    # Phoenix
    p.add_argument("--phoenix-host", default="http://localhost:6006")
    args = p.parse_args()

    if args.provider == "langfuse" and not (args.langfuse_pk and args.langfuse_sk):
        print("Langfuse requires --langfuse-pk and --langfuse-sk", file=sys.stderr)
        sys.exit(2)

    session_id = f"verify-{uuid.uuid4().hex[:8]}"
    print(f"[verify] driving {args.invocations} invocations against {args.runtime_id}")
    print(f"[verify] session_id={session_id}")

    prompts = [
        "What's 2 plus 2?",
        "Continue: tell me one fun fact.",
        "Final question: what was the first answer you gave me?",
    ]
    for i in range(args.invocations):
        prompt = prompts[i % len(prompts)]
        try:
            invoke_agent(args.api_base, args.runtime_id, prompt, session_id)
            print(f"[verify]  invocation {i + 1}/{args.invocations} ok")
        except Exception as e:
            print(f"[verify] invocation failed: {e}", file=sys.stderr)
            sys.exit(2)

    print("[verify] giving exporter time to flush...")
    time.sleep(10)

    if args.provider == "langfuse":
        assert_langfuse(args.langfuse_host, args.langfuse_pk, args.langfuse_sk, args.service_name, session_id)
    else:
        assert_phoenix(args.phoenix_host, args.service_name, session_id)


if __name__ == "__main__":
    main()
