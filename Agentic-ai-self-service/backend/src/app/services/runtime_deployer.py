"""Runtime deployment operations for AgentCore.

Uses pure boto3 APIs — no CLI dependencies.
Handles runtime creation, code upload to S3, IAM role creation,
and runtime lifecycle management.

Requirements: 5.4
"""

import io
import json
import logging
import os
import re
import time
import zipfile
from typing import Optional

import boto3

logger = logging.getLogger(__name__)

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07")


def _strip_ansi(text: str) -> str:
    """Strip ANSI escape codes from CLI output."""
    return _ANSI_ESCAPE.sub("", text)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sanitize_runtime_name(name: str) -> str:
    """Sanitize a name for agentcore requirements.

    Rules: starts with a letter, only letters/numbers/underscores, max 48 chars.

    Thin wrapper over the shared ``naming.sanitize_agentcore_name`` (underscore
    style) — kept as a named function because step handlers/tests import it.
    """
    from app.services.naming import sanitize_agentcore_name

    return sanitize_agentcore_name(
        name, style="underscore", prefix="agent", fallback="agent_default"
    )


def _merge_deps_into_zip(target_zf: zipfile.ZipFile, bundle_bytes: bytes) -> None:
    """Extract dependency bundle contents into the target zip, excluding cache files.

    Reads *bundle_bytes* as an in-memory zip and copies every entry into
    *target_zf* **except** paths that contain ``__pycache__`` or end with
    ``.pyc``.  This keeps the final code zip free of stale bytecode that
    could conflict with the AgentCore Runtime's Python version.

    Requirements: 4.4, 4.5, 5.5
    """
    with zipfile.ZipFile(io.BytesIO(bundle_bytes), "r") as bundle_zf:
        for item in bundle_zf.namelist():
            if "__pycache__" in item or item.endswith(".pyc"):
                continue
            data = bundle_zf.read(item)
            target_zf.writestr(item, data)


