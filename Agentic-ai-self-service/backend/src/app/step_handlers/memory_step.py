"""Step handler: Create AgentCore Memory resource.

Creates a memory resource via bedrock-agentcore-control API.
Returns memory_id to be passed as env var to the runtime.

References:
- https://github.com/awslabs/amazon-bedrock-agentcore-samples/tree/main/01-tutorials/04-AgentCore-memory
- https://github.com/aws/bedrock-agentcore-starter-toolkit (operations/memory/manager.py)
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import app.services._otel_platform  # noqa: F401

import json
import logging
import os
import time
import uuid

import boto3

from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services.deployment_state_store import DeploymentStateStore
from app.services.naming import sanitize_agentcore_name

logger = logging.getLogger(__name__)


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_deployment_store() -> DeploymentStateStore:
    return DeploymentStateStore(
        table_name=_get_env("DEPLOYMENT_TABLE_NAME", "DeploymentState"),
        region=_get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")),
    )


def _extract_memory_id(resp: dict) -> str:
    """Extract memory ID from an API response, trying multiple key patterns.

    AWS bedrock-agentcore-control responses may use different key names across
    API versions.  We try known patterns, then scan all string values, and
    finally extract from ARN if present.
    """
    # Direct top-level keys
    for key in ("memoryId", "id", "memory_id"):
        val = resp.get(key)
        if val and isinstance(val, str) and len(val) >= 12:
            return val

    # Nested under a wrapper key (e.g. {"memory": {"memoryId": "..."}})
    for wrapper in ("memory",):
        nested = resp.get(wrapper)
        if isinstance(nested, dict):
            for key in ("memoryId", "id", "memory_id"):
                val = nested.get(key)
                if val and isinstance(val, str) and len(val) >= 12:
                    return val

    # Extract from ARN: arn:aws:bedrock-agentcore:...:memory/<id>
    for key in ("arn", "memoryArn"):
        arn = resp.get(key, "")
        if isinstance(arn, str) and "/memory/" in arn:
            return arn.split("/memory/")[-1]
        if isinstance(arn, str) and ":memory/" in arn:
            return arn.split(":memory/")[-1]

    # Last resort: scan all top-level string values that look like an ID
    resp_keys = [k for k in resp.keys() if k != "ResponseMetadata"]
    logger.warning(
        "Could not find memoryId in known keys. Response keys: %s, values: %s",
        resp_keys,
        {k: type(resp[k]).__name__ for k in resp_keys},
    )
    for key in resp_keys:
        val = resp[key]
        if isinstance(val, str) and len(val) >= 12 and not val.startswith("arn:"):
            logger.warning("Using fallback key '%s' as memory ID: %s", key, val)
            return val

    return ""


def _find_memory_by_name(client, memory_name: str, retries: int = 2) -> str | None:
    """Search list_memories for a memory matching the given name.

    IMPORTANT: list_memories response items do NOT contain a ``name`` field —
    only ``arn``, ``id``, ``status``, ``createdAt``, ``updatedAt``.  The memory
    name is embedded as a prefix of the ``id`` (e.g. ``support_memory-P42Idq8EvI``).
    We match by checking if the id starts with ``{memory_name}-``, or falls back
    to an exact ``name`` field match in case the API changes.
    """
    for attempt in range(retries + 1):
        if attempt > 0:
            time.sleep(3)
        try:
            resp = client.list_memories(maxResults=100)
            resp_keys = [k for k in resp.keys() if k != "ResponseMetadata"]
            logger.warning("list_memories response keys: %s (attempt %d)", resp_keys, attempt + 1)

            # Try all list-typed values in the response (handles any key name)
            for key in resp_keys:
                val = resp[key]
                if not isinstance(val, list):
                    continue
                logger.warning("list_memories key '%s': %d items", key, len(val))
                for idx, mem in enumerate(val):
                    if not isinstance(mem, dict):
                        continue
                    if idx == 0:
                        logger.warning("Memory item keys: %s", list(mem.keys()))

                    mem_id = mem.get("id") or mem.get("memoryId") or ""

                    # Match by name field (if present) or by id prefix
                    matched = False
                    if mem.get("name") == memory_name:
                        matched = True
                    elif mem_id.startswith(f"{memory_name}-") or mem_id == memory_name:
                        matched = True

                    if matched:
                        if not mem_id:
                            mem_id = _extract_memory_id(mem)
                        logger.warning(
                            "Found memory '%s': id=%s, status=%s",
                            memory_name,
                            mem_id,
                            mem.get("status", "?"),
                        )
                        if mem_id:
                            return mem_id
            logger.warning(
                "Memory '%s' not found in list_memories (attempt %d)",
                memory_name,
                attempt + 1,
            )
        except Exception as e:
            logger.warning("Could not list memories (attempt %d): %s", attempt + 1, e)
    return None


def _wait_for_memory_ready(client, memory_id: str, timeout: int = 120) -> dict:
    """Poll until memory is ACTIVE/READY or timeout.

    Bug 156: control-plane get_memory reporting ACTIVE LEADS the data-plane
    CreateEvent path the agent uses to write conversation turns — the first
    invocation right after deploy can still hit "Memory status is not active,
    unable to process CreateEvent" (observed live in the free-form kitchen-sink
    flow; the agent degraded gracefully with "Could not save to memory"). After
    the status flips ACTIVE we add a short settle so the data plane catches up.
    """
    for _ in range(timeout // 5):
        resp = client.get_memory(memoryId=memory_id)
        status = resp.get("status", "")
        if status in ("ACTIVE", "READY"):
            time.sleep(10)  # data-plane CreateEvent settle margin
            return resp
        if "FAILED" in status:
            raise RuntimeError(f"Memory entered {status}")
        time.sleep(5)
    raise RuntimeError(f"Memory {memory_id} did not become ACTIVE in {timeout}s")


def handler(event: dict, context) -> dict:
    deployment_id = event.get("deployment_id", "")

    try:
        store = _get_deployment_store()
        store.update_step(deployment_id, DeploymentStepName.MEMORY, DeploymentStatusEnum.IN_PROGRESS)

        memory_config = event.get("memory_config") or {}
        region = _get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1"))

        # AgentCore CreateMemory enforces name regex [a-zA-Z][a-zA-Z0-9_]{0,47}
        # (letters/digits/UNDERSCORE only, start with a letter, <=48 chars). The
        # canvas lets a user type any free-form memory name (e.g. "custom-mem" or
        # "My Memory"), which would otherwise hard-fail the deploy at CreateMemory
        # with a ValidationException. Sanitize: non-allowed chars -> underscore,
        # ensure a leading letter, cap length. (Bug 155, caught in free-form test.)
        raw_memory_name = memory_config.get("name", "AgentCoreMemory") or "AgentCoreMemory"
        memory_name = sanitize_agentcore_name(
            raw_memory_name, style="underscore", prefix="mem", fallback="AgentCoreMemory"
        )
        enabled = memory_config.get("enabled", True)

        if not enabled:
            return {
                **event,
                "memory_result": {
                    "success": True,
                    "message": "Memory disabled, skipping",
                },
            }

        agentcore_ctrl = boto3.client("bedrock-agentcore-control", region_name=region)

        # Check if memory with this name already exists
        existing_memory_id = _find_memory_by_name(agentcore_ctrl, memory_name)

        if existing_memory_id:
            memory_id = existing_memory_id
        else:
            # Create IAM role for memory
            iam_client = boto3.client("iam")
            memory_role_name = f"AgentCoreMemory-{memory_name}"
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
                    Description=f"Memory execution role for {memory_name}",
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
                time.sleep(10)
                # Manifest: record the memory exec role for generic teardown.
                store.record_resource(
                    deployment_id,
                    {"type": "iam_role", "name": memory_role_name, "region": region},
                )
            except iam_client.exceptions.EntityAlreadyExistsException:
                memory_role_arn = iam_client.get_role(RoleName=memory_role_name)["Role"]["Arn"]

            # Create memory with short-term only (no strategies = STM only)
            create_params = {
                "clientToken": str(uuid.uuid4()),
                "name": memory_name,
                "description": f"Memory for AgentCore deployment {deployment_id}",
                "memoryExecutionRoleArn": memory_role_arn,
                "memoryStrategies": [],
                "eventExpiryDuration": memory_config.get("eventExpiryDuration", 90),
            }

            # Add strategies if configured
            # AWS API expects keys like: semanticMemoryStrategy, summaryMemoryStrategy,
            # episodicMemoryStrategy, userPreferenceMemoryStrategy, customMemoryStrategy
            STRATEGY_KEY_MAP = {
                "semantic": "semanticMemoryStrategy",
                "summary": "summaryMemoryStrategy",
                "episodic": "episodicMemoryStrategy",
                "user_preferences": "userPreferenceMemoryStrategy",
                "custom": "customMemoryStrategy",
            }
            strategies = memory_config.get("strategies", [])
            if strategies:
                memory_strategies = []
                for strategy in strategies:
                    # Canonical shape is a dict {type,name,...} (MemoryStrategyConfig).
                    # Be defensive: a bare string ("semantic") is coerced to
                    # {"type": <string>} rather than 500-ing with AttributeError.
                    if isinstance(strategy, str):
                        strategy = {"type": strategy}
                    elif not isinstance(strategy, dict):
                        logger.warning("Ignoring malformed memory strategy: %r", strategy)
                        continue
                    strategy_type = strategy.get("type", "semantic").lower()
                    api_key = STRATEGY_KEY_MAP.get(strategy_type)
                    if not api_key:
                        logger.warning("Unknown strategy type '%s', skipping", strategy_type)
                        continue
                    # Strategy names must match [a-zA-Z][a-zA-Z0-9_]{0,47} — no hyphens
                    import re as _re

                    raw_name = strategy.get("name", f"{memory_name}_{strategy_type}")
                    safe_name = _re.sub(r"[^a-zA-Z0-9_]", "_", raw_name)
                    safe_name = _re.sub(r"_+", "_", safe_name).strip("_")[:48]
                    if not safe_name or not safe_name[0].isalpha():
                        safe_name = "S" + safe_name
                    # Default namespace must satisfy AgentCore strategy-specific
                    # validation rules. See tasks/lessons.md Bugs 98/99.
                    # - summary: requires {sessionId} substring
                    # - episodic: reflection namespace `{memoryStrategyId}/actors/{actorId}/`
                    #   must be prefix-compatible with the user's namespace
                    # We pick safe defaults per type that satisfy these rules.
                    if strategy_type == "summary":
                        default_ns = "/strategies/{memoryStrategyId}/actors/{actorId}/sessions/{sessionId}/"
                    elif strategy_type == "episodic":
                        default_ns = "/strategies/{memoryStrategyId}/actors/{actorId}/"
                    elif strategy_type in ("user_preferences", "user_preference"):
                        default_ns = "/strategies/{memoryStrategyId}/actors/{actorId}/"
                    elif strategy_type == "semantic":
                        default_ns = "/strategies/{memoryStrategyId}/actors/{actorId}/"
                    else:
                        default_ns = "/strategies/{memoryStrategyId}/actors/{actorId}/"
                    namespaces = strategy.get("namespaces") or [default_ns]
                    strategy_config = {
                        api_key: {
                            "name": safe_name,
                            "description": strategy.get("description", f"{strategy_type} strategy"),
                            "namespaces": namespaces,
                        }
                    }
                    memory_strategies.append(strategy_config)
                create_params["memoryStrategies"] = memory_strategies

            memory_id = None
            try:
                resp = agentcore_ctrl.create_memory(**create_params)
                resp_keys = [k for k in resp.keys() if k != "ResponseMetadata"]
                logger.warning("create_memory response keys: %s", resp_keys)
                memory_id = _extract_memory_id(resp)
                logger.warning("Created memory, extracted id: '%s'", memory_id)
            except Exception as e:
                err_str = str(e).lower()
                if "already exists" in err_str or "conflict" in err_str:
                    logger.info("Memory '%s' already exists, looking it up again", memory_name)
                    memory_id = _find_memory_by_name(agentcore_ctrl, memory_name)
                    if not memory_id:
                        raise RuntimeError(
                            f"Memory '{memory_name}' already exists but could not be found via list_memories. "
                            f"Delete the stuck memory from the AWS console and retry."
                        ) from e
                else:
                    raise

            if not memory_id:
                # create_memory succeeded but we couldn't parse the ID — try list as fallback
                logger.warning("create_memory succeeded but ID extraction failed, trying list_memories")
                memory_id = _find_memory_by_name(agentcore_ctrl, memory_name)

            if not memory_id:
                raise RuntimeError(
                    f"Memory '{memory_name}' was created but ID could not be extracted. "
                    f"Check CloudWatch logs for create_memory response keys."
                )

            # Manifest: record the memory resource for generic teardown right
            # after create succeeds (before the readiness wait, which can be
            # killed mid-poll and otherwise leak the memory).
            store.record_resource(
                deployment_id,
                {"type": "memory", "id": memory_id, "region": region},
            )

            # Wait for memory to be ready
            _wait_for_memory_ready(agentcore_ctrl, memory_id)

        memory_result = {
            "success": True,
            "memory_id": memory_id,
            "memory_name": memory_name,
        }

        # Persist memory_result to the deployment record IMMEDIATELY so the
        # DELETE handler can clean it up even if a downstream step fails
        # before status_update writes the full record. Otherwise the memory
        # leaks. See tasks/lessons.md Bug 85.
        try:
            ddb = boto3.client("dynamodb", region_name=region)
            ddb.update_item(
                TableName=os.environ.get("DEPLOYMENT_TABLE_NAME", "DeploymentState"),
                Key={"deployment_id": {"S": deployment_id}},
                UpdateExpression="SET memory_result = :mr",
                ExpressionAttributeValues={
                    ":mr": {"M": {
                        "success": {"BOOL": True},
                        "memory_id": {"S": memory_id},
                        "memory_name": {"S": memory_name},
                    }},
                },
            )
            logger.info("Persisted memory_result mid-flight for %s", deployment_id)
        except Exception as persist_err:
            # Don't fail the step on a metadata-write race — memory was
            # successfully created and downstream steps will use it.
            logger.warning("Failed to persist memory_result mid-flight: %s", persist_err)

        return {**event, "memory_result": memory_result}

    except Exception:
        logger.exception("Memory step failed for deployment %s", deployment_id)
        raise
