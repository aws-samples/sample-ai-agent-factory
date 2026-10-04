"""Step handler: Create AgentCore Memory resource.

Creates a memory resource via bedrock-agentcore-control API.
Returns memory_id to be passed as env var to the runtime.

References:
- https://github.com/awslabs/amazon-bedrock-agentcore-samples/tree/main/01-tutorials/04-AgentCore-memory
- https://github.com/aws/bedrock-agentcore-starter-toolkit (operations/memory/manager.py)
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import json
import logging
import os
import time
import uuid

import app.services._otel_platform  # noqa: F401
from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services import step_clients
from app.services.aws_pagination import list_all
from app.services.deployment_state_store import DeploymentStateStore
from app.services.iam_boundary import create_role_kwargs, ensure_role_boundary
from app.services.naming import regional_iam_role_name, sanitize_agentcore_name
from app.services.resource_ownership import (
    DEPLOYMENT_ID_TAG_KEY,
    OWNER_SUB_HASH_TAG_KEY,
    ForeignResourceError,
    assert_agentcore_resource_owned,
    assert_this_deployment_may_mutate,
    owner_sub_hash,
    tag_map,
)
from app.services.resource_tagging import governed_tag_list, governed_tags

logger = logging.getLogger(__name__)

_MEMORY_READY_STATUSES = frozenset({"ACTIVE", "READY"})
_MEMORY_WAIT_STATUSES = frozenset({"CREATING", "UPDATING"})
_MAX_READINESS_CHECKS = 120


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


def _find_memory_summary_by_name(
    client,
    memory_name: str,
    retries: int = 2,
) -> dict | None:
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
            memories = list_all(
                client,
                "list_memories",
                item_keys=("memories", "memorySummaries", "items"),
                request={"maxResults": 100},
            )
            logger.warning(
                "list_memories returned %d item(s) (attempt %d)",
                len(memories),
                attempt + 1,
            )
            for idx, mem in enumerate(memories):
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
                        return {**mem, "id": mem_id}
            logger.warning(
                "Memory '%s' not found in list_memories (attempt %d)",
                memory_name,
                attempt + 1,
            )
        except Exception as e:
            logger.warning("Could not list memories (attempt %d): %s", attempt + 1, e)
    return None


def _find_memory_by_name(client, memory_name: str, retries: int = 2) -> str | None:
    """Backward-compatible id-only wrapper used by older callers and tests."""
    summary = _find_memory_summary_by_name(client, memory_name, retries)
    return str(summary.get("id") or "") if summary else None


def _memory_payload(response: dict | None) -> dict:
    if not isinstance(response, dict):
        return {}
    nested = response.get("memory")
    return nested if isinstance(nested, dict) else response


def _memory_status(response: dict | None) -> str:
    return str(_memory_payload(response).get("status") or "").upper()


def _memory_arn(response: dict | None) -> str:
    payload = _memory_payload(response)
    return str(payload.get("arn") or payload.get("memoryArn") or "")


def _require_owner_sub(event: dict) -> str:
    owner_sub = str(event.get("owner_sub") or "")
    if not owner_sub:
        raise RuntimeError(
            "Memory deployment requires an authenticated owner so the resource cannot be shared across tenants."
        )
    return owner_sub


def _ownership_extra(owner_sub: str, deployment_id: str) -> dict[str, str]:
    return {
        OWNER_SUB_HASH_TAG_KEY: owner_sub_hash(owner_sub),
        DEPLOYMENT_ID_TAG_KEY: deployment_id,
    }


def _assert_owner_hash(
    resource_label: str,
    tags: dict | list | None,
    owner_sub: str,
) -> dict[str, str]:
    mapped = tag_map(tags)
    expected = owner_sub_hash(owner_sub)
    if mapped.get(OWNER_SUB_HASH_TAG_KEY) != expected:
        raise ForeignResourceError(
            f"{resource_label} already exists but is not bound to the authenticated "
            "caller. Rename the memory, or migrate the resource by adding the exact "
            "OwnerSubHash expected by this platform before redeploying."
        )
    return mapped


def _assert_memory_owned_by_caller(
    client,
    memory_id: str,
    region: str,
    owner_sub: str,
) -> tuple[dict, dict[str, str]]:
    """Require stack ownership and the exact caller binding before adoption."""
    detail = assert_agentcore_resource_owned(
        client,
        "memory",
        memory_id,
        region,
    )
    arn = _memory_arn(detail)
    if not arn:
        raise ForeignResourceError(f"Memory {memory_id} has no readable ARN, so caller ownership cannot be verified.")
    tags = client.list_tags_for_resource(resourceArn=arn).get("tags")
    return detail, _assert_owner_hash(f"Memory {memory_id}", tags, owner_sub)


def _classify_memory_status(memory_id: str, status: str, checks: int) -> bool:
    """Return readiness; poll only documented transitional states."""
    if status in _MEMORY_READY_STATUSES:
        return True
    if status in _MEMORY_WAIT_STATUSES:
        if checks >= _MAX_READINESS_CHECKS:
            raise RuntimeError(f"Memory {memory_id} remained {status} after {checks} readiness checks.")
        return False
    if status == "DELETING":
        raise RuntimeError(f"Memory {memory_id} is DELETING and cannot be adopted by this deployment.")
    if status.endswith("FAILED"):
        raise RuntimeError(f"Memory {memory_id} entered {status}.")
    raise RuntimeError(
        f"Memory {memory_id} returned unsupported status {status or '<missing>'}; readiness cannot be proven."
    )


def _persist_memory_result(
    store: DeploymentStateStore,
    deployment_id: str,
    memory_result: dict,
) -> None:
    try:
        store.update_status(
            deployment_id,
            DeploymentStatusEnum.IN_PROGRESS,
            memory_result=memory_result,
        )
        logger.info("Persisted memory_result mid-flight for %s", deployment_id)
    except Exception as persist_err:  # noqa: BLE001
        # The manifest remains the authoritative cleanup inventory. This status
        # write improves recovery UX but must not erase a successfully journaled
        # AWS resource when DynamoDB has a transient write race.
        logger.warning(
            "Failed to persist memory_result mid-flight: %s",
            type(persist_err).__name__,
        )


def _check_memory_readiness(event: dict, store: DeploymentStateStore) -> dict:
    """One bounded readiness observation; Step Functions owns the wait loop."""
    deployment_id = str(event.get("deployment_id") or "")
    owner_sub = _require_owner_sub(event)
    region = str(event.get("target_region") or _get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")))
    prior = event.get("memory_result")
    if not isinstance(prior, dict) or not prior.get("memory_id"):
        raise RuntimeError("Memory readiness check received no persisted memory_id.")

    memory_id = str(prior["memory_id"])
    client = step_clients.client(event, "bedrock-agentcore-control")
    detail, _tags = _assert_memory_owned_by_caller(
        client,
        memory_id,
        region,
        owner_sub,
    )
    checks = int(prior.get("readiness_checks") or 0) + 1
    status = _memory_status(detail)
    service_ready = _classify_memory_status(memory_id, status, checks)
    # AgentCore's control plane can report ACTIVE before the memory data plane
    # accepts CreateEvent. Require two ACTIVE observations separated by the
    # state-machine Wait state. The initial create/adoption result counts as the
    # first observation when it already reported ACTIVE, so every path still gets
    # at least one full settle interval without sleeping inside Lambda.
    active_observations = int(prior.get("active_observations") or 0) + 1 if service_ready else 0
    ready = service_ready and active_observations >= 2
    memory_result = {
        **prior,
        "status": status,
        "ready": ready,
        "readiness_checks": checks,
        "active_observations": active_observations,
    }
    _persist_memory_result(store, deployment_id, memory_result)
    return {**event, "memory_result": memory_result}


def _memory_role_reference(
    iam_client,
    detail: dict,
    *,
    owner_sub: str,
    region: str,
    deployment_id: str,
) -> tuple[str, bool]:
    """Return the exact role used by an adopted memory after caller proof."""
    role_arn = str(_memory_payload(detail).get("memoryExecutionRoleArn") or "")
    if ":role/" not in role_arn:
        raise ForeignResourceError(
            "The existing memory has no readable memoryExecutionRoleArn, so its "
            "execution identity cannot be adopted safely."
        )
    role_name = role_arn.rsplit("/", 1)[-1]
    role = iam_client.get_role(RoleName=role_name).get("Role") or {}
    assert_this_deployment_may_mutate(
        f"IAM role {role_name}",
        role.get("Tags"),
        region,
    )
    tags = _assert_owner_hash(f"IAM role {role_name}", role.get("Tags"), owner_sub)
    return role_name, tags.get(DEPLOYMENT_ID_TAG_KEY) == deployment_id


def handler(event: dict, context) -> dict:
    deployment_id = event.get("deployment_id", "")

    try:
        store = _get_deployment_store()
        store.update_step(deployment_id, DeploymentStepName.MEMORY, DeploymentStatusEnum.IN_PROGRESS)

        if isinstance(event.get("memory_result"), dict) and event["memory_result"].get("memory_id"):
            return _check_memory_readiness(event, store)

        memory_config = event.get("memory_config") or {}
        # Resource clients already route through step_clients to the event's target
        # account/region. Use that same target for names, tags, manifest rows, and
        # ownership checks; stamping the home region onto an account-global IAM role
        # makes a later target-region teardown correctly refuse our own role.
        region = event.get("target_region") or _get_env(
            "APP_AWS_REGION",
            _get_env("AWS_REGION", "us-east-1"),
        )

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
                    "status": "DISABLED",
                    "ready": True,
                    "readiness_checks": 0,
                    "active_observations": 0,
                },
            }

        owner_sub = _require_owner_sub(event)
        ownership_extra = _ownership_extra(owner_sub, str(deployment_id))
        agentcore_ctrl = step_clients.client(event, "bedrock-agentcore-control")
        iam_client = step_clients.client(event, "iam")

        # Check if memory with this name already exists
        existing_memory = _find_memory_summary_by_name(agentcore_ctrl, memory_name)

        memory_role_name: str | None = None
        memory_role_created = False
        memory_created = False
        initial_status = ""
        if existing_memory:
            memory_id = str(existing_memory["id"])
            detail, memory_tags = _assert_memory_owned_by_caller(
                agentcore_ctrl,
                memory_id,
                region,
                owner_sub,
            )
            initial_status = _memory_status(detail)
            _classify_memory_status(memory_id, initial_status, 0)
            memory_created = memory_tags.get(DEPLOYMENT_ID_TAG_KEY) == str(deployment_id)
            memory_role_name, memory_role_created = _memory_role_reference(
                iam_client,
                detail,
                owner_sub=owner_sub,
                region=region,
                deployment_id=str(deployment_id),
            )
            # Record both references. On a retry of the deployment that created
            # them, the DeploymentId tags preserve True provenance; on a later
            # same-caller adoption they remain inventory-only False rows.
            store.record_resource(
                deployment_id,
                {
                    "type": "iam_role",
                    "name": memory_role_name,
                    "region": region,
                    "created_by_deployment": memory_role_created,
                },
            )
            store.record_resource(
                deployment_id,
                {
                    "type": "memory",
                    "id": memory_id,
                    "region": region,
                    "created_by_deployment": memory_created,
                },
            )
        else:
            # Create IAM role for memory
            memory_role_name = regional_iam_role_name(
                f"AgentCoreMemory-{memory_name}",
                region,
            )
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
                    # IAM roles are account-global with no region in the name, so a
                    # teardown sweeping "AgentCoreMemory-*" would delete every
                    # deployment's memory roles in the account. cleanup.sh reads this
                    # tag instead of trusting the name prefix.
                    Tags=governed_tag_list(region, event.get("resource_tags"), ownership_extra),
                    **create_role_kwargs(),
                )
            except iam_client.exceptions.EntityAlreadyExistsException:
                # `AgentCoreMemory-{memory_name}` is built from a user-chosen name and
                # IAM role names are account-global, so this branch cannot tell our own
                # redeploy from a collision. The role is about to be handed to
                # `create_memory` as `memoryExecutionRoleArn`, which would run this
                # deployment's memory under permissions somebody else granted -- so
                # prove it is ours rather than trusting the name.
                _existing_mem_role = iam_client.get_role(RoleName=memory_role_name)["Role"]
                assert_this_deployment_may_mutate(
                    f"IAM role {memory_role_name}", _existing_mem_role.get("Tags"), region
                )
                existing_role_tags = _assert_owner_hash(
                    f"IAM role {memory_role_name}",
                    _existing_mem_role.get("Tags"),
                    owner_sub,
                )
                memory_role_arn = _existing_mem_role["Arn"]
                # F-06: retrofit the permissions boundary once ownership is proven.
                ensure_role_boundary(iam_client, memory_role_name, role=_existing_mem_role)
                memory_role_created = existing_role_tags.get(DEPLOYMENT_ID_TAG_KEY) == str(deployment_id)
                store.record_resource(
                    deployment_id,
                    {
                        "type": "iam_role",
                        "name": memory_role_name,
                        "region": region,
                        "created_by_deployment": memory_role_created,
                    },
                )
            else:
                memory_role_arn = role_resp["Role"]["Arn"]
                memory_role_created = True
                # Journal immediately after CreateRole acknowledges the resource.
                # Policy attachment and IAM propagation can still fail; teardown
                # must already know this exact account-global role name.
                store.record_resource(
                    deployment_id,
                    {
                        "type": "iam_role",
                        "name": memory_role_name,
                        "region": region,
                        "created_by_deployment": True,
                    },
                )
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
                                        # One namespace covers both planes: there is no
                                        # `bedrock-agentcore-control:` IAM prefix, so the entry that
                                        # used to sit here authorized nothing. An IAM service prefix is
                                        # the SigV4 signing name, and both the data-plane and
                                        # control-plane clients sign as `bedrock-agentcore`. See the
                                        # prefix note in services/per_agent_identity.py.
                                        "bedrock-agentcore:*",
                                    ],
                                    "Resource": "*",
                                }
                            ],
                        }
                    ),
                )
                time.sleep(10)

            # Create memory with short-term only (no strategies = STM only)
            create_params = {
                "clientToken": str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        (f"agentcore-memory:{region}:{deployment_id}:{memory_name}"),
                    )
                ),
                "name": memory_name,
                "description": f"Memory for AgentCore deployment {deployment_id}",
                "memoryExecutionRoleArn": memory_role_arn,
                "memoryStrategies": [],
                "eventExpiryDuration": memory_config.get("eventExpiryDuration", 90),
                # P0-B: AgentCore Memory bills per event stored, so it is precisely the resource a
                # cost-allocation tag exists for. ownership_extra stays the winning layer.
                "tags": governed_tags(region, event.get("resource_tags"), ownership_extra),
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
            # IAM role propagation to AgentCore's CreateMemory validator is
            # eventually consistent: a role created seconds earlier can fail
            # with "Please provide a role with a valid trust policy" even
            # though the trust policy is correct. Under gateway+memory+
            # observability deploys the 10s post-create sleep is not always
            # enough (matrix-run finding, 2/3 repro). Retry with backoff
            # instead of failing the whole deploy.
            memory_created = True
            resp: dict = {}
            for attempt in range(5):
                try:
                    resp = agentcore_ctrl.create_memory(**create_params)
                    resp_keys = [k for k in resp.keys() if k != "ResponseMetadata"]
                    logger.warning("create_memory response keys: %s", resp_keys)
                    memory_id = _extract_memory_id(resp)
                    logger.warning("Created memory, extracted id: '%s'", memory_id)
                    break
                except Exception as e:
                    err_str = str(e).lower()
                    if "already exists" in err_str or "conflict" in err_str:
                        logger.info("Memory '%s' already exists, looking it up again", memory_name)
                        conflict_summary = _find_memory_summary_by_name(
                            agentcore_ctrl,
                            memory_name,
                        )
                        if not conflict_summary:
                            raise RuntimeError(
                                f"Memory '{memory_name}' already exists but could not be found via list_memories. "
                                f"Delete the stuck memory from the AWS console and retry."
                            ) from e
                        memory_id = str(conflict_summary["id"])
                        detail, memory_tags = _assert_memory_owned_by_caller(
                            agentcore_ctrl,
                            memory_id,
                            region,
                            owner_sub,
                        )
                        initial_status = _memory_status(detail)
                        _classify_memory_status(memory_id, initial_status, 0)
                        memory_created = memory_tags.get(DEPLOYMENT_ID_TAG_KEY) == str(deployment_id)
                        actual_role_name, actual_role_created = _memory_role_reference(
                            iam_client,
                            detail,
                            owner_sub=owner_sub,
                            region=region,
                            deployment_id=str(deployment_id),
                        )
                        if actual_role_name != memory_role_name:
                            store.record_resource(
                                deployment_id,
                                {
                                    "type": "iam_role",
                                    "name": actual_role_name,
                                    "region": region,
                                    "created_by_deployment": actual_role_created,
                                },
                            )
                            memory_role_name = actual_role_name
                            memory_role_created = actual_role_created
                        break
                    if "trust policy" in err_str and attempt < 4:
                        wait = 8 * (attempt + 1)
                        logger.warning(
                            "create_memory rejected role (IAM propagation lag), retry %d/4 in %ds",
                            attempt + 1,
                            wait,
                        )
                        time.sleep(wait)
                        continue
                    raise

            if not memory_id:
                # create_memory succeeded but we couldn't parse the ID — try list as fallback
                logger.warning("create_memory succeeded but ID extraction failed, trying list_memories")
                fallback_summary = _find_memory_summary_by_name(
                    agentcore_ctrl,
                    memory_name,
                )
                memory_id = str(fallback_summary.get("id") or "") if fallback_summary else None
                if fallback_summary:
                    initial_status = _memory_status(fallback_summary)

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
                {
                    "type": "memory",
                    "id": memory_id,
                    "region": region,
                    "created_by_deployment": memory_created,
                },
            )
            initial_status = initial_status or _memory_status(resp) or "CREATING"
            _classify_memory_status(memory_id, initial_status, 0)

        memory_result = {
            "success": True,
            "memory_id": memory_id,
            "memory_name": memory_name,
            "status": initial_status,
            # Always pass through at least one state-machine Wait. If creation or
            # adoption already observed ACTIVE, that observation is retained so
            # the next ACTIVE check completes after the data-plane settle margin.
            "ready": False,
            "readiness_checks": 0,
            "active_observations": (1 if initial_status in _MEMORY_READY_STATUSES else 0),
        }
        if memory_role_name:
            memory_result["memory_role_name"] = memory_role_name
            memory_result["memory_role_created_by_deployment"] = memory_role_created

        # Persist immediately so a downstream failure still leaves an
        # owner-checked deployment-id cleanup handle.
        _persist_memory_result(store, deployment_id, memory_result)

        return {**event, "memory_result": memory_result}

    except Exception:
        logger.exception("Memory step failed for deployment %s", deployment_id)
        raise