def _create_code_zip(
    agent_code: str,
    requirements_txt: str,
    entrypoint: str,
    deps_bundle: Optional[bytes] = None,
) -> bytes:
    """Create in-memory zip with agent code and optionally bundled deps.

    If *deps_bundle* is provided its contents are merged into the zip root
    via ``_merge_deps_into_zip``, giving the AgentCore Runtime all
    dependencies without a ``pip install`` phase.

    ``requirements.txt`` is only written when *requirements_txt* contains
    non-whitespace content.

    Requirements: 5.1, 5.2, 5.3, 5.4
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(entrypoint, agent_code)
        if requirements_txt.strip():
            zf.writestr("requirements.txt", requirements_txt)
        if deps_bundle:
            _merge_deps_into_zip(zf, deps_bundle)
    buf.seek(0)
    return buf.read()


def upload_code_to_s3(
    s3_client,
    bucket: str,
    key: str,
    agent_code: str,
    requirements_txt: str,
    entrypoint: str = "agent.py",
    deps_bundle: Optional[bytes] = None,
) -> str:
    """Upload agent code zip to S3, optionally with bundled dependencies.

    Returns the S3 URI.
    """
    zip_bytes = _create_code_zip(agent_code, requirements_txt, entrypoint, deps_bundle)
    s3_client.put_object(Bucket=bucket, Key=key, Body=zip_bytes)
    logger.info("Uploaded code to s3://%s/%s (%d bytes)", bucket, key, len(zip_bytes))
    return f"s3://{bucket}/{key}"


def create_runtime_iam_role(
    iam_client,
    role_name: str,
    account_id: str,
    region: str,
    connected_tools: Optional[list] = None,
    otel_secret_arn: Optional[str] = None,
    resource_tags: Optional[dict] = None,
) -> str:
    """Create or reuse an IAM execution role for an AgentCore runtime.

    ``resource_tags`` (Phase 2 governance tagging) are merged onto the role
    alongside the mandatory ManagedBy tag. Returns the role ARN.
    """
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

    # Bug 139: tag every runtime exec role ManagedBy=agentcore-flows so the
    # delete-path IAM grant can be scoped by aws:ResourceTag instead of a broad
    # role/*-role wildcard that would match unrelated account roles.
    # Phase 2 (Loom): merge the resolved governance tags (owner/application/
    # cost-center/…) so cost attribution + ABAC work off real AWS resource tags.
    # IAM keys/values must be strings; the ManagedBy tag is always last so it
    # can't be overridden by a caller-supplied governance tag of the same key.
    _managed_tag = [
        {"Key": str(k), "Value": str(v)}
        for k, v in (resource_tags or {}).items()
        if k and k != "ManagedBy"
    ]
    _managed_tag.append({"Key": "ManagedBy", "Value": "agentcore-flows"})
    try:
        resp = iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description=f"Execution role for AgentCore runtime {role_name}",
            Tags=_managed_tag,
        )
        role_arn = resp["Role"]["Arn"]
        logger.info("Created runtime IAM role: %s", role_arn)
    except iam_client.exceptions.EntityAlreadyExistsException:
        role_arn = iam_client.get_role(RoleName=role_name)["Role"]["Arn"]
        logger.info("Reusing existing runtime IAM role: %s", role_arn)
        # Ensure the tag is present on reused roles too (idempotent).
        try:
            iam_client.tag_role(RoleName=role_name, Tags=_managed_tag)
        except Exception as _tag_err:  # noqa: BLE001
            logger.warning("Could not tag reused role %s: %s", role_name, _tag_err)

    # Attach core permissions
    # SECURITY: Scope S3 access to the specific artifacts bucket rather than "*".
    # Bedrock model access uses "*" because model ARNs are dynamic and vary
    # by region/account. CloudWatch Logs uses "*" as log group ARNs are
    # created dynamically by the runtime.
    artifacts_bucket = os.environ.get("ARTIFACTS_BUCKET_NAME", "")
    s3_resources = (
        [
            f"arn:aws:s3:::{artifacts_bucket}",
            f"arn:aws:s3:::{artifacts_bucket}/*",
        ]
        if artifacts_bucket
        else ["*"]
    )  # Fallback to wildcard only if bucket name unavailable

    core_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "BedrockModelAccess",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                "Resource": "*",
            },
            {
                "Sid": "S3CodeAccess",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:ListBucket"],
                "Resource": s3_resources,
            },
            {
                "Sid": "CloudWatchLogs",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": "*",
            },
        ],
    }

    # Add tool-specific permissions
    tools = connected_tools or []
    for tool in tools:
        if tool == "gateway":
            core_policy["Statement"].append(
                {
                    "Sid": "GatewayAccess",
                    "Effect": "Allow",
                    "Action": [
                        "bedrock-agentcore:InvokeGateway",
                        "bedrock-agentcore:ListGateways",
                        "bedrock-agentcore:GetGateway",
                    ],
                    "Resource": "*",
                }
            )
        elif tool == "browser":
            core_policy["Statement"].append(
                {
                    "Sid": "BrowserAccess",
                    "Effect": "Allow",
                    "Action": ["bedrock-agentcore:*Browser*"],
                    "Resource": "*",
                }
            )
        elif tool == "code_interpreter":
            core_policy["Statement"].append(
                {
                    "Sid": "CodeInterpreterAccess",
                    "Effect": "Allow",
                    "Action": ["bedrock-agentcore:*CodeInterpreter*"],
                    "Resource": "*",
                }
            )
        elif tool == "guardrails":
            core_policy["Statement"].append(
                {
                    "Sid": "GuardrailsAccess",
                    "Effect": "Allow",
                    "Action": ["bedrock:ApplyGuardrail", "bedrock:GetGuardrail"],
                    "Resource": "*",
                }
            )
        elif tool == "memory":
            core_policy["Statement"].append(
                {
                    "Sid": "MemoryAccess",
                    "Effect": "Allow",
                    "Action": [
                        "bedrock-agentcore:*Memory*",
                        "bedrock-agentcore:CreateEvent",
                        "bedrock-agentcore:GetLastKTurns",
                        "bedrock-agentcore:RetrieveMemories",
                        "bedrock-agentcore:ListSessions",
                        "bedrock-agentcore:ListActors",
                        "bedrock-agentcore:ListEvents",
                        "bedrock-agentcore-control:GetMemory",
                        "bedrock-agentcore-control:ListMemories",
                    ],
                    "Resource": "*",
                }
            )
        elif tool in ("evaluation", "observability"):
            core_policy["Statement"].append(
                {
                    "Sid": "EvaluationAccess",
                    "Effect": "Allow",
                    "Action": [
                        "bedrock-agentcore:Evaluate",
                        "bedrock-agentcore-control:CreateOnlineEvaluationConfig",
                        "bedrock-agentcore-control:GetOnlineEvaluationConfig",
                        "bedrock-agentcore-control:ListOnlineEvaluationConfigs",
                        "bedrock-agentcore-control:ListEvaluators",
                        "bedrock-agentcore-control:GetEvaluator",
                        "logs:StartQuery",
                        "logs:GetQueryResults",
                    ],
                    "Resource": "*",
                }
            )
        elif tool == "policy":
            core_policy["Statement"].append(
                {
                    "Sid": "PolicyAccess",
                    "Effect": "Allow",
                    "Action": [
                        "bedrock-agentcore-control:CreatePolicyEngine",
                        "bedrock-agentcore-control:GetPolicyEngine",
                        "bedrock-agentcore-control:ListPolicyEngines",
                        "bedrock-agentcore-control:CreatePolicy",
                        "bedrock-agentcore-control:GetPolicy",
                        "bedrock-agentcore-control:ListPolicies",
                        "bedrock-agentcore-control:UpdateGateway",
                    ],
                    "Resource": "*",
                }
            )

    # Scoped Secrets Manager access for OTLP auth header (Langfuse,
    # Honeycomb, etc.). Bug 9 reminder: keep this in sync with the SFN path.
    if otel_secret_arn:
        core_policy["Statement"].append(
            {
                "Sid": "OtelAuthHeaderSecret",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": [otel_secret_arn],
            }
        )

    iam_client.put_role_policy(
        RoleName=role_name,
        PolicyName="AgentCoreRuntimePolicy",
        PolicyDocument=json.dumps(core_policy),
    )

    # Wait for IAM propagation. AgentCore's service-side IAM cache for the
    # role's S3 access check needs ~60s — a shorter sleep caused every fresh
    # deploy to fail with `ValidationException: Access denied when trying to
    # retrieve zip file from S3`. Verified live 2026-05-16. See lessons Bug 52.
    # The downstream create_agent_runtime() also retries on this specific
    # error so we're double-belted; this sleep keeps the happy path one-shot.
    time.sleep(15)
    return role_arn


def _build_network_configuration(vpc_config: Optional[dict]) -> dict:
    """Build the AgentCore networkConfiguration block.

    VPC mode (Loom-study 0.1) when vpc_config carries subnets + security groups;
    PUBLIC otherwise. Accepts both snake_case (our model) and camelCase keys.
    Verified against the live control-plane model: networkModeConfig = VpcConfig
    {subnets, securityGroups}.
    """
    if not vpc_config:
        return {"networkMode": "PUBLIC"}
    subnets = vpc_config.get("subnet_ids") or vpc_config.get("subnets") or []
    sgs = vpc_config.get("security_group_ids") or vpc_config.get("securityGroups") or []
    if not subnets or not sgs:
        # Incomplete VPC config → fail safe to PUBLIC rather than a rejected call.
        return {"networkMode": "PUBLIC"}
    return {
        "networkMode": "VPC",
        "networkModeConfig": {"subnets": list(subnets), "securityGroups": list(sgs)},
    }


def create_agent_runtime(
    agentcore_ctrl,
    runtime_name: str,
    role_arn: str,
    s3_bucket: str,
    s3_key: str,
    entrypoint: str = "agent.py",
    python_runtime: str = "PYTHON_3_13",
    protocol: str = "HTTP",
    env_vars: Optional[dict] = None,
    authorizer_config: Optional[dict] = None,
    vpc_config: Optional[dict] = None,
) -> dict:
    """Create an AgentCore runtime using the boto3 control API.

    ``vpc_config`` (Loom-study 0.1): when supplied ({subnet_ids, security_group_ids})
    the runtime is created in VPC network mode so it can reach VPC-private
    resources. Previously networkMode was HARDCODED to PUBLIC and the modeled
    vpc_config field was read by no deployer (dead config). Falls back to PUBLIC
    when absent.

    Returns dict with runtime_id, arn, status.
    """
    network_configuration = _build_network_configuration(vpc_config)
    create_params = {
        "agentRuntimeName": runtime_name,
        "agentRuntimeArtifact": {
            "codeConfiguration": {
                "code": {
                    "s3": {
                        "bucket": s3_bucket,
                        "prefix": s3_key,
                    }
                },
                "runtime": python_runtime,
                "entryPoint": [entrypoint],
            }
        },
        "roleArn": role_arn,
        "networkConfiguration": network_configuration,
        "protocolConfiguration": {"serverProtocol": protocol},
    }

    if env_vars:
        create_params["environmentVariables"] = env_vars

    if authorizer_config:
        create_params["authorizerConfiguration"] = authorizer_config

    def _create_with_transient_retry():
        """Retry create_agent_runtime on two known transient ValidationExceptions.

        Two distinct failure modes share the same outer exception type:

        1. **S3 region redirect (Bug 63 root cause).** AgentCore's service-side
           S3 client returns `S3 operation failed: Moved Permanently (Status
           Code: 301)` on the FIRST call to a bucket whose region it hasn't
           cached. The 301 response itself warms the cache — a retry within
           seconds succeeds. Verified live 2026-05-18 with a controlled
           diagnostic: identical (role, bucket, key) failed on call 1 with
           301 and succeeded on call 2 ~30s later.

        2. **IAM-propagation race.** Service-side IAM cache for the runtime
           role's S3 read permission can lag the IAM control plane after
           `put_role_policy`. Surfaces as `Access denied when trying to
           retrieve zip file from S3`. Less common now that we pre-create
           the shared role at stack init (Bug 60), but kept as a safety net.

        Budget: 8 × 5s = 40s. The 301 case resolves in <1s; we just need a
        few retry slots. Way under the SFN 240s ceiling.
        """
        retryable_markers = (
            "Access denied when trying to retrieve",  # IAM-propagation race
            "Moved Permanently",                       # S3 region cache miss
            "Status Code: 301",                        # S3 region cache miss
        )
        last_err = None
        attempts = 8
        for attempt in range(attempts):
            try:
                return agentcore_ctrl.create_agent_runtime(**create_params)
            except Exception as e:
                err_str = str(e)
                if "ValidationException" in err_str and any(
                    m in err_str for m in retryable_markers
                ):
                    last_err = e
                    logger.info(
                        "create_agent_runtime transient (attempt %d/%d): %s",
                        attempt + 1, attempts, err_str[:200],
                    )
                    time.sleep(5)
                    continue
                raise
        raise last_err if last_err else RuntimeError("create_agent_runtime failed")

    try:
        resp = _create_with_transient_retry()
    except Exception as e:
        if "ConflictException" in str(e) or "already exists" in str(e):
            # Find existing runtime by paginating through all runtimes
            logger.info("Runtime '%s' already exists, searching to update...", runtime_name)
            found_id = None
            found_arn = ""
            next_token = None
            for _ in range(20):  # max 20 pages
                list_kwargs = {}
                if next_token:
                    list_kwargs["nextToken"] = next_token
                try:
                    existing = agentcore_ctrl.list_agent_runtimes(**list_kwargs)
                except Exception:
                    # Fallback: try with maxResults
                    list_kwargs["maxResults"] = 100
                    existing = agentcore_ctrl.list_agent_runtimes(**list_kwargs)

                runtimes = existing.get("agentRuntimeSummaries", existing.get("agentRuntimes", []))
                for rt in runtimes:
                    if rt.get("agentRuntimeName") == runtime_name:
                        found_id = rt.get("agentRuntimeId", "")
                        found_arn = rt.get("agentRuntimeArn", "")
                        break
                if found_id:
                    break
                next_token = existing.get("nextToken")
                if not next_token:
                    break

            if found_id:
                logger.info("Found existing runtime: %s, updating...", found_id)
                try:
                    update_params = {
                        "agentRuntimeId": found_id,
                        "agentRuntimeArtifact": create_params["agentRuntimeArtifact"],
                        "roleArn": role_arn,
                        "networkConfiguration": create_params["networkConfiguration"],
                        "protocolConfiguration": create_params["protocolConfiguration"],
                    }
                    if env_vars:
                        update_params["environmentVariables"] = env_vars
                    agentcore_ctrl.update_agent_runtime(**update_params)
                except Exception as update_err:
                    logger.warning("Update failed: %s. Returning existing runtime.", update_err)
                return {
                    "runtime_id": found_id,
                    "arn": found_arn,
                    "status": "UPDATING",
                }
            else:
                logger.error("Could not find existing runtime '%s' in list", runtime_name)
                raise
        else:
            raise

    runtime_id = resp.get("agentRuntimeId", "")
    arn = resp.get("agentRuntimeArn", "")
    logger.info("Created runtime: id=%s, arn=%s", runtime_id, arn)

    return {
        "runtime_id": runtime_id,
        "arn": arn,
        "status": resp.get("status", "CREATING"),
    }


def wait_for_runtime_ready(agentcore_ctrl, runtime_id: str, timeout: int = 600) -> dict:
    """Poll until runtime is READY/ACTIVE or timeout."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = agentcore_ctrl.get_agent_runtime(agentRuntimeId=runtime_id)
            status = resp.get("status", "")
            logger.info("Runtime %s status: %s", runtime_id, status)

            if status in ("READY", "ACTIVE"):
                return {
                    "success": True,
                    "runtime_id": runtime_id,
                    "arn": resp.get("agentRuntimeArn", ""),
                    "status": status,
                }
            if "FAILED" in status:
                # Surface AgentCore's own reason — CREATE_FAILED alone is
                # undiagnosable. The field name varies across API versions.
                reason = (
                    resp.get("statusReason")
                    or resp.get("failureReason")
                    or resp.get("reasonCode")
                    or (resp.get("statusReasons") or [""])[0]
                    or ""
                )
                logger.error("Runtime %s %s: %s", runtime_id, status, reason)
                return {
                    "success": False,
                    "runtime_id": runtime_id,
                    "status": status,
                    "error": f"Runtime entered {status}" + (f": {reason}" if reason else ""),
                }
        except Exception as e:
            logger.warning("Error checking runtime status: %s", e)

        time.sleep(15)

    return {
        "success": False,
        "runtime_id": runtime_id,
        "error": f"Runtime did not become READY within {timeout}s",
    }


