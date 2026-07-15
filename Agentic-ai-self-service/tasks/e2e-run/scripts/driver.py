#!/usr/bin/env python3
"""Matrix-tester cell driver against the fresh us-east-1 stack.

Lifecycle per cell:
  refresh token -> POST /api/deploy -> poll /api/deploy/{id} to terminal
  -> resolve runtime_id/arn -> invoke (multi-turn) -> real-response gate
  -> record verdict+evidence into state.json -> (optional) DELETE the flow.

NEVER prints connector secrets. Redacts tokens in any captured body.
"""
import base64
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import boto3

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()
    _SSL_CTX.check_hostname = False
    _SSL_CTX.verify_mode = ssl.CERT_NONE

ROOT = Path(__file__).resolve().parents[1]
P = json.loads((ROOT / "platform.json").read_text())
API = P["api_url"].rstrip("/")
REGION = P["region"]
STATE_FILE = ROOT / "state.json"
EVID_ROOT = ROOT / "evidence"
TOKEN_FILE = ROOT / ".token"
REFRESH_FILE = ROOT / ".refresh"

TERMINAL_OK = {"SUCCEEDED", "SUCCESS", "DEPLOYED", "COMPLETED", "READY"}
TERMINAL_BAD = {"FAILED", "FAILURE", "TIMED_OUT", "ABORTED", "ERROR"}

APOLOGY = re.compile(
    r"i'?m sorry|i am sorry|i apologi|i cannot|i can'?t|i am unable|i'?m unable|"
    r"i don'?t have access|i do not have access|i was not able|i wasn'?t able|"
    r"unable to (call|use|access|invoke|retrieve|reach)|no tool was called|"
    r"tool (call|use) failed|(connection|connect) failed|as an ai|"
    r"i do not have the ability",
    re.I,
)
WRAPPED_ERR = re.compile(
    r"^Error:|^Exception:|Traceback \(most recent call last\)|botocore\.exceptions|"
    r"ClientError|__type\"\:|\"errorMessage\"|\"stackTrace\"|Internal server error|"
    r"ServiceUnavailable|An error occurred|cannot access",
    re.I,
)


def refresh_token() -> str:
    if TOKEN_FILE.exists():
        try:
            c = json.loads(TOKEN_FILE.read_text())
            if c.get("exp", 0) - 600 > time.time():
                return c["id_token"]
        except Exception:
            pass
    rt = json.loads(REFRESH_FILE.read_text())["refresh_token"]
    idp = boto3.Session(region_name=REGION).client("cognito-idp")
    resp = idp.initiate_auth(
        ClientId=P["user_pool_client_id"],
        AuthFlow="REFRESH_TOKEN_AUTH",
        AuthParameters={"REFRESH_TOKEN": rt},
    )
    ar = resp["AuthenticationResult"]
    tok = ar["IdToken"]
    access = ar.get("AccessToken", "")
    claims = json.loads(base64.urlsafe_b64decode(tok.split(".")[1] + "==="))
    TOKEN_FILE.write_text(json.dumps({"id_token": tok, "access_token": access, "exp": claims["exp"]}))
    return tok


def access_token() -> str:
    """Return a cached/fresh Cognito ACCESS token (the stream Function URL needs
    token_use=access, not the ID token the API-GW authorizer accepts)."""
    refresh_token()  # ensures .token is fresh and carries access_token
    try:
        c = json.loads(TOKEN_FILE.read_text())
        return c.get("access_token", "")
    except Exception:
        return ""


