#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Minimal correct MCP streamable-http client for live gateway testing.

Handles: initialize -> notifications/initialized -> calls. Parses SSE or JSON.
Captures Mcp-Session-Id. NOTHING mocked. Usage:

  python3 mcp_client.py <token> <command> [json-args]

commands:
  tools_list
  call <toolName> <argsJson>
"""
import json
import os
import re
import ssl
import sys
import urllib.request
import urllib.error

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()

def _default_gateway_url():
    """Resolve the gateway URL from env, else SSM Parameter Store (never hardcoded).

    The CDK stack publishes the URL at ``/enterprise-mcp-gateway/gateway/url``.
    boto3 is imported lazily so this script needs no AWS deps when GATEWAY_URL is
    set in the environment.
    """
    env = os.environ.get("GATEWAY_URL")
    if env:
        return env
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )
    prefix = os.environ.get("SSM_PREFIX", "/enterprise-mcp-gateway")
    try:
        import boto3

        ssm = boto3.client("ssm", region_name=region)
        return ssm.get_parameter(Name=f"{prefix}/gateway/url")["Parameter"]["Value"]
    except Exception:
        return ""


GATEWAY_URL = _default_gateway_url()

PROTOCOL_VERSION = "2025-06-18"


def _parse_response(raw_bytes, headers):
    """Return (json_obj_or_None, raw_text). Handles SSE and plain JSON."""
    text = raw_bytes.decode("utf-8", errors="replace")
    ctype = headers.get("Content-Type", "") if headers else ""
    if "text/event-stream" in ctype or text.lstrip().startswith("event:") or "\ndata:" in text or text.startswith("data:"):
        # SSE: collect data: lines, take last JSON payload
        last = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if payload and payload != "[DONE]":
                    try:
                        last = json.loads(payload)
                    except json.JSONDecodeError:
                        pass
        return last, text
    try:
        return json.loads(text), text
    except json.JSONDecodeError:
        return None, text


def post(token, body, session_id=None):
    # Only ever speak HTTPS to the gateway. Guarding the scheme prevents urllib
    # from honouring file:// or other schemes if GATEWAY_URL is misconfigured
    # (addresses Bandit B310). nosec B310: scheme is asserted to be https below.
    if not GATEWAY_URL.lower().startswith("https://"):
        raise ValueError(f"Refusing non-HTTPS gateway URL: {GATEWAY_URL!r}")
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(GATEWAY_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("Authorization", f"Bearer {token}")
    if session_id:
        req.add_header("Mcp-Session-Id", session_id)
    try:
        # GATEWAY_URL is asserted https:// above; TLS context is pinned. nosec B310
        with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as resp:  # nosec B310
            raw = resp.read()
            hdrs = dict(resp.headers)
            obj, text = _parse_response(raw, resp.headers)
            sid = resp.headers.get("Mcp-Session-Id") or session_id
            return {"status": resp.status, "headers": hdrs, "json": obj, "text": text, "session_id": sid}
    except urllib.error.HTTPError as e:
        raw = e.read()
        obj, text = _parse_response(raw, e.headers)
        sid = e.headers.get("Mcp-Session-Id") if e.headers else session_id
        return {"status": e.code, "headers": dict(e.headers) if e.headers else {}, "json": obj, "text": text, "session_id": sid}
    except Exception as e:
        return {"status": -1, "headers": {}, "json": None, "text": f"TRANSPORT_ERROR: {type(e).__name__}: {e}", "session_id": session_id}


def initialize(token):
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "e2e-tester", "version": "1.0"},
        },
    }
    r = post(token, body)
    sid = r["session_id"]
    if r["status"] == 200 and sid:
        # send initialized notification
        note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        post(token, note, session_id=sid)
    return r


def main():
    token = sys.argv[1]
    cmd = sys.argv[2]

    init = initialize(token)
    sid = init["session_id"]
    out = {"initialize": {"status": init["status"], "session_id": sid,
                          "result_keys": list((init["json"] or {}).get("result", {}).keys()) if init.get("json") else None,
                          "raw_snippet": (init["text"] or "")[:400]}}

    if cmd == "tools_list":
        r = post(token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, session_id=sid)
        out["tools_list"] = {"status": r["status"], "json": r["json"], "raw_snippet": (r["text"] or "")[:600]}
    elif cmd == "call":
        tool = sys.argv[3]
        args = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
        r = post(token, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": tool, "arguments": args}}, session_id=sid)
        out["call"] = {"tool": tool, "args": args, "status": r["status"],
                       "json": r["json"], "raw_snippet": (r["text"] or "")[:1500]}
    # Demonstrate the secure pattern even in a dev tool: never log tokens/credentials.
    # Mask values of sensitive-looking keys and any Bearer tokens found in strings.
    def _redact(obj):
        sensitive = ("authorization", "token", "secret", "password", "credential", "apikey", "api_key")
        if isinstance(obj, dict):
            return {k: ("[REDACTED]" if any(s in k.lower() for s in sensitive) else _redact(v))
                    for k, v in obj.items()}
        if isinstance(obj, list):
            return [_redact(v) for v in obj]
        if isinstance(obj, str):
            return re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]+", "Bearer [REDACTED]", obj)
        return obj

    print(json.dumps(_redact(out), indent=2, default=str))


if __name__ == "__main__":
    main()
