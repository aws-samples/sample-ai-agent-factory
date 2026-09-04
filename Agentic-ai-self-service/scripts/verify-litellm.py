"""Live end-to-end verifier: LiteLLM MCP Gateway + LiteLLM-as-registry.

Companion to verify-external-mcp.py. That script proves the AgentCore Gateway
path against real AWS; this one proves the LiteLLM path against a REAL LiteLLM
proxy, because the unit suites for it are necessarily mock-based — they assert
what we *believe* LiteLLM returns. This asserts what it actually returns.

Two shapes were confirmed by running this, and neither is guessable from the
LiteLLM docs alone:
  * ``GET /v1/mcp/server`` returns a BARE LIST, not an object with a ``data`` key.
  * ``GET /mcp-rest/tools/list`` returns an OBJECT with a ``tools`` key.
A parser written for one shape silently returns zero items for the other, which
is exactly the empty-tool-plane failure the readiness gate exists to catch — so
these two shapes are the highest-value thing in this file.

What it exercises (all real HTTP against the proxy, no mocks):
  1. The gateway probe + parsers: _get_json/_items/_server_aliases/_tool_names,
     resolve_mcp_url, and probe_litellm_gateway.
  2. The readiness gate: fail-loud on a pinned server the proxy does not serve,
     and on a rejected API key.
  3. The registry projection: list_litellm_servers -> _project -> RegistryEntry,
     enablement, and is_read_only.
  4. The deploy governance gate: present+enabled = approved, absent = blocked.

Deliberately NOT covered here, and why:
  * The control-plane SSRF validator. It requires https and rejects private IPs,
    so it rejects a localhost proxy by design. It is covered by unit tests and is
    live-proven against the deployed API (it is what returns the 400s there).
    This script therefore calls the probe/parse layer directly, which is the
    layer whose contract with LiteLLM is unproven.
  * The AgentCore-Runtime-to-LiteLLM network leg, which needs a publicly
    reachable proxy. What runs here proves the wiring we generate is correct;
    it cannot prove the customer's proxy is reachable from their VPC.

The sidecar half of the registry needs a DynamoDB table. Point
AGENT_REGISTRY_TABLE_NAME at a deployed one to exercise the real merge, or omit
it to skip just that step. Only reads and refused writes are issued, so it is
safe against a live table.

Setup (no Docker needed, but DO read the two warnings after the commands):

    V=/tmp/litellm-verify
    python3 -m venv $V/venv
    $V/venv/bin/pip install 'litellm[proxy]' prisma
    cat > $V/config.yaml <<'YAML'
    model_list:
      - model_name: dummy-gpt
        litellm_params: {model: openai/gpt-4o-mini, api_key: sk-unused}
    mcp_servers:
      aws_knowledge:
        url: "https://knowledge-mcp.global.api.aws/mcp"
        transport: "http"
        description: "AWS Knowledge MCP - documentation search"
    general_settings: {master_key: sk-verify-1234}
    YAML

    # 1. Postgres. NOT optional -- see warning A.
    brew install postgresql@16
    PG=$(brew --prefix postgresql@16)/bin
    $PG/initdb -D /tmp/llpg -U litellm --auth=trust
    $PG/pg_ctl -D /tmp/llpg -o "-p 5439 -k /tmp/llpg" start
    $PG/createdb -h /tmp/llpg -p 5439 -U litellm litellm

    # 2. Schema. `prisma generate` needs the venv on PATH to find prisma-client-py.
    export DATABASE_URL="postgresql://litellm@localhost:5439/litellm?host=/tmp/llpg"
    export PATH="$V/venv/bin:$PATH"
    prisma generate && prisma db push --accept-data-loss --skip-generate

    # 3. Run it, then WAIT -- see warning B.
    STORE_MODEL_IN_DB=True LITELLM_MASTER_KEY=sk-verify-1234 \
      $V/venv/bin/litellm --config $V/config.yaml --port 4000 > $V/proxy.log 2>&1 &

Warning A -- the MCP endpoints require a database. Without one, litellm 1.99.0
answers BOTH probe endpoints with
``{"error":{"message":"No connected db.","type":"no_db_connection","code":"400"}}``
and only ``/mcp/enabled`` responds. An earlier version of this recipe omitted
Postgres and the script could not run at all.

Warning B -- first startup takes roughly half an hour, and looks like a crash the
whole time. Because step 2 creates the schema with ``prisma db push``, LiteLLM's
own ``litellm_proxy_extras`` finds a non-empty schema, hits prisma **P3005**, and
baselines by resolving all 158 migrations one at a time at ~10s each. Throughout,
``/health/liveliness`` returns nothing at all (curl exit 000) -- the process is
alive and making progress. Do not kill it. Track progress with:

    grep -c 'Resolving migration' /tmp/litellm-verify/proxy.log   # target: 158

and wait for ``curl -o /dev/null -w '%{http_code}' localhost:4000/health/liveliness``
to return 200 before running this script. (Letting LiteLLM own the schema on an
empty database instead of pre-pushing it would plausibly skip the baseline, but
that path is untested here -- the recipe above is the one that was actually run.)

Teardown: ``pkill -f 'litellm --config'``, ``pg_ctl -D /tmp/llpg stop -m fast``,
``rm -rf /tmp/llpg /tmp/litellm-verify``.

Usage:
    python3 scripts/verify-litellm.py [base_url] [api_key]
    defaults: http://127.0.0.1:4000  sk-verify-1234
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "backend", "src"))

from app.services import litellm_gateway_deployer as G  # noqa: E402
from app.services.registry_providers import litellm as L  # noqa: E402

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:4000").rstrip("/")
KEY = sys.argv[2] if len(sys.argv) > 2 else "sk-verify-1234"

_failures: list[str] = []


def check(label: str, got, want=None, predicate=None) -> None:
    """Assert and keep going, so one failure does not hide the rest."""
    ok = predicate(got) if predicate else (got == want)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {got!r}")
    if not ok:
        _failures.append(f"{label}: got {got!r}" + (f", want {want!r}" if predicate is None else ""))


print(f"LiteLLM live verification against {BASE}\n")

# -- 1. the wire shapes, which are the whole point of this script --------------
print("1. Raw payload shapes (a parser written for the wrong one silently sees 0)")
servers_payload = G._get_json(BASE + G._SERVERS_PATH, KEY)
tools_payload = G._get_json(BASE + G._TOOLS_LIST_PATH, KEY)
check(f"{G._SERVERS_PATH} is a bare list", type(servers_payload).__name__, "list")
check(f"{G._TOOLS_LIST_PATH} is an object", type(tools_payload).__name__, "dict")

print("\n2. Our parsers against those real payloads")
aliases = G._server_aliases(servers_payload)
tool_names = G._tool_names(tools_payload)
check("_items finds servers", len(G._items(servers_payload)), predicate=lambda n: n >= 1)
check("_server_aliases", aliases, predicate=lambda a: "aws_knowledge" in a)
check("_tool_names", len(tool_names), predicate=lambda n: n >= 1)
check("tool names look like MCP tools", tool_names[:3], predicate=lambda t: all(isinstance(x, str) and x for x in t))

print("\n3. resolve_mcp_url")
check("aggregate endpoint", G.resolve_mcp_url(BASE), f"{BASE}/mcp/")
check("pinned server endpoint", G.resolve_mcp_url(BASE, ["aws_knowledge"]), f"{BASE}/aws_knowledge/mcp")

print("\n4. probe_litellm_gateway (the readiness gate)")
probe = G.probe_litellm_gateway(BASE, KEY)
check("reports servers", probe.get("servers"), predicate=lambda s: bool(s))
check("reports tools", len(probe.get("tools") or []), predicate=lambda n: n >= 1)

print("\n5. The gate fails LOUD, not silently (an agent with no tools is not a success)")
try:
    G.probe_litellm_gateway(BASE, KEY, ["definitely-not-configured"])
    unknown_server_msg = ""  # reached only if it wrongly succeeded
except G.LiteLLMGatewayError as e:
    unknown_server_msg = str(e)
# The message must name the server that is missing, not just fail — an operator
# who pinned a typo needs to see which alias the proxy does not serve.
check(
    "unknown pinned server raises and names it",
    unknown_server_msg[:60],
    predicate=lambda _: "definitely-not-configured" in unknown_server_msg,
)

try:
    G.probe_litellm_gateway(BASE, "sk-wrong-key-entirely")
    rejected_key_raised = False
except G.LiteLLMGatewayError:
    rejected_key_raised = True
check("rejected key raises", rejected_key_raised, True)

# -- 6. registry side ---------------------------------------------------------
print("\n6. Registry projection (settings/secret plumbing stubbed; HTTP is real)")
L.get_litellm_registry_config = lambda: {"base_url": BASE, "api_key_ref": "stub", "verified": True}
L._read_api_key = lambda _ref: KEY

raw_servers = L.list_litellm_servers()
check("list_litellm_servers", len(raw_servers), predicate=lambda n: n >= 1)
server = raw_servers[0]
check("_server_name", L._server_name(server), predicate=lambda s: bool(s))
# This release reports no enabled/disabled/status field at all, so presence in
# the list is the signal. Kept as an explicit check because a release that DOES
# report one must not regress into being ignored.
check("_server_is_enabled", L._server_is_enabled(server), True)

entry = L._project(server)
provider = L.LiteLLMRegistryProvider()
caps = provider.capabilities()
check("projected status is approved", entry.status, "approved")
check("projected source is litellm", entry.source, L.SOURCE_LITELLM)
check("projected entry is read-only", caps.is_read_only(entry), True)

probe_r = L.probe_litellm_registry(BASE, KEY)
check("probe_litellm_registry reachable", probe_r.get("reachable"), True)

print("\n7. Deploy governance gate against the real catalog (fail-closed)")
name = L._server_name(server)
check("present+enabled = approved", L.litellm_unapproved_integrations([name]), [])
check("absent = blocked", L.litellm_unapproved_integrations(["no-such-server"]), ["no-such-server"])
check("mixed blocks only the absent one", L.litellm_unapproved_integrations([name, "nope"]), ["nope"])

# -- 8. the merge, only if a real table is available --------------------------
table = os.environ.get("AGENT_REGISTRY_TABLE_NAME")
if not table:
    print("\n8. SKIPPED (set AGENT_REGISTRY_TABLE_NAME to a deployed table for the real merge)")
else:
    print(f"\n8. Merge of the DynamoDB sidecar ({table}) and the LiteLLM projection")
    merged = provider.list_for_org("default-org")
    by_source: dict[str, int] = {}
    for e in merged:
        by_source[str(e.source)] = by_source.get(str(e.source), 0) + 1
        print(f"     {e.agent_slug:20} source={str(e.source):9} read_only={caps.is_read_only(e)}")
    check("projection reaches the merged list", by_source.get(L.SOURCE_LITELLM, 0), predicate=lambda n: n >= 1)
    # Writes to a projected slug must refuse BEFORE mutating anything.
    for verb, fn in (
        ("update", lambda: provider.update("default-org", entry.agent_slug, {"description": "should not persist"})),
        ("delete", lambda: provider.delete("default-org", entry.agent_slug)),
    ):
        try:
            fn()
            check(f"{verb} on a projected entry refuses", "accepted", predicate=lambda _: False)
        except L.UnsupportedRegistryOperation:
            check(f"{verb} on a projected entry refuses", "UnsupportedRegistryOperation", predicate=lambda _: True)

print("\n" + "=" * 64)
if _failures:
    print(f"FAILED ({len(_failures)}):")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("All LiteLLM live checks passed.")