def api(method: str, path: str, body=None, timeout=60):
    url = f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    # Retry transient transport errors (intermittent DNS "nodename nor servname
    # provided", connection resets) with backoff — a single local network blip
    # must NOT fail a long deploy/probe run. HTTP errors (4xx/5xx) are NOT
    # retried here; they carry a real response the caller inspects.
    last_err = None
    for _attempt in range(4):
        tok = refresh_token()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {tok}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
                raw = r.read().decode()
                code = r.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            code = e.code
        except Exception as e:
            last_err = str(e)
            # macOS Python 3.13 + custom SSL context intermittently fails DNS
            # ("nodename nor servname provided") while the system resolver + curl
            # succeed. Fall back to curl (uses the OS resolver directly) before
            # giving up — proven reliable when urllib's resolver flakes.
            cr = _curl(method, url, tok, data, timeout)
            if cr is not None:
                code, raw = cr
                try:
                    return code, json.loads(raw)
                except Exception:
                    return code, {"_raw": raw[:2000]}
            time.sleep(3 * (_attempt + 1))
            continue
        try:
            return code, json.loads(raw)
        except Exception:
            return code, {"_raw": raw[:2000]}
    return 0, {"_transport_error": last_err}


def _curl(method, url, tok, data, timeout):
    """Fallback HTTP via the curl binary (OS resolver) when urllib DNS flakes.

    Returns (code, body_str) or None if curl itself failed."""
    import subprocess
    cmd = ["curl", "-s", "-S", "--max-time", str(timeout),
           "-X", method, url,
           "-H", f"Authorization: Bearer {tok}",
           "-H", "Content-Type: application/json",
           "-w", "\n%{http_code}"]
    if data is not None:
        cmd += ["--data-binary", data.decode() if isinstance(data, bytes) else data]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
        if out.returncode != 0:
            return None
        body, _, code = out.stdout.rpartition("\n")
        return int(code), body
    except Exception:
        return None


def load_state():
    return json.loads(STATE_FILE.read_text())


def save_cell(cell_id, record):
    st = load_state()
    st.setdefault("cells", {})[cell_id] = record
    STATE_FILE.write_text(json.dumps(st, indent=2))


def deploy_and_wait(payload, evid: Path, max_wait=1500):
    evid.mkdir(parents=True, exist_ok=True)
    (evid / "payload.json").write_text(json.dumps(payload, indent=2))
    code, body = api("POST", "/api/deploy", payload)
    (evid / "deploy.resp.json").write_text(json.dumps({"http": code, "body": body}, indent=2))
    if code not in (200, 202):
        return {"phase": "deploy", "http": code, "body": body, "deployment_id": None}
    dep_id = body.get("deploymentId") or body.get("deployment_id")
    exec_arn = body.get("executionArn") or body.get("execution_arn")
    (evid / "deployment_id").write_text(dep_id or "")
    deadline = time.time() + max_wait
    last = None
    state = {}
    while time.time() < deadline:
        c, state = api("GET", f"/api/deploy/{dep_id}")
        status = (state.get("status") or "").upper()
        if status != last:
            print(f"  [{time.strftime('%H:%M:%S')}] status={status}", flush=True)
            last = status
        (evid / "last_status.json").write_text(json.dumps(state, indent=2))
        if status in TERMINAL_OK or status in TERMINAL_BAD:
            break
        time.sleep(15)
    return {"phase": "deployed", "http": code, "deployment_id": dep_id,
            "execution_arn": exec_arn, "final_status": (state.get("status") or "").upper(),
            "state": state}


def gate(body_text, canary=None, require_canary=True):
    """Return (passed, gates dict)."""
    g = {}
    g["body_shape"] = bool(body_text) and not WRAPPED_ERR.search(body_text or "")
    g["apology"] = not APOLOGY.search(body_text or "")
    if canary and require_canary:
        g["canary"] = canary in (body_text or "")
    passed = g["body_shape"] and g["apology"] and (g.get("canary", True))
    return passed, g


def invoke(runtime_id, prompt, session_id=None, history=None, timeout=120):
    payload = {"runtimeId": runtime_id, "input": prompt}
    if session_id:
        # AgentCore requires runtimeSessionId >= 33 chars; pad short test ids so
        # multi-turn same-session probes aren't rejected before reaching the runtime.
        payload["sessionId"] = session_id if len(session_id) >= 33 else (session_id + "x" * (33 - len(session_id)))
    if history:
        payload["history"] = history
    code, body = api("POST", "/api/test-runtime", payload, timeout=timeout)
    return code, body


