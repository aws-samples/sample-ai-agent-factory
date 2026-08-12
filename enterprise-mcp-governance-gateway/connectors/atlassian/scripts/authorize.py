#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""One-command per-user Atlassian authorization (fixes the racy two-step flow).

Problem it solves: if the MCP client (Kiro) initiates the OAuth consent and a
SEPARATE listener completes it, the client may retry the tool call and create a new
consent session — so the session the browser completes is already superseded
("Invalid or expired session"). This script does the whole thing in ONE process,
with ONE token, so the identity/session that STARTS the consent is the same one that
FINISHES it — no race:

  1. Call an Atlassian read tool through the gateway -> receive the URL-mode consent
     elicitation (or exit if already authorized).
  2. Open that URL in the browser; you approve in Atlassian.
  3. A local listener catches the redirect's session_id and calls
     CompleteResourceTokenAuth to bind + vault YOUR token.

After this succeeds, your MCP client (Kiro), signed in as the SAME user, just works —
no consent prompt, tokens auto-refresh.

Usage:
    source scripts/get-token.sh        # $AGENTCORE_JWT for the user you'll use in Kiro
    .venv/bin/python connectors/atlassian/scripts/authorize.py
"""
import http.server
import json
import os
import sys
import threading
import urllib.parse
import urllib.request

import boto3

REGION = os.environ.get("AWS_REGION", "us-west-2")
JWT = os.environ.get("AGENTCORE_JWT", "")
PORT = 8080
PROBE_TOOL = "Atlassian___getVisibleJiraProjects"  # a read tool -> triggers consent

if not JWT:
    sys.exit("No token. Run:  source scripts/get-token.sh   (same user you use in Kiro), then re-run.")

_ssm = boto3.client("ssm", region_name=REGION)
_dp = boto3.client("bedrock-agentcore", region_name=REGION)
GATEWAY_URL = _ssm.get_parameter(Name="/enterprise-mcp-gateway/gateway/url")["Parameter"]["Value"]
_result = {}


def _trigger_consent() -> str | None:
    """Call a read tool; return the consent auth URL, or None if already authorized."""
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": PROBE_TOOL, "arguments": {}},
    }).encode()
    req = urllib.request.Request(
        GATEWAY_URL, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "Mcp-Protocol-Version": "2025-11-25",
                 "Authorization": f"Bearer {JWT}"},
    )
    # Only allow https:// — refuse file:/, custom, or plaintext schemes (B310).
    if not GATEWAY_URL.lower().startswith("https://"):
        raise ValueError(f"Refusing non-HTTPS gateway URL: {GATEWAY_URL!r}")
    with urllib.request.urlopen(req) as r:  # nosec B310 - scheme checked above
        resp = json.loads(r.read())
    elicits = ((resp.get("error") or {}).get("data") or {}).get("elicitations") or []
    if elicits and elicits[0].get("url"):
        return elicits[0]["url"]
    # No elicitation => already authorized (or a real error)
    if "result" in resp and not resp["result"].get("isError"):
        print("Already authorized — no consent needed. You're good to go in Kiro.")
        return None
    # Log only the response SHAPE and the error CODE — never message text or body, which
    # can carry tokens or user data. Truncation is not redaction, so the message is omitted
    # entirely; re-run with the gateway's own logs if you need the detail.
    err = resp.get("error") or {}
    print("Unexpected response (no consent URL):",
          f"keys={sorted(resp.keys())}",
          f"error.code={err.get('code')}" if err else "",
          "(error message omitted)" if err.get("message") else "")
    return None


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        sid = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
               .get("session_id") or [None])[0]
        ok, err = False, None
        if sid:
            try:
                _dp.complete_resource_token_auth(userIdentifier={"userToken": JWT}, sessionUri=sid)
                ok = True
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
        else:
            err = "no session_id in redirect"
        _result.update(ok=ok, err=err)
        self.send_response(200 if ok else 500)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write((("<h1>Authorized - return to Kiro.</h1>") if ok
                          else f"<h1>Authorization failed</h1><p>{err}</p>").encode())
        threading.Timer(0.5, lambda: os._exit(0 if ok else 1)).start()

    def log_message(self, *a):
        pass


def main():
    print(f"Gateway: {GATEWAY_URL}")
    auth_url = _trigger_consent()
    if not auth_url:
        return
    # Start the listener BEFORE opening the browser so we catch the redirect.
    srv = http.server.HTTPServer(("127.0.0.1", PORT), _Handler)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    print("Opening your browser to approve Atlassian access (log in + Allow)...")
    print(f"  If it doesn't open, paste this into the browser where you're logged into Atlassian:\n  {auth_url}")
    import webbrowser
    webbrowser.open(auth_url)
    import time
    for _ in range(300):
        if _result:
            break
        time.sleep(1)
    if _result.get("ok"):
        print("\n✓ Authorized and token vaulted. In Kiro, the Atlassian tools now return data.")
    else:
        print(f"\n✗ {_result.get('err') or 'timed out waiting for the browser redirect'}")
        sys.exit(1)


if __name__ == "__main__":
    main()
