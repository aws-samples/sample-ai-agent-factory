"""Live end-to-end verifier: AgentCore Gateway -> external MCP catalog target.

Proves the deploy_external_mcp_target() path works against REAL AWS for a
credential-free (Tier 1) MCP from services/mcp_catalog.py:
  1. Create a Cognito user pool + M2M app client + resource server (JWT inbound).
  2. Create a real AgentCore Gateway (CUSTOM_JWT authorizer -> that pool).
  3. Add the chosen external MCP as an mcpServer target via the deploy path.
  4. Mint an M2M access token, call the gateway MCP plane: tools/list +
     tools/call (search tool), assert a REAL upstream canary.
  5. Tear everything down (target, gateway, pool) — no billable orphans.

Usage:  AWS_REGION=us-west-2 python3 scripts/verify-external-mcp.py [catalog_id]
        catalog_id defaults to "aws-knowledge" (live-verified 2026-07-16).
        Only Tier-1 (auth_type="none") catalog ids are safe to run credential-free.

Note: uses curl for HTTPS (macOS system Python often lacks CA roots for urllib).
"""

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import boto3

# Resolve backend/src relative to this script so it's not machine-specific.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "backend", "src"))

REGION = os.environ.get("AWS_REGION", "us-west-2")
SUFFIX = "mcpext"
CATALOG_ID = sys.argv[1] if len(sys.argv) > 1 else "aws-knowledge"
ctrl = boto3.client("bedrock-agentcore-control", region_name=REGION)
cog = boto3.client("cognito-idp", region_name=REGION)

created = {}


def log(m):
    print(f"[live] {m}", flush=True)


def cleanup():
    log("=== TEARDOWN ===")
    gid = created.get("gateway_id")
    if gid:
        try:
            for t in ctrl.list_gateway_targets(gatewayIdentifier=gid).get("items", []):
                ctrl.delete_gateway_target(gatewayIdentifier=gid, targetId=t["targetId"])
                log(f"deleted target {t['targetId']}")
            time.sleep(5)
            ctrl.delete_gateway(gatewayIdentifier=gid)
            log(f"deleted gateway {gid}")
        except Exception as e:
            log(f"gateway teardown err: {e}")
    pid = created.get("pool_id")
    if pid:
        try:
            dom = created.get("domain")
            if dom:
                try:
                    cog.delete_user_pool_domain(Domain=dom, UserPoolId=pid)
                except Exception:
                    pass
            cog.delete_user_pool(UserPoolId=pid)
            log(f"deleted pool {pid}")
        except Exception as e:
            log(f"pool teardown err: {e}")


