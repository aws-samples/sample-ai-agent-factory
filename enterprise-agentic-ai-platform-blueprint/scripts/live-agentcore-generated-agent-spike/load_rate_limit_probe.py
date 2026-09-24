"""Load, rate-limit, concurrency and soak probes for the deployed revision.

All modes are standalone and credential-safe: bearer tokens live only in
process memory, evidence holds HTTP codes, counts, latencies and SHA-256
fingerprints. The inference legs speak the same OpenAI-compatible
``/inference/v1/chat/completions`` contract the generated agent uses through
``LiteLLMModel`` (bearer from Identity/Cognito M2M, ``guardrail_identifier``
carried on every call).

Modes
-----
rate-limit-rpm   burst N non-streaming completions inside one minute; the
                 Platform allocation (RPM) must admit at most the allocation and
                 answer the rest with exactly HTTP 429, then recover after the
                 window.
rate-limit-tpm   a few long-prompt completions inside one minute; the token
                 allocation (TPM) must answer with exactly HTTP 429 before the
                 request allocation is reached.
zero-rate        one completion for a model outside the allocation; the
                 wildcard zero-rate entry must answer with exactly HTTP 429.
concurrency      K concurrent ``InvokeAgentRuntime`` sessions through the
                 positive live probe; reports per-session pass/fail and the
                 Runtime status afterwards.
soak             one positive invoke every ``--interval`` seconds for
                 ``--duration`` minutes; reports success ratio and latency
                 percentiles.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

import boto3
import httpx

HERE = Path(__file__).resolve().parent
PROBE = HERE / "live_invoke_probe.py"


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def verify_identity(session: boto3.Session, expected_account: str) -> None:
    account = session.client("sts").get_caller_identity()["Account"]
    if account != expected_account:
        raise SystemExit(
            f"credentials belong to account ...{account[-4:]}, expected ...{expected_account[-4:]}; refusing"
        )


def mint_bearer(secret_arn: str, region: str) -> str:
    """Client-credentials token; the secret and token never leave memory."""
    sm = boto3.client("secretsmanager", region_name=region)
    data = json.loads(sm.get_secret_value(SecretId=secret_arn)["SecretString"])
    scope = data.get("scope") or data.get("inferenceScope")
    resp = httpx.post(
        data["tokenEndpoint"],
        data={"grant_type": "client_credentials", "scope": scope},
        auth=(data["clientId"], data["clientSecret"]),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


class Bearer:
    """Re-mints the client-credentials token before the 5-minute Cognito validity
    lapses, so a long probe never mistakes token expiry (HTTP 403) for throttling."""

    def __init__(self, secret_arn: str, region: str, max_age: float = 180.0) -> None:
        self._secret_arn, self._region, self._max_age = secret_arn, region, max_age
        self._token, self._minted = mint_bearer(secret_arn, region), time.monotonic()
        self.mints = 1

    def get(self) -> str:
        if time.monotonic() - self._minted > self._max_age:
            self._token, self._minted = mint_bearer(self._secret_arn, self._region), time.monotonic()
            self.mints += 1
        return self._token


def inference_base(gateway_url: str) -> str:
    trimmed = gateway_url.rstrip("/")
    if not trimmed.endswith("/mcp"):
        raise SystemExit("gateway url must end with /mcp")
    return f"{trimmed[: -len('/mcp')].rstrip('/')}/inference/v1"


def completion(client: httpx.Client, base: str, bearer: "Bearer", model: str, guardrail: str, prompt: str, max_tokens: int) -> dict:
    started = time.monotonic()
    at = time.time()
    resp = client.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {bearer.get()}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0,
            "stream": False,
            "guardrail_identifier": guardrail,
        },
        timeout=120,
    )
    latency = round(time.monotonic() - started, 2)
    body = resp.text
    usage = None
    if resp.status_code == 200:
        try:
            usage = resp.json().get("usage")
        except ValueError:
            usage = None
    return {
        "status": resp.status_code,
        "at": round(at, 3),
        "latencySeconds": latency,
        "retryAfter": resp.headers.get("retry-after"),
        "bodyFingerprint": fingerprint(body),
        "bodyMentionsRateLimit": ("rate" in body.lower() and "limit" in body.lower()) or "throttl" in body.lower(),
        "usage": usage,
    }


def classify_429(results: list[dict]) -> dict:
    statuses = [r["status"] for r in results]
    return {
        "statuses": statuses,
        "admitted": statuses.count(200),
        "throttled": statuses.count(429),
        "otherCodes": sorted({s for s in statuses if s not in (200, 429)}),
        "first429Index": statuses.index(429) if 429 in statuses else None,
    }


def mode_rpm(args, bearer: "Bearer") -> dict:
    base = inference_base(args.inference_gateway_url)
    time.sleep(args.window_wait)  # start from a clean rate window
    results = []
    with httpx.Client() as client:
        for i in range(args.burst):
            if i and args.spacing:
                time.sleep(args.spacing)
            results.append(completion(client, base, bearer, args.model_id, args.guardrail_id, "Reply with exactly the word ok.", 8))
        summary = classify_429(results)
        time.sleep(61)
        recovery = completion(client, base, bearer, args.model_id, args.guardrail_id, "Reply with exactly the word ok.", 8)
    summary.update(
        allocationRpm=args.allocation_rpm,
        burst=args.burst,
        spacingSeconds=args.spacing,
        elapsedSeconds=round(sum(r["latencySeconds"] for r in results) + args.spacing * max(0, args.burst - 1), 1),
        throttledBodyFingerprints=sorted({r["bodyFingerprint"] for r in results if r["status"] == 429}),
        throttledMentionRateLimit=all(r["bodyMentionsRateLimit"] for r in results if r["status"] == 429) if summary["throttled"] else None,
        recoveryStatusAfterWindow=recovery["status"],
        latenciesSeconds=[r["latencySeconds"] for r in results],
        timeline=[[round(r["at"] - results[0]["at"], 1), r["status"]] for r in results],
        admittedPerMinuteWindow=[
            sum(1 for r in results if r["status"] == 200 and w * 60 <= r["at"] - results[0]["at"] < (w + 1) * 60)
            for w in range(int((results[-1]["at"] - results[0]["at"]) // 60) + 1)
        ],
    )
    summary["passed"] = (
        summary["throttled"] >= 1
        and summary["admitted"] <= args.allocation_rpm
        and summary["admitted"] >= 1
        and not summary["otherCodes"]
        and recovery["status"] == 200
    )
    return summary


def mode_tpm(args, bearer: "Bearer") -> dict:
    base = inference_base(args.inference_gateway_url)
    time.sleep(args.window_wait)
    filler = ("The quick brown fox jumps over the lazy dog. " * 90).strip()  # ~800 tokens
    prompt = "\n".join([filler] * args.tpm_prompt_blocks) + "\nReply with exactly the word ok."
    results = []
    with httpx.Client() as client:
        for _ in range(args.tpm_requests):
            results.append(completion(client, base, bearer, args.model_id, args.guardrail_id, prompt, 8))
    summary = classify_429(results)
    tokens = [r["usage"].get("total_tokens") for r in results if r.get("usage")]
    summary.update(
        allocationTpm=args.allocation_tpm,
        requests=args.tpm_requests,
        promptBlocks=args.tpm_prompt_blocks,
        admittedTotalTokens=sum(t for t in tokens if isinstance(t, int)),
        throttledBodyFingerprints=sorted({r["bodyFingerprint"] for r in results if r["status"] == 429}),
    )
    summary["passed"] = (
        summary["throttled"] >= 1
        and summary["admitted"] >= 1
        and summary["admitted"] < args.allocation_rpm
        and not summary["otherCodes"]
    )
    return summary


def mode_zero_rate(args, bearer: "Bearer") -> dict:
    base = inference_base(args.inference_gateway_url)
    with httpx.Client() as client:
        result = completion(client, base, bearer, args.zero_rate_model_id, args.guardrail_id, "Reply with exactly the word ok.", 8)
    return {
        "modelFingerprint": fingerprint(args.zero_rate_model_id),
        "status": result["status"],
        "bodyFingerprint": result["bodyFingerprint"],
        "bodyMentionsRateLimit": result["bodyMentionsRateLimit"],
        "passed": result["status"] == 429,
    }


def run_positive_probe(args, tag: str) -> dict:
    out = args.evidence_dir / f"{args.mode}-{tag}.json"
    started = time.monotonic()
    proc = subprocess.run(
        [
            sys.executable,
            str(PROBE),
            "--mode",
            "positive",
            "--expected-account",
            args.expected_account,
            "--stack-name",
            args.stack_name,
            "--evidence-out",
            str(out),
            "--region",
            args.region,
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    elapsed = round(time.monotonic() - started, 2)
    result = {"tag": tag, "exitCode": proc.returncode, "wallSeconds": elapsed}
    if out.exists():
        data = json.loads(out.read_text())
        result.update(
            passed=data.get("passed") is True,
            httpStatus=data.get("httpStatus"),
            invokeLatencySeconds=data.get("invokeLatencySeconds"),
            errorFingerprint=fingerprint(str(data.get("error"))) if data.get("error") else None,
        )
    else:
        result.update(passed=False, stderrFingerprint=fingerprint(proc.stderr[-2000:]))
    return result


def runtime_status(session: boto3.Session, stack_name: str) -> str:
    cfn = session.client("cloudformation")
    outputs = {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName=stack_name)["Stacks"][0].get("Outputs", [])}
    runtime_arn = next(v for k, v in outputs.items() if k.lower().endswith("runtimearn"))
    agentcore = session.client("bedrock-agentcore-control")
    return agentcore.get_agent_runtime(agentRuntimeId=runtime_arn.rsplit("/", 1)[-1])["status"]


def mode_concurrency(args, session: boto3.Session) -> dict:
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(run_positive_probe, args, f"c{i:02d}") for i in range(args.concurrency)]
        results = [f.result() for f in futures]
    passed = sum(1 for r in results if r["passed"])
    latencies = [r["invokeLatencySeconds"] for r in results if r.get("invokeLatencySeconds")]
    return {
        "concurrency": args.concurrency,
        "sessionsPassed": passed,
        "sessionsFailed": args.concurrency - passed,
        "httpStatuses": sorted({r.get("httpStatus") for r in results}, key=str),
        "errorFingerprints": sorted({r["errorFingerprint"] for r in results if r.get("errorFingerprint")}),
        "invokeLatencySeconds": {"min": min(latencies) if latencies else None, "max": max(latencies) if latencies else None},
        "runtimeStatusAfter": runtime_status(session, args.stack_name),
        "expectAllPass": args.expect_all_pass,
        "passed": (passed == args.concurrency) if args.expect_all_pass else (passed >= 1 and runtime_status(session, args.stack_name) == "READY"),
    }


def mode_soak(args, session: boto3.Session) -> dict:
    deadline = time.monotonic() + args.duration * 60
    results = []
    while time.monotonic() < deadline:
        tick = time.monotonic()
        results.append(run_positive_probe(args, f"s{len(results):03d}"))
        remaining = args.interval - (time.monotonic() - tick)
        if remaining > 0 and time.monotonic() + remaining < deadline:
            time.sleep(remaining)
    latencies = sorted(r["invokeLatencySeconds"] for r in results if r.get("invokeLatencySeconds"))
    passed = sum(1 for r in results if r["passed"])
    pct = lambda p: latencies[min(len(latencies) - 1, int(round(p * (len(latencies) - 1))))] if latencies else None  # noqa: E731
    return {
        "durationMinutes": args.duration,
        "intervalSeconds": args.interval,
        "invocations": len(results),
        "passed": passed == len(results) and len(results) > 0,
        "successes": passed,
        "failures": len(results) - passed,
        "errorFingerprints": sorted({r["errorFingerprint"] for r in results if r.get("errorFingerprint")}),
        "latencySeconds": {"p50": pct(0.5), "p95": pct(0.95), "max": latencies[-1] if latencies else None, "mean": round(statistics.fmean(latencies), 2) if latencies else None},
        "runtimeStatusAfter": runtime_status(session, args.stack_name),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--mode", required=True, choices=("rate-limit-rpm", "rate-limit-tpm", "zero-rate", "concurrency", "soak"))
    ap.add_argument("--expected-account", required=True)
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--evidence-dir", required=True, type=Path)
    # inference legs
    ap.add_argument("--secret-arn")
    ap.add_argument("--inference-gateway-url")
    ap.add_argument("--model-id")
    ap.add_argument("--guardrail-id")
    ap.add_argument("--allocation-rpm", type=int, default=10)
    ap.add_argument("--allocation-tpm", type=int, default=10_000)
    ap.add_argument("--burst", type=int, default=14)
    ap.add_argument("--spacing", type=float, default=0.0, help="seconds between burst requests (0 = as fast as possible)")
    ap.add_argument("--tpm-requests", type=int, default=5)
    ap.add_argument("--tpm-prompt-blocks", type=int, default=4)
    ap.add_argument("--zero-rate-model-id")
    ap.add_argument("--window-wait", type=int, default=61)
    # runtime legs
    ap.add_argument("--stack-name")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--expect-all-pass", action="store_true")
    ap.add_argument("--duration", type=int, default=30, help="soak minutes")
    ap.add_argument("--interval", type=int, default=40, help="soak seconds between invokes")
    args = ap.parse_args()

    session = boto3.Session(region_name=args.region)
    verify_identity(session, args.expected_account)
    args.evidence_dir.mkdir(parents=True, exist_ok=True)

    if args.mode in ("rate-limit-rpm", "rate-limit-tpm", "zero-rate"):
        for name in ("secret_arn", "inference_gateway_url", "model_id", "guardrail_id"):
            if not getattr(args, name):
                raise SystemExit(f"--{name.replace('_', '-')} is required for {args.mode}")
        if args.mode == "zero-rate" and not args.zero_rate_model_id:
            raise SystemExit("--zero-rate-model-id is required for zero-rate")
        bearer = Bearer(args.secret_arn, args.region)
        result = {"rate-limit-rpm": mode_rpm, "rate-limit-tpm": mode_tpm, "zero-rate": mode_zero_rate}[args.mode](args, bearer)
        result["bearerMints"] = bearer.mints
    else:
        if not args.stack_name:
            raise SystemExit("--stack-name is required for runtime modes")
        result = mode_concurrency(args, session) if args.mode == "concurrency" else mode_soak(args, session)

    result.update(probe="load-rate-limit", mode=args.mode, region=args.region, finishedAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    (args.evidence_dir / f"{args.mode}.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
