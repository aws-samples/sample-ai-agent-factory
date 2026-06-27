"""Live E2E test for Gateway and Memory patterns.

These patterns require additional AWS resources (Gateway, Cognito, Memory).

Run:
    cd backend
    PYTHONPATH=src APP_AWS_REGION=us-east-1 python tests/test_e2e_gateway_memory.py
"""

import json
import os
import re
import sys
import time
import traceback
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import boto3

from app.services.deployment import generate_unified_agent_code
from app.services.gateway_deployer import deploy_gateway
from app.services.runtime_deployer import (
    upload_code_to_s3,
    create_agent_runtime,
    create_runtime_iam_role,
    wait_for_runtime_ready,
    destroy_runtime,
    sanitize_runtime_name,
)
from app.models import RuntimeConfiguration, ModelConfiguration, ModelProvider

REGION = os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))
# Resolved lazily inside main() so this module imports cleanly under pytest
# collection even when ARTIFACTS_BUCKET_NAME isn't set (the file is an
# end-to-end driver script, not a unit test).
BUCKET = os.environ.get("ARTIFACTS_BUCKET_NAME", "")
BUNDLE_KEY = "agentcore-deps/strands-mcp.zip"
MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
RUN_ID = uuid.uuid4().hex[:6]


def log(msg):
    # SECURITY (CodeQL py/clear-text-logging-sensitive-data): this e2e harness
    # handles live Cognito client_secrets. Redact anything that looks like a
    # secret/token before it reaches stdout. Rebuilding the string here also
    # severs the taint flow from any secret-bearing local the caller passes.
    safe = re.sub(
        r"(?i)(secret|password|token|api[_-]?key)\s*[=:]\s*\S+",
        r"\1=***REDACTED***",
        str(msg),
    )
    print(f"[{time.strftime('%H:%M:%S')}] {safe}", flush=True)