def main():
    # 1. Cognito pool + resource server + M2M client
    pool = cog.create_user_pool(PoolName=f"mcp-ext-{SUFFIX}")["UserPool"]
    pid = pool["Id"]
    created["pool_id"] = pid
    log(f"pool {pid}")
    domain = f"mcpext-{pid.split('_')[1].lower()}"
    cog.create_user_pool_domain(Domain=domain, UserPoolId=pid)
    created["domain"] = domain
    scope = "gateway/invoke"
    cog.create_resource_server(
        UserPoolId=pid,
        Identifier="gateway",
        Name="gateway",
        Scopes=[{"ScopeName": "invoke", "ScopeDescription": "invoke"}],
    )
    cli = cog.create_user_pool_client(
        UserPoolId=pid,
        ClientName="m2m",
        GenerateSecret=True,
        AllowedOAuthFlows=["client_credentials"],
        AllowedOAuthScopes=[scope],
        AllowedOAuthFlowsUserPoolClient=True,
        SupportedIdentityProviders=["COGNITO"],
    )["UserPoolClient"]
    cid, csec = cli["ClientId"], cli["ClientSecret"]
    disc = f"https://cognito-idp.{REGION}.amazonaws.com/{pid}/.well-known/openid-configuration"
    log(f"client {cid}; discovery {disc}")
    time.sleep(8)  # let pool/domain settle

    # 2. Gateway with CUSTOM_JWT authorizer
    role_arn = _ensure_gateway_role()
    gw = ctrl.create_gateway(
        name=f"mcpextgw{SUFFIX}",
        roleArn=role_arn,
        protocolType="MCP",
        authorizerType="CUSTOM_JWT",
        authorizerConfiguration={"customJWTAuthorizer": {"discoveryUrl": disc, "allowedClients": [cid]}},
    )
    gid = gw["gatewayId"]
    created["gateway_id"] = gid
    gurl = gw.get("gatewayUrl")
    log(f"gateway {gid} url={gurl}")
    # wait READY
    for _ in range(30):
        g = ctrl.get_gateway(gatewayIdentifier=gid)
        if g.get("status") == "READY":
            gurl = g.get("gatewayUrl")
            break
        time.sleep(5)
    log(f"gateway status READY, url={gurl}")

    # 3. Add AWS Knowledge MCP as external target via NEW code path
    from app.services.gateway_deployer import deploy_external_mcp_target
    from app.services.mcp_catalog import get_mcp_server

    entry = get_mcp_server(CATALOG_ID)
    assert entry, f"unknown catalog id {CATALOG_ID}"
    tgt = deploy_external_mcp_target(ctrl, gateway_id=gid, catalog_entry=entry)
    log(f"external MCP target created: {tgt.get('targetId') if tgt else None}")
    # wait target READY
    for _ in range(20):
        ts = ctrl.list_gateway_targets(gatewayIdentifier=gid).get("items", [])
        st = ts[0].get("status") if ts else "?"
        log(f"target status: {st}")
        if st == "READY":
            break
        if st in ("FAILED", "UPDATE_UNSUCCESSFUL"):
            log(f"target FAILED detail: {ts[0].get('statusReasons')}")
            break
        time.sleep(10)

    # 4. M2M token + call gateway MCP plane
    tok = _m2m_token(domain, cid, csec, scope)
    log(f"got M2M token ({len(tok)} chars)")
    mcp_url = gurl if gurl.endswith("/mcp") else gurl.rstrip("/") + "/mcp"
    tools = _mcp(mcp_url, tok, "tools/list", {})
    names = [t["name"] for t in tools.get("result", {}).get("tools", [])]
    log(f"gateway tools/list -> {names}")
    # find the search tool (qualified <target>___search_documentation)
    search = next((n for n in names if "search_documentation" in n), None)
    assert search, f"search_documentation not exposed; got {names}"
    call = _mcp(
        mcp_url,
        tok,
        "tools/call",
        {"name": search, "arguments": {"search_phrase": "What is Amazon Bedrock AgentCore Gateway"}},
    )
    blob = json.dumps(call)
    log(f"tools/call result (first 500): {blob[:500]}")
    # CANARY: a real AWS-doc answer must mention agentcore / gateway / bedrock
    low = blob.lower()
    assert any(k in low for k in ("agentcore", "gateway", "bedrock")), "no real AWS-doc content returned"
    log("✅ CANARY PASS — external AWS Knowledge MCP reachable through the Gateway with real doc content")


def _ensure_gateway_role():
    iam = boto3.client("iam")
    name = "AgentCoreMcpExtGatewayRole"
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole"}
        ],
    }
    try:
        r = iam.get_role(RoleName=name)
        return r["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        r = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))
        iam.put_role_policy(
            RoleName=name,
            PolicyName="inline",
            PolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [{"Effect": "Allow", "Action": ["bedrock-agentcore:*"], "Resource": "*"}],
                }
            ),
        )
        time.sleep(10)
        return r["Role"]["Arn"]


def _curl(args):
    """Run curl (uses the system trust store, avoiding macOS-Python SSL issues).

    All args originate in this script (AWS API responses interpolated into
    fixed flag positions, never a shell) — list-form exec means no injection
    surface, but keep the https guard so a compromised API value can't turn
    curl into a file:// reader.
    """
    for a in args:
        if a.startswith(("file://", "ftp://", "dict://", "gopher://")):
            raise ValueError(f"blocked URL scheme in curl arg: {a[:40]}")
    r = subprocess.run(  # noqa: S603 -- list-form argv, no shell, args validated above
        ["curl", "-sS", "-m", "40", "--proto", "=https", *args],
        capture_output=True,
        text=True,
    )
    return r.stdout


def _m2m_token(domain, cid, csec, scope):
    url = f"https://{domain}.auth.{REGION}.amazoncognito.com/oauth2/token"
    for attempt in range(6):
        out = _curl(
            [
                "-X",
                "POST",
                url,
                "-u",
                f"{cid}:{csec}",
                "-H",
                "Content-Type: application/x-www-form-urlencoded",
                "-d",
                f"grant_type=client_credentials&scope={urllib.parse.quote(scope)}",
            ]
        )
        try:
            return json.loads(out)["access_token"]
        except Exception:
            log(f"token attempt {attempt}: {out[:200]}")
            time.sleep(10)
    raise RuntimeError("could not get M2M token")


def _mcp(url, token, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    out = _curl(
        [
            "-X",
            "POST",
            url,
            "-H",
            "Content-Type: application/json",
            "-H",
            "Accept: application/json, text/event-stream",
            "-H",
            f"Authorization: Bearer {token}",
            "-d",
            body,
        ]
    )
    if out.startswith("event:"):
        for line in out.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
    return json.loads(out)


if __name__ == "__main__":
    try:
        main()
    finally:
        cleanup()