def invoke_direct(runtime_id, prompt, session_id=None, arn=None, timeout=300):
    """Invoke the runtime via the bedrock-agentcore data plane directly (SigV4).

    This is exactly the call the platform makes, but with no API-GW 30s ceiling —
    the right path for tool-heavy / MCP-gateway turns. The stream Function URL is
    AuthType=AWS_IAM yet the handler also demands a Cognito bearer in the SAME
    Authorization header SigV4 occupies (platform bug), so it's unusable; this
    direct path sidesteps it entirely.
    """
    import boto3
    if not arn:
        arn = f"arn:aws:bedrock-agentcore:{REGION}:{boto3.client('sts').get_caller_identity()['Account']}:runtime/{runtime_id}"
    # retries=0 + explicit connect_timeout so a slow/cold runtime can't stack
    # botocore's default 3 retries on top of `timeout` and hang for many minutes
    # (observed live: a cold gateway turn exceeding read_timeout retried silently).
    _Config = __import__("botocore.config", fromlist=["Config"]).Config
    dp = boto3.client("bedrock-agentcore", region_name=REGION,
                      config=_Config(connect_timeout=15, read_timeout=timeout,
                                     retries={"max_attempts": 0}))
    kwargs = dict(agentRuntimeArn=arn, qualifier="DEFAULT",
                  payload=json.dumps({"prompt": prompt}).encode(),
                  contentType="application/json", accept="application/json")
    if session_id:
        # AgentCore session ids must be >=33 chars.
        sid = session_id if len(session_id) >= 33 else (session_id + "x" * (33 - len(session_id)))
        kwargs["runtimeSessionId"] = sid
    try:
        r = dp.invoke_agent_runtime(**kwargs)
        raw = r["response"].read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, {"success": False, "error": str(e)[:500]}
    try:
        parsed = json.loads(raw)
        resp = parsed.get("response", raw) if isinstance(parsed, dict) else raw
    except Exception:
        resp = raw
    return 200, {"success": bool(resp), "response": resp}


def invoke_stream(runtime_id, prompt, session_id=None, history=None, timeout=300):
    """Invoke via the SigV4 (AWS_IAM) Lambda Function URL for >30s tool-heavy turns.

    The API-GW sync /api/test-runtime path has a hard 30s ceiling; multi-tool /
    MCP-gateway turns exceed it. This posts to test_runtime_stream_url and parses
    the SSE token stream into a single response string.
    """
    url = P.get("test_runtime_stream_url")
    if not url:
        return 0, {"success": False, "error": "no test_runtime_stream_url in platform.json"}
    payload = {"runtimeId": runtime_id, "input": prompt}
    if session_id:
        payload["sessionId"] = session_id
    if history:
        payload["history"] = history
    data = json.dumps(payload).encode()
    # The stream Function URL is AuthType=AWS_IAM at the infra layer (SigV4 required
    # to reach the handler), and the handler ALSO verifies a Cognito ACCESS token
    # (token_use=access). So we need BOTH: SigV4 sign with service "lambda", then
    # carry the Bearer access token in a NON-signed header the handler reads.
    import botocore.auth as _auth
    import botocore.awsrequest as _awsreq
    import botocore.session as _bsession

    at = access_token()
    creds = _bsession.get_session().get_credentials().get_frozen_credentials()
    # SigV4 owns Authorization (Function URL AWS_IAM). The handler reads the
    # Cognito access token from a custom header (X-Cognito-Token) so it does not
    # clash with the SigV4 Authorization header.
    aws_req = _awsreq.AWSRequest(method="POST", url=url, data=data,
                                 headers={"Content-Type": "application/json",
                                          "X-Cognito-Token": at})
    _auth.SigV4Auth(creds, "lambda", REGION).add_auth(aws_req)
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in aws_req.headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            raw = r.read().decode("utf-8", errors="replace")
            code = r.status
    except urllib.error.HTTPError as e:
        return e.code, {"success": False, "error": e.read().decode()[:500]}
    except Exception as e:
        return 0, {"success": False, "error": str(e)}
    # parse SSE: collect token events, detect error/done
    tokens, err = [], None
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except Exception:
            continue
        if ev.get("type") == "token":
            tokens.append(ev.get("token", ""))
        elif ev.get("type") in ("text", "chunk"):
            tokens.append(ev.get("text") or ev.get("chunk") or "")
        elif ev.get("type") == "error":
            err = ev.get("error")
        elif ev.get("response"):
            tokens.append(ev["response"])
    resp = "".join(tokens)
    return code, {"success": bool(resp) and not err, "response": resp, "error": err}