def wait_for_default_endpoint_ready(
    agentcore_ctrl, runtime_id: str, timeout: int = 180
) -> dict:
    """Poll until the runtime's DEFAULT endpoint is READY (Bug 166).

    ``get_agent_runtime`` returning READY is NOT sufficient to invoke: the
    AgentCore data plane invokes against an *endpoint* qualifier (DEFAULT), and
    the DEFAULT endpoint is provisioned ASYNCHRONOUSLY — it can still be CREATING
    (or not yet listed) for a window AFTER the runtime itself reports READY.
    Invoking in that window fails with ``ResourceNotFoundException: No endpoint
    or agent found with qualifier 'DEFAULT'`` — surfaced to the user as the
    opaque "Runtime not found." So the launch step must gate on the ENDPOINT,
    not just the runtime.

    Returns ``{"success": True, "endpoint_arn": ...}`` once DEFAULT is READY.
    The endpoint is auto-created by ``create_agent_runtime``; we only WAIT for
    it here (no explicit create — that would race the service-side creator and
    raise ConflictException).
    """
    start = time.time()
    last_seen = ""
    while time.time() - start < timeout:
        try:
            eps = agentcore_ctrl.list_agent_runtime_endpoints(
                agentRuntimeId=runtime_id
            ).get("runtimeEndpoints", [])
            for ep in eps:
                if ep.get("name") == "DEFAULT":
                    last_seen = ep.get("status", "")
                    if last_seen == "READY":
                        return {
                            "success": True,
                            "endpoint_arn": ep.get("agentRuntimeEndpointArn", ""),
                            "status": "READY",
                        }
                    if "FAILED" in last_seen:
                        return {
                            "success": False,
                            "status": last_seen,
                            "error": f"DEFAULT endpoint entered {last_seen}",
                        }
        except Exception as e:  # noqa: BLE001 — transient list errors are retried
            logger.warning(
                "Error listing endpoints for runtime %s (will retry)", runtime_id
            )
        time.sleep(5)

    return {
        "success": False,
        "status": last_seen or "ABSENT",
        "error": (
            f"DEFAULT endpoint for runtime {runtime_id} did not become READY "
            f"within {timeout}s (last status: {last_seen or 'not listed'})"
        ),
    }


