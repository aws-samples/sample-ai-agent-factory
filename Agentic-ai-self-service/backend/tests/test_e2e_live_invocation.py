"""Live end-to-end test: deploy each AgentCore pattern and invoke the runtime.

This script:
1. Generates agent code for each pattern
2. Uploads code.zip with bundled deps to S3
3. Creates IAM role + AgentCore Runtime (PYTHON_3_13)
4. Waits for READY status
5. Invokes the runtime with a test prompt
6. Collects results and cleans up
7. Prints a summary table

Run:
    cd backend
    PYTHONPATH=src python tests/test_e2e_live_invocation.py
"""

import json
import os
import sys
import time
import traceback
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import boto3

from app.services.deployment import generate_unified_agent_code
from app.services.code_generator import generate_agent_code as sfn_generate_agent_code
from app.services.runtime_deployer import (
    upload_code_to_s3,
    create_agent_runtime,
    create_runtime_iam_role,
    wait_for_runtime_ready,
    destroy_runtime,
    sanitize_runtime_name,
)
from app.models import RuntimeConfiguration, ModelConfiguration, ModelProvider

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REGION = os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))
# Resolved lazily inside main() so this module imports cleanly under pytest
# collection even when ARTIFACTS_BUCKET_NAME isn't set (the file is an
# end-to-end driver script, not a unit test).
BUCKET = os.environ.get("ARTIFACTS_BUCKET_NAME", "")
BUNDLE_KEY = "agentcore-deps/strands-mcp.zip"
MODEL_ID = "us.anthropic.claude-sonnet-5"
TEST_PROMPT = "What is 2 + 2? Answer in one sentence."
TIMEOUT_READY = 300  # seconds to wait for READY
RUN_ID = uuid.uuid4().hex[:6]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def download_bundle(s3_client) -> bytes:
    log(f"Downloading deps bundle s3://{BUCKET}/{BUNDLE_KEY} ...")
    resp = s3_client.get_object(Bucket=BUCKET, Key=BUNDLE_KEY)
    data = resp["Body"].read()
    log(f"Bundle downloaded: {len(data):,} bytes")
    return data


def deploy_pattern(
    name: str,
    agent_code: str,
    deps_bundle: bytes,
    connected_tools: list[str],
    s3_client,
    iam_client,
    agentcore_ctrl,
    account_id: str,
) -> dict:
    """Deploy a single pattern and return result dict."""
    runtime_name = sanitize_runtime_name(f"e2e-{RUN_ID}-{name}")
    s3_key = f"e2e-tests/{RUN_ID}/{name}/code.zip"
    result = {
        "pattern": name,
        "runtime_name": runtime_name,
        "runtime_id": None,
        "deploy": "FAIL",
        "ready": "FAIL",
        "invoke": "FAIL",
        "response": None,
        "error": None,
        "cleanup": "SKIP",
    }

    try:
        # Upload code
        log(f"[{name}] Uploading code to s3://{BUCKET}/{s3_key}")
        upload_code_to_s3(
            s3_client,
            BUCKET,
            s3_key,
            agent_code,
            "",
            "agent.py",
            deps_bundle=deps_bundle,
        )

        # Create IAM role
        role_name = f"e2e-{RUN_ID}-{name}-role"
        log(f"[{name}] Creating IAM role: {role_name}")
        role_arn = create_runtime_iam_role(iam_client, role_name, account_id, REGION, connected_tools)
        time.sleep(8)  # IAM propagation

        # Create runtime
        log(f"[{name}] Creating runtime: {runtime_name}")
        env_vars = {"AWS_REGION": REGION, "MODEL_ID": MODEL_ID}
        rt = create_agent_runtime(
            agentcore_ctrl,
            runtime_name,
            role_arn,
            BUCKET,
            s3_key,
            "agent.py",
            "PYTHON_3_13",
            "HTTP",
            env_vars,
        )
        result["runtime_id"] = rt["runtime_id"]
        result["deploy"] = "PASS"
        log(f"[{name}] Runtime created: {rt['runtime_id']}")

        # Wait for READY
        log(f"[{name}] Waiting for READY (up to {TIMEOUT_READY}s)...")
        ready = wait_for_runtime_ready(agentcore_ctrl, rt["runtime_id"], timeout=TIMEOUT_READY)
        if ready.get("success"):
            result["ready"] = "PASS"
            log(f"[{name}] Runtime is READY")
        else:
            result["ready"] = "FAIL"
            result["error"] = ready.get("error", "timeout")
            log(f"[{name}] Runtime NOT ready: {result['error']}")
            return result

        # Invoke
        log(f"[{name}] Invoking runtime...")
        runtime_arn = rt.get("arn", "")
        if not runtime_arn:
            runtime_arn = f"arn:aws:bedrock-agentcore:{REGION}:{account_id}:runtime/{rt['runtime_id']}"

        data_client = boto3.client(
            "bedrock-agentcore",
            region_name=REGION,
            config=boto3.session.Config(read_timeout=30, connect_timeout=10, retries={"max_attempts": 0}),
        )

        payload = json.dumps({"prompt": TEST_PROMPT})
        # Retry invoke up to 3 times (cold start)
        for attempt in range(3):
            try:
                resp = data_client.invoke_agent_runtime(
                    agentRuntimeArn=runtime_arn,
                    payload=payload,
                )
                body = resp.get("response") or resp.get("body", b"")
                if hasattr(body, "read"):
                    body = body.read()
                if isinstance(body, bytes):
                    body = body.decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(body)
                    answer = parsed.get("response", body)
                except (json.JSONDecodeError, TypeError):
                    answer = body

                result["invoke"] = "PASS"
                result["response"] = str(answer)[:200]
                log(f"[{name}] Invoke SUCCESS: {result['response'][:80]}...")
                break
            except Exception as inv_err:
                if attempt < 2:
                    log(f"[{name}] Invoke attempt {attempt + 1} failed (cold start?), retrying in 10s...")
                    time.sleep(10)
                else:
                    result["error"] = str(inv_err)[:200]
                    log(f"[{name}] Invoke FAILED after 3 attempts: {result['error']}")

    except Exception as e:
        result["error"] = str(e)[:300]
        log(f"[{name}] ERROR: {result['error']}")
        traceback.print_exc()

    return result