def invoke_customer_stream(runtime_id, prompt, session_id=None, history=None, timeout=120):
    """Invoke via the CUSTOMER path the frontend actually uses: POST
    /api/test-runtime-stream through API Gateway with a Cognito bearer. Returns
    the SSE 'done' full_response (or concatenated tokens). This is the path that
    matters for production — the SigV4 Function URL (invoke_stream) is
    provisioned-but-unwired by design.
    """
    payload = {"runtimeId": runtime_id, "input": prompt}
    if session_id:
        payload["sessionId"] = session_id
    if history:
        payload["history"] = history
    code, body = api("POST", "/api/test-runtime-stream", payload, timeout=timeout)
    # The route returns raw SSE text; our api() wraps non-JSON as {"_raw": ...}.
    sse = body.get("_raw") if isinstance(body, dict) else None
    if sse is None and isinstance(body, dict):
        # Some gateways may pass the SSE straight through as a string field.
        sse = body.get("response") or ""
    tokens, full, err = [], None, None
    for line in (sse or "").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except Exception:
            continue
        if ev.get("type") == "token":
            tokens.append(ev.get("token", ""))
        elif ev.get("type") == "done":
            full = ev.get("full_response")
        elif ev.get("type") == "error":
            err = ev.get("error")
    resp = full if full is not None else "".join(tokens)
    return code, {"success": bool(resp) and not err, "response": resp, "error": err}


def resolve_runtime(state):
    """Pull the invokable runtime id/arn from a deployment state dict.

    The control-plane runtime_id carries a random suffix (-XXXXXXXX) that the
    /api/test-runtime DDB scan keys on; agentcore_runtime_name lacks it and
    will 'Runtime not found'. Prefer runtime_id, then derive from the ARN.
    """
    rid = state.get("runtime_id") or state.get("runtimeId")
    arn = state.get("runtime_arn") or state.get("runtimeArn")
    # The deployment record stores runtime_endpoint (the ENDPOINT arn, i.e.
    # .../runtime/<id>/runtime-endpoint/DEFAULT). invoke_agent_runtime wants the
    # BARE runtime arn + qualifier="DEFAULT" separately — passing the endpoint arn
    # yields ResourceNotFoundException. Derive the bare runtime arn here.
    if not arn:
        ep = state.get("runtime_endpoint") or state.get("runtimeEndpoint") or ""
        if "/runtime-endpoint/" in ep:
            arn = ep.split("/runtime-endpoint/")[0]
        elif ep.startswith("arn:"):
            arn = ep
    if not rid and arn:
        rid = arn.split("/")[-1]
    if not rid:
        rid = state.get("agentcore_runtime_name")
    return rid, arn


def delete_flow(runtime_id, evid: Path):
    evid.mkdir(parents=True, exist_ok=True)
    code, body = api("DELETE", f"/api/runtime/{runtime_id}", timeout=300)
    (evid / "delete.resp.json").write_text(json.dumps({"http": code, "body": body}, indent=2))
    return code, body


if __name__ == "__main__":
    # smoke: print token length + health
    print("token_len", len(refresh_token()))
    print(api("GET", "/api/deployments?workflow_id=__smoke__"))