def _resolve_runtime_identifier(agentcore_ctrl, identifier: str) -> str:
    """Convert a runtime NAME (or already-an-id) to the canonical agentRuntimeId.

    AgentCore distinguishes the human-readable runtime name (e.g.
    `my_agent_v1`) from the canonical id (e.g. `my_agent_v1-AbCdEfGh01`).
    `delete_agent_runtime`/`get_agent_runtime` accept ONLY the canonical id —
    passing the friendly name returns AccessDeniedException (not 404),
    masking the real cause. See tasks/lessons.md Bug 50.

    Heuristic: if the input looks like the canonical id (has `-` followed by
    a 10-char hash) it's used as-is. Otherwise we list and match by name.
    """
    if not identifier:
        return identifier
    # Canonical id pattern: <name>-<10 hash chars>. Anchored on both ends and
    # restricted to the AgentCore-permitted name alphabet so the regex stays
    # linear (no `.+` polynomial backtracking on adversarial input).
    if re.match(r"^[A-Za-z0-9_-]+-[A-Za-z0-9]{10}$", identifier):
        return identifier
    # Name lookup — paginate list_agent_runtimes
    try:
        next_token = None
        for _ in range(20):  # max 20 pages
            kwargs = {}
            if next_token:
                kwargs["nextToken"] = next_token
            try:
                resp = agentcore_ctrl.list_agent_runtimes(**kwargs)
            except Exception:
                kwargs["maxResults"] = 100
                resp = agentcore_ctrl.list_agent_runtimes(**kwargs)
            runtimes = resp.get("agentRuntimeSummaries", resp.get("agentRuntimes", []))
            for rt in runtimes:
                if rt.get("agentRuntimeName") == identifier:
                    return rt.get("agentRuntimeId", identifier)
            next_token = resp.get("nextToken")
            if not next_token:
                break
    except Exception as e:
        logger.warning("Could not resolve runtime name %s to id: %s", identifier, e)
    return identifier  # fall through; caller will see ResourceNotFound and treat as no-op