def main():
    if not BUCKET:
        raise RuntimeError(
            "ARTIFACTS_BUCKET_NAME env var must be set to the deployed "
            "artifacts bucket (e.g. agentcore-workflow-dev-artifacts-<account>)."
        )
    log(f"=== E2E Gateway + Memory Test (run={RUN_ID}, region={REGION}) ===")

    s3_client = boto3.client("s3", region_name=REGION)
    iam_client = boto3.client("iam")
    agentcore_ctrl = boto3.client("bedrock-agentcore-control", region_name=REGION)
    account_id = boto3.client("sts").get_caller_identity()["Account"]

    # Download bundle
    log("Downloading deps bundle...")
    resp = s3_client.get_object(Bucket=BUCKET, Key=BUNDLE_KEY)
    deps_bundle = resp["Body"].read()
    log(f"Bundle: {len(deps_bundle):,} bytes")

    model = ModelConfiguration(provider=ModelProvider.ANTHROPIC, model_id=MODEL_ID, temperature=0.7, top_p=0.9)
    rc = RuntimeConfiguration(
        name="test-agent",
        entrypoint="agent.py",
        framework="strands_agents",
        model=model,
        system_prompt="You are a helpful assistant. Answer concisely.",
    )

    results = []
    gateway_result = None
    memory_id = None

    # ===================================================================
    # Pattern: Runtime + Gateway (weather tool)
    # ===================================================================
    try:
        log("\n" + "=" * 60)
        log("PATTERN: gateway — Runtime + Gateway + Weather Tool")
        log("=" * 60)

        gw_name = f"e2e-{RUN_ID}-gw"
        log(f"Deploying gateway: {gw_name}")
        gateway_result = deploy_gateway(
            gateway_config={"name": gw_name},
            region=REGION,
            template_id=None,
            gateway_tools=["weather_api"],
            identity_config=None,
            custom_tools=[],
        )

        if not gateway_result.get("success"):
            raise RuntimeError(f"Gateway deploy failed: {gateway_result.get('error')}")

        # Log only the (public) gateway id — not gateway_result, which nests
        # client_info.client_secret. Referencing the secret-bearing dict in a log
        # call trips py/clear-text-logging-sensitive-data's taint heuristic even
        # though only a url is interpolated; pull a non-secret scalar out first.
        _gw_id = gateway_result.get("gateway_id", "?")
        log(f"Gateway deployed: id={_gw_id}")

        # Generate gateway agent code
        code = generate_unified_agent_code(
            rc,
            connected_tools=["gateway"],
            gateway_result=gateway_result,
            region=REGION,
        )

        # Deploy runtime
        runtime_name = sanitize_runtime_name(f"e2e-{RUN_ID}-gateway")
        s3_key = f"e2e-tests/{RUN_ID}/gateway/code.zip"
        upload_code_to_s3(s3_client, BUCKET, s3_key, code, "", "agent.py", deps_bundle=deps_bundle)

        role_name = f"e2e-{RUN_ID}-gateway-role"
        role_arn = create_runtime_iam_role(iam_client, role_name, account_id, REGION, ["gateway"])
        time.sleep(8)

        client_info = gateway_result.get("client_info", {})
        env_vars = {
            "AWS_REGION": REGION,
            "MODEL_ID": MODEL_ID,
            "GATEWAY_URL": gateway_result.get("gateway_url", ""),
            "COGNITO_CLIENT_ID": client_info.get("client_id", ""),
            "COGNITO_CLIENT_SECRET": client_info.get("client_secret", ""),
            "COGNITO_TOKEN_ENDPOINT": client_info.get("token_endpoint", ""),
            "COGNITO_SCOPE": client_info.get("scope", ""),
        }

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
        log(f"Runtime created: {rt['runtime_id']}")

        ready = wait_for_runtime_ready(agentcore_ctrl, rt["runtime_id"], timeout=300)
        if not ready.get("success"):
            raise RuntimeError(f"Not ready: {ready.get('error')}")
        log("Runtime READY")

        # Invoke with weather prompt
        runtime_arn = rt.get("arn") or f"arn:aws:bedrock-agentcore:{REGION}:{account_id}:runtime/{rt['runtime_id']}"
        data_client = boto3.client(
            "bedrock-agentcore",
            region_name=REGION,
            config=boto3.session.Config(read_timeout=60, connect_timeout=10, retries={"max_attempts": 0}),
        )

        invoke_ok = False
        answer = ""
        for attempt in range(3):
            try:
                resp = data_client.invoke_agent_runtime(
                    agentRuntimeArn=runtime_arn,
                    payload=json.dumps({"prompt": "What is the weather in Paris right now? Be brief."}),
                )
                body = resp.get("response") or resp.get("body", b"")
                if hasattr(body, "read"):
                    body = body.read()
                if isinstance(body, bytes):
                    body = body.decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(body)
                    answer = parsed.get("response", body)
                except Exception:
                    answer = body
                invoke_ok = True
                log(f"Invoke SUCCESS: {str(answer)[:120]}")
                break
            except Exception as e:
                if attempt < 2:
                    log(f"Invoke attempt {attempt + 1} failed, retrying in 15s...")
                    time.sleep(15)
                else:
                    answer = str(e)[:200]
                    log(f"Invoke FAILED: {answer}")

        results.append(
            {
                "pattern": "gateway",
                "deploy": "PASS",
                "ready": "PASS",
                "invoke": "PASS" if invoke_ok else "FAIL",
                "response": str(answer)[:200],
                "runtime_id": rt["runtime_id"],
                "role_name": role_name,
            }
        )

    except Exception as e:
        log(f"Gateway pattern ERROR: {e}")
        traceback.print_exc()
        results.append(
            {
                "pattern": "gateway",
                "deploy": "FAIL",
                "ready": "FAIL",
                "invoke": "FAIL",
                "response": str(e)[:200],
                "runtime_id": None,
                "role_name": f"e2e-{RUN_ID}-gateway-role",
            }
        )

    # ===================================================================
    # Pattern: Runtime + Memory
    # ===================================================================
    try:
        log("\n" + "=" * 60)
        log("PATTERN: memory — Runtime + Memory")
        log("=" * 60)

        mem_name = f"e2e_{RUN_ID}_mem"
        log(f"Creating memory: {mem_name}")
        try:
            # Create IAM role for memory
            memory_role_name = f"AgentCoreMemory-{mem_name}"
            trust_policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            }
            try:
                role_resp = iam_client.create_role(
                    RoleName=memory_role_name,
                    AssumeRolePolicyDocument=json.dumps(trust_policy),
                    Description=f"Memory execution role for {mem_name}",
                )
                memory_role_arn = role_resp["Role"]["Arn"]
                iam_client.put_role_policy(
                    RoleName=memory_role_name,
                    PolicyName="MemoryExecutionPolicy",
                    PolicyDocument=json.dumps(
                        {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Effect": "Allow",
                                    "Action": [
                                        "bedrock:InvokeModel",
                                        "bedrock:InvokeModelWithResponseStream",
                                        "bedrock-agentcore:*",
                                        "bedrock-agentcore-control:*",
                                    ],
                                    "Resource": "*",
                                }
                            ],
                        }
                    ),
                )
                time.sleep(10)  # IAM propagation
            except iam_client.exceptions.EntityAlreadyExistsException:
                memory_role_arn = iam_client.get_role(RoleName=memory_role_name)["Role"]["Arn"]

            mem_resp = agentcore_ctrl.create_memory(
                clientToken=str(uuid.uuid4()),
                name=mem_name,
                description="E2E test memory",
                memoryExecutionRoleArn=memory_role_arn,
                eventExpiryDuration=90,
            )
            memory_id = mem_resp.get("memoryId") or mem_resp.get("memory", {}).get("memoryId")
            log(f"Memory created: {memory_id}")

            # Wait for memory to become ACTIVE
            for _ in range(24):
                try:
                    mem_status = agentcore_ctrl.get_memory(memoryId=memory_id)
                    status = mem_status.get("status", "")
                    if status in ("ACTIVE", "READY"):
                        log(f"Memory is {status}")
                        break
                    if "FAILED" in status:
                        log(f"Memory entered {status}")
                        break
                except Exception:
                    pass
                time.sleep(5)

        except Exception as mem_err:
            log(f"Memory creation failed: {mem_err}")
            traceback.print_exc()
            memory_id = None
            raise RuntimeError(f"Memory creation failed: {mem_err}")

        code = generate_unified_agent_code(
            rc,
            connected_tools=["memory"],
            memory_id=memory_id,
            region=REGION,
        )

        runtime_name = sanitize_runtime_name(f"e2e-{RUN_ID}-memory")
        s3_key = f"e2e-tests/{RUN_ID}/memory/code.zip"
        upload_code_to_s3(s3_client, BUCKET, s3_key, code, "", "agent.py", deps_bundle=deps_bundle)

        role_name = f"e2e-{RUN_ID}-memory-role"
        role_arn = create_runtime_iam_role(iam_client, role_name, account_id, REGION, ["memory"])
        time.sleep(8)

        env_vars = {
            "AWS_REGION": REGION,
            "MODEL_ID": MODEL_ID,
            "MEMORY_ID": memory_id or "",
        }
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
        log(f"Runtime created: {rt['runtime_id']}")

        ready = wait_for_runtime_ready(agentcore_ctrl, rt["runtime_id"], timeout=300)
        if not ready.get("success"):
            raise RuntimeError(f"Not ready: {ready.get('error')}")
        log("Runtime READY")

        runtime_arn = rt.get("arn") or f"arn:aws:bedrock-agentcore:{REGION}:{account_id}:runtime/{rt['runtime_id']}"
        data_client = boto3.client(
            "bedrock-agentcore",
            region_name=REGION,
            config=boto3.session.Config(read_timeout=30, connect_timeout=10, retries={"max_attempts": 0}),
        )

        invoke_ok = False
        answer = ""
        for attempt in range(3):
            try:
                resp = data_client.invoke_agent_runtime(
                    agentRuntimeArn=runtime_arn,
                    payload=json.dumps({"prompt": "What is 2 + 2? Answer in one sentence."}),
                )
                body = resp.get("response") or resp.get("body", b"")
                if hasattr(body, "read"):
                    body = body.read()
                if isinstance(body, bytes):
                    body = body.decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(body)
                    answer = parsed.get("response", body)
                except Exception:
                    answer = body
                invoke_ok = True
                log(f"Invoke SUCCESS: {str(answer)[:120]}")
                break
            except Exception as e:
                if attempt < 2:
                    log(f"Invoke attempt {attempt + 1} failed, retrying in 10s...")
                    time.sleep(10)
                else:
                    answer = str(e)[:200]
                    log(f"Invoke FAILED: {answer}")

        results.append(
            {
                "pattern": "memory",
                "deploy": "PASS",
                "ready": "PASS",
                "invoke": "PASS" if invoke_ok else "FAIL",
                "response": str(answer)[:200],
                "runtime_id": rt["runtime_id"],
                "role_name": role_name,
            }
        )

    except Exception as e:
        log(f"Memory pattern ERROR: {e}")
        traceback.print_exc()
        results.append(
            {
                "pattern": "memory",
                "deploy": "FAIL",
                "ready": "FAIL",
                "invoke": "FAIL",
                "response": str(e)[:200],
                "runtime_id": None,
                "role_name": f"e2e-{RUN_ID}-memory-role",
            }
        )

    # ===================================================================
    # Pattern: Runtime + Gateway + Memory
    # ===================================================================
    try:
        log("\n" + "=" * 60)
        log("PATTERN: gateway+memory — Runtime + Gateway + Memory")
        log("=" * 60)

        if not gateway_result or not gateway_result.get("success"):
            raise RuntimeError("Skipping: gateway not available from previous step")

        code = generate_unified_agent_code(
            rc,
            connected_tools=["gateway", "memory"],
            gateway_result=gateway_result,
            memory_id=memory_id,
            region=REGION,
        )

        runtime_name = sanitize_runtime_name(f"e2e-{RUN_ID}-gw-mem")
        s3_key = f"e2e-tests/{RUN_ID}/gw-mem/code.zip"
        upload_code_to_s3(s3_client, BUCKET, s3_key, code, "", "agent.py", deps_bundle=deps_bundle)

        role_name = f"e2e-{RUN_ID}-gw-mem-role"
        role_arn = create_runtime_iam_role(iam_client, role_name, account_id, REGION, ["gateway", "memory"])
        time.sleep(8)

        client_info = gateway_result.get("client_info", {})
        env_vars = {
            "AWS_REGION": REGION,
            "MODEL_ID": MODEL_ID,
            "GATEWAY_URL": gateway_result.get("gateway_url", ""),
            "COGNITO_CLIENT_ID": client_info.get("client_id", ""),
            "COGNITO_CLIENT_SECRET": client_info.get("client_secret", ""),
            "COGNITO_TOKEN_ENDPOINT": client_info.get("token_endpoint", ""),
            "COGNITO_SCOPE": client_info.get("scope", ""),
            "MEMORY_ID": memory_id or "",
        }

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
        log(f"Runtime created: {rt['runtime_id']}")

        ready = wait_for_runtime_ready(agentcore_ctrl, rt["runtime_id"], timeout=300)
        if not ready.get("success"):
            raise RuntimeError(f"Not ready: {ready.get('error')}")
        log("Runtime READY")

        runtime_arn = rt.get("arn") or f"arn:aws:bedrock-agentcore:{REGION}:{account_id}:runtime/{rt['runtime_id']}"
        data_client = boto3.client(
            "bedrock-agentcore",
            region_name=REGION,
            config=boto3.session.Config(read_timeout=60, connect_timeout=10, retries={"max_attempts": 0}),
        )

        invoke_ok = False
        answer = ""
        for attempt in range(3):
            try:
                resp = data_client.invoke_agent_runtime(
                    agentRuntimeArn=runtime_arn,
                    payload=json.dumps({"prompt": "What is the weather in Tokyo? Be brief."}),
                )
                body = resp.get("response") or resp.get("body", b"")
                if hasattr(body, "read"):
                    body = body.read()
                if isinstance(body, bytes):
                    body = body.decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(body)
                    answer = parsed.get("response", body)
                except Exception:
                    answer = body
                invoke_ok = True
                log(f"Invoke SUCCESS: {str(answer)[:120]}")
                break
            except Exception as e:
                if attempt < 2:
                    log(f"Invoke attempt {attempt + 1} failed, retrying in 15s...")
                    time.sleep(15)
                else:
                    answer = str(e)[:200]
                    log(f"Invoke FAILED: {answer}")

        results.append(
            {
                "pattern": "gateway+memory",
                "deploy": "PASS",
                "ready": "PASS",
                "invoke": "PASS" if invoke_ok else "FAIL",
                "response": str(answer)[:200],
                "runtime_id": rt["runtime_id"],
                "role_name": role_name,
            }
        )

    except Exception as e:
        log(f"Gateway+Memory pattern ERROR: {e}")
        traceback.print_exc()
        results.append(
            {
                "pattern": "gateway+memory",
                "deploy": "FAIL",
                "ready": "FAIL",
                "invoke": "FAIL",
                "response": str(e)[:200],
                "runtime_id": None,
                "role_name": f"e2e-{RUN_ID}-gw-mem-role",
            }
        )

    # ===================================================================
    # Cleanup
    # ===================================================================
    log("\n" + "=" * 60)
    log("CLEANUP")
    log("=" * 60)

    for r in results:
        if r.get("runtime_id"):
            try:
                destroy_runtime(r["runtime_id"], REGION)
                log(f"[{r['pattern']}] Runtime deleted")
            except Exception as e:
                log(f"[{r['pattern']}] Runtime cleanup: {e}")
        rn = r.get("role_name")
        if rn:
            try:
                pols = iam_client.list_role_policies(RoleName=rn).get("PolicyNames", [])
                for p in pols:
                    iam_client.delete_role_policy(RoleName=rn, PolicyName=p)
                attached = iam_client.list_attached_role_policies(RoleName=rn).get("AttachedPolicies", [])
                for p in attached:
                    iam_client.detach_role_policy(RoleName=rn, PolicyArn=p["PolicyArn"])
                iam_client.delete_role(RoleName=rn)
                log(f"[{r['pattern']}] IAM role deleted")
            except Exception:
                pass

    # Cleanup gateway
    if gateway_result and gateway_result.get("gateway_id"):
        try:
            gw_id = gateway_result["gateway_id"]
            targets = agentcore_ctrl.list_gateway_targets(gatewayIdentifier=gw_id)
            for t in targets.get(
                "items",
                targets.get("targets", targets.get("gatewayTargetSummaries", [])),
            ):
                tid = t.get("targetId") or t.get("gatewayTargetId") or t.get("name")
                if tid:
                    agentcore_ctrl.delete_gateway_target(gatewayIdentifier=gw_id, targetId=tid)
                    log(f"Gateway target {tid} deleted")
            time.sleep(3)  # Wait for target deletion to propagate
            agentcore_ctrl.delete_gateway(gatewayIdentifier=gw_id)
            log("Gateway deleted")
        except Exception as e:
            log(f"Gateway cleanup: {e}")

    # Cleanup cognito
    if gateway_result and gateway_result.get("user_pool_id"):
        try:
            cognito = boto3.client("cognito-idp", region_name=REGION)
            cognito.delete_user_pool(UserPoolId=gateway_result["user_pool_id"])
            log("Cognito user pool deleted")
        except Exception as e:
            log(f"Cognito cleanup: {e}")

    # Cleanup Lambda
    if gateway_result and gateway_result.get("lambda_arn"):
        try:
            lam = boto3.client("lambda", region_name=REGION)
            lam.delete_function(FunctionName=gateway_result["lambda_arn"])
            log("Tools Lambda deleted")
        except Exception as e:
            log(f"Lambda cleanup: {e}")

    # Cleanup memory
    if memory_id:
        try:
            agentcore_ctrl.delete_memory(memoryId=memory_id)
            log("Memory deleted")
        except Exception as e:
            log(f"Memory cleanup: {e}")

    # Cleanup memory IAM role
    mem_role_name = f"AgentCoreMemory-e2e_{RUN_ID}_mem"
    try:
        pols = iam_client.list_role_policies(RoleName=mem_role_name).get("PolicyNames", [])
        for p in pols:
            iam_client.delete_role_policy(RoleName=mem_role_name, PolicyName=p)
        iam_client.delete_role(RoleName=mem_role_name)
        log("Memory IAM role deleted")
    except Exception:
        pass

    # Print summary
    log(f"\n{'=' * 80}")
    log("RESULTS SUMMARY")
    log(f"{'=' * 80}")
    log(f"{'Pattern':<20} {'Deploy':<8} {'Ready':<8} {'Invoke':<8} {'Response/Error'}")
    log("-" * 80)
    passed = 0
    for r in results:
        resp = r["response"][:50] if r["response"] else "—"
        log(f"{r['pattern']:<20} {r['deploy']:<8} {r['ready']:<8} {r['invoke']:<8} {resp}")
        if r["invoke"] == "PASS":
            passed += 1
    log(f"\nTOTAL: {passed}/{len(results)} patterns invoked successfully")
    log(f"{'=' * 80}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
