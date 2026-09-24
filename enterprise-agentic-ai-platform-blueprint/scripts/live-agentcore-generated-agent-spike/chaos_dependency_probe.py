"""Chaos and dependency-failure probes for the deployed revision.

The pipeline-owned resources expose no fault-injection hook, and mutating them
out of band would itself be the pipeline-bypass anti-pattern the architecture
forbids, so these experiments induce dependency failure at the boundaries the
generated agent depends on and observe whether each fails closed:

inference-guardrail   completions with (a) a non-existent guardrail id and
                      (b) no guardrail at all -> the mandatory-guardrail
                      control must reject both (no guardrail-free inference)
inference-auth        completions with (a) no bearer, (b) a malformed bearer,
                      (c) a syntactically valid but forged JWT -> 401/403
runtime-fuzz          InvokeAgentRuntime with malformed / empty / wrong-shape /
                      oversized payloads and a bogus session id -> controlled
                      4xx/5xx per request, no hang, Runtime still READY, and
                      a positive invocation still passes afterwards

Evidence holds status codes, error classes, latencies and fingerprints only.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import boto3
import httpx
from botocore.exceptions import ClientError

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


def inference_base(gateway_url: str) -> str:
    trimmed = gateway_url.rstrip("/")
    if not trimmed.endswith("/mcp"):
        raise SystemExit("gateway url must end with /mcp")
    return f"{trimmed[: -len('/mcp')].rstrip('/')}/inference/v1"


def post_completion(client: httpx.Client, base: str, headers: dict, body: dict) -> dict:
    started = time.monotonic()
    resp = client.post(f"{base}/chat/completions", headers=headers, json=body, timeout=120)
    text = resp.text
    content = None
    if resp.status_code == 200:
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            content = None
    return {
        "status": resp.status_code,
        "latencySeconds": round(time.monotonic() - started, 2),
        "bodyFingerprint": fingerprint(text),
        "errorClass": (
            "guardrail" if "guardrail" in text.lower() else
            "unauthorized" if resp.status_code in (401, 403) else
            "validation" if resp.status_code == 400 else
            "ok" if resp.status_code == 200 else "other"
        ),
        "gotModelContent": bool(content),
    }


def forged_jwt() -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT", "kid": "forged"}).encode()).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "forged", "scope": "inference/invoke", "exp": int(time.time()) + 300}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.{base64.urlsafe_b64encode(b'not-a-signature').rstrip(b'=').decode()}"


def mode_inference_guardrail(args) -> dict:
    bearer = mint_bearer(args.secret_arn, args.region)
    base = inference_base(args.inference_gateway_url)
    headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}
    prompt = {"role": "user", "content": "Reply with exactly the word ok."}
    with httpx.Client() as client:
        positive = post_completion(client, base, headers, {"model": args.model_id, "messages": [prompt], "max_tokens": 8, "temperature": 0, "guardrail_identifier": args.guardrail_id})
        bogus = post_completion(client, base, headers, {"model": args.model_id, "messages": [prompt], "max_tokens": 8, "temperature": 0, "guardrail_identifier": "gr-does-not-exist-0000"})
        absent = post_completion(client, base, headers, {"model": args.model_id, "messages": [prompt], "max_tokens": 8, "temperature": 0})
    result = {"positive": positive, "bogusGuardrail": bogus, "guardrailAbsent": absent}
    result["guardrailEnforcedAtGateway"] = bogus["status"] != 200 and absent["status"] != 200
    result["passed"] = positive["status"] == 200 and positive["gotModelContent"]
    return result


def mode_inference_auth(args) -> dict:
    base = inference_base(args.inference_gateway_url)
    body = {"model": args.model_id, "messages": [{"role": "user", "content": "Reply with exactly the word ok."}], "max_tokens": 8, "guardrail_identifier": args.guardrail_id}
    with httpx.Client() as client:
        no_bearer = post_completion(client, base, {"Content-Type": "application/json"}, body)
        malformed = post_completion(client, base, {"Authorization": "Bearer not-a-token", "Content-Type": "application/json"}, body)
        forged = post_completion(client, base, {"Authorization": f"Bearer {forged_jwt()}", "Content-Type": "application/json"}, body)
    result = {"noBearer": no_bearer, "malformedBearer": malformed, "forgedJwt": forged}
    result["passed"] = all(r["status"] in (401, 403) and not r["gotModelContent"] for r in (no_bearer, malformed, forged))
    return result


def runtime_arn(session: boto3.Session, stack_name: str) -> str:
    cfn = session.client("cloudformation")
    outputs = {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName=stack_name)["Stacks"][0].get("Outputs", [])}
    return next(v for k, v in outputs.items() if k.lower().endswith("runtimearn"))


def invoke_raw(client, arn: str, payload: bytes, session_id: str, content_type: str = "application/json") -> dict:
    started = time.monotonic()
    try:
        resp = client.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=session_id, payload=payload, contentType=content_type, accept="application/json")
        body = resp["response"].read() if hasattr(resp.get("response"), "read") else b""
        return {"status": resp["ResponseMetadata"]["HTTPStatusCode"], "latencySeconds": round(time.monotonic() - started, 2), "bodyFingerprint": fingerprint(body.decode("utf-8", "replace")), "bodyBytes": len(body), "error": None}
    except ClientError as exc:
        err = exc.response.get("Error", {})
        return {"status": exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode"), "latencySeconds": round(time.monotonic() - started, 2), "error": err.get("Code"), "messageFingerprint": fingerprint(str(err.get("Message")))}
    except Exception as exc:  # noqa: BLE001 - classify transport failures without leaking
        return {"status": None, "latencySeconds": round(time.monotonic() - started, 2), "error": type(exc).__name__}


def positive_probe(args, tag: str) -> dict:
    out = args.evidence_dir / f"runtime-fuzz-{tag}.json"
    proc = subprocess.run([sys.executable, str(PROBE), "--mode", "positive", "--expected-account", args.expected_account, "--stack-name", args.stack_name, "--evidence-out", str(out), "--region", args.region], capture_output=True, text=True, timeout=600)
    data = json.loads(out.read_text()) if out.exists() else {}
    return {"exitCode": proc.returncode, "passed": data.get("passed") is True, "httpStatus": data.get("httpStatus")}


def mode_runtime_fuzz(args, session: boto3.Session) -> dict:
    arn = runtime_arn(session, args.stack_name)
    client = session.client("bedrock-agentcore", config=boto3.session.Config(read_timeout=120, retries={"max_attempts": 0}))
    sid = lambda: uuid.uuid4().hex + uuid.uuid4().hex[:8]  # noqa: E731 - >=33 chars as the service requires
    cases = {
        "notJson": (b"this is not json", sid(), "application/json"),
        "emptyBody": (b"", sid(), "application/json"),
        "emptyObject": (b"{}", sid(), "application/json"),
        "wrongShape": (json.dumps({"prompt": ["list", "not", "string"], "actor_id": 42}).encode(), sid(), "application/json"),
        "hugePrompt": (json.dumps({"prompt": "A" * 900_000}).encode(), sid(), "application/json"),
        "controlChars": (json.dumps({"prompt": "\u0000\u0001\ufffe" * 64}).encode(), sid(), "application/json"),
        "shortSessionId": (json.dumps({"prompt": "Reply ok"}).encode(), "short", "application/json"),
        "wrongContentType": (json.dumps({"prompt": "Reply ok"}).encode(), sid(), "text/plain"),
    }
    results = {name: invoke_raw(client, arn, payload, session_id, ctype) for name, (payload, session_id, ctype) in cases.items()}
    agentcore = session.client("bedrock-agentcore-control")
    status_after = agentcore.get_agent_runtime(agentRuntimeId=arn.rsplit("/", 1)[-1])["status"]
    after = positive_probe(args, "after")
    hung = [n for n, r in results.items() if r["latencySeconds"] > 110]
    return {
        "cases": results,
        "runtimeStatusAfter": status_after,
        "positiveAfter": after,
        "noneHung": not hung,
        "passed": status_after == "READY" and after["passed"] and not hung and all(r["status"] != 200 or r.get("bodyBytes", 0) > 0 for r in results.values()),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--mode", required=True, choices=("inference-guardrail", "inference-auth", "runtime-fuzz"))
    ap.add_argument("--expected-account", required=True)
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--evidence-dir", required=True, type=Path)
    ap.add_argument("--secret-arn")
    ap.add_argument("--inference-gateway-url")
    ap.add_argument("--model-id")
    ap.add_argument("--guardrail-id")
    ap.add_argument("--stack-name")
    args = ap.parse_args()
    session = boto3.Session(region_name=args.region)
    verify_identity(session, args.expected_account)
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    if args.mode.startswith("inference"):
        for name in ("secret_arn", "inference_gateway_url", "model_id", "guardrail_id"):
            if not getattr(args, name):
                raise SystemExit(f"--{name.replace('_', '-')} is required")
        result = mode_inference_guardrail(args) if args.mode == "inference-guardrail" else mode_inference_auth(args)
    else:
        if not args.stack_name:
            raise SystemExit("--stack-name is required")
        result = mode_runtime_fuzz(args, session)
    result.update(probe="chaos-dependency-failure", mode=args.mode, region=args.region, finishedAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    (args.evidence_dir / f"{args.mode}.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