def _resolve_runtime_name_for_cleanup(
    canonical_id: str, region: str
) -> Optional[str]:
    """Map an AgentCore canonical runtime id back to the friendly runtime_name.

    The TriggersTable is keyed by the human-friendly ``runtime_name`` (e.g.
    ``my_agent``), but ``destroy_runtime`` only has the canonical AgentCore id
    (``<agentcore_runtime_name>-<10hash>``). The deployer records the
    runtime_name<->runtime_id / agentcore_runtime_name mapping in the
    AgentVersions table, so scan there for a matching row.

    Best-effort: returns the friendly name if a version row matches, otherwise
    falls back to the canonical id with its 10-char hash suffix stripped (which
    equals the friendly name in the single-version per-agent path). Returns
    None only if resolution is impossible — callers treat that as "nothing to
    clean up". Never raises; the caller's cleanup is best-effort.
    """
    if not canonical_id:
        return None
    try:
        # The versions store keys rows by friendly runtime_name and stamps each
        # with runtime_id / agentcore_runtime_name. We only have the AgentCore
        # id here, so do a bounded scan of the AgentVersions table and match on
        # either field. The table is tenant-small in practice; cap pages.
        table_name = os.environ.get("AGENT_VERSIONS_TABLE_NAME", "AgentVersions")
        table = boto3.resource("dynamodb", region_name=region).Table(table_name)
        scan_kwargs: dict = {
            "ProjectionExpression": (
                "runtime_name, runtime_id, agentcore_runtime_name"
            ),
        }
        for _ in range(20):  # cap at 20 pages of scan
            resp = table.scan(**scan_kwargs)
            for item in resp.get("Items", []):
                if (
                    item.get("runtime_id") == canonical_id
                    or item.get("agentcore_runtime_name") == canonical_id
                ):
                    name = item.get("runtime_name")
                    if name:
                        return name
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_kwargs["ExclusiveStartKey"] = last_key
    except Exception as e:
        logger.warning(
            "Could not resolve runtime_name for %s via versions store: %s",
            canonical_id,
            e,
        )
    # Fallback: strip the canonical 10-char hash suffix. For per-agent runtimes
    # the friendly runtime_name equals the un-hashed AgentCore name.
    stripped = re.sub(r"-[A-Za-z0-9]{10}$", "", canonical_id)
    return stripped or None


