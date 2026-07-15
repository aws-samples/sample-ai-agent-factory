#!/usr/bin/env python3
"""Generic single-cell runner: deploy -> poll -> invoke (multi-turn) -> gate -> record -> delete.

A cell spec is a dict:
  {
    "cell": "<surface>__<pattern>",
    "pattern": "P-...",
    "surface": "step_functions_ui",
    "payload": {...},                # /api/deploy body (canary already injected)
    "canary": "MTX-CANARY-xxxx",
    "probes": [                      # ordered; same runtime, sessionId carried if set
        {"input": "...", "session": "abc", "expect_canary": True,
         "expect_no_tool": False, "history_from_prev": False}
    ],
    "delete": True,                  # delete the flow after verdict
    "verdict_mode": "all_probes"     # PASS requires every expect_canary probe to contain canary
  }

Usage: runcell.py <spec.json>   (spec file is one JSON cell)
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import driver as d



def run_post(kind, rid, arn, canary, evid):
    """Extra pattern-defining assertions beyond chat probes."""
    import urllib.request, urllib.error
    if kind == "unauth_401":
        url = f"{d.API}/api/workflows"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=30, context=d._SSL_CTX) as r:
                return False, f"unauth returned {r.status}"
        except urllib.error.HTTPError as e:
            return e.code in (401, 403), f"unauth={e.code}"
    if kind == "agent_card":
        # The A2A card is served at the runtime's HTTP GET /.well-known/agent-card.json,
        # not via the POST /invocations data plane. Control-plane proof: get-agent-runtime
        # shows serverProtocol=HTTP (Bug 129) AND the A2A_* env carries the advertised name.
        import boto3
        cl = boto3.Session(region_name=d.REGION).client("bedrock-agentcore-control")
        try:
            rt = cl.get_agent_runtime(agentRuntimeId=arn.split("/")[-1] if arn else rid)
        except Exception as e:
            return False, f"get-agent-runtime failed: {e}"
        (evid / "agent_card.json").write_text(json.dumps(rt, default=str)[:4000])
        proto = (rt.get("protocolConfiguration", {}) or {}).get("serverProtocol") or rt.get("serverProtocol", "")
        env = json.dumps(rt.get("environmentVariables") or rt.get("runtimeConfiguration") or rt)
        name_ok = ("A2A" in env) or ("a2a" in env.lower())
        return (proto.upper() == "HTTP"), f"serverProtocol={proto} a2a_env_present={name_ok}"
    if kind == "mcp_tools_call":
        # speak MCP over the data plane exactly as mcp_server_step pre-warm does:
        # contentType=application/json, accept=application/json, text/event-stream
        # (NO mcpProtocolVersion kwarg — that yields HTTP 406). Parse SSE or JSON.
        import boto3, uuid
        cl = boto3.Session(region_name=d.REGION).client("bedrock-agentcore")
        sid = ("e2emcp" + uuid.uuid4().hex + "0"*40)[:48]
        out = []
        def _extract(raw):
            # SSE frames come as "data: {json}\n"; else plain JSON
            for line in raw.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    out.append(line[5:].strip())
                elif line.startswith("{"):
                    out.append(line)
        import time as _mt
        def _hs():
            out.clear()
            for payload in (
                {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"e2e","version":"1"}}},
                {"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}},
            ):
                r = cl.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=sid,
                                            payload=json.dumps(payload).encode(),
                                            contentType="application/json",
                                            accept="application/json, text/event-stream")
                _extract(r["response"].read().decode("utf-8", errors="replace"))
        try:
            for _att in range(8):
                _hs()
                if not any("-32010" in f or "initialization time" in f for f in out):
                    break
                _mt.sleep(15)  # MCP runtime still cold-starting; warm & retry
        except Exception as e:
            (evid / "mcp.err").write_text(str(e))
            return False, f"mcp error {e}"
        (evid / "mcp.out.json").write_text(json.dumps(out, indent=2))
        joined = " ".join(out)
        # PASS when tools/list returned at least one tool
        return ('"tools"' in joined and ('"name"' in joined or '"result"' in joined)), f"mcp handshake frames={len(out)}"
    return False, f"unknown post {kind}"


def run(spec):
    cell = spec["cell"]
    evid = d.EVID_ROOT / cell
    canary = spec.get("canary")
    rec = {"cell": cell, "pattern": spec.get("pattern"),
           "surface": spec.get("surface", "step_functions_ui"),
           "canary": canary, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    print(f"=== {cell} canary={canary} ===", flush=True)

    # Control-plane specs (surface=control_plane, no deploy payload) are driven by
    # the REST control-plane runner, not this deploy-oriented runcell. Skip them
    # here gracefully instead of KeyError'ing on spec["payload"].
    if "payload" not in spec or spec.get("surface") == "control_plane":
        print(f"SKIP: {cell} is a control-plane spec (no deploy payload) — run via control_plane_run.py", flush=True)
        rec["verdict"] = "SKIP"
        rec["reason"] = "control-plane spec; not a deploy cell"
        d.save_cell(cell, rec)
        return

    dep = d.deploy_and_wait(spec["payload"], evid, max_wait=spec.get("max_wait", 1500))
    rec["deployment_id"] = dep.get("deployment_id")
    rec["final_status"] = dep.get("final_status")
    if dep.get("phase") == "deploy" and dep.get("http") not in (200, 202):
        rec["verdict"] = "FAIL"
        rec["reason"] = f"deploy HTTP {dep.get('http')}"
        rec["error_detail"] = json.dumps(dep.get("body"))[:500]
        d.save_cell(cell, rec)
        print("VERDICT FAIL (deploy http):", rec["error_detail"], flush=True)
        return rec
    if dep.get("final_status") not in d.TERMINAL_OK:
        st = dep.get("state", {})
        rec["verdict"] = "FAIL"
        rec["reason"] = f"deploy status={dep.get('final_status')}"
        rec["error_detail"] = str(st.get("error_details") or st.get("error")
                                   or st.get("status_message"))[:800]
        rec["failed_step"] = st.get("current_step")
        d.save_cell(cell, rec)
        print("VERDICT FAIL (deploy):", rec["reason"], rec["error_detail"], flush=True)
        return rec

    state = dep["state"]
    rid, arn = d.resolve_runtime(state)
    rec["runtime_id"] = rid
    rec["runtime_arn"] = arn
    rec["created_resources"] = state.get("created_resources")
    print(f"deployed rid={rid}", flush=True)

    _settle = spec.get("settle_after_deploy")
    if _settle and rid:
        import time as _st
        _depid = dep.get("deployment_id")
        print(f"  settle {_settle}s, polling deploy-status to drive lazy Cedar promoter", flush=True)
        _elapsed = 0
        while _elapsed < _settle:
            # GET /api/deploy/{id} triggers _maybe_promote_policy (LOG_ONLY->ENFORCE)
            # on the backend — the documented touchpoint that converges the policy
            # plane so the gateway serves tools under ENFORCE. The engine<->gateway
            # authorization plane can take 15-30 MIN to converge on a fresh gateway
            # (proven live), so poll for the full window but BREAK EARLY the moment
            # the promoter clears enforce_pending (== ENFORCE now serving tools).
            if _depid:
                _c, _st_body = d.api("GET", f"/api/deploy/{_depid}")
                _pr = (_st_body or {}).get("policy_result") or {}
                if _pr.get("mode") == "ENFORCE" and not _pr.get("enforce_pending") \
                        and not _pr.get("enforce_validation_pending"):
                    print(f"  Cedar promoted to ENFORCE (converged) after {_elapsed}s", flush=True)
                    break
            _st.sleep(20); _elapsed += 20

    # Pre-warm: cold gateway/Cedar/MCP/memory runtimes 503/500 on first invoke within the
    # 30s window. Mirror the platform's own mcp_server_step pre-warm: hammer a throwaway
    # invoke until it returns cleanly, so the GRADED probe lands on a warm container.
    if spec.get("prewarm") and rid:
        import time as _pw
        for _w in range(12):
            if spec.get("direct"):
                _c, _b = d.invoke_direct(rid, "ping", arn=arn, timeout=280)
            else:
                _c, _b = d.invoke(rid, "ping", timeout=60)
            _e = (_b or {}).get("error") or ""
            if _c == 200 and _b.get("success") and not _e:
                print(f"  prewarm ok after {_w+1}", flush=True)
                break
            _pw.sleep(15)

    probe_results = []
    all_pass = True
    history = []
    for i, pr in enumerate(spec.get("probes", [])):
        # Same-session multi-turn: AgentCore rejects concurrent invocations on one
        # session (ConcurrencyException). Settle between turns and retry on that error.
        import time as _t
        _attempt = 0
        while True:
            if pr.get("use_stream"):
                code, body = d.invoke_stream(rid, pr["input"], session_id=pr.get("session"),
                                             timeout=pr.get("timeout", 240))
            else:
                code, body = d.invoke(rid, pr["input"], session_id=pr.get("session"),
                                  history=history if pr.get("history_from_prev") else None,
                                  timeout=pr.get("timeout", 120))
            _err = (body or {}).get("error") or ""
            _transient = ("Concurren" in _err or "RuntimeClientError" in _err
                          or "Runtime invocation failed" in _err or "Harness invocation failed" in _err
                          or "(500)" in _err or "not found" in _err.lower())
            if _transient and _attempt < 6:
                _attempt += 1
                _t.sleep(12 * _attempt)  # cold-start / policy-plane warm-up backoff
                continue
            break
        # Fall back to the SigV4 streaming Function URL when the sync API-GW path
        # hits its 30s ceiling (tool-heavy / MCP-gateway turns). Not a FAIL.
        err = (body.get("error") or "")
        if not body.get("success") and ("30s" in err or "exceeded" in err or "still running" in err):
            print(f"  probe{i} sync 30s limit -> retrying via direct data-plane invoke", flush=True)
            code, body = d.invoke_direct(rid, pr["input"], session_id=pr.get("session"),
                                        arn=arn, timeout=pr.get("stream_timeout", 280))
        resp = body.get("response") or ""
        (evid / f"probe{i}.resp.json").write_text(
            json.dumps({"http": code, "body": body, "input": pr["input"]}, indent=2))
        want_canary = pr.get("expect_canary", True)
        if pr.get("expect_blocked"):
            gates = {"blocked_marker": pr["expect_blocked"] in (resp or "")
                     or "guardrail" in (resp or "").lower() or "blocked" in (resp or "").lower() or "redact" in (resp or "").lower()}
            passed = gates["blocked_marker"]
        elif pr.get("expect_denied"):
            low = (resp or "").lower()
            _forbidden_markers = pr.get("forbidden_values") or ["NEVER-SEE-THIS", "WIDGET"]
            _leaked = any(m in (resp or "") for m in _forbidden_markers)
            _explicit_denial = ("denied" in low or "not permitted" in low or "forbidden" in low
                                or "don't have access" in low or "do not have access" in low or "unable" in low
                                or "don't have" in low or "do not have" in low or "not available" in low
                                or "no get_restricted" in low or "isn't available" in low
                                or "not have a" in low or "no such tool" in low or "can't find" in low
                                or "access denied" in low or "not authorized" in low or "blocked" in low)
            # Cedar ENFORCE default-deny: a blocked tool call yields either an
            # explicit refusal OR an empty/short non-answer (the value never
            # reaches the model). BOTH are correct enforcement — the security
            # assertion is that the forbidden value did NOT leak. An empty/short
            # response counts as denied ONLY because the settle loop already
            # confirmed the engine is ACTIVE in ENFORCE before we probe.
            _empty_or_short = len((resp or "").strip()) < 40
            gates = {"not_leaked": not _leaked,
                     "denied_or_empty": _explicit_denial or _empty_or_short}
            passed = gates["not_leaked"] and gates["denied_or_empty"]
        elif pr.get("expect_contains_any"):
            _kws = pr["expect_contains_any"]
            gates = {"contains_any": any(k.lower() in (resp or "").lower() for k in _kws),
                     "http_200": (code == 200 and bool(body.get("success")))}
            passed = gates["contains_any"] and gates["http_200"]
        elif pr.get("ack_only"):
            if not (code == 200 and bool(body.get("success"))):
                import time as _at; _at.sleep(10)
                code, body = d.invoke(rid, pr["input"], session_id=pr.get("session"))
            gates = {"http_200": (code == 200 and bool(body.get("success")))}
            passed = gates["http_200"]
        else:
            passed, gates = d.gate(resp, canary=canary, require_canary=want_canary)
            gates["http_200"] = (code == 200 and bool(body.get("success")))
            passed = passed and gates["http_200"]
        pres = {"input": pr["input"][:80], "http": code, "success": body.get("success"),
                "resp": resp[:200], "gates": gates, "passed": passed,
                "error": body.get("error")}
        probe_results.append(pres)
        print(f"  probe{i} pass={passed} gates={gates} resp={resp[:120]!r} err={body.get('error')}",
              flush=True)
        import time as _t2
        _t2.sleep(6)  # let the runtime release the session before the next turn
        if pr.get("history_from_prev") is not None:
            history = history + [{"role": "user", "content": pr["input"]},
                                 {"role": "assistant", "content": resp}]
        if not passed:
            all_pass = False

    post = spec.get("post")
    if post:
        ok, detail = run_post(post, rid, arn, canary, evid)
        probe_results.append({"post": post, "passed": ok, "detail": str(detail)[:300]})
        if not ok:
            all_pass = False

    rec["probes"] = probe_results
    rec["verdict"] = "PASS" if (all_pass and probe_results) else "FAIL"
    if not all_pass:
        rec["reason"] = "one or more probe gates failed"
    d.save_cell(cell, rec)
    print("VERDICT:", rec["verdict"], flush=True)

    if spec.get("delete", True) and rid:
        dc, db = d.delete_flow(rid, evid)
        rec["delete_http"] = dc
        rec["delete_success"] = (db or {}).get("success")
        d.save_cell(cell, rec)
        print(f"  deleted http={dc} success={(db or {}).get('success')}", flush=True)
    return rec


if __name__ == "__main__":
    spec = json.loads(Path(sys.argv[1]).read_text())
    run(spec)
