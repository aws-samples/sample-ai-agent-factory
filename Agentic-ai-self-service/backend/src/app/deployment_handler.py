"""Deployment Lambda handler for the AgentCore Visual Workflow Platform.

Lightweight FastAPI app wrapped with Mangum that handles deployment-related
API endpoints:

- POST /api/deploy          → start Step Functions execution
- GET  /api/deploy/{id}     → query deployment state
- POST /api/test-runtime    → invoke a deployed runtime
- DELETE /api/runtime/{id}  → delete runtime and clean up resources

Requirements: 3.1, 3.7, 9.1, 9.2, 9.3, 9.4
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import app.services._otel_platform  # noqa: F401

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import boto3
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from mangum import Mangum

from app.models.deployment_models import (
    DeploymentState,
    DeploymentStatusEnum,
    DeployRequest,
    DeployResponse,
    DeleteResponse,
    TestRequest,
    TestResponse,
)
from app.models.tool_generation_models import (
    AgentGenerateRequest,
    AgentGenerateResponse,
    ToolGenerateRequest,
    ToolGenerateResponse,
    ToolTestRequest,
)
from app.services.config import load_config
from app.services.deployment_state_store import DeploymentStateStore
from app.services.gateway_deployer import cleanup_gateway_resources, get_cognito_token
from app.services.harness_deployer import destroy_harness, invoke_harness
from app.services.runtime_deployer import destroy_runtime
from app.services.tool_generator import generate_tool
from app.services.tool_tester import test_tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Application configuration
# ---------------------------------------------------------------------------

config = load_config()

DEPLOYMENT_TABLE_NAME = os.environ.get(
    "DEPLOYMENTS_TABLE_NAME",
    os.environ.get("DEPLOYMENT_TABLE_NAME", "AgentCoreDeployments"),
)
STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")


# ---------------------------------------------------------------------------
# Boto3 wrapper functions
# ---------------------------------------------------------------------------


def _create_sfn_client(region: str):
    return boto3.client("stepfunctions", region_name=region)


def _start_sfn_execution(sfn_client, state_machine_arn: str, name: str, input_json: str) -> dict:
    return sfn_client.start_execution(
        stateMachineArn=state_machine_arn,
        name=name,
        input=input_json,
    )


def _create_agentcore_client(region: str):
    from botocore.config import Config

    # 28s read timeout — maximise time for AgentCore cold starts while staying
    # just under API Gateway's 29s hard limit.
    # The frontend has retry logic (5 attempts) for cold start timeouts.
    return boto3.client(
        "bedrock-agentcore",
        region_name=region,
        config=Config(read_timeout=25, connect_timeout=5, retries={"max_attempts": 0}),
    )


# ---------------------------------------------------------------------------
# Deployment state store (lazy-initialised)
# ---------------------------------------------------------------------------

_state_store: Optional[DeploymentStateStore] = None


def _get_state_store() -> DeploymentStateStore:
    global _state_store
    if _state_store is None:
        _state_store = DeploymentStateStore(
            table_name=DEPLOYMENT_TABLE_NAME,
            region=config.aws_region,
        )
    return _state_store


def _scan_for_runtime(table, runtime_id: str) -> Optional[dict]:
    """Look up a deployment record by runtime_id.

    Audit issue #7: previously this did a full O(N) Scan with a
    FilterExpression. The DeploymentsTable now has a `runtime_id-index` GSI,
    so we Query that GSI first (O(1) on the partition key). If the GSI Query
    returns nothing — which happens for partial-failed deploys whose
    runtime_id was never populated, so the item was never projected onto
    the GSI — we fall back to the original paginated Scan so the caller
    can still find the record via the deployment_id surrogate.
    """
    if not runtime_id:
        return None

    # Fast path: Query the runtime_id GSI.
    try:
        query_kwargs: dict = {
            "IndexName": "runtime_id-index",
            "KeyConditionExpression": "runtime_id = :rid",
            "ExpressionAttributeValues": {":rid": runtime_id},
            "Limit": 1,
        }
        resp = table.query(**query_kwargs)
        items = resp.get("Items", [])
        if items:
            return items[0]
    except Exception as exc:
        # GSI may be missing on stacks that haven't redeployed since the
        # CDK change; log and fall through to the scan path so we don't
        # break delete/test on those deployments.
        logger.warning(
            "runtime_id-index Query failed (%s); falling back to Scan", exc,
        )

    # Fallback: paginated Scan (covers partial-failed deploys whose
    # runtime_id attribute was never set, plus pre-GSI stacks).
    scan_kwargs: dict = {
        "FilterExpression": "runtime_id = :rid",
        "ExpressionAttributeValues": {":rid": runtime_id},
    }
    while True:
        resp = table.scan(**scan_kwargs)
        items = resp.get("Items", [])
        if items:
            return items[0]
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return None


def _gateway_implied(
    gateway_tools: Optional[list],
    connectors: Optional[list],
    connected_tools: Optional[list],
) -> bool:
    """Whether a gateway must be deployed even if no explicit ``gateway_config`` was sent.

    Bug B (harness deploys with no tools): the SFN state machine gates its
    gateway step on ``$.gateway_config`` being present (``HasGateway?`` choice in
    platform_stack.py). But gateway TOOLS and SaaS CONNECTORS logically REQUIRE a
    gateway to serve them — and callers that only select tools/connectors
    (notably the Harness authoring form, which has no Gateway node to populate
    ``gatewayConfig``) never send an explicit ``gateway_config``. Without it the
    gateway step is skipped, no gateway is created, and the runtime/harness comes
    up with ZERO tools (the harness then silently falls back to default Strands
    tools). The direct path (services/deployment.py) already derives the gateway
    from ``"gateway" in connected_tools`` and synthesizes ``{"name": ...}``
    itself — this mirrors that so BOTH paths behave identically.
    """
    return bool(gateway_tools or connectors or "gateway" in (connected_tools or []))


def _maybe_promote_policy(deployment_state: Optional[dict], region: str) -> bool:
    """Lazy-promote a pending Cedar policy engine from LOG_ONLY to ENFORCE.

    Bug 178/181: the gateway's policy-authorization plane converges ~3-5 min after
    deploy, too long to block the deploy pipeline. So the policy step attaches
    LOG_ONLY + records ``policy_result.enforce_pending``; this helper is called at
    the natural post-deploy touchpoints — the test/invoke path AND the status poll
    (Bug 181) — so ENFORCE engages the first time the agent is used OR its status is
    checked, whichever comes first, without any extra infra. Idempotent +
    best-effort: any failure leaves LOG_ONLY (tools keep working) and the next
    touchpoint retries. On success it MUTATES ``deployment_state['policy_result']``
    in place and persists the new mode. Returns True when it flipped to ENFORCE.
    """
    if not deployment_state:
        return False
    if not (deployment_state.get("policy_result") or {}).get("enforce_pending"):
        return False
    try:
        from app.services.policy_promoter import try_promote_to_enforce
        outcome = try_promote_to_enforce(deployment_state, region)
        logger.info("policy promote outcome: %s", outcome)
        if outcome and outcome.get("promoted"):
            pr = dict(deployment_state.get("policy_result") or {})
            pr["mode"] = "ENFORCE"
            pr["downgraded_to_log_only"] = False
            pr["enforce_pending"] = None
            pr["promoted_at_first_use"] = True
            deployment_state["policy_result"] = pr
            try:
                from app.models.deployment_models import DeploymentStatusEnum
                _get_state_store().update_status(
                    deployment_state.get("deployment_id"),
                    DeploymentStatusEnum.SUCCEEDED,
                    policy_result=pr,
                )
            except Exception:  # noqa: BLE001
                logger.warning("policy promote: could not persist ENFORCE mode")
            logger.info("policy promote: gateway now in ENFORCE mode")
            return True
    except Exception:  # noqa: BLE001
        logger.warning("policy promote: skipped (will retry next touchpoint)")
    return False


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------


def _get_user_id(request: Request) -> Optional[str]:
    """Extract user sub from JWT claims (API Gateway HTTP API JWT authorizer)."""
    try:
        return request.scope.get("aws.event", {}).get(
            "requestContext", {}
        ).get("authorizer", {}).get("jwt", {}).get("claims", {}).get("sub")
    except Exception:
        return None


deployment_app = FastAPI(
    title="AgentCore Deployment API",
    description="Deployment orchestration endpoints",
    version="0.1.0",
)

# SECURITY: Restrict allowed methods and headers instead of wildcard "*"
deployment_app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Amz-Date", "X-Api-Key"],
)

# Phase 1 Gap 1A — version + slot management endpoints. Mounted on the
# deployment Lambda because the versions table belongs to the runtime-control
# plane (same data plane as /api/deploy and /api/test-runtime). API GW routes
# for /api/runtimes/{name}/versions* are added in infra/stacks/platform_stack.py
# per the Bug 21 router-enumeration rule.
from app.routers.versions import router as versions_router  # noqa: E402
from app.routers.evaluations import router as evaluations_router  # noqa: E402
from app.routers.registry import router as registry_router  # noqa: E402
from app.routers.cost import router as cost_router  # noqa: E402
from app.routers.hitl import router as hitl_router  # noqa: E402
from app.routers.prompts import router as prompts_router  # noqa: E402  # Phase 3 Gap 3H
from app.routers.connectors import router as connectors_router  # noqa: E402
from app.routers.triggers import router as triggers_router  # noqa: E402

deployment_app.include_router(versions_router)
# Phase 1 Gap 1C — evaluation results endpoint. Mounted on the deployment
# Lambda because it queries CloudWatch Logs Insights and the AgentCore
# control plane, both of which the deployment Lambda role already grants.
deployment_app.include_router(evaluations_router)
# Phase 2 Gap 2A — agent registry. Mounted here because the deployment
# Lambda owns the AgentRegistry table grant + already has the auth helper.
deployment_app.include_router(registry_router)
# Phase 2 Gap 2B — cost analytics. Queries CloudWatch Logs Insights for
# per-runtime token/cost rollups (same grant set as evaluations).
deployment_app.include_router(cost_router)
# Phase 2 Gap 2D — human-in-the-loop approval queue. Reads the HITL table's
# owner_sub GSI and decides requests; deployment Lambda has the table grant.
deployment_app.include_router(hitl_router)
# Phase 3 Gap 3H — prompt management library. Mounted on the deployment
# Lambda because it owns the PromptLibrary table grant + already has the auth
# helper, and the deploy hook resolves prompt refs in this same process.
deployment_app.include_router(prompts_router)
# Phase 3 Gap 3E — pre-built connector catalog. Read-only catalog (no tenant
# data) mounted on the deployment Lambda alongside the gateway tooling. Routes
# /api/connectors + /api/connectors/{proxy+} are added in platform_stack.py per
# the Bug 21 router-enumeration rule.
deployment_app.include_router(connectors_router)
# Phase 3 Gap 3F — scheduled / event triggers registry. Mounted here because
# the deployment Lambda owns the TriggersTable grant + the agentcore-trigger/*
# Secrets Manager grant and already has the get_caller_sub auth helper.
deployment_app.include_router(triggers_router)


@deployment_app.get("/health")
async def health_check() -> dict:
    checks = {"api": "ok"}
    try:
        store = _get_state_store()
        store._table.table_status  # lightweight DynamoDB connectivity check
        checks["dynamodb"] = "ok"
    except Exception:
        checks["dynamodb"] = "degraded"
    overall = "healthy" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": overall, "checks": checks}


# ---------------------------------------------------------------------------
# POST /api/deploy
# ---------------------------------------------------------------------------


@deployment_app.post(
    "/api/deploy",
    status_code=202,
    response_model=DeployResponse,
    response_model_by_alias=True,
)
async def handle_deploy(request: DeployRequest, raw_request: Request) -> DeployResponse:
    """Start a Step Functions execution for a new deployment."""
    deployment_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    user_id = _get_user_id(raw_request)

    # Phase 1 Gap 1A — versioning. Mint a sortable version_id for this deploy
    # and resolve the AgentCore-side runtime name (friendly + version suffix)
    # so each version maps to a distinct AgentCore runtime ARN. The previous
    # production version, if any, becomes parent_version_id so the version
    # graph can be reconstructed from DDB without scanning history.
    from app.services.agent_versions_store import (
        AgentVersion,
        get_slots_store,
        get_versions_store,
        new_version_id,
        short_version_suffix,
    )
    from app.services.runtime_deployer import sanitize_runtime_name

    friendly_runtime_name = sanitize_runtime_name(
        request.config.name or f"agent-{deployment_id[:8]}"
    )
    version_id = new_version_id()
    # AgentCore runtime name: 48 char limit, must match [a-zA-Z][a-zA-Z0-9_]{0,47}.
    # We reserve 9 chars for "_<8-hex>", giving the friendly portion up to 39 chars.
    suffix = short_version_suffix(version_id)
    agentcore_runtime_name = f"{friendly_runtime_name[:39]}_{suffix}"

    # Look up the previous production version, if any, to record lineage.
    # SECURITY (H-1, security review 2026-05-28): the AgentVersionsTable PK is
    # `runtime_name` and shared across tenants. Before we read the existing
    # slot row to derive parent_version_id (and before status_update_step
    # writes a new one) we MUST refuse the deploy if the friendly name is
    # already owned by a different sub. Without this check, Tenant B can
    # deploy `config.name="alice_bot"` and clobber Alice's slot row,
    # locking her out and leaking her version_id via /slots.
    # See tasks/lessons.md Bug 122.
    parent_version_id: Optional[str] = None
    try:
        slots = get_slots_store().get(friendly_runtime_name)
        if slots is not None and slots.owner_sub and slots.owner_sub != (user_id or ""):
            # Cross-tenant collision. Use 409 (not 404) — the existence of the
            # name is verifiable by trying to deploy it; a 404 here would be
            # misleading because the name IS in use, just not by this caller.
            # The owner_sub itself is never returned, so no extra info leaks.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Runtime name '{friendly_runtime_name}' is already in use "
                    f"by another tenant. Pick a different name."
                ),
            )
        if slots and slots.production_version_id:
            parent_version_id = slots.production_version_id
    except HTTPException:
        raise
    except Exception:
        # Slots table may be missing on first deploy of a fresh stack — not fatal.
        logger.warning("RuntimeSlotsStore.get failed; treating as first deploy",
                       exc_info=True)

    # Defense in depth: also check the AgentVersions table for any foreign-owner
    # row under this friendly name. Covers the case where slots may not yet
    # exist (a partial earlier deploy that never reached status_update) but
    # the versions table already has rows owned by another sub.
    #
    # Bug 192b — only a LIVE claim should hold the name. A `failed` deploy never
    # produced a usable runtime, and a `superseded` row is a retired version; such
    # rows must NOT lock the name forever (a customer hit 409 on 'omar1'/'agent_
    # harness' purely because of leftover failed-deploy rows). Only `pending`
    # (in-flight) and `succeeded` (live) foreign rows block a new deploy.
    _LIVE_CLAIM = {"pending", "succeeded"}
    try:
        existing_versions = get_versions_store().list_for_runtime(friendly_runtime_name)
        for v in existing_versions:
            if (
                v.owner_sub
                and v.owner_sub != (user_id or "")
                and (v.status or "pending") in _LIVE_CLAIM
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Runtime name '{friendly_runtime_name}' is already in use "
                        f"by another tenant. Pick a different name."
                    ),
                )
    except HTTPException:
        raise
    except Exception:
        logger.warning(
            "AgentVersionsStore.list_for_runtime failed during ownership check; "
            "treating as first deploy",
            exc_info=True,
        )

    # Phase 3 Gap 3H — resolve a library-prompt reference in config.system_prompt
    # to its actual body BEFORE the config is serialized into the SFN input.
    # Tenant-scoped to the deploying caller (owner OR same-org visibility);
    # an inline-string systemPrompt is left untouched (back-compat). Never
    # hard-fails: a missing/foreign ref logs and keeps the original value.
    from app.services.prompt_resolver import resolve_system_prompt
    resolve_system_prompt(request.config, user_id)

    state = DeploymentState(
        deployment_id=deployment_id,
        workflow_id=request.node_id,
        user_id=user_id,
        status=DeploymentStatusEnum.PENDING,
        started_at=now,
        version_id=version_id,
        parent_version_id=parent_version_id,
        deployment_slot=request.deployment_slot or "production",
        agentcore_runtime_name=agentcore_runtime_name,
        # Phase B — persist the chosen path up-front so the delete/test handlers
        # know whether to route to harness_deployer even on partial-failed deploys.
        deployment_mode=request.deployment_mode or "runtime",
    )

    store = _get_state_store()
    store.create(state)

    # Persist a *pending* AgentVersion row so partial-failed deploys still
    # surface in the version history for the UI. status flips to succeeded
    # in status_update_step on completion. See Bug 85 — every step that
    # creates a shared resource MUST persist its ID immediately, not wait
    # for the final status_update.
    try:
        get_versions_store().put(
            AgentVersion(
                runtime_name=friendly_runtime_name,
                version_id=version_id,
                owner_sub=user_id or "",
                created_at=now.isoformat(),
                deployment_id=deployment_id,
                agentcore_runtime_name=agentcore_runtime_name,
                parent_version_id=parent_version_id,
                description=request.version_description,
                status="pending",
            )
        )
    except Exception:
        # Don't fail the deploy on a versions-table write error — log it and
        # continue. The deploy itself is still tracked via DeploymentState.
        logger.exception(
            "Failed to write initial AgentVersion %s/%s",
            friendly_runtime_name,
            version_id,
        )

    # Auto-derive connected_tools from sibling configs so the codegen step
    # always sees the right tool list, even when a caller forgot to include
    # `connectedTools` explicitly. See tasks/lessons.md Bug 89.
    auto_connected = list(request.connected_tools or [])
    if request.knowledge_base_config and "knowledge_base" not in auto_connected:
        auto_connected.append("knowledge_base")
    if request.memory_config and "memory" not in auto_connected:
        auto_connected.append("memory")
    if request.gateway_config and "gateway" not in auto_connected:
        auto_connected.append("gateway")
    if request.guardrails_config and "guardrails" not in auto_connected:
        auto_connected.append("guardrails")
    if getattr(request, "a2a_config", None) and "a2a" not in auto_connected:
        auto_connected.append("a2a")
    if request.observability_config and "observability" not in auto_connected:
        auto_connected.append("observability")

    sfn_input = {
        "deployment_id": deployment_id,
        "workflow_id": request.node_id,
        "config": request.config.model_dump(mode="json", by_alias=True),
        "connected_tools": auto_connected,
        "template_id": request.template_id,
        # Phase 1 Gap 1A — every step handler keys S3 paths and AgentCore
        # runtime names off the version. friendly_runtime_name is the user's
        # input; agentcore_runtime_name is what we actually pass to AgentCore.
        "version_id": version_id,
        "friendly_runtime_name": friendly_runtime_name,
        "agentcore_runtime_name": agentcore_runtime_name,
        "deployment_slot": request.deployment_slot or "production",
        "parent_version_id": parent_version_id,
        "owner_sub": user_id or "",
        # Phase B (Bug 9) — deployment_mode MUST reach the SFN path so the state
        # machine can route HARNESS deploys to harness_step instead of the
        # codegen/runtime steps. Mirrors the direct path in services/deployment.py.
        "deployment_mode": request.deployment_mode or "runtime",
    }
    # Only include gateway fields if gateway is configured. When a gateway is
    # only IMPLIED (tools/connectors selected but no explicit gateway_config —
    # e.g. the Harness authoring form), synthesize a minimal config so the SFN
    # gateway step actually runs. See _gateway_implied for the full rationale.
    if request.gateway_config:
        sfn_input["gateway_config"] = request.gateway_config
    elif _gateway_implied(request.gateway_tools, request.connectors, auto_connected):
        # deploy_gateway only requires `name`; it fills the rest. Reuse the
        # friendly runtime/harness name so the gateway is recognizably paired
        # with its agent.
        sfn_input["gateway_config"] = {"name": friendly_runtime_name}
    if request.gateway_tools:
        sfn_input["gateway_tools"] = request.gateway_tools
    if request.identity_config:
        sfn_input["identity_config"] = request.identity_config.model_dump(mode="json", by_alias=True)
    if request.custom_tools:
        sfn_input["custom_tools"] = [t.model_dump(mode="json", by_alias=True) for t in request.custom_tools]
    if request.connectors:
        # Bug 9 — connectors must reach the SFN gateway_step. The step mints the
        # Secrets Manager secret from the transient secret_value and then drops
        # it, so we re-include the otherwise-excluded secret_value HERE (and
        # only here) on the SFN input. It is never written to DDB, the canvas
        # JSON, or logs — only carried in the SFN execution input so the step
        # can mint the secret and thread back only the resulting ARN.
        sfn_input["connectors"] = []
        for c in request.connectors:
            conn = c.model_dump(mode="json", by_alias=False, exclude_none=True)
            if c.secret_value:
                conn["secret_value"] = c.secret_value
            sfn_input["connectors"].append(conn)
    if request.memory_config:
        sfn_input["memory_config"] = request.memory_config
    if request.evaluation_config:
        sfn_input["evaluation_config"] = request.evaluation_config
    if request.policy_config:
        sfn_input["policy_config"] = request.policy_config
    if request.mcp_server_config:
        sfn_input["mcp_server_config"] = request.mcp_server_config
    if request.knowledge_base_config:
        sfn_input["knowledge_base_config"] = request.knowledge_base_config
    if request.guardrails_config:
        sfn_input["guardrails_config"] = request.guardrails_config
    if getattr(request, "observability_config", None):
        sfn_input["observability_config"] = request.observability_config
    if getattr(request, "a2a_config", None):
        sfn_input["a2a_config"] = request.a2a_config

    execution_arn: Optional[str] = None
    try:
        sfn_client = _create_sfn_client(config.aws_region)
        sfn_response = _start_sfn_execution(
            sfn_client,
            state_machine_arn=STATE_MACHINE_ARN,
            name=f"deploy-{deployment_id}",
            input_json=json.dumps(sfn_input, default=str),
        )
        execution_arn = sfn_response.get("executionArn")

        store.update_status(deployment_id, DeploymentStatusEnum.PENDING)
        _update_execution_arn(store, deployment_id, execution_arn)

    except Exception as exc:
        logger.error("Failed to start Step Functions execution: %s", exc)
        store.update_status(
            deployment_id,
            DeploymentStatusEnum.FAILED,
            error_details=f"Failed to start execution: {exc}",
        )
        # SECURITY: Do not leak internal error details to the client.
        # Full error is logged server-side and stored in DynamoDB for debugging.
        raise HTTPException(
            status_code=500,
            detail="Deployment initiation failed. Check deployment status for details.",
        )

    return DeployResponse(
        deployment_id=deployment_id,
        execution_arn=execution_arn,
        status=DeploymentStatusEnum.PENDING,
        message="Deployment started",
    )


def _update_execution_arn(store: DeploymentStateStore, deployment_id: str, execution_arn: str) -> None:
    from app.services.deployment_state_store import _update_item

    _update_item(
        store._table,
        key={"deployment_id": deployment_id},
        update_expr="SET execution_arn = :arn",
        expr_values={":arn": execution_arn},
    )


# ---------------------------------------------------------------------------
# GET /api/deploy/{deployment_id}
# ---------------------------------------------------------------------------


@deployment_app.get("/api/deploy/{deployment_id}")
async def handle_deploy_status(deployment_id: str) -> dict:
    """Query deployment state from DynamoDB."""
    deployment_id = _validate_deployment_id(deployment_id)
    store = _get_state_store()
    state = store.get(deployment_id)

    if state is None:
        raise HTTPException(status_code=404, detail=f"Deployment '{deployment_id}' not found")

    result = state.model_dump(mode="json")
    # Bug 181: the status poll is a natural post-deploy touchpoint to lazy-promote
    # a pending Cedar engine to ENFORCE (the gateway's policy plane converges a few
    # minutes after deploy). _maybe_promote_policy mutates the dict's policy_result
    # in place on success + persists it, so the returned status reflects real
    # enforcement even before the first invoke. Best-effort; never fails the read.
    try:
        if (result.get("policy_result") or {}).get("enforce_pending"):
            _maybe_promote_policy(result, config.aws_region)
    except Exception:  # noqa: BLE001
        logger.warning("status: policy promote attempt skipped")
    return result


# ---------------------------------------------------------------------------
# GET /api/deployments?workflow_id=...
# ---------------------------------------------------------------------------


@deployment_app.get("/api/deployments")
async def handle_list_deployments(
    request: Request,
    workflow_id: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    """List deployments by workflow_id or user_id (from JWT), optionally filtered by status."""
    store = _get_state_store()
    user_id = _get_user_id(request)
    if user_id:
        states = store.query_by_user(user_id, status_filter=status)
    elif workflow_id and workflow_id.strip():
        states = store.query_by_workflow(workflow_id.strip(), status_filter=status)
    else:
        raise HTTPException(status_code=400, detail="workflow_id query parameter is required")
    return [s.model_dump(mode="json") for s in states]


# ---------------------------------------------------------------------------
# POST /api/test-runtime
# ---------------------------------------------------------------------------


@deployment_app.post("/api/test-runtime", response_model=TestResponse, response_model_by_alias=True)
async def handle_test_runtime(request: TestRequest, raw_request: Request) -> TestResponse:
    """Invoke a deployed runtime via boto3 API. Caller must own the deployment.

    Requirements: 9.1, 9.2, 9.4
    """
    if request.simulated:
        return TestResponse(
            success=True,
            response="[Simulated] Mock response - deploy a real agent to test.",
        )

    try:
        runtime_id = request.runtime_id
        if not runtime_id:
            return TestResponse(success=False, error="No runtime_id provided")
        # SECURITY: Validate runtime_id format
        if not re.match(r"^[a-zA-Z0-9_-]+$", runtime_id) or len(runtime_id) > 128:
            return TestResponse(success=False, error="Invalid runtime_id format")

        region = config.aws_region
        caller_sub = _get_user_id(raw_request)

        # Look up deployment state to get runtime_arn and gateway_config
        store = _get_state_store()
        deployment_state = None

        # Find deployment record by runtime_id
        try:
            table = store._table
            deployment_state = _scan_for_runtime(table, runtime_id)
        except Exception as exc:
            logger.warning(
                "Failed to look up deployment state for runtime_id=%s: %s",
                runtime_id,
                exc,
            )

        # Tenant isolation: caller must own the deployment.
        # Pre-tenancy records (user_id=None) are accessible to keep legacy
        # data working until a backfill pass; new deploys always carry user_id.
        # See tasks/lessons.md Bug 37.
        if deployment_state:
            owner = deployment_state.get("user_id")
            if owner and owner != caller_sub:
                raise HTTPException(status_code=404, detail="Runtime not found")

        # Build prompt with conversation history
        prompt = request.input
        if request.history:
            history_text = "\n".join(
                f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}" for m in request.history[-6:]
            )
            prompt = f"Previous conversation:\n{history_text}\n\nUser: {request.input}"

        # Phase B — HARNESS mode routes to the DATA-plane invoke_harness instead
        # of invoke_agent_runtime. The harness ARN was persisted on the record;
        # the streamed text comes back as ``output`` -> ``response``.
        if deployment_state and deployment_state.get("deployment_mode") == "harness":
            harness_arn = deployment_state.get("harness_arn", "")
            if not harness_arn:
                return TestResponse(success=False, error="Harness ARN not found for this deployment")
            session_id = request.session_id or runtime_id
            result = invoke_harness(region, harness_arn, prompt, session_id)
            # SECURITY (CodeQL py/stack-trace-exposure): invoke_harness returns
            # raw exception text in `error`; log it server-side and return a
            # generic message rather than leaking internals to the caller.
            harness_ok = bool(result.get("success"))
            if not harness_ok:
                logger.warning("Harness invoke failed: %s", result.get("error"))
            return TestResponse(
                success=harness_ok,
                response=result.get("output", ""),
                error=None if harness_ok else "Harness invocation failed",
                session_id=session_id,
                arn=harness_arn,
            )

        # Get runtime ARN
        runtime_arn = ""
        if deployment_state:
            runtime_arn = deployment_state.get("runtime_arn", "")

        if not runtime_arn:
            # Construct ARN directly from runtime_id instead of calling control plane API
            try:
                sts = boto3.client("sts", region_name=region)
                account_id = sts.get_caller_identity()["Account"]
                runtime_arn = f"arn:aws:bedrock-agentcore:{region}:{account_id}:runtime/{runtime_id}"
                logger.info("Constructed runtime ARN: %s", runtime_arn)
            except Exception as e:
                logger.warning("Could not construct runtime ARN: %s", e)
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot resolve runtime ARN for runtime_id={runtime_id}. Check logs for details.",
                )

        # Validate auth token if gateway was deployed
        if deployment_state:
            gateway_result = deployment_state.get("gateway_result") or {}
            client_info = gateway_result.get("client_info")
            if client_info:
                try:
                    get_cognito_token(client_info)
                except Exception:
                    logger.warning("Could not get Cognito token for gateway auth validation")

        # Bug 178/181: lazy-promote a pending Cedar policy engine LOG_ONLY->ENFORCE.
        # The gateway's policy-authorization plane converges ~3-5 min after deploy;
        # this first-invoke is one natural touchpoint to flip it (the status poll is
        # the other — see _maybe_promote_policy). Best-effort + idempotent.
        _maybe_promote_policy(deployment_state, region)

        # Invoke runtime via boto3 API. Pass session_id BOTH as runtimeSessionId
        # (AgentCore-level routing) and inside payload (so the agent's invoke()
        # body can read it for memory persistence) — see tasks/lessons.md Bug 29:
        # the memory agent reads payload["session_id"], not the AgentCore context.
        try:
            agentcore_client = _create_agentcore_client(region)
            payload_body: dict[str, str] = {"prompt": prompt}
            if request.session_id:
                payload_body["session_id"] = request.session_id
            invoke_params = {
                "agentRuntimeArn": runtime_arn,
                "payload": json.dumps(payload_body),
            }
            if request.session_id:
                invoke_params["runtimeSessionId"] = request.session_id

            resp = agentcore_client.invoke_agent_runtime(**invoke_params)

            # The AgentCore data-plane API returns:
            #   resp['response'] — the agent's response body (str or StreamingBody)
            #   resp['runtimeSessionId'] — session ID for follow-ups
            #   resp['statusCode'] — HTTP status from the agent
            # Legacy path: resp['body'] (streaming body) — fallback only
            raw_response = resp.get("response", "") or resp.get("body", b"")

            # Handle StreamingBody or bytes
            if hasattr(raw_response, "read"):
                raw_response = raw_response.read()
            if isinstance(raw_response, bytes):
                raw_response = raw_response.decode("utf-8", errors="replace")

            session_id = resp.get("runtimeSessionId") or resp.get("sessionId")

            # Parse response
            response_text = _parse_response_body(raw_response)

            return TestResponse(
                success=True,
                response=response_text,
                session_id=session_id,
                arn=runtime_arn,
            )

        except Exception as e:
            error_msg = str(e)
            if "ResourceNotFound" in error_msg:
                return TestResponse(success=False, error="Runtime not found. It may have been deleted.")
            # Bug 157: the sync path uses a ~25s read timeout to stay under API
            # Gateway's 29s hard cap. Tool-heavy agents (>30s) blow past it even
            # though the agent keeps running server-side. Detect the read-timeout
            # and return an actionable message pointing at the streaming Function
            # URL instead of an opaque "Read timeout on endpoint URL" error.
            etype = type(e).__name__
            if (
                "ReadTimeout" in etype
                or "ConnectTimeout" in etype
                or "Read timeout" in error_msg
                or "timed out" in error_msg.lower()
            ):
                return TestResponse(
                    success=False,
                    error=(
                        "Agent still running; exceeded 30s sync test limit — "
                        "use the streaming endpoint for tool-heavy agents that "
                        "take longer than 30 seconds to respond."
                    ),
                )
            # SECURITY (CodeQL py/stack-trace-exposure): don't return the raw
            # exception text to the client — log it, return a generic message.
            logger.warning("Runtime invocation error: %s", error_msg)
            return TestResponse(success=False, error="Runtime invocation failed.")

    except Exception:
        logger.exception("Unexpected error in test-runtime")
        return TestResponse(
            success=False,
            error="An internal error occurred. Check server logs for details.",
        )


# ---------------------------------------------------------------------------
# POST /api/test-runtime-stream  (SSE streaming)
# ---------------------------------------------------------------------------


@deployment_app.post("/api/test-runtime-stream")
async def handle_test_runtime_stream(request: TestRequest):
    """Invoke a deployed runtime and return the response as SSE-formatted text.

    NOTE: API Gateway + Lambda (Mangum) cannot truly stream — the entire
    response is buffered before delivery. We collect the full response and
    format it as SSE events so the frontend can reuse its SSE parser.
    For real streaming, use Lambda Function URLs (future enhancement).
    """
    if request.simulated:
        words = "[Simulated] Mock response - deploy a real agent to test.".split()
        lines = [f"data: {json.dumps({'type': 'token', 'token': w + ' '})}\n\n" for w in words]
        lines.append(f"data: {json.dumps({'type': 'done'})}\n\n")
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse("".join(lines), media_type="text/event-stream")

    runtime_id = request.runtime_id
    if not runtime_id or not re.match(r"^[a-zA-Z0-9_-]+$", runtime_id) or len(runtime_id) > 128:
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(
            f"data: {json.dumps({'type': 'error', 'error': 'Invalid runtime_id'})}\n\n",
            media_type="text/event-stream",
        )

    region = config.aws_region

    # Build prompt
    prompt = request.input
    if request.history:
        history_text = "\n".join(
            f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}" for m in request.history[-6:]
        )
        prompt = f"Previous conversation:\n{history_text}\n\nUser: {request.input}"

    # Resolve runtime ARN
    store = _get_state_store()
    deployment_state = None
    try:
        table = store._table
        deployment_state = _scan_for_runtime(table, runtime_id)
    except Exception:
        pass

    # Bug 190 — HARNESS mode must route to the data-plane invoke_harness, NOT
    # invoke_agent_runtime. The frontend uses THIS streaming route for harness
    # tests too (DeployPanel calls /api/test-runtime-stream regardless of mode),
    # so without this branch a harness test calls invoke_agent_runtime with the
    # HARNESS arn and fails with "No endpoint or agent found with qualifier
    # 'DEFAULT' for agent arn:...:harness/...". Mirror the sync handle_test_runtime
    # harness path (and stream_handler.py's branch) here.
    if deployment_state and deployment_state.get("deployment_mode") == "harness":
        from fastapi.responses import PlainTextResponse

        harness_arn = deployment_state.get("harness_arn", "")
        if not harness_arn:
            return PlainTextResponse(
                f"data: {json.dumps({'type': 'error', 'error': 'Harness ARN not found for this deployment'})}\n\n",
                media_type="text/event-stream",
            )
        result = invoke_harness(region, harness_arn, prompt, request.session_id or runtime_id)
        if not result.get("success"):
            # SECURITY (CodeQL py/stack-trace-exposure): invoke_harness surfaces
            # raw exception text in `error`; never return that to the external
            # SSE client. Log the detail server-side, emit a generic message.
            logger.warning("Harness stream invoke failed: %s", result.get("error"))
            return PlainTextResponse(
                f"data: {json.dumps({'type': 'error', 'error': 'Harness invocation failed'})}\n\n",
                media_type="text/event-stream",
            )
        out = result.get("output", "")
        lines = []
        words = out.split(" ")
        for i, word in enumerate(words):
            token = word + (" " if i < len(words) - 1 else "")
            lines.append(f"data: {json.dumps({'type': 'token', 'token': token})}\n\n")
        lines.append(f"data: {json.dumps({'type': 'done', 'session_id': request.session_id or runtime_id, 'full_response': out})}\n\n")
        return PlainTextResponse("".join(lines), media_type="text/event-stream")

    runtime_arn = (deployment_state or {}).get("runtime_arn", "")
    if not runtime_arn:
        try:
            sts = boto3.client("sts", region_name=region)
            account_id = sts.get_caller_identity()["Account"]
            runtime_arn = f"arn:aws:bedrock-agentcore:{region}:{account_id}:runtime/{runtime_id}"
        except Exception as e:
            logger.exception("Cannot resolve runtime ARN")
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse(
                f"data: {json.dumps({'type': 'error', 'error': 'Cannot resolve runtime ARN'})}\n\n",
                media_type="text/event-stream",
            )

    try:
        agentcore_client = _create_agentcore_client(region)
        invoke_params = {
            "agentRuntimeArn": runtime_arn,
            "payload": json.dumps({"prompt": prompt}),
        }
        if request.session_id:
            invoke_params["runtimeSessionId"] = request.session_id

        resp = agentcore_client.invoke_agent_runtime(**invoke_params)
        session_id = resp.get("runtimeSessionId") or resp.get("sessionId")

        raw_response = resp.get("response", "") or resp.get("body", b"")
        if hasattr(raw_response, "read"):
            raw_response = raw_response.read()
        if isinstance(raw_response, bytes):
            raw_response = raw_response.decode("utf-8", errors="replace")

        parsed = _parse_response_body(str(raw_response))

        # Build SSE events — word-by-word tokens + final done event
        lines = []
        words = parsed.split(" ")
        for i, word in enumerate(words):
            token = word + (" " if i < len(words) - 1 else "")
            lines.append(f"data: {json.dumps({'type': 'token', 'token': token})}\n\n")
        lines.append(f"data: {json.dumps({'type': 'done', 'session_id': session_id, 'full_response': parsed})}\n\n")

        from fastapi.responses import PlainTextResponse
        return PlainTextResponse("".join(lines), media_type="text/event-stream")

    except Exception as e:
        logger.exception("Runtime invocation failed")
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(
            f"data: {json.dumps({'type': 'error', 'error': 'Internal error'})}\n\n",
            media_type="text/event-stream",
        )


def _parse_response_body(body: str) -> str:
    """Parse an invocation response body.

    Handles all AgentCore response formats:
    1. JSON dict with "response" key (primary AgentCore data-plane format)
    2. JSON dict with "body" key (legacy/alternative format)
    3. JSON dict with "output" key (alternative format)
    4. JSON dict with no known keys (fallback to str(dict))
    5. JSON non-dict (return str of parsed value)
    6. SSE stream format (data: prefixed lines)
    7. Plain text fallback
    """
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            # Try known keys in priority order: response > body > output > str(dict)
            for key in ("response", "body", "output"):
                val = data.get(key)
                if val is not None:
                    return str(val) if not isinstance(val, str) else val
            # No known keys — return the whole dict as JSON
            return json.dumps(data)
        return str(data)
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    # Handle SSE stream format
    chunks = []
    for line in body.split("\n"):
        if line.startswith("data: "):
            chunks.append(line[6:])
    if chunks:
        try:
            last = json.loads(chunks[-1])
            return last.get("response", " ".join(chunks))
        except json.JSONDecodeError:
            return " ".join(chunks)

    return body


# ---------------------------------------------------------------------------
# DELETE /api/runtime/{runtime_id}
# ---------------------------------------------------------------------------


def _validate_runtime_id(runtime_id: str) -> str:
    """Validate and sanitize a runtime_id to prevent injection.

    SECURITY: Runtime IDs should be alphanumeric with hyphens only (UUID-like).
    This prevents path traversal or injection via malicious IDs.
    """
    if not runtime_id or len(runtime_id) > 128:
        raise HTTPException(status_code=400, detail="Invalid runtime_id: must be 1-128 characters")
    if not re.match(r"^[a-zA-Z0-9_-]+$", runtime_id):
        raise HTTPException(
            status_code=400,
            detail="Invalid runtime_id: only alphanumeric, hyphens, and underscores allowed",
        )
    return runtime_id


def _validate_deployment_id(deployment_id: str) -> str:
    """Validate and sanitize a deployment_id to prevent injection."""
    if not deployment_id or len(deployment_id) > 128:
        raise HTTPException(status_code=400, detail="Invalid deployment_id: must be 1-128 characters")
    if not re.match(r"^[a-zA-Z0-9_-]+$", deployment_id):
        raise HTTPException(
            status_code=400,
            detail="Invalid deployment_id: only alphanumeric, hyphens, and underscores allowed",
        )
    return deployment_id


def _delete_managed_resource(res: dict, region: str) -> str:
    """Delete one resource from a deployment's created_resources[] manifest.

    Type-dispatched + idempotent (NotFound is treated as success). Returns a
    human log line, or "" for an unknown type (so older/foreign entries no-op
    rather than fail the whole teardown).
    """
    import boto3

    rtype = res.get("type", "")
    rid = res.get("id") or res.get("name") or ""
    rname = res.get("name") or ""
    res_region = res.get("region") or region

    def _gone(e: Exception) -> bool:
        s = str(e)
        # Bug 187 — a ValidationException is NOT proof the resource is gone. In
        # particular delete_gateway on a gateway that still HAS TARGETS raises
        # "...has targets associated with it. Delete all targets before deleting
        # the gateway." Treating that as "already gone" silently ORPHANS the
        # gateway (+ its targets). Only treat genuine not-found shapes as gone;
        # for ValidationException, require it to actually say "not found".
        if "NotFound" in s or "ResourceNotFound" in s or "NoSuchEntity" in s:
            return True
        if "ValidationException" in s and ("not found" in s.lower() or "does not exist" in s.lower()):
            return True
        return False

    try:
        if rtype == "agent_runtime":
            r = destroy_runtime(rid, res_region)
            return f"[manifest] runtime {rid}: {r.get('message', 'deleted')}"
        if rtype == "harness":
            r = destroy_harness(rid, res_region)
            return f"[manifest] harness {rid}: {r.get('note', 'deleted')}"
        if rtype == "memory":
            boto3.client("bedrock-agentcore-control", region_name=res_region).delete_memory(memoryId=rid)
            return f"[manifest] memory {rid} deleted"
        if rtype == "gateway":
            # Bug 187 — delete_gateway FAILS if the gateway still has targets
            # ("...has targets associated with it. Delete all targets before
            # deleting the gateway."). Delete every target first, then the
            # gateway. Without this the teardown leaked the gateway + targets on
            # EVERY gateway-bearing deployment (the error was mis-swallowed as
            # "already gone" — see _gone). Best-effort per target so one stuck
            # target doesn't block the rest.
            _ctrl = boto3.client("bedrock-agentcore-control", region_name=res_region)
            try:
                _tgts = _ctrl.list_gateway_targets(gatewayIdentifier=rid).get("items", [])
            except Exception:  # noqa: BLE001
                _tgts = []
            for _t in _tgts:
                _tid = _t.get("targetId")
                if not _tid:
                    continue
                try:
                    _ctrl.delete_gateway_target(gatewayIdentifier=rid, targetId=_tid)
                except Exception as _te:  # noqa: BLE001
                    if not _gone(_te):
                        logger.warning("gateway %s target %s delete: %s", rid, _tid, str(_te)[:160])
            # Targets delete asynchronously; delete_gateway rejects while any
            # remain. Retry the gateway delete briefly so target teardown can
            # propagate (typically a few seconds).
            for _attempt in range(8):
                try:
                    _ctrl.delete_gateway(gatewayIdentifier=rid)
                    break
                except Exception as _ge:  # noqa: BLE001
                    if _gone(_ge):
                        break
                    if "target" in str(_ge).lower() and _attempt < 7:
                        time.sleep(5)
                        continue
                    raise
            return f"[manifest] gateway {rid} deleted"
        if rtype == "oauth2_credential_provider":
            boto3.client("bedrock-agentcore-control", region_name=res_region).delete_oauth2_credential_provider(name=rname or rid)
            return f"[manifest] oauth2 provider {rname or rid} deleted"
        if rtype == "api_key_credential_provider":
            boto3.client("bedrock-agentcore-control", region_name=res_region).delete_api_key_credential_provider(name=rname or rid)
            return f"[manifest] apikey provider {rname or rid} deleted"
        if rtype == "secret":
            boto3.client("secretsmanager", region_name=res_region).delete_secret(
                SecretId=rid, ForceDeleteWithoutRecovery=True
            )
            return f"[manifest] secret {rid} deleted"
        if rtype == "s3_object":
            # rid is an s3://bucket/key URI for a staged connector OpenAPI spec.
            if rid.startswith("s3://"):
                _b, _, _k = rid[5:].partition("/")
                if _b and _k:
                    boto3.client("s3", region_name=res_region).delete_object(Bucket=_b, Key=_k)
            return f"[manifest] s3 object {rid} deleted"
        if rtype == "iam_role":
            iam = boto3.client("iam")
            for pn in iam.list_role_policies(RoleName=rname or rid).get("PolicyNames", []):
                iam.delete_role_policy(RoleName=rname or rid, PolicyName=pn)
            for ap in iam.list_attached_role_policies(RoleName=rname or rid).get("AttachedPolicies", []):
                iam.detach_role_policy(RoleName=rname or rid, PolicyArn=ap["PolicyArn"])
            iam.delete_role(RoleName=rname or rid)
            return f"[manifest] iam role {rname or rid} deleted"
        if rtype == "lambda":
            boto3.client("lambda", region_name=res_region).delete_function(FunctionName=rname or rid)
            return f"[manifest] lambda {rname or rid} deleted"
        if rtype == "policy_engine":
            ctrl = boto3.client("bedrock-agentcore-control", region_name=res_region)
            # Bug 179: delete ALL child policies and WAIT until the engine reports
            # zero, THEN delete the engine. delete_policy is async + list_policies
            # is eventually-consistent, so a single pass races
            # delete_policy_engine ("Policy engine still contains N policies").
            # The lazy-promote (Bug 178) can also add policies after deploy, so we
            # re-list each round. Poll a few rounds before the engine delete.
            for _round in range(8):
                remaining = 0
                try:
                    listed = ctrl.list_policies(policyEngineId=rid, maxResults=100)
                    pols = listed.get("policies", listed.get("items", []))
                    remaining = len(pols)
                    for p in pols:
                        pid = p.get("policyId") or p.get("id")
                        if pid:
                            try:
                                ctrl.delete_policy(policyEngineId=rid, policyId=pid)
                            except Exception:  # noqa: BLE001
                                pass
                except Exception:  # noqa: BLE001
                    pass
                if remaining == 0:
                    break
                time.sleep(5)
            # Retry the engine delete across the "still contains N policies" lag.
            for _attempt in range(6):
                try:
                    ctrl.delete_policy_engine(policyEngineId=rid)
                    break
                except Exception as pe:  # noqa: BLE001
                    if "still contains" in str(pe) and _attempt < 5:
                        time.sleep(5)
                        continue
                    raise
            return f"[manifest] policy engine {rid} deleted"
        if rtype == "guardrail":
            boto3.client("bedrock", region_name=res_region).delete_guardrail(guardrailIdentifier=rid)
            return f"[manifest] guardrail {rid} deleted"
        if rtype == "knowledge_base":
            # Bug 167: delete the KB and WAIT for it to reach a terminal deleted
            # state BEFORE the manifest reclaims the s3_vectors_bucket + KB role
            # (priority ordering guarantees this type runs first). Deleting a KB
            # with dataDeletionPolicy=DELETE makes Bedrock delete the underlying
            # vector data, which needs the store + a role it can assume — both
            # must still exist at KB-delete time.
            ba = boto3.client("bedrock-agent", region_name=res_region)
            try:
                for ds in ba.list_data_sources(knowledgeBaseId=rid).get(
                    "dataSourceSummaries", []
                ):
                    try:
                        ba.delete_data_source(
                            knowledgeBaseId=rid, dataSourceId=ds["dataSourceId"]
                        )
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass
            ba.delete_knowledge_base(knowledgeBaseId=rid)
            # Poll to a terminal deleted state (ResourceNotFound) so the
            # downstream bucket/role deletes don't race the cascade.
            for _ in range(24):  # ~2 min
                try:
                    ba.get_knowledge_base(knowledgeBaseId=rid)
                except Exception as ge:  # noqa: BLE001
                    if _gone(ge):
                        break
                time.sleep(5)
            return f"[manifest] knowledge base {rid} deleted"
        if rtype == "s3_vectors_bucket":
            # Auto-provisioned S3 Vectors bucket backing a managed KB (Bug 145).
            # Indexes must be deleted before the bucket.
            s3v = boto3.client("s3vectors", region_name=res_region)
            bname = rname or rid
            try:
                for ix in s3v.list_indexes(vectorBucketName=bname).get("indexes", []):
                    try:
                        s3v.delete_index(vectorBucketName=bname, indexName=ix["indexName"])
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass
            s3v.delete_vector_bucket(vectorBucketName=bname)
            return f"[manifest] s3 vectors bucket {bname} deleted"
        if rtype == "cognito_user_pool":
            cog = boto3.client("cognito-idp", region_name=res_region)
            # Bug 175: a user pool with a configured domain CANNOT be deleted until
            # the domain is gone ("User pool cannot be deleted. It has a domain
            # configured that should be deleted first."). delete_user_pool_domain
            # is async, so we delete it then POLL until describe_user_pool shows no
            # Domain before deleting the pool — otherwise the pool delete races the
            # domain teardown and orphans the pool.
            try:
                dom = cog.describe_user_pool(UserPoolId=rid).get("UserPool", {}).get("Domain")
                if dom:
                    cog.delete_user_pool_domain(UserPoolId=rid, Domain=dom)
                    for _ in range(12):  # ~1 min
                        try:
                            still = cog.describe_user_pool(UserPoolId=rid).get("UserPool", {}).get("Domain")
                        except Exception:  # noqa: BLE001
                            still = None
                        if not still:
                            break
                        time.sleep(5)
            except Exception:  # noqa: BLE001
                pass
            # Delete the pool, retrying briefly if the domain teardown is still
            # settling (the same InvalidParameter "has a domain" can lag).
            for _attempt in range(6):
                try:
                    cog.delete_user_pool(UserPoolId=rid)
                    break
                except Exception as ce:  # noqa: BLE001
                    if "domain" in str(ce).lower() and _attempt < 5:
                        time.sleep(5)
                        continue
                    raise
            return f"[manifest] cognito pool {rid} deleted"
        # Unknown type: no-op (do not fail teardown).
        return ""
    except Exception as e:  # noqa: BLE001
        if _gone(e):
            return f"[manifest] {rtype} {rid or rname} already gone"
        raise


@deployment_app.delete(
    "/api/runtime/{runtime_id}",
    response_model=DeleteResponse,
    response_model_by_alias=True,
)
async def handle_delete_runtime(runtime_id: str, raw_request: Request) -> DeleteResponse:
    """Delete a runtime and clean up all associated resources. Caller must own it."""
    runtime_id = _validate_runtime_id(runtime_id)
    cleanup_messages: list[str] = []
    # Audit #11 (tasks/lessons.md Bug 106): track per-step cleanup failures.
    # Bug 44 only flipped the success flag for runtime-destroy; gateway / KB /
    # memory / guardrail / policy-engine / mcp-server cleanups still swallowed
    # exceptions silently into cleanup_messages while returning success=True.
    # Now: any failure here flips overall_success to False so the caller gets
    # an honest signal that resources may have leaked.
    cleanup_failures: list[str] = []
    region = config.aws_region
    caller_sub = _get_user_id(raw_request)

    # Look up deployment state for gateway config.
    # Try by runtime_id first, then fall back to deployment_id (covers partial failures
    # where the agent runtime was never created but gateway/MCP server were).
    gateway_config = None
    deployment_record = None
    try:
        store = _get_state_store()
        table = store._table
        deployment_record = _scan_for_runtime(table, runtime_id)
        if not deployment_record:
            # runtime_id might actually be a deployment_id (frontend fallback)
            direct = store.get(runtime_id)
            if direct:
                deployment_record = direct.model_dump(mode="json")
        if deployment_record:
            gateway_result = deployment_record.get("gateway_result")
            if gateway_result:
                gateway_config = gateway_result
            # Tenant isolation: caller must own the deployment.
            # See tasks/lessons.md Bug 37.
            owner = deployment_record.get("user_id")
            if owner and owner != caller_sub:
                raise HTTPException(status_code=404, detail="Runtime not found")
    except HTTPException:
        raise
    except Exception as exc:
        cleanup_messages.append(f"State lookup error: {exc}")

    # Step 0a: GENERIC manifest-driven teardown. Iterate created_resources[] and
    # delete every recorded sub-resource by type. This is the primary teardown
    # path that makes cleanup complete-by-construction (no orphans when a new
    # component is added). The per-component *_result cleanups below remain as a
    # fallback for older records that predate the manifest, and are idempotent
    # against anything this loop already deleted.
    # When a manifest is present it is AUTHORITATIVE: it lists every created
    # sub-resource, so the legacy per-component *_result fallbacks below are
    # SKIPPED. Running them after the manifest would re-issue deletes against
    # already-gone resources and count the resulting ResourceNotFoundExceptions
    # as cleanup failures — a false-negative success:false even though teardown
    # fully succeeded (observed live in the free-form matrix). Old records that
    # predate the manifest have no created_resources and still use the fallbacks.
    manifest_used = bool(deployment_record and deployment_record.get("created_resources"))
    if manifest_used:
        seen_mres: set = set()
        # Bug 167: tear down in DEPENDENCY order. A "primary" resource whose
        # delete cascades into a backing store or needs its exec role (KB ->
        # S3 Vectors store + role; runtime/gateway -> their child resources)
        # MUST be deleted (and reach a terminal state) BEFORE the secondaries it
        # depends on. Lower priority number = deleted earlier. Unlisted types
        # default to the middle band, then backing-stores/roles/secrets last.
        _DELETE_PRIORITY = {
            "knowledge_base": 0,
            "agent_runtime": 1,
            "harness": 1,
            "gateway": 2,
            "policy_engine": 2,
            "lambda": 5,
            "memory": 6,
            "guardrail": 6,
            "oauth2_credential_provider": 7,
            "api_key_credential_provider": 7,
            # Secondaries that must OUTLIVE the primaries above:
            "s3_vectors_bucket": 8,
            "iam_role": 9,
            "cognito_user_pool": 9,
            "secret": 9,
            "s3_object": 9,
        }
        _ordered = sorted(
            deployment_record.get("created_resources") or [],
            key=lambda r: _DELETE_PRIORITY.get(str(r.get("type")), 4),
        )
        for _res in _ordered:
            try:
                key = (str(_res.get("type")), str(_res.get("id") or _res.get("name")))
                if key in seen_mres:
                    continue
                seen_mres.add(key)
                _msg = _delete_managed_resource(_res, region)
                if _msg:
                    cleanup_messages.append(_msg)
            except Exception as exc:  # noqa: BLE001
                cleanup_messages.append(f"Manifest cleanup error ({_res.get('type')}): {exc}")
                cleanup_failures.append(str(_res.get("type")))

    # Step 0: Clean up MCP server runtime if one was deployed
    # (legacy fallback — skipped when the manifest already handled teardown)
    mcp_server_runtime_id = deployment_record.get("mcp_server_runtime_id") if deployment_record else None
    if mcp_server_runtime_id and not manifest_used:
        try:
            mcp_destroy = destroy_runtime(mcp_server_runtime_id, region)
            cleanup_messages.append(f"MCP server runtime destroyed: {mcp_destroy.get('message', 'ok')}")
            # destroy_runtime() can return success:false without raising
            # (AccessDenied path) — surface that as a cleanup failure too.
            if not mcp_destroy.get("success", True):
                cleanup_failures.append("mcp_server_runtime")
        except Exception as exc:
            cleanup_messages.append(f"MCP server runtime cleanup error: {exc}")
            cleanup_failures.append("mcp_server_runtime")

    # Step 0.5: Clean up policy engine if one was attached
    # Correct order: detach from gateway → delete policies → delete engine
    if deployment_record and not manifest_used:
        policy_result = deployment_record.get("policy_result") or {}
        policy_engine_id = policy_result.get("engine_id")
        if policy_engine_id:
            try:
                agentcore_ctrl = boto3.client("bedrock-agentcore-control", region_name=region)

                # 1. Detach engine from gateway
                gw_result = deployment_record.get("gateway_result") or {}
                gw_id = gw_result.get("gateway_id")
                if gw_id:
                    try:
                        gw_detail = agentcore_ctrl.get_gateway(gatewayIdentifier=gw_id)
                        # UpdateGateway requires gatewayIdentifier, name,
                        # roleArn, authorizerType. Detach the policy engine
                        # by re-issuing the update WITHOUT
                        # policyEngineConfiguration; preserve every other
                        # field we got back from get_gateway so we don't
                        # accidentally clear unrelated config.
                        update_params = {
                            "gatewayIdentifier": gw_id,
                            "name": gw_detail.get("name", ""),
                            "roleArn": gw_detail.get("roleArn", ""),
                            "authorizerType": gw_detail.get("authorizerType", "CUSTOM_JWT"),
                            "protocolType": gw_detail.get("protocolType", "MCP"),
                        }
                        for optional_field in (
                            "description",
                            "authorizerConfiguration",
                            "protocolConfiguration",
                            "kmsKeyArn",
                        ):
                            if gw_detail.get(optional_field):
                                update_params[optional_field] = gw_detail[optional_field]
                        agentcore_ctrl.update_gateway(**update_params)
                        cleanup_messages.append(f"Policy engine detached from gateway {gw_id}")
                        # Wait for gateway to stabilize
                        for _ in range(12):
                            gw = agentcore_ctrl.get_gateway(gatewayIdentifier=gw_id)
                            if gw.get("status") == "READY":
                                break
                            time.sleep(5)
                    except Exception as detach_exc:
                        cleanup_messages.append(f"Policy engine detach warning: {detach_exc}")

                # 2. Delete all policies attached to the engine
                try:
                    policies_resp = agentcore_ctrl.list_policies(policyEngineId=policy_engine_id)
                    for pol in policies_resp.get("policies", policies_resp.get("items", [])):
                        pol_id = pol.get("policyId")
                        if pol_id:
                            agentcore_ctrl.delete_policy(policyEngineId=policy_engine_id, policyId=pol_id)
                            cleanup_messages.append(f"Policy deleted: {pol_id}")
                    # Wait for policy deletions to propagate
                    time.sleep(5)
                except Exception as pol_exc:
                    cleanup_messages.append(f"Policy deletion warning: {pol_exc}")

                # 3. Delete the engine itself
                agentcore_ctrl.delete_policy_engine(policyEngineId=policy_engine_id)
                cleanup_messages.append(f"Policy engine deleted: {policy_engine_id}")
            except Exception as exc:
                cleanup_messages.append(f"Policy engine cleanup error: {exc}")
                cleanup_failures.append("policy_engine")

    # Step 0.6: Clean up memory if one was created
    if deployment_record and not manifest_used:
        memory_result = deployment_record.get("memory_result") or {}
        memory_id = memory_result.get("memory_id")
        if memory_id:
            try:
                agentcore_ctrl = boto3.client("bedrock-agentcore-control", region_name=region)
                agentcore_ctrl.delete_memory(memoryId=memory_id)
                cleanup_messages.append(f"Memory deleted: {memory_id}")
            except Exception as exc:
                cleanup_messages.append(f"Memory cleanup error: {exc}")
                cleanup_failures.append("memory")
            # Bug 158: memory_step creates an AgentCoreMemory-<name> IAM role; the
            # delete path deleted the memory but ORPHANED the role (confirmed live).
            # Delete it too (best-effort, idempotent).
            memory_name = memory_result.get("memory_name")
            if memory_name:
                mem_role_name = f"AgentCoreMemory-{memory_name}"
                try:
                    iam_c = boto3.client("iam")
                    for pn in iam_c.list_role_policies(RoleName=mem_role_name).get("PolicyNames", []):
                        iam_c.delete_role_policy(RoleName=mem_role_name, PolicyName=pn)
                    iam_c.delete_role(RoleName=mem_role_name)
                    cleanup_messages.append(f"Memory IAM role deleted: {mem_role_name}")
                except Exception as exc:
                    if "NoSuchEntity" not in str(exc):
                        cleanup_messages.append(f"Memory role cleanup error: {exc}")

    # Step 0.7: Clean up guardrail if we created it
    if deployment_record and not manifest_used:
        guardrails_result = deployment_record.get("guardrails_result") or {}
        if guardrails_result.get("created_by_flow"):
            guardrail_id = guardrails_result.get("guardrail_id")
            if guardrail_id:
                try:
                    bedrock_client = boto3.client("bedrock", region_name=region)
                    bedrock_client.delete_guardrail(guardrailIdentifier=guardrail_id)
                    cleanup_messages.append(f"Guardrail deleted: {guardrail_id}")
                except Exception as exc:
                    cleanup_messages.append(f"Guardrail cleanup error: {exc}")
                    cleanup_failures.append("guardrail")

    # Step 1: Clean up gateway resources
    if gateway_config and not manifest_used:
        try:
            gw_log = cleanup_gateway_resources(
                runtime_id=runtime_id,
                region=region,
                gateway_config=gateway_config,
            )
            cleanup_messages.extend(gw_log)
            # cleanup_gateway_resources() never raises but reports per-resource
            # errors as " ... error:" lines in its log. Treat any of those as
            # a cleanup failure so we don't return success=True when a target
            # / pool / Lambda was actually leaked.
            if any(" error:" in line or " error " in line for line in gw_log):
                cleanup_failures.append("gateway")
        except Exception as exc:
            cleanup_messages.append(f"Gateway cleanup error: {exc}")
            cleanup_failures.append("gateway")

    # Step 1.5: Clean up Knowledge Base resources
    kb_result = (deployment_record or {}).get("knowledge_base_result") or {}
    dep_id = (deployment_record or {}).get("deployment_id", "")
    kb_lambda_suffix = dep_id[:8] if dep_id else ""
    if kb_lambda_suffix and not manifest_used:
        try:
            lambda_client = boto3.client("lambda", region_name=region)
            kb_fn_name = f"AgentCore-KBTool-{kb_lambda_suffix}"
            try:
                lambda_client.delete_function(FunctionName=kb_fn_name)
                cleanup_messages.append(f"KB Lambda deleted: {kb_fn_name}")
            except Exception:
                pass  # May not exist
            iam_client = boto3.client("iam")
            kb_role_name = f"AgentCoreKBToolRole-{kb_lambda_suffix}"
            try:
                # Detach policies and delete role
                for policy in iam_client.list_attached_role_policies(RoleName=kb_role_name).get("AttachedPolicies", []):
                    iam_client.detach_role_policy(RoleName=kb_role_name, PolicyArn=policy["PolicyArn"])
                for policy_name in iam_client.list_role_policies(RoleName=kb_role_name).get("PolicyNames", []):
                    iam_client.delete_role_policy(RoleName=kb_role_name, PolicyName=policy_name)
                iam_client.delete_role(RoleName=kb_role_name)
                cleanup_messages.append(f"KB Lambda role deleted: {kb_role_name}")
            except Exception:
                pass  # May not exist
        except Exception as exc:
            cleanup_messages.append(f"KB Lambda cleanup error: {exc}")
            cleanup_failures.append("kb_lambda")
    # Delete Knowledge Base if we created it
    if kb_result.get("created_by_flow"):
        try:
            bedrock_agent = boto3.client("bedrock-agent", region_name=region)
            kb_id = kb_result.get("kb_id")
            ds_id = kb_result.get("data_source_id")
            if ds_id and kb_id:
                bedrock_agent.delete_data_source(knowledgeBaseId=kb_id, dataSourceId=ds_id)
            if kb_id:
                bedrock_agent.delete_knowledge_base(knowledgeBaseId=kb_id)
                cleanup_messages.append(f"Knowledge Base deleted: {kb_id}")
            # Delete KB IAM role
            kb_role_arn = kb_result.get("kb_role_arn", "")
            if kb_role_arn:
                kb_iam_role_name = kb_role_arn.split("/")[-1] if "/" in kb_role_arn else ""
                if kb_iam_role_name:
                    iam_c = boto3.client("iam")
                    for p in iam_c.list_attached_role_policies(RoleName=kb_iam_role_name).get("AttachedPolicies", []):
                        iam_c.detach_role_policy(RoleName=kb_iam_role_name, PolicyArn=p["PolicyArn"])
                    for pn in iam_c.list_role_policies(RoleName=kb_iam_role_name).get("PolicyNames", []):
                        iam_c.delete_role_policy(RoleName=kb_iam_role_name, PolicyName=pn)
                    iam_c.delete_role(RoleName=kb_iam_role_name)
                    cleanup_messages.append(f"KB IAM role deleted: {kb_iam_role_name}")
        except Exception as exc:
            cleanup_messages.append(f"KB cleanup error: {exc}")
            cleanup_failures.append("knowledge_base")

    # Step 2: Destroy the runtime via boto3 — or the Harness (Phase B). HARNESS
    # mode targets the managed harness instead of an AgentCore Runtime; the
    # harness id was persisted on the record (fall back to runtime_id, which the
    # frontend sends as the deployment id for harness deploys).
    runtime_destroy_failed = False
    if deployment_record and deployment_record.get("deployment_mode") == "harness":
        try:
            harness_id = deployment_record.get("harness_id") or runtime_id
            destroy_result = destroy_harness(harness_id, region)
            cleanup_messages.append(
                f"Harness destroy: {destroy_result.get('note', destroy_result.get('harness_id', 'ok'))}"
            )
            if not destroy_result.get("success", True) and not manifest_used:
                runtime_destroy_failed = True
            # Tear down the OAuth2 credential provider registered so the harness
            # could call its connected gateway (no orphan).
            _hr = deployment_record.get("harness_result") or {}
            _gw_prov = _hr.get("gateway_outbound_provider_name") if isinstance(_hr, dict) else None
            if _gw_prov:
                try:
                    import boto3 as _boto3

                    _boto3.client(
                        "bedrock-agentcore-control", region_name=region
                    ).delete_oauth2_credential_provider(name=_gw_prov)
                    cleanup_messages.append(f"Harness gateway OAuth provider {_gw_prov} deleted")
                except Exception as _e:  # noqa: BLE001
                    if "ResourceNotFound" not in str(_e) and "NotFound" not in str(_e):
                        cleanup_messages.append(f"Harness gateway OAuth provider delete error: {_e}")
        except Exception:
            logger.exception("Harness destroy error for %s", runtime_id)
            cleanup_messages.append("Harness destroy error (check server logs)")
            if not manifest_used:
                runtime_destroy_failed = True
    else:
        try:
            destroy_result = destroy_runtime(runtime_id, region)
            cleanup_messages.append(destroy_result.get("message", "Runtime destroy completed"))
            # destroy_runtime returns success:false on AccessDenied / other errors;
            # propagate that to the top-level response. See tasks/lessons.md Bug 44
            # — previously this handler returned success:true even when the
            # underlying destroy failed, masking IAM and AccessDenied errors.
            # EXCEPTION (Bug 159): when the manifest already deleted the
            # agent_runtime in Step 0a, this second destroy can see the runtime
            # already-gone OR mid-state-transition and report success:false — that
            # is NOT a real failure (the resource is gone). Only count it when the
            # manifest did NOT handle the runtime.
            if not destroy_result.get("success", True) and not manifest_used:
                runtime_destroy_failed = True
        except Exception:
            logger.exception("Runtime destroy error for %s", runtime_id)
            cleanup_messages.append("Runtime destroy error (check server logs)")
            if not manifest_used:
                runtime_destroy_failed = True

    # Bug 192 — release the runtime NAME so it can be redeployed. The slots +
    # versions rows (AgentVersionsTable / RuntimeSlotsTable) are the cross-tenant
    # name lock used by the deploy guard (H-1). If teardown leaves them behind, the
    # friendly name stays permanently locked even after the AWS resource is gone,
    # and a later deploy of the same name fails with 409 "already in use by another
    # tenant" — exactly what a customer hit after a prior harness was torn down.
    # Delete ONLY rows owned by this caller (tenant-safe) for the friendly name on
    # the deployment record. Best-effort: never fail the teardown on this.
    try:
        friendly = None
        if deployment_record:
            friendly = (
                deployment_record.get("friendly_runtime_name")
                or deployment_record.get("workflow_id")
            )
            # Fall back to deriving from the agentcore runtime name (strip _<suffix>).
            if not friendly:
                acn = deployment_record.get("agentcore_runtime_name") or ""
                friendly = acn.rsplit("_", 1)[0] if "_" in acn else acn
        if friendly:
            from app.services.agent_versions_store import (
                get_slots_store,
                get_versions_store,
            )

            vstore = get_versions_store()
            removed_versions = 0
            for v in vstore.list_for_runtime(friendly):
                # Only the owner may release the name (and pre-tenancy rows with no
                # owner_sub are safe to clean up).
                if not v.owner_sub or v.owner_sub == (caller_sub or ""):
                    vstore.delete(friendly, v.version_id)
                    removed_versions += 1
            sstore = get_slots_store()
            slot = sstore.get(friendly)
            if slot is not None and (not slot.owner_sub or slot.owner_sub == (caller_sub or "")):
                sstore.delete(friendly)
            if removed_versions or slot is not None:
                cleanup_messages.append(f"Released runtime name '{friendly}' (slots/versions)")
    except Exception:  # noqa: BLE001
        logger.warning("Slots/versions release failed for %s", runtime_id, exc_info=True)
        cleanup_messages.append("Runtime name release skipped (check server logs)")

    # Audit #11 (tasks/lessons.md Bug 106): overall_success is False if either
    # runtime-destroy failed (Bug 44) OR any other cleanup step (gateway, KB,
    # memory, guardrail, policy engine, MCP server) leaked. Failed steps are
    # listed in the response message so the caller can act on the leak.
    overall_success = not runtime_destroy_failed and not cleanup_failures
    if cleanup_failures:
        cleanup_messages.append(
            f"Cleanup failures in: {', '.join(sorted(set(cleanup_failures)))}"
        )
    summary = "; ".join(cleanup_messages) if cleanup_messages else "Cleanup completed"
    return DeleteResponse(success=overall_success, message=summary)


# ---------------------------------------------------------------------------
# POST /api/generate-tool
# ---------------------------------------------------------------------------


@deployment_app.post("/api/generate-tool")
async def handle_generate_tool(request: ToolGenerateRequest):
    """Generate a Lambda tool using Claude Sonnet on Bedrock.

    Clarification mode (first message, no history): synchronous (<5s).
    Generation mode (has history): async self-invoke + polling to avoid
    API Gateway's 30s hard timeout (Sonnet generation takes 40-60s).
    """
    try:
        has_prior_context = bool(request.conversation_history) or request.existing_tool is not None

        if not has_prior_context:
            # Clarification mode — fast, stays synchronous
            result = generate_tool(
                prompt=request.prompt,
                conversation_history=request.conversation_history,
                existing_tool=request.existing_tool,
                region=config.aws_region,
            )
            return ToolGenerateResponse(**result)

        # Generation mode — async to avoid 30s API Gateway timeout
        job_id = f"gen-{uuid.uuid4().hex[:12]}"

        table = _get_deploy_table()
        table.put_item(
            Item={
                "deployment_id": job_id,
                "status": "running",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        lambda_client = boto3.client("lambda", region_name=config.aws_region)
        function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
        lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=json.dumps(
                {
                    "_async_generate": True,
                    "job_id": job_id,
                    "prompt": request.prompt,
                    "conversation_history": request.conversation_history,
                    "existing_tool": request.existing_tool,
                    "region": config.aws_region,
                }
            ).encode(),
        )

        return {"jobId": job_id, "status": "running"}
    except Exception as e:
        # Catch-all so the client gets structured JSON instead of plaintext
        # "Internal Server Error" 500. See tasks/lessons.md Bug 33.
        # SECURITY (CodeQL py/stack-trace-exposure): log the exception detail
        # server-side; return a generic message (no exception type/text) to the
        # client.
        logger.exception("handle_generate_tool failed")
        raise HTTPException(
            status_code=500,
            detail={"error": "Tool generation failed."},
        ) from e


@deployment_app.get("/api/generate-tool/{job_id}")
async def handle_get_generate_result(job_id: str):
    """Poll for async tool generation results."""
    if not re.match(r"^[a-zA-Z0-9_-]+$", job_id) or len(job_id) > 256:
        raise HTTPException(status_code=400, detail="Invalid job_id format")
    table = _get_deploy_table()
    try:
        item = table.get_item(Key={"deployment_id": job_id}).get("Item")
    except Exception as exc:
        logger.warning("Failed to get generate result for job_id=%s: %s", job_id, exc)
        item = None

    if not item:
        raise HTTPException(status_code=404, detail="Job not found")

    status = item.get("status", "running")
    if status == "running":
        return {"jobId": job_id, "status": "running"}

    # Completed — return full results
    tool_json = item.get("tool_json")
    test_cases_json = item.get("test_cases_json")
    return {
        "jobId": job_id,
        "status": "completed",
        "success": item.get("success", False),
        "tool": json.loads(tool_json) if tool_json else None,
        "message": item.get("message", ""),
        "error": item.get("error"),
        "responseType": item.get("response_type", "generation"),
        "testCases": json.loads(test_cases_json) if test_cases_json else [],
    }


# ---------------------------------------------------------------------------
# POST /api/test-tool
# ---------------------------------------------------------------------------


def _get_deploy_table():
    """Get the DynamoDB deployments table resource."""
    dynamodb = boto3.resource("dynamodb", region_name=config.aws_region)
    return dynamodb.Table(DEPLOYMENT_TABLE_NAME)


@deployment_app.post("/api/test-tool")
async def handle_test_tool(request: ToolTestRequest):
    """Start an async tool test. Returns a testId for polling.

    The actual test runs in an async Lambda invocation to avoid the
    API Gateway 30s timeout. Poll GET /api/test-tool/{testId} for results.
    """
    try:
        test_id = f"test-{uuid.uuid4().hex[:12]}"

        # Store initial "running" state
        table = _get_deploy_table()
        table.put_item(
            Item={
                "deployment_id": test_id,
                "status": "running",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        # Async invoke self to run the test (InvocationType=Event returns immediately)
        lambda_client = boto3.client("lambda", region_name=config.aws_region)
        function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
        lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=json.dumps(
                {
                    "_async_test": True,
                    "test_id": test_id,
                    "lambda_code": request.lambda_code,
                    "test_cases": [tc.model_dump(by_alias=True) for tc in request.test_cases],
                    "region": config.aws_region,
                }
            ).encode(),
        )

        return {"testId": test_id, "status": "running"}
    except Exception as e:
        # Catch-all so the client gets structured JSON instead of plaintext
        # "Internal Server Error" 500. See tasks/lessons.md Bug 33.
        logger.exception("handle_test_tool failed")
        raise HTTPException(
            status_code=500,
            detail={"error": f"Tool test failed: {type(e).__name__}: {e}"},
        ) from e


@deployment_app.get("/api/test-tool/{test_id}")
async def handle_get_test_result(test_id: str):
    """Poll for async test results."""
    if not re.match(r"^[a-zA-Z0-9_-]+$", test_id) or len(test_id) > 256:
        raise HTTPException(status_code=400, detail="Invalid test_id format")
    table = _get_deploy_table()
    try:
        item = table.get_item(Key={"deployment_id": test_id}).get("Item")
    except Exception as exc:
        logger.warning("Failed to get test result for test_id=%s: %s", test_id, exc)
        item = None

    if not item:
        raise HTTPException(status_code=404, detail="Test not found")

    status = item.get("status", "running")
    if status == "running":
        return {"testId": test_id, "status": "running"}

    # Test completed — return full results
    return {
        "testId": test_id,
        "status": "completed",
        "success": item.get("success", False),
        "allPassed": item.get("all_passed", False),
        "results": json.loads(item.get("results_json", "[]")),
        "error": item.get("error"),
    }


# ---------------------------------------------------------------------------
# POST /api/generate-canvas — Phase 1 Gap 1E (NL agent generator)
# ---------------------------------------------------------------------------


@deployment_app.post("/api/generate-canvas", response_model=AgentGenerateResponse, response_model_by_alias=True)
async def handle_generate_canvas(
    request: AgentGenerateRequest, raw_request: Request
) -> AgentGenerateResponse:
    """Generate an AgentCore canvas spec from a natural-language description.

    Two-turn pattern (mirrors /api/generate-tool):
    - First call (empty ``conversationHistory``): returns a clarification
      message asking 2-4 questions about KB sources, memory, tools, etc.
    - Subsequent calls (history populated): emits a canvas spec via Bedrock
      tool-use, validated against the structural rules in the generator's
      prompt. Up to 3 generation attempts with self-correcting validation
      errors fed back into the next turn.

    The returned ``spec`` is shaped like a frontend WorkflowTemplate (subset
    used by ``instantiateTemplate``) so the UI can drop it onto the canvas
    via the existing template-instantiation flow.

    SECURITY (M-1, security review 2026-05-28): every invocation hits
    Bedrock Converse (~$0.06 per call). API GW throttling is the only
    rate limit, so we record caller_sub at INFO so abuse is attributable
    after the fact and surfaces in CloudWatch Insights queries against the
    deployment Lambda log group.
    """
    user_id = _get_user_id(raw_request) or "<no-sub>"
    logger.info(
        "generate-canvas invoked by sub=%s prompt_len=%d history_len=%d",
        user_id,
        len(request.prompt or ""),
        len(request.conversation_history or []),
    )
    try:
        from app.services.agent_generator import generate_canvas as _generate

        result = _generate(
            prompt=request.prompt,
            conversation_history=request.conversation_history or [],
            region=config.aws_region,
        )
        return AgentGenerateResponse(
            success=bool(result.get("success")),
            response_type=result.get("responseType", "spec"),
            message=result.get("message"),
            spec=result.get("spec"),
            error=result.get("error"),
        )
    except Exception as exc:
        logger.exception("generate-canvas failed (sub=%s)", user_id)
        # Don't leak internal error detail to the client.
        raise HTTPException(
            status_code=500,
            detail={"error": "Canvas generation failed. Check server logs."},
        ) from exc


# ---------------------------------------------------------------------------
# POST /api/generate-cfn-template
# ---------------------------------------------------------------------------


@deployment_app.post("/api/generate-cfn-template")
async def handle_generate_cfn_template(request: DeployRequest):
    """Generate a downloadable CloudFormation template bundle.

    Returns a presigned S3 URL to download the zip, or the zip bytes
    directly if no S3 bucket is configured.
    """
    try:
        from app.services.cfn_template_generator import CfnTemplateGenerator

        generator = CfnTemplateGenerator()
        bundle = generator.generate(request)
        zip_bytes = bundle.to_zip()

        # Try to upload to S3 and return presigned URL
        bucket = os.environ.get("ARTIFACTS_BUCKET_NAME", "")
        if bucket:
            s3_client = boto3.client("s3", region_name=config.aws_region)
            s3_key = f"cfn-templates/{bundle.deployment_name}-{uuid.uuid4().hex[:8]}.zip"
            s3_client.put_object(Bucket=bucket, Key=s3_key, Body=zip_bytes)

            url = s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": s3_key},
                ExpiresIn=3600,
            )
            return {"download_url": url, "filename": f"{bundle.deployment_name}-cfn.zip"}

        # Fallback: return base64-encoded zip
        import base64

        return {
            "zip_base64": base64.b64encode(zip_bytes).decode(),
            "filename": f"{bundle.deployment_name}-cfn.zip",
        }

    except Exception as e:
        logger.exception("CFN template generation failed")
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# POST /api/export-python  (Phase 3 Gap 3G — eject standalone Python project)
# ---------------------------------------------------------------------------


@deployment_app.post("/api/export-python")
async def handle_export_python(request: DeployRequest, raw_request: Request):
    """Export a downloadable, standalone Python agent project.

    Mirrors handle_generate_cfn_template: build the project bundle, zip it,
    upload to the artifacts bucket and return a 3600s presigned URL (base64
    fallback when no bucket). Unlike the CFN export, the S3 key is
    owner-stamped per the tenant-isolation rules.
    """
    try:
        from app.services.python_exporter import build_and_zip

        zip_bytes, deployment_name = build_and_zip(request)

        bucket = os.environ.get("ARTIFACTS_BUCKET_NAME", "")
        if bucket:
            caller_sub = _get_user_id(raw_request) or "anonymous"
            s3_client = boto3.client("s3", region_name=config.aws_region)
            s3_key = f"python-exports/{caller_sub}/{deployment_name}-{uuid.uuid4().hex[:8]}.zip"
            s3_client.put_object(Bucket=bucket, Key=s3_key, Body=zip_bytes)

            url = s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": s3_key},
                ExpiresIn=3600,
            )
            return {"download_url": url, "filename": f"{deployment_name}-python.zip"}

        import base64

        return {
            "zip_base64": base64.b64encode(zip_bytes).decode(),
            "filename": f"{deployment_name}-python.zip",
        }

    except Exception:
        logger.exception("Python export failed")
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# Mangum handler (Lambda entry point)
# ---------------------------------------------------------------------------

_mangum_handler = Mangum(deployment_app, lifespan="off")


def handler(event, context):
    """Lambda entry point. Intercepts async events before Mangum."""
    if isinstance(event, dict):
        if event.get("_async_test"):
            return _handle_async_test(event)
        if event.get("_async_generate"):
            return _handle_async_generate(event)

    # Normal API Gateway request → Mangum/FastAPI
    return _mangum_handler(event, context)


def _handle_async_test(event: dict):
    """Run tool test and store results in DynamoDB."""
    test_id = event["test_id"]
    table = _get_deploy_table()

    try:
        result = test_tool(
            lambda_code=event["lambda_code"],
            test_cases=event["test_cases"],
            region=event.get("region", "us-east-1"),
        )

        table.update_item(
            Key={"deployment_id": test_id},
            UpdateExpression="SET #s = :s, success = :ok, all_passed = :ap, results_json = :rj, #e = :e",
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues={
                ":s": "completed",
                ":ok": result.get("success", False),
                ":ap": result.get("allPassed", False),
                ":rj": json.dumps(result.get("results", [])),
                ":e": result.get("error"),
            },
        )
    except Exception as exc:
        logger.exception("Async tool test failed: %s", exc)
        table.update_item(
            Key={"deployment_id": test_id},
            UpdateExpression="SET #s = :s, #e = :e",
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues={":s": "completed", ":e": str(exc)},
        )


def _handle_async_generate(event: dict):
    """Run tool generation in background and store results in DynamoDB."""
    job_id = event["job_id"]
    table = _get_deploy_table()

    try:
        result = generate_tool(
            prompt=event["prompt"],
            conversation_history=event.get("conversation_history"),
            existing_tool=event.get("existing_tool"),
            region=event.get("region", "us-east-1"),
        )

        tool_data = result.get("tool")
        test_cases = result.get("testCases", [])

        table.update_item(
            Key={"deployment_id": job_id},
            UpdateExpression="SET #s = :s, success = :ok, message = :msg, #e = :e, response_type = :rt, tool_json = :tj, test_cases_json = :tcj",
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues={
                ":s": "completed",
                ":ok": result.get("success", False),
                ":msg": result.get("message", ""),
                ":e": result.get("error"),
                ":rt": result.get("responseType", "generation"),
                ":tj": json.dumps(tool_data) if tool_data else None,
                ":tcj": json.dumps(test_cases) if test_cases else "[]",
            },
        )
    except Exception as exc:
        logger.exception("Async tool generation failed: %s", exc)
        table.update_item(
            Key={"deployment_id": job_id},
            UpdateExpression="SET #s = :s, success = :ok, #e = :e",
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues={":s": "completed", ":ok": False, ":e": str(exc)},
        )