def destroy_runtime(runtime_id: str, region: str) -> dict:
    """Delete an AgentCore runtime AND its execution role.

    Order of operations matters: capture roleArn via get-agent-runtime BEFORE
    delete-agent-runtime — after deletion the get fails and the role is orphaned
    (verified live 2026-05-16 — the API DELETE path leaked roles before this
    fix; see tasks/lessons.md Bug 25 / Bug 27 — drift between cleanup.sh and
    runtime_deployer.destroy_runtime).

    The `runtime_id` arg may be either the canonical agentRuntimeId or the
    friendly agentRuntimeName — we resolve it (Bug 50).

    Idempotent on already-deleted runtimes/roles.
    """
    agentcore_ctrl = boto3.client("bedrock-agentcore-control", region_name=region)
    canonical_id = _resolve_runtime_identifier(agentcore_ctrl, runtime_id)

    # AgentCore returns AccessDeniedException (NOT ResourceNotFound) when the
    # runtime ID doesn't exist. Treat both as "runtime is gone" so DELETE on a
    # never-created runtime still proceeds to IAM-role cleanup. See lessons Bug 55.
    def _is_runtime_gone(err: Exception) -> bool:
        s = str(err)
        return "ResourceNotFound" in s or "AccessDeniedException" in s

    # Capture roleArn first so we can also delete the IAM role.
    role_arn = ""
    try:
        rt = agentcore_ctrl.get_agent_runtime(agentRuntimeId=canonical_id)
        role_arn = rt.get("roleArn", "") or ""
    except Exception as e:
        if not _is_runtime_gone(e):
            logger.warning("Could not get-agent-runtime before delete: %s", e)

    try:
        agentcore_ctrl.delete_agent_runtime(agentRuntimeId=canonical_id)
        logger.info("Deleted runtime: %s", canonical_id)
    except Exception as e:
        if not _is_runtime_gone(e):
            return {"success": False, "message": f"Runtime destroy error: {e}"}
        logger.info("Runtime %s already deleted (or never existed)", canonical_id)

    # Best-effort: delete the matched IAM execution role(s) too.
    # When the runtime never existed, role_arn is empty (get_agent_runtime
    # returned AccessDenied); fall back to the conventional names used by
    # the SFN IAM step (`AgentCoreRuntime-{name}`) and the direct-deploy path
    # (`{name}-role`). See lessons Bug 57.
    candidate_role_names: list[str] = []
    if role_arn:
        # roleArn format: arn:aws:iam::<acct>:role/<RoleName>
        candidate_role_names.append(role_arn.rsplit("/", 1)[-1])
    # Use the original argument as the runtime "name" component for the
    # convention-based fallback. canonical_id may include a `-XxXxXxXxXx`
    # suffix; strip that to recover the runtime name.
    # NOTE (Gap P3.3B): this `AgentCoreRuntime-{name}` candidate also matches
    # per-agent least-privilege roles minted by iam_step (mode == 'per_agent'),
    # so they are cleaned up here too — and are NOT skipped by the Bug-62 guard
    # below (which only skips the stack shared role / '-shared' suffix).
    name_for_role = re.sub(r"-[A-Za-z0-9]{10}$", "", runtime_id)
    for role_name in (
        f"AgentCoreRuntime-{name_for_role}",
        f"{name_for_role}-role",
    ):
        if role_name not in candidate_role_names:
            candidate_role_names.append(role_name)

    # Bug 60 introduced a stack-managed shared role. Bug 62: never delete
    # that role — every DELETE /api/runtime would nuke it, breaking every
    # other runtime in the stack (and DemoTriage). Compare role NAME (not
    # ARN) so this still works when the cleanup is via the name fallback.
    shared_role_arn = os.environ.get("SHARED_RUNTIME_ROLE_ARN", "")
    shared_role_name = (
        shared_role_arn.rsplit("/", 1)[-1] if shared_role_arn else ""
    )

    iam = boto3.client("iam")
    deleted_any_role = False
    for role_name in candidate_role_names:
        if not role_name:
            continue
        # Skip stack-managed shared roles. Match exact (Bug 60's role) or any
        # role with `-shared` suffix as defense in depth against future shared
        # roles. See tasks/lessons.md Bug 62.
        if role_name == shared_role_name or role_name.endswith("-shared"):
            logger.info("Skipping shared role %s (Bug 62 guard)", role_name)
            continue
        try:
            # Detach managed policies
            for p in iam.list_attached_role_policies(RoleName=role_name).get(
                "AttachedPolicies", []
            ):
                try:
                    iam.detach_role_policy(RoleName=role_name, PolicyArn=p["PolicyArn"])
                except Exception as e:
                    logger.warning("detach_role_policy %s: %s", p.get("PolicyArn"), e)
            # Delete inline policies
            for pn in iam.list_role_policies(RoleName=role_name).get(
                "PolicyNames", []
            ):
                try:
                    iam.delete_role_policy(RoleName=role_name, PolicyName=pn)
                except Exception as e:
                    logger.warning("delete_role_policy %s: %s", pn, e)
            iam.delete_role(RoleName=role_name)
            logger.info("Deleted runtime execution role: %s", role_name)
            deleted_any_role = True
        except iam.exceptions.NoSuchEntityException:
            # Role doesn't exist for this candidate — try the next.
            continue
        except Exception as e:
            # Bug 139: the cleanup-only IAM grant is now tag-scoped
            # (aws:ResourceTag/ManagedBy=agentcore-flows). An AccessDenied here
            # therefore means this candidate name is NOT a role we created
            # (untagged / belongs to someone else / never existed) — that's the
            # guard working as intended, not a failure. Log it quietly so it
            # doesn't read as an orphan-leak alarm; only surface other errors loud.
            if "AccessDenied" in str(e):
                logger.debug(
                    "Skipping role %s during cleanup: not an agentcore-managed role "
                    "(tag-scoped grant denied). Candidate name, not an orphan.",
                    role_name,
                )
            else:
                logger.warning(
                    "Runtime %s role cleanup (%s) failed: %s", runtime_id, role_name, e
                )

    # Phase 1 Gap 1D — best-effort dashboard cleanup. The dashboard was
    # created in runtime_launch_step.py with name `agentcore-{runtime_id}`.
    # delete_dashboards is idempotent and a failure here doesn't fail the
    # destroy. Same Bug 25/27 cascade-cleanup pattern.
    try:
        from app.services.observability_dashboard import delete_dashboard_for_runtime
        delete_dashboard_for_runtime(canonical_id, region)
    except Exception as e:
        logger.warning("Dashboard cleanup for %s failed: %s", canonical_id, e)

    # Phase 1 Gap 1C cleanup (M-2 + real-tester finding 2026-05-28):
    # cascade-delete the AgentCore OnlineEvaluationConfig + its CloudWatch
    # eval-results log group + the AgentCoreEval-* IAM execution role.
    # evaluation_step.py names the config `eval_<sanitized_runtime_id>`
    # and the role `AgentCoreEval-<agent_id[:32]>`. Best-effort: failures
    # here don't fail the destroy. Same pattern as Bug 25/27. See Bug 124.
    try:
        ctrl = boto3.client("bedrock-agentcore-control", region_name=region)
        logs_client = boto3.client("logs", region_name=region)
        normalised_runtime = re.sub(r"[^a-zA-Z0-9_]", "_", canonical_id)[:32]
        next_token: Optional[str] = None
        for _ in range(20):  # cap pagination at 20 pages × 50 = 1000 configs
            kw: dict = {"maxResults": 50}
            if next_token:
                kw["nextToken"] = next_token
            try:
                resp = ctrl.list_online_evaluation_configs(**kw)
            except Exception:
                break
            for cfg in resp.get("onlineEvaluationConfigs", []):
                cfg_name = cfg.get("onlineEvaluationConfigName", "")
                if normalised_runtime not in cfg_name:
                    continue
                cfg_id = cfg.get("onlineEvaluationConfigId", "")
                if not cfg_id:
                    continue
                try:
                    ctrl.delete_online_evaluation_config(onlineEvaluationConfigId=cfg_id)
                    logger.info("Deleted OnlineEvaluationConfig %s", cfg_id)
                except Exception as e:
                    logger.warning("Failed to delete eval config %s: %s", cfg_id, e)
                # Eval results log group is per-config — see Bug 120.
                try:
                    logs_client.delete_log_group(
                        logGroupName=f"/aws/bedrock-agentcore/evaluations/results/{cfg_id}"
                    )
                    logger.info(
                        "Deleted eval-results log group for config %s", cfg_id
                    )
                except Exception as e:
                    msg = str(e)
                    if "ResourceNotFound" in msg:
                        pass
                    else:
                        logger.warning(
                            "Failed to delete eval log group for %s: %s", cfg_id, e
                        )
            next_token = resp.get("nextToken")
            if not next_token:
                break
        # Bug 124: also delete the AgentCoreEval-* IAM exec role. evaluation_step
        # mints it as `AgentCoreEval-{agent_id[:32]}` where agent_id is the
        # AgentCore runtime_id. Match the same prefix here. Idempotent on
        # already-gone roles.
        eval_role_name = f"AgentCoreEval-{normalised_runtime}"
        try:
            for pn in iam.list_role_policies(RoleName=eval_role_name).get(
                "PolicyNames", []
            ):
                try:
                    iam.delete_role_policy(RoleName=eval_role_name, PolicyName=pn)
                except Exception:
                    pass
            iam.delete_role(RoleName=eval_role_name)
            logger.info("Deleted eval execution role %s", eval_role_name)
        except iam.exceptions.NoSuchEntityException:
            pass
        except Exception as e:
            logger.warning(
                "Failed to delete eval role %s: %s", eval_role_name, e
            )
    except Exception as e:
        logger.warning("Eval-config cleanup for %s failed: %s", canonical_id, e)

    # Bug 124 — Phase 3 Gap 3F: tear down any scheduled / event triggers so a
    # destroyed runtime doesn't leave a live cron/webhook invoking a dead ARN.
    # destroy_runtime only has canonical_id; resolve the friendly runtime_name
    # (the triggers PK) from the versions/slots store, then for each trigger
    # delete the provisioned EventBridge Scheduler schedule / events.Rule /
    # Lambda Function URL + the webhook HMAC secret, and finally the DDB rows.
    # Best-effort: every failure is logged and never fails the destroy.
    try:
        from app.services.trigger_store import get_trigger_store

        # Resolve the friendly runtime_name that keys the TriggersTable. The
        # deployer already records runtime_name<->runtime_id in AgentVersions;
        # use that mapping (or the owner GSI) to find the rows to clean up.
        runtime_name = _resolve_runtime_name_for_cleanup(canonical_id, region)
        if runtime_name:
            store = get_trigger_store()
            scheduler = boto3.client("scheduler", region_name=region)
            events = boto3.client("events", region_name=region)
            lam = boto3.client("lambda", region_name=region)
            sm = boto3.client("secretsmanager", region_name=region)
            for trig in store.list_for_runtime(runtime_name):
                if trig.scheduler_name:
                    try:
                        scheduler.delete_schedule(Name=trig.scheduler_name)
                    except Exception as e:
                        logger.warning("Trigger schedule cleanup failed: %s", e)
                if trig.eventbridge_rule_arn:
                    try:
                        rule_name = trig.eventbridge_rule_arn.rsplit("/", 1)[-1]
                        for t in events.list_targets_by_rule(Rule=rule_name).get(
                            "Targets", []
                        ):
                            events.remove_targets(Rule=rule_name, Ids=[t["Id"]])
                        events.delete_rule(Name=rule_name)
                    except Exception as e:
                        logger.warning("Trigger rule cleanup failed: %s", e)
                if trig.function_url:
                    try:
                        lam.delete_function_url_config(
                            FunctionName=trig.function_url
                        )
                    except Exception as e:
                        logger.warning("Trigger function-url cleanup failed: %s", e)
                if trig.webhook_secret_ref:
                    try:
                        sm.delete_secret(
                            SecretId=trig.webhook_secret_ref,
                            ForceDeleteWithoutRecovery=True,
                        )
                    except Exception as e:
                        logger.warning("Trigger secret cleanup failed: %s", e)
                try:
                    store.delete(trig.runtime_name, trig.trigger_id)
                except Exception as e:
                    logger.warning("Trigger row cleanup failed: %s", e)
    except Exception as e:
        logger.warning("Triggers cleanup for %s failed: %s", canonical_id, e)

    # NOTE on the RuntimeSlots row: the friendly-name slot/version rows are
    # cleaned up by the OWNER-SCOPED release block in deployment_handler.py
    # (the Bug-192 release, ~L1683-1700), which deletes them only when
    # slot.owner_sub matches the caller. We deliberately do NOT delete the slot
    # row here: destroy_runtime() has no caller_sub, so an unconditional delete
    # would regress the cross-tenant name-lock invariant (a legacy null-owner
    # deployment record + a friendly-name collision could let one tenant drop
    # another tenant's slot). Tenant-safe slot teardown belongs to the caller-
    # aware path, not this resource-level destroy.

    if deleted_any_role:
        return {"success": True, "message": f"Runtime {runtime_id} and execution role deleted"}
    return {"success": True, "message": f"Runtime {runtime_id} deleted"}