def cleanup_runtime(agentcore_ctrl, iam_client, result: dict):
    """Delete runtime and IAM role."""
    name = result["pattern"]
    rid = result["runtime_id"]
    if rid:
        try:
            log(f"[{name}] Deleting runtime {rid}...")
            destroy_runtime(rid, REGION)
            result["cleanup"] = "PASS"
            log(f"[{name}] Runtime deleted")
        except Exception as e:
            result["cleanup"] = f"FAIL: {e}"
            log(f"[{name}] Cleanup failed: {e}")

    # Delete IAM role
    role_name = f"e2e-{RUN_ID}-{name}-role"
    try:
        # Delete inline policies first
        policies = iam_client.list_role_policies(RoleName=role_name).get("PolicyNames", [])
        for pol in policies:
            iam_client.delete_role_policy(RoleName=role_name, PolicyName=pol)
        # Detach managed policies
        attached = iam_client.list_attached_role_policies(RoleName=role_name).get("AttachedPolicies", [])
        for pol in attached:
            iam_client.detach_role_policy(RoleName=role_name, PolicyArn=pol["PolicyArn"])
        iam_client.delete_role(RoleName=role_name)
        log(f"[{name}] IAM role deleted")
    except Exception:
        pass  # Best effort


# ---------------------------------------------------------------------------
# Pattern Definitions
# ---------------------------------------------------------------------------


def build_patterns() -> list[dict]:
    """Build all test patterns with their generated code."""
    model = ModelConfiguration(
        provider=ModelProvider.ANTHROPIC,
        model_id=MODEL_ID,
        temperature=0.7,
        top_p=0.9,
    )
    rc = RuntimeConfiguration(
        name="test-agent",
        entrypoint="agent.py",
        framework="strands_agents",
        model=model,
        system_prompt="You are a helpful assistant. Answer questions concisely.",
    )

    patterns = []

    # --- Pattern 1: Standalone Runtime ---
    patterns.append(
        {
            "name": "standalone",
            "code": generate_unified_agent_code(rc, connected_tools=[], region=REGION),
            "connected_tools": [],
            "description": "Runtime only, no components",
        }
    )

    # --- Pattern 2: Runtime + Code Interpreter ---
    patterns.append(
        {
            "name": "code-interpreter",
            "code": generate_unified_agent_code(rc, connected_tools=["code_interpreter"], region=REGION),
            "connected_tools": ["code_interpreter"],
            "description": "Runtime + Code Interpreter (@tool execute_python)",
        }
    )

    # --- Pattern 3: Runtime + Browser ---
    patterns.append(
        {
            "name": "browser",
            "code": generate_unified_agent_code(rc, connected_tools=["browser"], region=REGION),
            "connected_tools": ["browser"],
            "description": "Runtime + Browser (@tool browse_web)",
        }
    )

    # --- Pattern 4: Runtime + Code Interpreter + Browser ---
    patterns.append(
        {
            "name": "ci-browser",
            "code": generate_unified_agent_code(
                rc,
                connected_tools=["code_interpreter", "browser"],
                region=REGION,
            ),
            "connected_tools": ["code_interpreter", "browser"],
            "description": "Runtime + Code Interpreter + Browser",
        }
    )

    # --- Pattern 5: Step Functions default (no template) ---
    from app.models.deployment_models import RuntimeConfig as SfnConfig

    sfn_cfg = SfnConfig(
        name="test",
        framework="strands_agents",
        model={"modelId": MODEL_ID, "provider": "bedrock"},
        systemPrompt="You are a helpful assistant. Answer questions concisely.",
    )
    patterns.append(
        {
            "name": "sfn-default",
            "code": sfn_generate_agent_code(config=sfn_cfg),
            "connected_tools": [],
            "description": "Step Functions path: default Strands agent",
        }
    )

    # --- Pattern 6: Step Functions web-search-agent template ---
    # This uses boto3 Converse API (no strands), needs base.zip bundle
    # We'll skip this one since it needs a different bundle
    # patterns.append({...})

    # --- Pattern 7: Step Functions MCP Server Runtime template ---
    patterns.append(
        {
            "name": "sfn-mcp-server",
            "code": sfn_generate_agent_code(config=sfn_cfg, template_id="mcp-server-runtime"),
            "connected_tools": [],
            "description": "Step Functions path: MCP Server Runtime template",
        }
    )

    return patterns


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if not BUCKET:
        raise RuntimeError(
            "ARTIFACTS_BUCKET_NAME env var must be set to the deployed "
            "artifacts bucket (e.g. agentcore-workflow-dev-artifacts-<account>)."
        )
    log(f"=== E2E Live Invocation Test (run={RUN_ID}, region={REGION}) ===")
    log(f"Bucket: {BUCKET}")
    log(f"Model: {MODEL_ID}")
    log(f"Prompt: {TEST_PROMPT}")
    log("")

    # Init clients
    s3_client = boto3.client("s3", region_name=REGION)
    iam_client = boto3.client("iam")
    agentcore_ctrl = boto3.client("bedrock-agentcore-control", region_name=REGION)
    account_id = boto3.client("sts").get_caller_identity()["Account"]

    # Download bundle once
    deps_bundle = download_bundle(s3_client)

    # Build patterns
    patterns = build_patterns()
    log(f"\nTesting {len(patterns)} patterns...\n")

    results = []
    for p in patterns:
        log(f"{'=' * 60}")
        log(f"PATTERN: {p['name']} — {p['description']}")
        log(f"{'=' * 60}")
        result = deploy_pattern(
            name=p["name"],
            agent_code=p["code"],
            deps_bundle=deps_bundle,
            connected_tools=p["connected_tools"],
            s3_client=s3_client,
            iam_client=iam_client,
            agentcore_ctrl=agentcore_ctrl,
            account_id=account_id,
        )
        results.append(result)
        log("")

    # Cleanup all
    log(f"\n{'=' * 60}")
    log("CLEANUP")
    log(f"{'=' * 60}")
    for result in results:
        cleanup_runtime(agentcore_ctrl, iam_client, result)

    # Print summary table
    log(f"\n{'=' * 80}")
    log("RESULTS SUMMARY")
    log(f"{'=' * 80}")
    header = f"{'Pattern':<20} {'Deploy':<8} {'Ready':<8} {'Invoke':<8} {'Cleanup':<8} {'Response/Error'}"
    log(header)
    log("-" * 80)

    total = len(results)
    passed = 0
    for r in results:
        status = r["response"][:40] if r["response"] else (r["error"][:40] if r["error"] else "—")
        log(f"{r['pattern']:<20} {r['deploy']:<8} {r['ready']:<8} {r['invoke']:<8} {r['cleanup']:<8} {status}")
        if r["invoke"] == "PASS":
            passed += 1

    log(f"\n{'=' * 80}")
    log(f"TOTAL: {passed}/{total} patterns invoked successfully")
    log(f"{'=' * 80}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
