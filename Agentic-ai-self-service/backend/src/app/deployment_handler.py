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
import contextvars
import copy
import hmac
import json
import logging
import os
import re
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Literal

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum

import app.services._otel_platform  # noqa: F401
from app.models.deployment_models import (
    INTERNAL_ONLY_STATE_FIELDS,
    DeleteResponse,
    DeploymentState,
    DeploymentStatusEnum,
    DeployRequest,
    DeployResponse,
    ImportRuntimeRequest,
    TestRequest,
    TestResponse,
)
from app.models.template_composition import template_implied_capabilities
from app.models.tool_generation_models import (
    AgentGenerateRequest,
    AgentGenerateResponse,
    ToolGenerateRequest,
    ToolGenerateResponse,
    ToolTestRequest,
)
from app.services.aws_errors import is_error
from app.services.aws_pagination import list_all
from app.services.config import load_config
from app.services.credential_scrub import strip_credential_leaves
from app.services.deletion_confirmation import (
    ConfirmationBudgetExhausted,
    DeletionFailedAfterAccept,
    delete_memory_confirmed,
    delete_policy_engine_confirmed,
    delete_vector_bucket_confirmed,
    wait_until_absent,
)
from app.services.deployment_payload_validation import (
    PayloadPhase,
    ValidationContext,
    validate_deployment_payload,
)
from app.services.deployment_state_store import (
    CO_RESIDENT_REFUSAL,
    GATEWAY_GRAPH_FIELD,
    DeploymentStateStore,
    collapse_secret_intent_rows,
    describe_gateway_targets_deleted,
    gateway_graph_membership,
    gateway_targets_deleted,
    manifest_delete_refusal,
    manifest_resource_key,
    note_gateway_targets_deleted,
)
from app.services.gateway_deployer import (
    ConnectorSecretBindingError,
    ConnectorSecretDeletionRefused,
    _authorize_tool_function_deletion,
    _release_shared_tool_lambda,
    assert_role_binding,
    bind_connector_secret_for_deployment,
    cleanup_gateway_resources,
    connector_identity_mode,
    delete_deployment_bound_secret,
    gateway_aws_session,
    get_cognito_token,
    is_shared_tool_function,
    manifest_secret_journal,
    secret_intent_journal,
    secrets_manager_arn_location,
    stage_customer_secret_for_deployment,
    stage_runtime_secret_for_deployment,
    tool_binding_requirement,
)
from app.services.gateway_mutation_lock import (
    allowed_clients,
    engine_detached,
    gateway_mutation_lock,
    shared_lambda_lock,
)
from app.services.gateway_name_claim import (
    POINTER_ABSENT,
    POINTER_ACTIVE,
    POINTER_MARKED,
    POINTER_RACED,
    GatewayNameClaimRefused,
    claim_account,
    hold_gateway_names_for_teardown,
    reclaim_recovery_pointer,
    recovered_gateway_rows,
    unfinished_recovery,
    with_recovered_rows,
)
from app.services.gateway_update import NO_CLIENT_ALLOWED, preserving_gateway_update
from app.services.harness_deployer import destroy_harness, invoke_harness
from app.services.invocation_identity import (
    InvocationIdentity,
    InvocationIdentityError,
    memory_invocation_identity,
)
from app.services.policy_lifecycle import delete_policy_confirmed
from app.services.rbac import require_scopes
from app.services.resource_ownership import (
    OwnershipConfigurationError,
    ResourceDeletionRefused,
    assert_agentcore_resource_owned,
    assert_aoss_policy_owned,
    assert_guardrail_owned,
    assert_knowledge_base_owned,
    assert_vector_bucket_owned,
    delete_owned_credential_provider,
    delete_owned_iam_role,
    delete_owned_s3_object,
    get_owned_aoss_collection,
    resource_is_missing,
    stack_id,
    tag_map,
)
from app.services.resource_tagging import (
    GovernanceTagError,
    stampable_governance_tags,
    validated_governance_tags,
)
from app.services.runtime_deployer import destroy_runtime, needs_provider_api_key
from app.services.runtime_invocation import (
    invoke_verified_http_runtime,
    parse_tool_receipts,
    promote_pending_policy,
)
from app.services.runtime_invocation import (
    parse_response_body as parse_verified_response_body,
)
from app.services.runtime_invocation import (
    runtime_payload as build_runtime_payload,
)
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


def _deployment_target_event(
    deployment_state: dict | None,
    fallback_region: str,
) -> dict:
    """Rebuild the exact target context frozen on a deployment record."""
    state = deployment_state or {}
    return {
        "target_account_id": state.get("target_account_id"),
        "target_region": state.get("target_region") or fallback_region,
        "target_role_arn": state.get("target_role_arn"),
    }


# ---------------------------------------------------------------------------
# Deployment state store (lazy-initialised)
# ---------------------------------------------------------------------------

_state_store: DeploymentStateStore | None = None


def _get_state_store() -> DeploymentStateStore:
    global _state_store
    if _state_store is None:
        _state_store = DeploymentStateStore(
            table_name=DEPLOYMENT_TABLE_NAME,
            region=config.aws_region,
        )
    return _state_store


def _scan_for_runtime(table, runtime_id: str) -> dict | None:
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
            "runtime_id-index Query failed (%s); falling back to Scan",
            exc,
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
    gateway_tools: list | None,
    connectors: list | None,
    connected_tools: list | None,
    external_mcp_servers: list | None = None,
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
    return bool(gateway_tools or connectors or external_mcp_servers or "gateway" in (connected_tools or []))


def _reject_unsupported_cross_account_features(request: DeployRequest) -> None:
    """Fail before persistence for features that still depend on home-account data.

    A target Runtime cannot safely honor ``per_agent`` identity by silently
    substituting the shared pre-provisioned target role. Likewise, HITL and
    approval policies write to/read from the platform account's approval queue;
    pointing a target Runtime/Harness at that table name resolves in the wrong
    account (and often the wrong region). Reject these combinations until that
    data plane is explicitly multi-account rather than shipping a green deploy
    that bypasses the requested isolation or approval control.
    """
    blockers: list[str] = []
    if request.identity_config and request.identity_config.mode == "per_agent":
        blockers.append("per-agent execution-role isolation")
    if "hitl" in (request.connected_tools or []):
        blockers.append("the HITL tool")

    policy_table = os.environ.get("TAG_POLICY_TABLE_NAME", "")
    if policy_table:
        try:
            from app.services.approval_policy_store import ApprovalPolicyStore

            policies = ApprovalPolicyStore(
                policy_table,
                os.environ.get(
                    "APP_AWS_REGION",
                    os.environ.get("AWS_REGION", "us-east-1"),
                ),
            ).list("default")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=503,
                detail=(
                    "Could not determine whether organization approval policies "
                    "apply to this cross-account deployment. Refusing to bypass a "
                    "governance control; retry when the policy store is available."
                ),
            ) from exc
        if any(policy.enabled and policy.tool_match for policy in policies):
            blockers.append("enabled organization approval policies")

    if blockers:
        raise HTTPException(
            status_code=400,
            detail=(
                "Cross-account deployment does not yet support "
                f"{', '.join(blockers)} because those features depend on "
                "platform-account execution roles or approval-queue data. "
                "Use the shared target role without HITL/approval policies, "
                "or deploy this agent in the platform account."
            ),
        )


def _credential_target_event(
    *,
    target_account_id: str | None,
    target_region: str,
    target_role_arn: str | None,
) -> dict:
    return {
        "target_account_id": target_account_id,
        "target_region": target_region,
        "target_role_arn": target_role_arn,
    }


def _cleanup_staged_credentials(
    secret_arns: list[str],
    *,
    deployment_id: str,
    target_event: dict,
) -> None:
    """Best-effort compensation for credentials staged before SFN starts."""
    if not secret_arns:
        return
    from app.services import step_clients

    region = target_event.get("target_region") or config.aws_region
    sm = step_clients.session_for_event(target_event).client("secretsmanager", region_name=region)
    for index, secret_arn in enumerate(dict.fromkeys(secret_arns)):
        try:
            delete_deployment_bound_secret(
                region=region,
                deployment_id=deployment_id,
                secret_ref=secret_arn,
                secrets_client=sm,
            )
        except ConnectorSecretDeletionRefused:
            logger.error(
                "Authentication-resource compensation refused item #%d because exact ownership was not proven",
                index,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Authentication-resource compensation failed for item #%d: %s",
                index,
                type(exc).__name__,
            )


def _api_validation_context() -> ValidationContext:
    """Where this API deployment actually runs, from TRUSTED sources only.

    The mirror of ``validate_step._validation_context``, and deliberately NOT derived from the
    request. The account comes from ``STATE_MACHINE_ARN`` -- a server-set environment variable
    naming the state machine this handler is about to start -- and the region from the loaded
    config. Both are what ``cnt_QAWqFk4LdKNGAO`` requires: a caller-supplied account segment has
    to be compared against a resolved account the caller cannot influence.

    ``STATE_MACHINE_ARN`` rather than an STS call on purpose. ``get_caller_identity`` would be a
    network round trip on the hot path of every deploy, and it can fail; an env var that the
    platform's own CDK sets cannot. A malformed or absent value yields ``None``, which weakens
    exactly the one account check and fails nothing -- the same trade the step handler makes.
    """
    account: str | None = None
    parts = (STATE_MACHINE_ARN or "").split(":")
    if len(parts) >= 5 and parts[4].isdigit() and len(parts[4]) == 12:
        account = parts[4]
    return ValidationContext(home_account_id=account, home_region=config.aws_region or None)


async def _reject_invalid_deploy_request(raw_request: Request) -> None:
    """Refuse a malformed or credential-bearing deploy request BEFORE anything is created.

    This validates the EXACT JSON body the caller sent, not ``request.model_dump()``. The
    distinction is the whole point: ``models/components.py`` declares
    ``ConnectorConfig.secret_value = Field(..., exclude=True)``, so the dump silently drops the
    one field that carries a raw connector credential. A scan built on the dump would report a
    clean payload while never having seen the bytes it exists to look at -- a validator that
    cannot fail, which is worse than no validator because it reads as evidence.

    Reading the body here is free and cannot consume it: FastAPI has already awaited and cached
    it on this same ``Request`` object in order to construct the ``DeployRequest`` argument, so
    ``.json()`` returns that cache.

    ``PayloadPhase.REQUEST`` -- raw secret material is still PERMITTED at the approved write-only
    paths, because staging has not run yet. The prepared payload is re-validated under
    ``PayloadPhase.PREPARED`` immediately before ``StartExecution``, and the state machine's own
    first task validates a third time (cnt_jljdNeOwgPnFx2: a downstream step authorizes
    independently rather than trusting its caller).
    """
    try:
        body = await raw_request.json()
    except Exception as exc:  # noqa: BLE001
        # Fails CLOSED. This branch should be unreachable -- an unparseable body fails FastAPI's
        # own model binding with a 422 before this function is called, and the body is cached on
        # this same Request -- so reaching it means an assumption this gate rests on is wrong.
        #
        # An earlier version of this returned here, reasoning that a cosmetic surprise should not
        # become an outage. That was wrong, and the reasoning inverted the stakes: the ONLY
        # payloads this function exists to stop are ones carrying raw credential material at a
        # path that cannot accept it, so "we could not look at the body" must never mean
        # "proceed". The PREPARED gate is not a substitute either -- it runs after the rows are
        # written and the secrets are staged, which is exactly the post-hoc enforcement
        # cnt_jljdNeOwgPnFx2 names as a pitfall.
        logger.error(
            "Deploy request body could not be read for validation (%s); refusing",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail=("The deployment request could not be validated and was refused. No resource was created."),
        ) from exc
    if not isinstance(body, dict):
        # Also closed. FastAPI cannot have built a DeployRequest from a non-object body, so this
        # is the same broken-assumption case rather than a caller error worth tolerating.
        logger.error("Deploy request body was %s, not an object; refusing", type(body).__name__)
        raise HTTPException(
            status_code=422,
            detail="The deployment request body must be a JSON object.",
        )

    result = validate_deployment_payload(
        body,
        phase=PayloadPhase.REQUEST,
        context=_api_validation_context(),
    )
    if result.is_valid:
        return

    # Field paths and stable codes to the log, never values -- ARCC cnt_ik6StRHfs118ea. The
    # body is NOT logged: the reason this gate exists is that it may contain a live credential.
    logger.warning(
        "Deploy request rejected at the API boundary: %s",
        [{"field": e.field, "code": e.code} for e in result.errors],
    )
    # 422, matching FastAPI's own code for a body that is syntactically fine but semantically
    # unusable. The detail carries the summary, which names fields and codes and echoes no value.
    raise HTTPException(status_code=422, detail=result.summary())


def _prepare_deployment_credentials(
    *,
    gateway_config: dict | None,
    connectors: list[dict] | None,
    external_mcp_servers: list[dict] | None,
    deployment_id: str,
    owner_sub: str,
    target_account_id: str | None,
    target_region: str,
    target_role_arn: str | None,
    store: DeploymentStateStore,
    resource_tags: dict | None = None,
    identity_config=None,
) -> tuple[dict | None, list[dict], list[dict], list[str]]:
    """Deep-copy, bind, record, and scrub every user-supplied gateway credential.

    The returned structures are safe to serialize into Step Functions history:
    they contain only deployment-bound Secrets Manager ARNs. If any bind or
    strict manifest append fails, every secret already staged by this call is
    compensation-deleted before the error escapes.
    """
    from app.services import step_clients

    prepared_gateway = copy.deepcopy(gateway_config) if gateway_config else None
    prepared_connectors = copy.deepcopy(connectors or [])
    prepared_mcp = copy.deepcopy(external_mcp_servers or [])
    target_event = _credential_target_event(
        target_account_id=target_account_id,
        target_region=target_region,
        target_role_arn=target_role_arn,
    )
    sm = None
    staged_arns: list[str] = []

    def _secrets_client():
        nonlocal sm
        if sm is None:
            sm = step_clients.session_for_event(target_event).client(
                "secretsmanager",
                region_name=target_region,
            )
        return sm

    def _bind(
        *,
        payload_key: str,
        raw_value: str | None,
        secret_ref: str | None,
    ) -> str:
        with (
            secret_intent_journal(manifest_secret_journal(store, deployment_id, target_account_id)),
            connector_identity_mode(identity_config),
        ):
            arn, _created = bind_connector_secret_for_deployment(
                region=target_region,
                owner_sub=owner_sub,
                deployment_id=deployment_id,
                payload_key=payload_key,
                raw_value=raw_value,
                secret_ref=secret_ref,
                secrets_client=_secrets_client(),
                resource_tags=resource_tags,
            )
        if arn not in staged_arns:
            staged_arns.append(arn)
            row = {
                "type": "secret",
                "id": arn,
                "region": target_region,
                # bind_connector_secret_for_deployment returns the exact
                # deployment-bound copy (including an idempotent retry of this
                # same deployment), never the caller's source secret.
                "created_by_deployment": True,
            }
            if target_account_id:
                row["account"] = target_account_id
            # Hard durability boundary: do not start SFN with a live plaintext
            # credential that teardown cannot find.
            store.record_resource_strict(deployment_id, row)
        return arn

    try:
        for connector in prepared_connectors:
            raw = connector.pop("secret_value", None)
            raw_alias = connector.pop("secretValue", None)
            ref = connector.get("secret_arn") or connector.get("secretArn")
            if raw or raw_alias or ref:
                auth_method = connector.get("auth_method") or connector.get("authMethod")
                connector["secret_arn"] = _bind(
                    payload_key="clientSecret" if auth_method == "oauth2_cc" else "apiKey",
                    raw_value=raw or raw_alias,
                    secret_ref=ref,
                )
                connector.pop("secretArn", None)

        for selection in prepared_mcp:
            raw = selection.pop("secret_value", None)
            raw_alias = selection.pop("secretValue", None)
            ref = selection.get("secret_arn") or selection.get("secretArn")
            if raw or raw_alias or ref:
                selection["secret_arn"] = _bind(
                    payload_key="apiKey",
                    raw_value=raw or raw_alias,
                    secret_ref=ref,
                )
                selection.pop("secretArn", None)

            oauth = copy.deepcopy(selection.get("oauth") or {})
            oauth_raw = oauth.pop("client_secret", None)
            oauth_raw_alias = oauth.pop("clientSecret", None)
            oauth_ref = (
                oauth.get("client_secret_arn")
                or oauth.get("clientSecretArn")
                or oauth.get("client_secret_ref")
                or oauth.get("clientSecretRef")
            )
            if oauth_raw or oauth_raw_alias or oauth_ref:
                oauth["client_secret_arn"] = _bind(
                    payload_key="clientSecret",
                    raw_value=oauth_raw or oauth_raw_alias,
                    secret_ref=oauth_ref,
                )
                for alias in ("clientSecretArn", "client_secret_ref", "clientSecretRef"):
                    oauth.pop(alias, None)
                selection["oauth"] = oauth

        if prepared_gateway is not None:
            raw = prepared_gateway.pop("litellm_api_key", None)
            raw_alias = prepared_gateway.pop("litellmApiKey", None)
            ref = prepared_gateway.get("litellm_api_key_ref") or prepared_gateway.get("litellmApiKeyRef")
            if raw or raw_alias or ref:
                prepared_gateway["litellm_api_key_ref"] = _bind(
                    payload_key="apiKey",
                    raw_value=raw or raw_alias,
                    secret_ref=ref,
                )
                prepared_gateway.pop("litellmApiKeyRef", None)
    except Exception:
        _cleanup_staged_credentials(
            staged_arns,
            deployment_id=deployment_id,
            target_event=target_event,
        )
        raise

    return prepared_gateway, prepared_connectors, prepared_mcp, staged_arns


def _prepare_runtime_credentials(
    *,
    runtime_config: dict,
    observability_config: dict | None,
    deployment_id: str,
    owner_sub: str,
    target_account_id: str | None,
    target_region: str,
    target_role_arn: str | None,
    store: DeploymentStateStore,
    resource_tags: dict | None = None,
    identity_config=None,
) -> tuple[dict, dict | None, dict | None, list[str]]:
    """Validate and stage model-provider and OTEL credentials before SFN.

    A caller-controlled ARN is inventory, not authority. Each active source
    secret is live-described in its source account, bound to this caller and
    stack, read once, and copied into an exact deployment-owned secret in the
    target account/region. The runtime receives only that copied ARN. Platform
    OTEL defaults are operator-controlled and therefore bypass caller ownership,
    but are still namespace-checked and copied into the target account.

    A standalone model-free MCP deploy never reaches this function while platform
    OTEL defaults are enabled: ``_reject_mcp_inheriting_platform_otel`` refuses it
    at preflight, before any row/secret/execution, because the FastMCP bundle
    carries no generic-agent observability and inheriting operator OTEL would
    silently change the runtime's IAM and environment.
    """
    from app.services import step_clients
    from app.services.observability import (
        get_platform_observability_defaults_lenient as get_platform_observability_defaults,
    )

    prepared_config = copy.deepcopy(runtime_config)
    prepared_observability = copy.deepcopy(observability_config) if observability_config is not None else None
    target_event = _credential_target_event(
        target_account_id=target_account_id,
        target_region=target_region,
        target_role_arn=target_role_arn,
    )
    target_session = None
    target_sm = None
    staged_arns: list[str] = []
    home_account_id: str | None = None

    def _target_session():
        nonlocal target_session
        if target_session is None:
            target_session = step_clients.session_for_event(target_event)
        return target_session

    def _target_secrets_client():
        nonlocal target_sm
        if target_sm is None:
            target_sm = _target_session().client("secretsmanager", region_name=target_region)
        return target_sm

    def _home_account() -> str:
        nonlocal home_account_id
        if home_account_id is None:
            home_account_id = boto3.client("sts", region_name=config.aws_region).get_caller_identity()["Account"]
        return home_account_id

    def _source_client(secret_ref: str):
        source_account, source_region, _name = secrets_manager_arn_location(secret_ref)
        if target_account_id and source_account == target_account_id:
            return _target_session().client("secretsmanager", region_name=source_region)
        if source_account == _home_account():
            return boto3.client("secretsmanager", region_name=source_region)
        raise ConnectorSecretBindingError(
            "The credential source account is neither the platform account nor the selected deployment target."
        )

    def _stage(
        secret_ref: str,
        *,
        namespace: str,
        purpose: str,
        trusted_platform_source: bool = False,
    ) -> str:
        with (
            secret_intent_journal(manifest_secret_journal(store, deployment_id, target_account_id)),
            connector_identity_mode(identity_config),
        ):
            arn = stage_runtime_secret_for_deployment(
                source_secret_ref=secret_ref,
                source_namespace=namespace,
                purpose=purpose,
                owner_sub=owner_sub,
                deployment_id=deployment_id,
                target_region=target_region,
                source_secrets_client=_source_client(secret_ref),
                target_secrets_client=_target_secrets_client(),
                trusted_platform_source=trusted_platform_source,
                resource_tags=resource_tags,
            )
        if arn not in staged_arns:
            staged_arns.append(arn)
            row = {
                "type": "secret",
                "id": arn,
                "region": target_region,
                # stage_runtime_secret_for_deployment always returns the
                # deployment-bound target copy, not the source credential.
                "created_by_deployment": True,
            }
            if target_account_id:
                row["account"] = target_account_id
            store.record_resource_strict(deployment_id, row)
        return arn

    try:
        provider_ref = prepared_config.get("providerApiKeyRef") or prepared_config.get("provider_api_key_ref")
        if provider_ref:
            prepared_config["providerApiKeyRef"] = _stage(
                str(provider_ref),
                namespace="agentcore-provider",
                purpose="model-provider-api-key",
            )
            prepared_config.pop("provider_api_key_ref", None)

        nested_observability = prepared_config.get("observability")
        effective_observability = (
            prepared_observability
            if prepared_observability is not None
            else nested_observability
            if isinstance(nested_observability, dict)
            else None
        )
        platform_defaults = get_platform_observability_defaults()
        prepared_platform_defaults = copy.deepcopy(platform_defaults) if platform_defaults else None

        if prepared_platform_defaults and prepared_platform_defaults.get("auth_header_secret_arn"):
            prepared_platform_defaults["auth_header_secret_arn"] = _stage(
                str(prepared_platform_defaults["auth_header_secret_arn"]),
                namespace="agentcore-otel",
                purpose="platform-otel-auth",
                trusted_platform_source=True,
            )
        elif effective_observability:
            otel_ref = effective_observability.get("auth_header_secret_arn") or effective_observability.get(
                "authHeaderSecretArn"
            )
            if otel_ref:
                effective_observability["auth_header_secret_arn"] = _stage(
                    str(otel_ref),
                    namespace="agentcore-otel",
                    purpose="user-otel-auth",
                )
                effective_observability.pop("authHeaderSecretArn", None)
                if prepared_observability is None:
                    prepared_config["observability"] = effective_observability
    except Exception:
        _cleanup_staged_credentials(
            staged_arns,
            deployment_id=deployment_id,
            target_event=target_event,
        )
        raise

    return prepared_config, prepared_observability, prepared_platform_defaults, staged_arns


def _prepare_knowledge_base_credentials(
    *,
    knowledge_base_config: dict | None,
    deployment_id: str,
    owner_sub: str,
    target_account_id: str | None,
    target_region: str,
    target_role_arn: str | None,
    store: DeploymentStateStore,
    resource_tags: dict | None = None,
    identity_config=None,
) -> tuple[dict | None, list[str]]:
    """Stage only the active Knowledge Base credential references.

    The modal preserves fields while users switch data-source/vector-store tabs.
    Serializing those stale ARNs into Step Functions history would retain
    unnecessary inventory and, before this boundary existed, could grant the KB
    role access to a secret the active configuration never used.  Inactive
    credential fields are removed; active customer secrets must explicitly opt in
    and are copied into deployment-bound target-account secrets.
    """
    if not knowledge_base_config:
        return None, []

    from app.services import step_clients

    prepared = copy.deepcopy(knowledge_base_config)
    data_source_type = str(prepared.get("dataSourceType") or "s3").lower()
    vector_store_type = str(prepared.get("vectorStoreType") or "s3_vectors").lower()
    all_fields = {
        "confluenceCredentialsSecretArn": "knowledge-base-confluence",
        "salesforceCredentialsSecretArn": "knowledge-base-salesforce",
        "sharePointCredentialsSecretArn": "knowledge-base-sharepoint",
        "rdsCredentialsSecretArn": "knowledge-base-rds",
    }
    active_fields: set[str] = set()
    if data_source_type == "confluence":
        active_fields.add("confluenceCredentialsSecretArn")
    elif data_source_type == "salesforce":
        active_fields.add("salesforceCredentialsSecretArn")
    elif data_source_type == "sharepoint":
        active_fields.add("sharePointCredentialsSecretArn")
    if vector_store_type == "rds":
        active_fields.add("rdsCredentialsSecretArn")

    for field in all_fields:
        if field not in active_fields:
            prepared.pop(field, None)

    target_event = _credential_target_event(
        target_account_id=target_account_id,
        target_region=target_region,
        target_role_arn=target_role_arn,
    )
    target_session = None
    target_sm = None
    home_account_id: str | None = None
    staged_arns: list[str] = []

    def _target_session():
        nonlocal target_session
        if target_session is None:
            target_session = step_clients.session_for_event(target_event)
        return target_session

    def _target_secrets_client():
        nonlocal target_sm
        if target_sm is None:
            target_sm = _target_session().client(
                "secretsmanager",
                region_name=target_region,
            )
        return target_sm

    def _home_account() -> str:
        nonlocal home_account_id
        if home_account_id is None:
            home_account_id = boto3.client(
                "sts",
                region_name=config.aws_region,
            ).get_caller_identity()["Account"]
        return home_account_id

    def _source_client(secret_ref: str):
        source_account, source_region, _name = secrets_manager_arn_location(secret_ref)
        if target_account_id and source_account == target_account_id:
            return _target_session().client(
                "secretsmanager",
                region_name=source_region,
            )
        if source_account == _home_account():
            return boto3.client(
                "secretsmanager",
                region_name=source_region,
            )
        raise ConnectorSecretBindingError(
            "The Knowledge Base credential source account is neither the platform "
            "account nor the selected deployment target."
        )

    try:
        for field in sorted(active_fields):
            secret_ref = str(prepared.get(field) or "")
            if not secret_ref:
                continue
            with (
                secret_intent_journal(manifest_secret_journal(store, deployment_id, target_account_id)),
                connector_identity_mode(identity_config),
            ):
                arn = stage_customer_secret_for_deployment(
                    source_secret_ref=secret_ref,
                    purpose=all_fields[field],
                    owner_sub=owner_sub,
                    deployment_id=deployment_id,
                    target_region=target_region,
                    source_secrets_client=_source_client(secret_ref),
                    target_secrets_client=_target_secrets_client(),
                    resource_tags=resource_tags,
                )
            prepared[field] = arn
            if arn not in staged_arns:
                staged_arns.append(arn)
                row = {
                    "type": "secret",
                    "id": arn,
                    "region": target_region,
                    "created_by_deployment": True,
                }
                if target_account_id:
                    row["account"] = target_account_id
                store.record_resource_strict(deployment_id, row)
    except Exception:
        _cleanup_staged_credentials(
            staged_arns,
            deployment_id=deployment_id,
            target_event=target_event,
        )
        raise

    return prepared, staged_arns


def _maybe_promote_policy(deployment_state: dict | None, region: str) -> bool:
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
    return promote_pending_policy(
        deployment_state,
        region,
        target_event=_deployment_target_event(deployment_state, region),
        state_store=_get_state_store(),
    )


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------


def _get_user_id(request: Request) -> str | None:
    """Extract user sub from JWT claims (API Gateway HTTP API JWT authorizer)."""
    try:
        return (
            request.scope.get("aws.event", {})
            .get("requestContext", {})
            .get("authorizer", {})
            .get("jwt", {})
            .get("claims", {})
            .get("sub")
        )
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
    allow_headers=[
        "Content-Type",
        "Authorization",
        "X-Amz-Date",
        "X-Api-Key",
        "X-AgentCore-Timestamp",
        "X-AgentCore-Delivery-Id",
        "X-AgentCore-Signature",
    ],
)


# Phase 5 (Loom) audit trail — record WRITE-ish control-plane actions to the
# audit store for the admin dashboard. Fixed action vocabulary (audit_store.
# classify_action); reads are ignored. Best-effort: an audit failure NEVER
# affects the underlying response (wrapped, logged, swallowed).
@deployment_app.middleware("http")
async def _audit_middleware(request, call_next):
    response = await call_next(request)
    try:
        from app.services.audit_store import (
            AuditEvent,
            classify_action,
            get_audit_store,
        )

        action = classify_action(request.method, request.url.path)
        if action is not None:
            from app.services.auth import get_caller_sub as _gcs

            try:
                actor = _gcs(request)
            except Exception:  # noqa: BLE001
                actor = "unknown"
            # Loom-study 0.5: populate session_uuid from the per-browser-session
            # id the frontend sends as X-Session-Id (was always empty). Lets admin
            # analytics distinguish activity streams on a shared account and build
            # a per-session timeline. Bounded to avoid unbounded header abuse.
            _sid = (request.headers.get("x-session-id") or "")[:128] or None
            get_audit_store().record(
                AuditEvent(
                    org_id="default",
                    actor_sub=actor,
                    action=action,
                    method=request.method,
                    path=request.url.path,
                    status_code=getattr(response, "status_code", 0),
                    session_uuid=_sid,
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit middleware skipped: %s", exc)
    return response


# Phase 1 Gap 1A — version + slot management endpoints. Mounted on the
# deployment Lambda because the versions table belongs to the runtime-control
# plane (same data plane as /api/deploy and /api/test-runtime). API GW routes
# for /api/runtimes/{name}/versions* are added in infra/stacks/platform_stack.py
# per the Bug 21 router-enumeration rule.
from app.routers.admin import router as admin_router  # noqa: E402  # Phase 5 audit dashboard
from app.routers.approvals import router as approvals_router  # noqa: E402
from app.routers.connectors import router as connectors_router  # noqa: E402
from app.routers.cost import budgets_router  # noqa: E402  # Phase 4 FinOps budgets
from app.routers.cost import router as cost_router  # noqa: E402
from app.routers.deploy_targets import router as deploy_targets_router  # noqa: E402
from app.routers.evaluations import router as evaluations_router  # noqa: E402
from app.routers.hitl import router as hitl_router  # noqa: E402
from app.routers.identity import router as identity_router  # noqa: E402
from app.routers.mcp_servers import router as mcp_servers_router  # noqa: E402
from app.routers.models import router as models_router  # noqa: E402
from app.routers.permissions import router as permissions_router  # noqa: E402
from app.routers.prompts import router as prompts_router  # noqa: E402  # Phase 3 Gap 3H
from app.routers.registry import router as registry_router  # noqa: E402
from app.routers.runtime_mcp import router as runtime_mcp_router  # noqa: E402  # standalone MCP product explorer
from app.routers.tags import router as tags_router  # noqa: E402  # Phase 2 governance tagging
from app.routers.triggers import router as triggers_router  # noqa: E402
from app.routers.triggers import webhook_router  # noqa: E402
from app.routers.versions import router as versions_router  # noqa: E402
from app.routers.vpc_profiles import router as vpc_profiles_router  # noqa: E402

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
# Phase 4 (Loom) FinOps — cost budgets (/api/cost/budgets). Reads spend from the
# same CloudWatch cost pipeline; owns the Budget DDB table grant.
deployment_app.include_router(budgets_router)
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
# Verified external MCP-server catalog (browsable in the Registry UI). Read-only,
# no tenant data — mounted alongside the connector catalog. Routes
# /api/mcp-servers + /api/mcp-servers/{proxy+} are added in platform_stack.py per
# the Bug 21 router-enumeration rule.
deployment_app.include_router(mcp_servers_router)
# Phase 1 (Loom-study 1.2/1.3) — identity inspection + OBO verification. Read
# endpoints (token-info + test-obo) on the deployment Lambda (owns the
# bedrock-agentcore identity permissions via harness/gateway steps' grants).
deployment_app.include_router(identity_router)
# Loom-study 1.6 — JIT IAM permission-request workflow (request/approve/reject).
deployment_app.include_router(permissions_router)
# Loom-study 2.2 — HITL approval-policy CRUD (/api/settings/approval-policies).
deployment_app.include_router(approvals_router)
# Loom-study 4.2 — named VPC config profiles (/api/settings/vpc-profiles).
deployment_app.include_router(vpc_profiles_router)
# Loom-study 5.1 — live model catalog (/api/models).
deployment_app.include_router(models_router)
# Product MCP explorer for standalone protocol=MCP runtimes. Mounted on the deployment
# Lambda because it owns InvokeAgentRuntime on the data plane and the get_caller_sub auth
# helper; the service resolves the owned runtime ARN server-side and never trusts a
# browser-supplied ARN/account/region/method. API-GW routes for /api/test-mcp-runtime/*
# are added explicitly in the platform stack (HTTP API needs a route per path).
deployment_app.include_router(runtime_mcp_router)
# Phase 3 Gap 3F — scheduled / event triggers registry. Mounted here because
# the deployment Lambda owns the TriggersTable grant + the agentcore-trigger/*
# Secrets Manager grant and already has the get_caller_sub auth helper.
deployment_app.include_router(triggers_router)
# Public ingress for webhook triggers. Authentication is the per-trigger HMAC
# contract in routers/triggers.py; this router deliberately does not use the JWT
# dependency that protects /api/*.
deployment_app.include_router(webhook_router)
# Tag the mounted copies with their origin so a duplicate include is
# detectable. The webhook ingress carries no JWT dependency and authenticates
# each request with the per-trigger HMAC alone, so mounting it twice would
# stand up a second unauthenticated-at-the-edge path; ``include_router`` copies
# the routes onto the app but the copies share the original endpoint fn.
_webhook_endpoints = {r.endpoint for r in webhook_router.routes if hasattr(r, "endpoint")}
for _route in deployment_app.routes:
    if getattr(_route, "endpoint", None) in _webhook_endpoints:
        _route.original_router = webhook_router
# Phase 2 governance tagging — tag policies + tag profiles. Mounted on the
# deployment Lambda because the deploy hook resolves tags in this same process
# and the Lambda owns the TagPolicy table grant. API-GW routes for
# /api/settings/tags + /api/settings/tag-profiles are added in platform_stack.py
# (Bug 21 router-enumeration rule: HTTP API needs explicit routes per path).
deployment_app.include_router(tags_router)
# User-facing, read-only target choices. Registration and role ARNs remain on
# the admin router; this route exposes only account id + region to deployers.
deployment_app.include_router(deploy_targets_router)
# Phase 5 (Loom) audit dashboard — /api/admin/audit (admin scope). Reads the
# audit store the middleware writes to.
deployment_app.include_router(admin_router)


@deployment_app.get("/health")
async def health_check() -> dict:
    checks = {"api": "ok"}
    try:
        store = _get_state_store()
        _ = store._table.table_status  # lightweight DynamoDB connectivity check
        checks["dynamodb"] = "ok"
    except Exception:
        checks["dynamodb"] = "degraded"
    overall = "healthy" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": overall, "checks": checks}


def _flow_store_for_ownership():
    """Return the store that can answer "who owns this flow", or None when none can.

    The flows table belongs to the workflow Lambda, which wires ``DynamoDBFlowStorage`` in
    ``main.py`` at import. This module runs in a DIFFERENT Lambda that never imports
    ``main.py``, so ``get_flow_storage()`` here is the empty in-memory store: an owner check
    built on it would 404 every flow that genuinely exists -- a gate that only works by
    refusing everything. So the table is opened directly from config when it is named.

    In Lambda with no table name there is nothing that can prove ownership, and None tells the
    caller to fail closed. Only a local run falls back to the in-memory store.
    """
    table_name = config.dynamodb_flows_table_name
    if table_name:
        from app.services.flow_storage import DynamoDBFlowStorage

        return DynamoDBFlowStorage(table_name=table_name, region=config.aws_region)
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return None
    from app.services.flow_storage import get_flow_storage

    return get_flow_storage()


def _reject_unowned_flow(flow_id: str | None, caller_sub: str) -> None:
    """Refuse a deploy that names a flow the caller does not own, before any side effect.

    Authentication is not authorization (ARCC cnt_GNnjRMToGfntf1): the 401 gate proved there
    IS a caller, this proves the caller's relationship to the object they referenced
    (cnt_dwzZ05hLnqhYXQ). No flow named means nothing to authorize -- a harness deploy and an
    unsaved canvas legitimately have none.

    The refusal is 404 for both "absent" and "someone else's", via ``assert_owner``, so the
    response cannot be used to discover which flow ids exist. A store that cannot be read is
    503: ownership was not proven, so the deploy does not proceed.

    The denial is logged on its own line, separately from authentication (cnt_6yTkcrHkEBKA7u),
    with the flow id and a HASH of the subject -- never the raw subject or any token material.
    """
    if not flow_id:
        return
    from app.services.auth import assert_owner
    from app.services.resource_ownership import owner_sub_hash

    store = _flow_store_for_ownership()
    if store is None:
        logger.error(
            "Deploy refused: flowId %s was named but no flows table is configured for this "
            "function, so ownership cannot be proven.",
            flow_id,
        )
        raise HTTPException(
            status_code=503,
            detail="Flow ownership could not be verified. No resource was created.",
        )
    try:
        flow = store.get(flow_id)
    except Exception as exc:
        logger.error(
            "Deploy refused: reading flowId %s failed (%s), so ownership cannot be proven.",
            flow_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Flow ownership could not be verified. No resource was created.",
        ) from exc
    try:
        assert_owner(getattr(flow, "owner_sub", None) if flow is not None else None, caller_sub)
    except HTTPException:
        logger.warning(
            "Authorization denied: caller %s may not deploy under flowId %s (%s).",
            owner_sub_hash(caller_sub),
            flow_id,
            "flow not found" if flow is None else "not the owner",
        )
        raise


def _reject_cfn_only_naming_profile(request: DeployRequest, operation: str) -> None:
    """Fail closed when a caller sends a CFN-ONLY field to another path.

    ``DeployRequest`` is shared by the live deploy, CloudFormation export and Python
    export routes. Accepting these on all three and then using them on only
    one recreates the silent-drop defect that originally hid the LiteLLM export gap.

    ``dataRetentionPolicy`` is refused here for the same reason and with more at stake. It
    controls ``DeletionPolicy``/``UpdateReplacePolicy`` on the data-bearing resources of a
    GENERATED template; there is no equivalent on a live AgentCore deployment or in a standalone
    Python project, so honouring it on those routes is impossible. A caller who sends
    ``dataRetentionPolicy: "Retain"`` to ``/api/deploy`` and gets a 202 has been told their data
    is protected when nothing was configured -- exactly the failure that a ``deletionPolicy``
    typo caused live, only worse, because the spelling is right and the route is wrong.

    Refusing depends on absence being distinguishable from an explicit value, which is why
    ``DeployRequest.data_retention_policy`` defaults to ``None`` rather than ``"Retain"``.
    """
    if request.naming_profile is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"namingProfile applies only to POST /api/generate-cfn-template and cannot "
                f"be used for {operation}. Remove namingProfile, or download the "
                "CloudFormation bundle to apply those infrastructure names."
            ),
        )
    if request.data_retention_policy is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"dataRetentionPolicy applies only to POST /api/generate-cfn-template and "
                f"cannot be used for {operation}: it sets CloudFormation DeletionPolicy and "
                "UpdateReplacePolicy attributes on a generated template, which this path does "
                "not produce. Remove dataRetentionPolicy, or download the CloudFormation "
                "bundle to control retention of the Knowledge Base, Cognito pools and Memory."
            ),
        )


def _reject_missing_provider_credential(request: DeployRequest) -> None:
    """Reject a live runtime whose first model call would be unauthenticated."""
    if (
        (request.deployment_mode or "runtime") == "runtime"
        and needs_provider_api_key(request.config)
        and not request.config.provider_api_key_ref
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "This runtime uses a model provider that requires an API key. "
                "Store the credential and supply providerApiKeyRef before deploying."
            ),
        )


def _reject_mcp_inheriting_platform_otel(request: DeployRequest) -> None:
    """Refuse a standalone MCP deploy that would silently inherit platform OTEL.

    The model-free FastMCP bundle carries no generic-agent observability, so an
    explicit ``observabilityConfig`` is already rejected by
    ``DeployRequest._mcp_protocol_admission``. Platform OTEL defaults are ambient
    server state, not a caller field, so they slip past that model-level check --
    but inheriting them would still change the runtime's IAM (a GetSecretValue
    grant on the OTEL auth secret) and its environment (an OTLP auth header) for a
    server that must carry neither. ``_prepare_runtime_credentials`` would read
    and stage that operator secret before the runtime even exists.

    Fail closed at preflight, alongside the other ``_reject_*`` gates and BEFORE
    any deployment/version row, credential staging or Step Functions start, so an
    inherited-OTEL conflict costs zero rows, secrets and executions -- rather than
    a post-hoc refusal in code generation after resources already exist. The fix
    is operator-side (disable platform OTEL for this canvas), so this is a 409.

    Scoped to ``runtime`` mode: a ``harness`` deploy that happens to carry the
    same gallery ``templateId`` is model-admitted and generates NO FastMCP
    runtime to receive OTEL, so the gallery id alone must not refuse it -- only
    the standalone runtime path is observability-free.

    Fails CLOSED when the policy cannot be READ. This gate uses the STRICT
    ``get_platform_observability_defaults`` (not the lenient wrapper the
    enrichment callers use): an SSM outage is an unknown state, not proof the
    operator disabled OTEL. Treating it as "not configured" would admit a
    runtime that may conflict with a locked policy, so an unreadable policy is a
    503 (retryable) with zero side effects -- never a 202.
    """
    if request.template_id != "mcp-server-runtime":
        return
    if (request.deployment_mode or "runtime") != "runtime":
        return
    from app.services.observability import (
        PlatformObservabilityUnavailable,
        get_platform_observability_defaults,
    )

    try:
        defaults = get_platform_observability_defaults()
    except PlatformObservabilityUnavailable as e:
        raise HTTPException(
            status_code=503,
            detail=(
                "Cannot deploy this standalone MCP runtime (templateId "
                "'mcp-server-runtime'): the platform OTEL / observability policy "
                "could not be read, so it is unknown whether a locked default "
                "exists that this model-free server must not inherit. This is a "
                "transient infrastructure error, not a 'disabled' answer -- retry "
                "once the platform observability policy is readable again."
            ),
        ) from e
    if defaults and defaults.get("enabled"):
        raise HTTPException(
            status_code=409,
            detail=(
                "This standalone MCP runtime (templateId 'mcp-server-runtime') is a "
                "model-free FastMCP tool server and cannot carry generic-agent "
                "observability, but the platform has OTEL defaults enabled that a "
                "deploy would otherwise inherit. Inheriting them would grant the MCP "
                "runtime an OTEL auth-secret read and inject an OTLP auth header it "
                "must not have. Deploy this MCP server on a platform without OTEL "
                "defaults, or disable the platform OTEL default for this deployment."
            ),
        )


def _resolve_governance_or_refuse(
    request: DeployRequest,
    *,
    # A closed vocabulary, not a label: the value SELECTS the validator below, so a typo would
    # silently give a live deploy the export path's wider tag rule.
    artifact: Literal["deployment", "export"] = "deployment",
) -> dict:
    """Resolve the governed tag set server-side, or refuse. No third outcome.

    P0-B. Three separate holes closed here, all of which let a deploy proceed with tags
    nobody currently governs:

    1. **Open on error.** The tag-policy store used to be best-effort on this path: any
       failure that was not a ``TagResolutionError`` was logged as "Tag resolution skipped
       (non-fatal)" and the deploy went on to ``store.create`` and the Step Functions start
       with ``resource_tags`` empty. A table outage, a throttle or a missing IAM grant
       therefore produced UNTAGGED resources and an HTTP 202. Required tags are only
       required when the store answers; a control that opens on error is not a control. Now
       an unavailable store is a 503 with zero side effects, matching what this same file
       already did for the same store in ``_resolve_export_tags`` and the admin path.

    2. **The client's own resolution was trusted.** The browser resolves the effective
       values against the policy set it loaded and sends the RESULT. An admin who adds a
       required tag, changes a default or edits a profile in between would be bypassed by
       every deploy panel left open. So the caller sends the ``sha256:`` policy revision and
       the profile ``updated_at`` it resolved against, they are re-checked against a fresh
       read inside the store (one read for the check and the values, so nothing slips
       between them), and a mismatch is a 409 telling the caller to reload.

    3. **Silence was accepted as consent.** A request that carries tags or a profile but no
       revision is refused (400) rather than treated as ungoverned, and so is one that
       selects a profile without its ``updated_at`` -- otherwise stripping one field from the
       payload defeats item 2 entirely, because the store can only check a value it is given.

    What is deliberately NOT refused: a request with no tags, no profile and no revision.
    Governance here is opt-in (``ensure_platform_policies`` seeds the platform keys as
    RECOMMENDED, not required), so an ordinary deploy with no tag state still resolves to
    whatever the policies' defaults say -- it just cannot skip the resolution.
    """
    from app.services.tag_policy_store import (
        TagGovernanceStaleError,
        TagResolutionError,
        get_tag_policy_store,
    )

    supplied = request.resource_tags or {}
    if (supplied or request.tag_profile) and not request.policy_revision:
        raise HTTPException(
            status_code=400,
            detail=(
                f"This {artifact} carries resource tags or a tag profile but no policyRevision, "
                "so the server cannot check that the values were resolved against the tag "
                "policies in force now. Reload the deploy panel and try again."
            ),
        )
    if request.tag_profile and not request.tag_profile_updated_at:
        # The revision covers the POLICY set only. A profile is a separate record with its
        # own values, so a current policyRevision says nothing about whether the profile was
        # edited after the caller read it -- and ``resolve_governance`` can only check a
        # timestamp it is given. Without this, omitting one optional-looking field silently
        # downgrades profile freshness to unchecked, which is the whole hole in item 2.
        raise HTTPException(
            status_code=400,
            detail=(
                f"This {artifact} selects the tag profile '{request.tag_profile}' but sends no "
                "tagProfileUpdatedAt, so the server cannot check that the profile's values are "
                "still the ones you saw. Reload the deploy panel and try again."
            ),
        )

    try:
        store = get_tag_policy_store()
        store.ensure_platform_policies("default")
        resolved = store.resolve_governance(
            "default",
            supplied=supplied,
            profile_name=request.tag_profile,
            expected_policy_revision=request.policy_revision,
            expected_profile_updated_at=request.tag_profile_updated_at,
        )
    except TagResolutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TagGovernanceStaleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed; see the docstring
        # The type alone, never the message: a botocore error echoes request parameters,
        # and the supplied tag VALUES are caller data we do not put in our logs.
        logger.error(
            "Tag governance could not be resolved (%s); refusing the %s with no side effects",
            type(exc).__name__,
            artifact,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                f"The tag policies could not be read, so this {artifact} was refused rather "
                "than created without the governance tags your organization requires. Nothing "
                "was created. Retry shortly; if it persists, this is a platform fault, not a "
                "problem with your request."
            ),
        ) from exc

    # Validate the RESOLVED set here, at the one boundary both artifacts pass through, so an
    # illegal tag is a 400 on the request rather than a failure partway through a deployment
    # that has already created resources. The live path used to validate nowhere at all and the
    # export path validated at generate() time; sharing one validator is what stops the two
    # answering differently, and doing it here is what makes the refusal free of side effects.
    # The set includes admin-configured defaults, not just what this caller typed, so the
    # message names the offending key -- never its value, which may be pasted credential
    # material. ARCC guidance on tagging (cnt_SaTYaDCgBBJTcv) is explicit that incorrect tag
    # propagation is a security failure and not only a cost-reporting one, because tags carry
    # ABAC decisions.
    #
    # The two artifacts take DIFFERENT validators here, and the asymmetry is the point rather
    # than a drift. A live deploy is stamped by this platform's step roles, whose IAM policies
    # enumerate the tag-key namespaces they may write (the ``aws:TagKeys`` allowlists in
    # ``infra/stacks/platform/step_lambdas.py``), so a key outside them is an AccessDenied
    # midway through a real deployment -- measured live, see ``stampable_governance_tags``. An
    # EXPORT is deployed under the recipient's own role in the recipient's own account, where
    # this platform's allowlist does not apply and asserting it would refuse a tag their account
    # would have accepted. Both still share every AWS-legality rule.
    try:
        if artifact == "export":
            return validated_governance_tags(resolved.tags)
        return stampable_governance_tags(resolved.tags)
    except GovernanceTagError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


#: F-82 fallback bound, in seconds, when the stack has not published one. Deliberately the
#: same arithmetic as infra/stacks/platform/config.py (30-minute state machine ceiling plus
#: 5 minutes of slack) so a stack deployed before that env var existed behaves identically
#: rather than reverting to the permanent lock.
_PENDING_CLAIM_TTL_DEFAULT_SECONDS = 30 * 60 + 300


def _pending_claim_still_live(created_at: str | None) -> bool:
    """Could a "pending" AgentVersion row still belong to a running deploy?

    FAILS CLOSED, and that direction is the whole point. Returning True keeps the 409 and
    preserves the cross-tenant guard exactly as it behaves today; returning False releases a
    name. So every uncertainty -- no timestamp, an unparseable one, a clock that puts the row
    in the future -- must answer True. The only case that releases is a timestamp we could
    read that is unambiguously older than the longest a deploy can possibly run.
    """
    if not created_at:
        return True
    try:
        created = datetime.fromisoformat(str(created_at))
    except (TypeError, ValueError):
        return True
    if created.tzinfo is None:
        # A naive timestamp is ambiguous by exactly the reader's offset from UTC, which can be
        # larger than the whole budget. Treat it as unreadable rather than guessing a zone.
        return True
    try:
        ttl = int(os.environ.get("DEPLOY_PENDING_CLAIM_TTL_SECONDS", "").strip() or _PENDING_CLAIM_TTL_DEFAULT_SECONDS)
    except ValueError:
        ttl = _PENDING_CLAIM_TTL_DEFAULT_SECONDS
    if ttl <= 0:
        return True
    age = (datetime.now(timezone.utc) - created).total_seconds()
    return age < ttl


#: Bug 192b — only a LIVE claim should hold a friendly runtime name. A `failed` deploy never
#: produced a usable runtime and a `superseded` row is a retired version; such rows must NOT lock
#: the name forever (a customer hit 409 on 'omar1'/'agent_harness' purely because of leftover
#: failed-deploy rows). Only `pending` (in-flight) and `succeeded` (live) rows are claims.
_LIVE_CLAIM = frozenset({"pending", "succeeded"})


def _version_claim_is_live(status: str | None, created_at: str | None) -> bool:
    """Does this AgentVersion row still hold the friendly runtime name?

    ONE definition, deliberately, because two consumers must agree:

    * the deploy guard, which refuses a name another tenant still holds, and
    * the teardown release, which frees the slot row once nothing holds the name.

    If the release were stricter than the guard, the release would keep a slot row that the guard
    ignores -- and the slot alone is enough to 409 another tenant, so the name would stay locked
    by a row nobody considers live. That is the same customer-visible 409 both were written to
    prevent, reachable only through the gap between them, which is why neither gets its own copy.

    F-82 — "pending" means IN-FLIGHT, and nothing was bounding how long that could last. The row
    is written at request time, before the execution starts, and the only thing that ever moves it
    off "pending" is status_update_step. A state machine that is aborted, or that hits its own
    top-level timeout, is terminated WITHOUT running the Catch, so status_update_step never runs
    and the row stays "pending" for good. Measured live: `sfx0920_abort` sat pending from
    2026-09-20 with its deployment still reading in_progress four days later. Past the state
    machine's own ceiling the execution is provably dead, so the claim stops blocking.
    """
    normalized = (status or "pending").strip() or "pending"
    if normalized not in _LIVE_CLAIM:
        return False
    if normalized == "pending":
        return _pending_claim_still_live(created_at)
    return True


# ---------------------------------------------------------------------------
# POST /api/deploy
# ---------------------------------------------------------------------------


@deployment_app.post(
    "/api/deploy",
    status_code=202,
    dependencies=[Depends(require_scopes("agent:write"))],
    response_model=DeployResponse,
    response_model_by_alias=True,
)
async def handle_deploy(request: DeployRequest, raw_request: Request) -> DeployResponse:
    """Start a Step Functions execution for a new deployment."""
    # FIRST, before anything else in this function: the shared payload validator over the exact
    # JSON body. It runs ahead of the guards below, ahead of the registry query, and far ahead of
    # any persistence, because a refusal here must cost nothing -- no DynamoDB row, no version
    # row, no staged secret, no execution.
    await _reject_invalid_deploy_request(raw_request)

    # SECOND, and still before any side effect: the caller must be authenticated.
    #
    # ``_get_user_id`` returns None for a missing authorizer claim AND swallows every exception,
    # so an API Gateway route wired without its JWT authorizer -- or wired to the wrong one --
    # yields None here rather than an error. Everything downstream then tolerates that: the
    # AgentVersion row is written with ``owner_sub=user_id or ""`` and the execution input with
    # ``owner_sub: user_id or ""``, producing rows and AWS resources owned by the empty string.
    # Those are unowned: no tenant filter matches them, no owner-checked delete will remove them,
    # and the ownership probes that gate teardown cannot prove provenance for them.
    #
    # The PREPARED gate does reject an empty owner_sub, but it runs after ``store.create``, after
    # the pending AgentVersion put, and after credential staging -- post-hoc enforcement of an
    # authorization decision, which cnt_jljdNeOwgPnFx2 names explicitly as a pitfall and requires
    # be made outside and ahead of the side-effecting path. A route-authorizer miswire must cost
    # zero rows, zero secrets and zero executions, so it is refused here instead.
    #
    # 401 rather than 403: the caller has presented no identity, so this is not a permission
    # decision about a known principal.
    #
    # RESOLVED EXACTLY ONCE, and the authorized value is the one every writer below uses. An
    # earlier version called ``_get_user_id`` here and again at the persistence site. Because
    # ``_get_user_id`` swallows every exception and returns None, two resolutions mean the
    # subject that passes this gate is not necessarily the subject that lands in ``owner_sub``:
    # a second call returning None would satisfy the gate and then write an unowned row. The
    # gate is only worth having if it authorizes the value that is actually persisted.
    user_id = (_get_user_id(raw_request) or "").strip()
    if not user_id:
        logger.error(
            "Deploy refused: no authenticated caller identity on the request. If this is not a "
            "client error, the route's JWT authorizer is misconfigured."
        )
        raise HTTPException(
            status_code=401,
            detail=("Authentication is required to deploy. No resource was created."),
        )

    # THIRD, still before any side effect: if the caller named a flow, it must be a flow that
    # exists and that THEY own. ``flow_id`` is caller-supplied and becomes part of the persisted
    # deployment record, so accepting it unchecked would let one tenant file their deployments
    # under another tenant's flow -- an object-level authorization gap of exactly the kind
    # cnt_dwzZ05hLnqhYXQ names (authorizing on caller identity alone, without checking the
    # caller's relationship to the object being referenced). Checked HERE, with the other two
    # gates, because cnt_jljdNeOwgPnFx2 requires the authorization decision be enforced outside
    # and ahead of the side-effecting path, not compensated for afterwards.
    _reject_unowned_flow(request.flow_id, user_id)

    _reject_cfn_only_naming_profile(request, "a live platform deployment")
    _reject_missing_provider_credential(request)
    _reject_mcp_inheriting_platform_otel(request)

    # Loom-study 4.2 — resolve a named VPC profile to a concrete vpc_config before
    # the SFN starts, so runtime_configure_step sees the subnets/SGs (Phase-0 0.1).
    # An explicit vpc_config wins; an unknown profile name is a loud 400.
    _vpc_profile = getattr(request.config, "vpc_profile", None)
    if _vpc_profile and not getattr(request.config, "vpc_config", None):
        try:
            from app.services.vpc_profile_store import resolve_vpc_config

            _resolved = resolve_vpc_config(
                "default",
                _vpc_profile,
                os.environ.get("TAG_POLICY_TABLE_NAME", ""),
                os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1")),
            )
        except Exception:  # noqa: BLE001
            _resolved = None
        if _resolved is None:
            raise HTTPException(status_code=400, detail=f"Unknown VPC profile: {_vpc_profile}")
        request.config.vpc_config = _resolved

    # Integration gating (Loom-study 1.4): when AWS Agent Registry federation is
    # enabled, an agent may only be wired to APPROVED external MCP servers / A2A
    # peers. Collect the connected external identifiers (endpoint URLs / names)
    # and reject the deploy if any is not APPROVED. No-op when federation is off.
    try:
        from app.services.aws_agent_registry import (
            RegistryQueryFailed,
            unapproved_integrations,
        )

        _idents: list[str] = []
        _mcp = request.mcp_server_config or {}
        if isinstance(_mcp, dict):
            for k in ("endpoint", "url", "name", "server_url", "serverUrl"):
                if _mcp.get(k):
                    _idents.append(str(_mcp[k]))
        _a2a = request.a2a_config or {}
        if isinstance(_a2a, dict):
            for u in _a2a.get("peer_allowlist") or _a2a.get("peerAllowlist") or []:
                _idents.append(str(u))
        try:
            _blocked = unapproved_integrations(_idents)
        except RegistryQueryFailed as _rqe:
            # Still fail CLOSED — a governance control that opens on error is not a
            # control. But 503, not 403, and name the real cause: approval status is
            # UNKNOWN, not "denied". Reporting this as 403 would send the operator to
            # approve records that may already be approved, while the actual fix is an
            # IAM action or the registry id.
            logger.error("integration gating could not resolve approval status: %s", _rqe)
            raise HTTPException(
                status_code=503,
                detail=(
                    "Integration gating is enabled but the Agent Registry could not be "
                    f"queried, so approval status is unknown ({_rqe}). Refusing the deploy "
                    "rather than let an unreviewed integration through. Check the active "
                    "registry backend: for AWS Agent Registry, that the deployment role "
                    "holds agent-registry:ListRegistryRecords and the registry id is "
                    "correct; for a LiteLLM catalog, that the proxy is reachable and its "
                    "virtual key is still valid."
                ),
            ) from _rqe
        if _blocked:
            raise HTTPException(
                status_code=403,
                detail=(
                    "These integrations are not APPROVED in the Agent Registry and "
                    f"cannot be used in a deployment: {_blocked}"
                ),
            )
    except HTTPException:
        raise
    except Exception as _gate_exc:  # noqa: BLE001
        # F-10: fail CLOSED. This arm used to log a "gating skipped" warning and let the deploy
        # proceed, so a DynamoDB throttle, a JSON decode error or an attribute error inside a
        # registry provider silently removed the governance gate -- an authorization decision
        # defaulting to allow, which ARCC cnt_dwzZ05hLnqhYXQ names as the anti-pattern. The only
        # legitimate skip is the explicit one: ``unapproved_integrations`` returns ``[]`` when
        # federation is not configured (``get_registry()`` is None) and never reaches this arm.
        # Same status and the same honesty as the ``RegistryQueryFailed`` arm above: approval
        # status is UNKNOWN, not denied, so 503 and not 403. The exception's message is not
        # served: it is a provider's raw text and may echo request parameters.
        logger.error(
            "integration gating could not complete (%s); refusing the deploy rather than skip the gate",
            type(_gate_exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Integration gating is enabled but the approval check failed before it could "
                f"reach a verdict ({type(_gate_exc).__name__}), so approval status is unknown. "
                "Refusing the deploy rather than let an unreviewed integration through. Retry; "
                "if it persists, check the active registry backend's health and configuration."
            ),
        ) from _gate_exc

    deployment_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    # ``user_id`` is NOT re-resolved here. It is the stripped, non-empty subject the 401 gate
    # above authorized; see the comment there for why a second resolution is unsafe.

    # Phase 1 Gap 1A — versioning. Mint a sortable version_id for this deploy
    # and resolve the AgentCore-side runtime name (friendly + version suffix)
    # so each version maps to a distinct AgentCore runtime ARN. The previous
    # production version, if any, becomes parent_version_id so the version
    # graph can be reconstructed from DDB without scanning history.
    from app.services.agent_versions_store import (
        AgentVersion,
        NameClaimConflict,
        get_slots_store,
        get_versions_store,
        new_version_id,
        short_version_suffix,
    )
    from app.services.runtime_deployer import sanitize_runtime_name

    friendly_runtime_name = sanitize_runtime_name(request.config.name or f"agent-{deployment_id[:8]}")
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
    parent_version_id: str | None = None
    slots_store = get_slots_store()
    versions_store = get_versions_store()
    try:
        # This read authorizes a globally shared name. Eventual consistency is not an
        # availability optimization here: a stale "missing" answer lets a second tenant
        # proceed until the transactional version put catches it, after a DeploymentState
        # row has already been written. Read the exact key strongly and fail closed when the
        # ownership oracle is unavailable.
        slots = slots_store.get(friendly_runtime_name, consistent=True)
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
    except Exception as exc:  # noqa: BLE001 - an unreadable ownership oracle is not permission
        logger.error(
            "RuntimeSlotsStore ownership read failed for %s (%s); refusing before persistence",
            friendly_runtime_name,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Runtime-name ownership could not be checked, so this deployment was refused "
                "before anything was created. Retry shortly."
            ),
        ) from exc

    # Defense in depth: also check the AgentVersions table for any foreign-owner
    # row under this friendly name. Covers the case where slots may not yet
    # exist (a partial earlier deploy that never reached status_update) but
    # the versions table already has rows owned by another sub.
    #
    # Bug 192b / F-82 — only a LIVE claim should hold the name, and `_version_claim_is_live` is
    # the single definition of that, shared with the teardown release so the two cannot drift.
    try:
        existing_versions = versions_store.list_for_runtime(friendly_runtime_name, consistent=True)
        for v in existing_versions:
            if not v.owner_sub or v.owner_sub == (user_id or ""):
                continue
            if not _version_claim_is_live(v.status, v.created_at):
                logger.info(
                    "Ignoring non-live claim on runtime name %s (version %s, status %s, created %s)",
                    friendly_runtime_name,
                    v.version_id,
                    v.status,
                    v.created_at,
                )
                continue
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Runtime name '{friendly_runtime_name}' is already in use "
                    f"by another tenant. Pick a different name."
                ),
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - an unreadable ownership oracle is not permission
        logger.error(
            "AgentVersionsStore ownership read failed for %s (%s); refusing before persistence",
            friendly_runtime_name,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Runtime-name ownership could not be checked, so this deployment was refused "
                "before anything was created. Retry shortly."
            ),
        ) from exc

    # Phase 3 Gap 3H — resolve a library-prompt reference in config.system_prompt
    # to its actual body BEFORE the config is serialized into the SFN input.
    # Tenant-scoped to the deploying caller (owner OR same-org visibility);
    # an inline-string systemPrompt is left untouched (back-compat). Never
    # hard-fails: a missing/foreign ref logs and keeps the original value.
    from app.services.prompt_resolver import resolve_system_prompt

    resolve_system_prompt(request.config, user_id)

    # Phase 7 (opt-in) deployment targets. Resolve the exact account, region,
    # and role before creating any persistent deployment/version records. This
    # both fails an invalid target cleanly and preserves the role actually used
    # so a later teardown does not depend on mutable registry state.
    target_account_id: str | None = None
    target_region: str | None = None
    target_role_arn: str | None = None
    target_runtime_role_arn: str | None = None
    target_mcp_runtime_role_arn: str | None = None
    target_harness_role_arn: str | None = None
    target_artifact_bucket: str | None = None
    if request.target_account_id or request.target_region:
        from app.services.deploy_target import (
            TargetError,
            resolve_registered_account_target,
            resolve_registered_region_target,
            targets_enabled,
        )

        if not targets_enabled():
            raise HTTPException(
                status_code=400,
                detail="Deployment targets are disabled; enable them (admin) before targeting an account/region",
            )
        try:
            target_account_id = request.target_account_id
            if target_account_id:
                target = resolve_registered_account_target(
                    target_account_id,
                    request.target_region,
                )
                target_region = target["region"]
                target_role_arn = target["role_arn"]
                target_runtime_role_arn = target["runtime_role_arn"]
                target_mcp_runtime_role_arn = target["mcp_runtime_role_arn"]
                target_harness_role_arn = target["harness_role_arn"]
                target_artifact_bucket = target["artifact_bucket"]
                _reject_unsupported_cross_account_features(request)
            else:
                target = resolve_registered_region_target(request.target_region)
                target_region = target["region"]
                target_artifact_bucket = target["artifact_bucket"]
        except TargetError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Phase 2 (Loom) governance tagging — resolve the org's tag policies against
    # the caller's supplied tags / selected profile BEFORE starting the deploy.
    # A missing REQUIRED tag fails fast with HTTP 400 (rather than dying mid-SFN).
    # P0-B: resolution FAILS CLOSED (see ``_resolve_governance_or_refuse``). It used to
    # swallow every non-TagResolutionError and log "Tag resolution skipped (non-fatal)",
    # so an outage of the tag-policy table deployed untagged resources and reported
    # success — while this same file already failed closed on that store in two other
    # places. A governance control that opens on error is not a governance control.
    #
    # F-55 ORDERING: this MUST stay above ``store.create`` and the pending AgentVersion put.
    # It used to sit below both, so a missing required tag returned HTTP 400 having already
    # written a DeploymentState row in PENDING and an AgentVersion row in "pending" -- and
    # nothing ever moved them, because the only thing that flips them is a step handler and no
    # execution was ever started. The caller saw a clean 400 while the tables accumulated a
    # deployment that did not exist. Refusing before either write makes the 400 honest.
    resolved_tags: dict = _resolve_governance_or_refuse(request)

    state = DeploymentState(
        deployment_id=deployment_id,
        # ``workflow_id`` is the FLOW (owner-checked above) and ``node_id`` the canvas node.
        # They were conflated here, which is why ValidateWorkflow's lookup could never succeed.
        # No flow named -> ``workflow_id`` is None and the row is simply absent from the
        # ``workflow_id-index`` rather than indexed under a node id.
        workflow_id=request.flow_id,
        node_id=request.node_id,
        user_id=user_id,
        status=DeploymentStatusEnum.PENDING,
        started_at=now,
        version_id=version_id,
        parent_version_id=parent_version_id,
        deployment_slot=request.deployment_slot or "production",
        agentcore_runtime_name=agentcore_runtime_name,
        # F-81: the exact partition key of the versions/slots rows this deploy is about to
        # create. The delete path has always read this field and it has never been written, so
        # the name release silently resolved to a key that does not exist. It is already on the
        # state machine input below; persisting it costs one attribute and is the only value
        # that stays correct when the friendly name is long enough to be truncated in
        # ``agentcore_runtime_name``.
        friendly_runtime_name=friendly_runtime_name,
        # Phase B — persist the chosen path up-front so the delete/test handlers
        # know whether to route to harness_deployer even on partial-failed deploys.
        deployment_mode=request.deployment_mode or "runtime",
        resource_manifest_version=1,
        resource_manifest_complete=False,
        resource_manifest_error=False,
        # Phase 7 (opt-in) — persist the deploy target so the SEPARATE delete
        # request can assume the same cross-account role to tear down. None →
        # home account (unchanged).
        target_account_id=target_account_id,
        target_region=target_region,
        target_role_arn=target_role_arn,
        target_artifact_bucket=target_artifact_bucket,
        # Persist the validated invocation protocol so restored/listed deployments and the
        # product MCP explorer know how to talk to this runtime rather than trusting a
        # browser claim. Legacy rows default to HTTP (see DeploymentState.runtime_protocol).
        runtime_protocol=request.config.protocol,
    )

    store = _get_state_store()
    store.create(state)

    # Persist a *pending* AgentVersion row so partial-failed deploys still
    # surface in the version history for the UI. status flips to succeeded
    # in status_update_step on completion. See Bug 85 — every step that
    # creates a shared resource MUST persist its ID immediately, not wait
    # for the final status_update.
    try:
        versions_store.put(
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
    except NameClaimConflict as exc:
        # The preflight reads above can both be true and still lose to another
        # tenant before this transaction. ``AgentVersionsStore.put`` binds the
        # sentinel owner in the same transaction as the version row, so this is
        # the authoritative admission decision. Nothing was staged and no state
        # machine was started. Settle the already-created DeploymentState
        # best-effort, but never let a secondary status-write outage turn this
        # sanitized 409 into a 500 or expose the transaction request.
        logger.warning(
            "Runtime name claim conflict for %s; refusing deployment %s before staging",
            friendly_runtime_name,
            deployment_id,
        )
        try:
            store.update_status(
                deployment_id,
                DeploymentStatusEnum.FAILED,
                error_details="Deployment admission refused: runtime name is already in use",
            )
        except Exception as settle_exc:  # noqa: BLE001 - preserve the primary refusal
            logger.error(
                "Could not settle refused deployment %s after name-claim conflict (%s)",
                deployment_id,
                type(settle_exc).__name__,
            )
        raise HTTPException(
            status_code=409,
            detail=(
                f"Runtime name '{friendly_runtime_name}' is already in use by another "
                "deployment. Pick a different name or retry after its teardown completes."
            ),
        ) from exc
    except Exception as exc:  # noqa: BLE001 - admission storage fails closed
        # A throttled, unavailable or misconfigured versions table cannot be
        # treated as "versioning is optional": that row is the ownership fence
        # used by invocation, promotion, triggers and teardown. Continuing would
        # stage credentials and start AWS work with no durable claim to clean up.
        logger.error(
            "AgentVersionsStore.put failed for deployment %s (%s); refusing before staging",
            deployment_id,
            type(exc).__name__,
        )
        try:
            store.update_status(
                deployment_id,
                DeploymentStatusEnum.FAILED,
                error_details=f"Deployment admission unavailable: {type(exc).__name__}",
            )
        except Exception as settle_exc:  # noqa: BLE001 - preserve the primary refusal
            logger.error(
                "Could not settle refused deployment %s after version-store failure (%s)",
                deployment_id,
                type(settle_exc).__name__,
            )
        raise HTTPException(
            status_code=503,
            detail=(
                "The deployment ownership record could not be created, so no credentials "
                "were staged and no workflow was started. Retry shortly."
            ),
        ) from exc

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
    # A template keeps the components it advertises (customer-support: Gateway + Memory),
    # as codegen and both exporters read it. Codegen alone is not enough: the state
    # machine runs the Memory step only when memory_config is present, so an implied
    # Memory with no config would ship a runtime without MEMORY_ID.
    template_implied = template_implied_capabilities(request.template_id)
    auto_connected.extend(sorted(template_implied - set(auto_connected)))
    prepared_memory_config = request.memory_config
    if "memory" in template_implied and not prepared_memory_config:
        prepared_memory_config = {"enabled": True}

    prepared_gateway_config = copy.deepcopy(request.gateway_config) if request.gateway_config else None
    if prepared_gateway_config is None and _gateway_implied(
        request.gateway_tools,
        request.connectors,
        auto_connected,
        request.external_mcp_servers,
    ):
        # deploy_gateway only requires `name`; it fills the rest. Reuse the
        # friendly runtime/harness name so the gateway is recognizably paired
        # with its agent.
        prepared_gateway_config = {"name": friendly_runtime_name}

    prepared_connectors: list[dict] = []
    for connector in request.connectors or []:
        item = connector.model_dump(mode="json", by_alias=False, exclude_none=True)
        if connector.secret_value:
            item["secret_value"] = connector.secret_value
        prepared_connectors.append(item)

    credential_region = target_region or config.aws_region
    credential_target_event = _credential_target_event(
        target_account_id=target_account_id,
        target_region=credential_region,
        target_role_arn=target_role_arn,
    )
    staged_secret_arns: list[str] = []

    # Provider and OTEL source ARNs are caller-controlled inventory, not authority.
    # Validate live ownership and copy their values into an exact deployment-bound
    # secret in the selected target before the SFN input is serialized. This also
    # makes cross-account runtimes independent of source-account secret grants.
    try:
        (
            prepared_runtime_config,
            prepared_observability_config,
            prepared_platform_observability_defaults,
            runtime_staged_secret_arns,
        ) = _prepare_runtime_credentials(
            runtime_config=request.config.model_dump(mode="json", by_alias=True),
            observability_config=request.observability_config,
            deployment_id=deployment_id,
            owner_sub=user_id or "",
            target_account_id=target_account_id,
            target_region=credential_region,
            target_role_arn=target_role_arn,
            store=store,
            resource_tags=resolved_tags,
            identity_config=request.identity_config,
        )
        staged_secret_arns.extend(runtime_staged_secret_arns)
    except ConnectorSecretBindingError as exc:
        store.update_status(
            deployment_id,
            DeploymentStatusEnum.FAILED,
            error_details=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Runtime authentication staging failed before Step Functions start: %s",
            type(exc).__name__,
        )
        store.update_status(
            deployment_id,
            DeploymentStatusEnum.FAILED,
            error_details=f"Runtime credential staging failed: {type(exc).__name__}",
        )
        raise HTTPException(
            status_code=500,
            detail="Deployment credential staging failed. No workflow was started.",
        ) from exc

    try:
        (
            prepared_knowledge_base_config,
            kb_staged_secret_arns,
        ) = _prepare_knowledge_base_credentials(
            knowledge_base_config=request.knowledge_base_config,
            deployment_id=deployment_id,
            owner_sub=user_id or "",
            target_account_id=target_account_id,
            target_region=credential_region,
            target_role_arn=target_role_arn,
            store=store,
            resource_tags=resolved_tags,
            identity_config=request.identity_config,
        )
        staged_secret_arns.extend(arn for arn in kb_staged_secret_arns if arn not in staged_secret_arns)
    except ConnectorSecretBindingError as exc:
        _cleanup_staged_credentials(
            staged_secret_arns,
            deployment_id=deployment_id,
            target_event=credential_target_event,
        )
        store.update_status(
            deployment_id,
            DeploymentStatusEnum.FAILED,
            error_details=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        _cleanup_staged_credentials(
            staged_secret_arns,
            deployment_id=deployment_id,
            target_event=credential_target_event,
        )
        logger.error(
            "Knowledge Base authentication staging failed before Step Functions start: %s",
            type(exc).__name__,
        )
        store.update_status(
            deployment_id,
            DeploymentStatusEnum.FAILED,
            error_details=(f"Knowledge Base credential staging failed: {type(exc).__name__}"),
        )
        raise HTTPException(
            status_code=500,
            detail="Deployment credential staging failed. No workflow was started.",
        ) from exc

    try:
        (
            prepared_gateway_config,
            prepared_connectors,
            prepared_external_mcp,
            gateway_staged_secret_arns,
        ) = _prepare_deployment_credentials(
            gateway_config=prepared_gateway_config,
            connectors=prepared_connectors,
            external_mcp_servers=request.external_mcp_servers,
            deployment_id=deployment_id,
            owner_sub=user_id or "",
            target_account_id=target_account_id,
            target_region=credential_region,
            target_role_arn=target_role_arn,
            store=store,
            resource_tags=resolved_tags,
            identity_config=request.identity_config,
        )
        staged_secret_arns.extend(arn for arn in gateway_staged_secret_arns if arn not in staged_secret_arns)
    except ConnectorSecretBindingError as exc:
        _cleanup_staged_credentials(
            staged_secret_arns,
            deployment_id=deployment_id,
            target_event=credential_target_event,
        )
        store.update_status(
            deployment_id,
            DeploymentStatusEnum.FAILED,
            error_details=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        _cleanup_staged_credentials(
            staged_secret_arns,
            deployment_id=deployment_id,
            target_event=credential_target_event,
        )
        logger.error(
            "Gateway authentication staging failed before Step Functions start: %s",
            type(exc).__name__,
        )
        store.update_status(
            deployment_id,
            DeploymentStatusEnum.FAILED,
            error_details=f"Credential staging failed: {type(exc).__name__}",
        )
        raise HTTPException(
            status_code=500,
            detail="Deployment credential staging failed. No workflow was started.",
        ) from exc

    # The standalone FastMCP runtime is model-free: its RuntimeConfig defaults
    # (framework, an implicit provider, the placeholder system prompt) are inert
    # for a protocol tool server. DeployRequest._mcp_protocol_admission already
    # rejected any EXPLICITLY-supplied model-only field, so the only ones that can
    # still be present here are those defaults. Drop them from the Step Functions
    # input rather than carrying model-only settings that the MCP code generator
    # and runtime never read — a deployed contract must not ship inert config.
    if request.template_id == "mcp-server-runtime" and isinstance(prepared_runtime_config, dict):
        for _inert in (
            "framework",
            "model",
            "modelProvider",
            "providerApiKeyRef",
            "providerBaseUrl",
            "systemPrompt",
            "multiAgentPattern",
            "multiAgentConfig",
        ):
            prepared_runtime_config.pop(_inert, None)

    sfn_input = {
        "deployment_id": deployment_id,
        "workflow_id": request.flow_id,
        "node_id": request.node_id,
        "config": prepared_runtime_config,
        "connected_tools": auto_connected,
        "template_id": request.template_id,
        # Phase 2 (Loom) governance tagging — resolved tag set applied to every
        # AWS resource the step handlers create (threaded via sfn_input).
        "resource_tags": resolved_tags,
        # Phase 7 (opt-in) — target account/region/role for the deploy's boto3
        # clients (services/step_clients reads these). None → home (unchanged).
        # The role ARN is resolved here (deployment Lambda has the Settings
        # table) so step Lambdas assume it without a Settings lookup.
        "target_account_id": target_account_id,
        "target_region": target_region,
        "target_role_arn": target_role_arn,
        "target_runtime_role_arn": target_runtime_role_arn,
        "target_mcp_runtime_role_arn": target_mcp_runtime_role_arn,
        "target_harness_role_arn": target_harness_role_arn,
        "target_artifact_bucket": target_artifact_bucket,
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
    if staged_secret_arns:
        # Internal coordination only. Later steps use this to avoid duplicate
        # manifest rows while retaining their legacy/direct-call fallback.
        sfn_input["recorded_secret_arns"] = staged_secret_arns
    if prepared_gateway_config:
        sfn_input["gateway_config"] = prepared_gateway_config
    if request.gateway_tools:
        sfn_input["gateway_tools"] = request.gateway_tools
    if request.identity_config:
        sfn_input["identity_config"] = request.identity_config.model_dump(mode="json", by_alias=True)
    if request.custom_tools:
        sfn_input["custom_tools"] = [t.model_dump(mode="json", by_alias=True) for t in request.custom_tools]
    if prepared_connectors:
        sfn_input["connectors"] = prepared_connectors
    if prepared_external_mcp:
        sfn_input["external_mcp_servers"] = prepared_external_mcp
    if prepared_memory_config:
        sfn_input["memory_config"] = prepared_memory_config
    if request.evaluation_config:
        sfn_input["evaluation_config"] = request.evaluation_config
    if request.policy_config:
        sfn_input["policy_config"] = request.policy_config
    if request.mcp_server_config:
        sfn_input["mcp_server_config"] = request.mcp_server_config
    if prepared_knowledge_base_config:
        sfn_input["knowledge_base_config"] = prepared_knowledge_base_config
    if request.guardrails_config:
        sfn_input["guardrails_config"] = request.guardrails_config
    if prepared_observability_config:
        sfn_input["observability_config"] = prepared_observability_config
    if prepared_platform_observability_defaults:
        # Freeze the operator defaults used at the deploy boundary. In particular,
        # this contains the deployment-bound target-account copy of the OTEL auth
        # secret rather than the long-lived platform source ARN from SSM.
        sfn_input["platform_observability_defaults"] = prepared_platform_observability_defaults
    if getattr(request, "a2a_config", None):
        sfn_input["a2a_config"] = request.a2a_config

    # F-55 PREPARED gate. Validate the EXACT dict that is about to become the execution input,
    # after every staging and preparation step above has run, and BEFORE StartExecution.
    #
    # Why here and not only at the API boundary. The two phases catch different things and
    # neither subsumes the other. REQUEST refuses a caller who sends a raw credential at a path
    # that is not an approved write-only one. PREPARED refuses a payload that WE built wrongly:
    # a raw value that staging failed to replace, a staged ARN naming the wrong account or the
    # wrong namespace, a server-authored field we forgot to set. Those are our bugs, not the
    # caller's, and this is the last point at which they cost nothing.
    #
    # Why it matters that this is the exact dict. The state machine's first task revalidates the
    # payload it receives; if we validated something *else* here -- a reconstruction, a dump, the
    # request model -- the two could disagree and the disagreement would surface as a deployment
    # that dies at ValidateWorkflow after the execution is already running and the secrets are
    # already staged. Validating ``sfn_input`` itself makes a PREPARED pass here a guarantee that
    # the in-SFN check passes too, so a refusal is always pre-execution.
    prepared_result = validate_deployment_payload(
        sfn_input,
        phase=PayloadPhase.PREPARED,
        context=_api_validation_context(),
    )
    if not prepared_result.is_valid:
        logger.error(
            "Prepared deployment payload rejected before StartExecution for %s: %s",
            deployment_id,
            prepared_result.as_error_dicts(),
        )
        # Compensate in full. Nothing has started, so unlike the StartExecution failure path
        # below there is no live execution to race: the staged secrets are unambiguously ours to
        # delete, and leaving them would strand a deployment-bound copy of a customer credential
        # that no later cleanup is ever triggered to remove.
        _cleanup_staged_credentials(
            staged_secret_arns,
            deployment_id=deployment_id,
            target_event=credential_target_event,
        )
        store.update_status(
            deployment_id,
            DeploymentStatusEnum.FAILED,
            error_details=prepared_result.summary(),
        )
        # The pending AgentVersion row too. The rows are written before staging runs, so a
        # refusal here would otherwise leave the version history showing a "pending" version
        # forever -- the same stranded-row defect the tag-resolution reorder above fixes.
        try:
            get_versions_store().update_status(
                friendly_runtime_name,
                version_id,
                status="failed",
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to mark AgentVersion %s/%s failed after a prepared-payload refusal",
                friendly_runtime_name,
                version_id,
            )
        # 500, not 4xx: reaching this point means the platform built an invalid payload from a
        # request that already passed the REQUEST gate. That is a server defect and must not be
        # reported to the caller as their mistake.
        raise HTTPException(
            status_code=500,
            detail=(
                "Deployment could not be started: the prepared deployment payload failed "
                "server-side validation. No workflow was started and no resource was created."
            ),
        )

    execution_arn: str | None = None
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
        # Only compensate when StartExecution itself did not return an execution
        # ARN. Once SFN is running, its manifest-driven failure cleanup owns these
        # resources; deleting them here would race a live deployment.
        if not execution_arn:
            _cleanup_staged_credentials(
                staged_secret_arns,
                deployment_id=deployment_id,
                target_event=credential_target_event,
            )
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
        ) from exc

    # No execution_arn on the response. It is written to the stored record above (an
    # operator needs it to find the execution in the console) but never served -- see
    # INTERNAL_ONLY_STATE_FIELDS in models/deployment_models.py. The caller polls
    # GET /api/deploy/{deployment_id}.
    return DeployResponse(
        deployment_id=deployment_id,
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


@deployment_app.get("/api/deploy/{deployment_id}", dependencies=[Depends(require_scopes("agent:read"))])
async def handle_deploy_status(deployment_id: str, raw_request: Request) -> dict:
    """Query deployment state from DynamoDB. Caller must own the deployment."""
    deployment_id = _validate_deployment_id(deployment_id)
    store = _get_state_store()
    state = store.get(deployment_id)

    if state is None:
        raise HTTPException(status_code=404, detail=f"Deployment '{deployment_id}' not found")

    # SECURITY — tenant isolation. This route had none: measured live 2026-09-20 against
    # acfe2e-p0920, e2e-user-a fetched a deployment owned by another tenant and got the whole
    # 32-field record, including that tenant's Cognito ``sub`` -- which is the very handle
    # every other ownership check in this module compares against -- plus their runtime id and
    # whatever error_details the record holds. Unguessable uuid4 ids were the only thing
    # standing in front of it, and an id is not a credential.
    #
    # Same rule and the same wording as handle_test_runtime / handle_delete_runtime: a 404,
    # not a 403, so the route is not an existence oracle. Pre-tenancy records (user_id=None)
    # stay readable, matching those routes' documented carve-out (tasks/lessons.md Bug 37).
    owner = state.user_id
    if owner and owner != _get_user_id(raw_request):
        raise HTTPException(status_code=404, detail=f"Deployment '{deployment_id}' not found")

    result = state.model_dump(mode="json", exclude=INTERNAL_ONLY_STATE_FIELDS)
    # Bug 181: the status poll is a natural post-deploy touchpoint to lazy-promote
    # a pending Cedar engine to ENFORCE (the gateway's policy plane converges a few
    # minutes after deploy). _maybe_promote_policy mutates the dict's policy_result
    # in place on success + persists it, so the returned status reflects real
    # enforcement even before the first invoke. Best-effort; never fails the read.
    try:
        _maybe_promote_policy(result, config.aws_region)
    except Exception:  # noqa: BLE001
        logger.warning("status: policy promote attempt skipped")
    # F-02: the served document is the storage model, and ``gateway_result`` is the whole gateway
    # step result. Rows written before ``_mint_client_secret_ref`` carry the Cognito app client's
    # secret INLINE at ``client_info.client_secret`` (``resolve_client_secret`` still honours it,
    # so that row shape is supported storage), next to ``client_id``, ``token_endpoint`` and
    # ``scope`` -- a complete client-credentials grant, readable by any ``agent:read`` caller for a
    # pre-tenancy row. ``INTERNAL_ONLY_STATE_FIELDS`` is a top-level exclude and cannot reach a
    # leaf, so the response boundary strips credential leaves itself, for every row shape. The
    # stored row is untouched: the legacy fallback keeps working (ARCC cnt_dwzZ05hLnqhYXQ,
    # cnt_n8LpZcqYi2t3I2). Last, after the promote, so nothing persisted is ever the redacted copy.
    return strip_credential_leaves(result)


# ---------------------------------------------------------------------------
# GET /api/deployments?workflow_id=...
# ---------------------------------------------------------------------------


@deployment_app.get("/api/deployments", dependencies=[Depends(require_scopes("agent:read"))])
async def handle_list_deployments(
    request: Request,
    workflow_id: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """List the CALLER's deployments, optionally narrowed to one flow and/or a status.

    An earlier version answered ``?workflow_id=`` for a caller with NO resolvable identity by
    querying the ``workflow_id-index`` unfiltered -- every tenant's deployments of that flow.
    It was unreachable only while the route's JWT authorizer stayed wired, which is precisely
    the miswire the deploy 401 gate exists for. The index is now a narrowing of the caller's
    own rows, never a substitute for knowing who the caller is (ARCC cnt_dwzZ05hLnqhYXQ).
    """
    store = _get_state_store()
    user_id = (_get_user_id(request) or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication is required.")
    flow_id = (workflow_id or "").strip()
    if flow_id:
        states = [s for s in store.query_by_workflow(flow_id, status_filter=status) if s.user_id == user_id]
    else:
        states = store.query_by_user(user_id, status_filter=status)
    # F-02: same read-side redaction as the status route; see the comment there.
    return [strip_credential_leaves(s.model_dump(mode="json", exclude=INTERNAL_ONLY_STATE_FIELDS)) for s in states]


# ---------------------------------------------------------------------------
# POST /api/test-runtime
# ---------------------------------------------------------------------------


@deployment_app.post(
    "/api/test-runtime",
    response_model=TestResponse,
    response_model_by_alias=True,
    dependencies=[Depends(require_scopes("invoke"))],
)
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

        # Find deployment record by runtime_id.
        #
        # A record is REQUIRED, and a failure to look one up is NOT the same as not
        # finding one. Both of those used to be treated as "no record", and the tenant
        # check below was written `if deployment_state:` — so an unrecorded runtime and
        # anything that broke the lookup both turned the check OFF, and the code then
        # SYNTHESIZED the ARN from the caller-supplied id. Any authenticated caller
        # could name any runtime in the account, including runtimes this platform never
        # created (this account really does hold foreign ones). A check you can disable
        # by breaking its input is not a check.
        try:
            table = store._table
            deployment_state = _scan_for_runtime(table, runtime_id)
        except Exception as exc:
            logger.warning(
                "Failed to look up deployment state for runtime_id=%s: %s",
                runtime_id,
                exc,
            )
            # 503, not the 404 below: "we could not tell" must not be reported as "it
            # does not exist", and above all must not fall through to success.
            raise HTTPException(
                status_code=503,
                detail="Could not verify this runtime right now. Try again shortly.",
            ) from exc

        # Tenant isolation: caller must own the deployment.
        # Pre-tenancy records (user_id=None) are accessible to keep legacy
        # data working until a backfill pass; new deploys always carry user_id.
        # See tasks/lessons.md Bug 37. Note what that carve-out is and is not: the
        # absence of an OWNER on a record is tolerated; the absence of a RECORD is not.
        owner = (deployment_state or {}).get("user_id")
        if not deployment_state or (owner and owner != caller_sub):
            # One raise for both cases, deliberately: a distinct status or body for
            # "exists but is not yours" would make runtime ids enumerable.
            raise HTTPException(status_code=404, detail="Runtime not found")

        # Wrong-door refusal for MCP-protocol runtimes. This HTTP test path speaks
        # the agent request/response envelope; an MCP-server runtime speaks JSON-RPC
        # over the bounded MCP data plane and is exercised through
        # /api/test-mcp-runtime instead. Invoking it here would send a shape it does
        # not understand. Refuse with 409 BEFORE any side effect (policy promotion,
        # invoke) so a mismatched call is free of consequences. Placed AFTER the
        # ownership check above so the protocol is disclosed only to the owner and
        # the endpoint never becomes a runtime-existence/kind oracle. Legacy records
        # carry no runtime_protocol and default to HTTP, so only an explicit "MCP"
        # is refused. See routers/runtime_mcp.py + models.DeploymentState.runtime_protocol.
        if str(deployment_state.get("runtime_protocol") or "HTTP").upper() == "MCP":
            raise HTTPException(
                status_code=409,
                detail=(
                    "This is an MCP-protocol runtime. Use POST /api/test-mcp-runtime "
                    "to discover and call its tools; the /api/test-runtime path only "
                    "invokes HTTP request/response agents."
                ),
            )

        from app.services import step_clients

        target_event = _deployment_target_event(deployment_state, region)
        region = target_event["target_region"]
        target_session = step_clients.session_for_event(target_event)

        runtime_arn = deployment_state.get("runtime_arn", "")
        if deployment_state.get("deployment_mode") != "harness" and not runtime_arn:
            # Synthesizing the ARN from the caller-supplied id is only safe because
            # ownership of a RECORD for that id was established above. Older records
            # (pre-``runtime_arn``) legitimately reach here.
            # Construct ARN directly from runtime_id instead of calling control plane API
            try:
                sts = target_session.client("sts", region_name=region)
                account_id = sts.get_caller_identity()["Account"]
                runtime_arn = f"arn:aws:bedrock-agentcore:{region}:{account_id}:runtime/{runtime_id}"
                logger.info("Constructed runtime ARN: %s", runtime_arn)
            except Exception as e:
                logger.warning("Could not construct runtime ARN: %s", e)
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot resolve runtime ARN for runtime_id={runtime_id}. Check logs for details.",
                ) from e

        return invoke_verified_http_runtime(
            request,
            caller_sub=caller_sub or "",
            deployment_state=deployment_state,
            runtime_id=runtime_id,
            runtime_arn=runtime_arn,
            region=region,
            target_session=target_session,
            promote_policy=_maybe_promote_policy,
            invoke_harness=invoke_harness,
            resolve_memory_identity=memory_invocation_identity,
            gateway_session=gateway_aws_session,
            get_gateway_token=get_cognito_token,
            start_harness_warmup=_start_harness_warmup,
        )

    except HTTPException:
        # HTTPException IS an Exception, so the broad handler below used to catch this
        # route's own deliberate raises and convert them into HTTP 200 +
        # {"success": false, "error": "An internal error occurred..."}. Measured live
        # 2026-09-20: a non-owner's tenant-isolation refusal (the 404 raised above)
        # arrived at the client as that generic internal-error body, and
        # logger.exception logged a routine authorization denial at ERROR level with a
        # full traceback -- so the refusal is indistinguishable from a crash in both
        # directions, for the caller and for whoever watches the error log. The 400 for
        # an unresolvable runtime ARN was swallowed the same way.
        # Re-raise so the status code survives. Same idiom as _run_delete_cleanup below.
        raise
    except Exception:
        logger.exception("Unexpected error in test-runtime")
        return TestResponse(
            success=False,
            error="An internal error occurred. Check server logs for details.",
        )


# ---------------------------------------------------------------------------
# POST /api/test-runtime-stream  (SSE streaming)
# ---------------------------------------------------------------------------


@deployment_app.post("/api/test-runtime-stream", dependencies=[Depends(require_scopes("invoke"))])
async def handle_test_runtime_stream(request: TestRequest, raw_request: Request):
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
    #
    # F-9: this lookup used to be best-effort and the tenant check below was written
    # `if deployment_state:`, so "no record" and "the lookup blew up" BOTH disabled it
    # and the ARN was then synthesized from the caller-supplied id. A record is now
    # required, and a failed lookup is reported as such rather than as absence.
    store = _get_state_store()
    try:
        table = store._table
        deployment_state = _scan_for_runtime(table, runtime_id)
    except Exception:
        logger.warning("Could not resolve deployment state for %s", runtime_id, exc_info=True)
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(
            f"data: {json.dumps({'type': 'error', 'error': 'Could not verify this runtime right now. Try again shortly.'})}\n\n",
            media_type="text/event-stream",
        )

    # SECURITY — tenant isolation. This route had NONE, and it is the route the UI
    # calls for every chat turn (DeployPanel.tsx:420, services/api/chat.ts:51), so it
    # is the primary invoke path in the product. The rule below is the same one
    # handle_test_runtime enforces above and stream_handler._stream_invoke enforces on
    # the Function-URL twin -- whose comment already claims the rule is "identical to
    # handle_test_runtime / delete". It wasn't: the hardened copy is the one no browser
    # calls (the Function URL is AWS_IAM-authed and per infra/stacks/platform/lambdas.py
    # "provisioned but NOT yet wired to the browser"), and the reachable copy took only
    # `request: TestRequest` -- no raw_request, so no caller identity to compare against.
    #
    # Measured live 2026-09-20 against acfe2e-p0920: e2e-user-a POSTed here with
    # runtimeId=web_search_agent_88a6981e-qE8xnm3uaM, owned by another tenant
    # (user_id=b458d4f8-...), and received that agent's real answer in SSE token frames.
    # The sync route refused the identical request. Zero tests referenced this route.
    #
    # Same wording as the other two paths on purpose: "Runtime not found" is not an
    # existence oracle, so a cross-tenant probe learns nothing from the refusal.
    # Pre-tenancy records (user_id=None) stay accessible, matching the sync route's
    # documented carve-out (tasks/lessons.md Bug 37).
    # A missing RECORD is refused with the same frame as a cross-tenant hit, for the
    # same reason: the refusal must not be an existence oracle. The refusal stays an
    # SSE error frame rather than an HTTP status because that is what the browser's
    # EventSource reader on this route consumes (DeployPanel.tsx:420).
    caller_sub = _get_user_id(raw_request)
    owner = (deployment_state or {}).get("user_id")
    if not deployment_state or (owner and owner != caller_sub):
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(
            f"data: {json.dumps({'type': 'error', 'error': 'Runtime not found'})}\n\n",
            media_type="text/event-stream",
        )

    # Wrong-door refusal for MCP-protocol runtimes, mirroring the sync
    # handle_test_runtime 409 above. This browser SSE path speaks the agent
    # request/response envelope; an MCP-server runtime is exercised through
    # /api/test-mcp-runtime instead. Refuse BEFORE any side effect (policy
    # promotion, invoke) and AFTER the ownership check, so the protocol is
    # disclosed only to the owner. The refusal is an SSE error frame (not an HTTP
    # status) because that is what this route's EventSource reader consumes; the
    # frame names /api/test-mcp-runtime so the UI can redirect. Legacy records
    # default to HTTP, so only an explicit "MCP" is refused.
    if str(deployment_state.get("runtime_protocol") or "HTTP").upper() == "MCP":
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(
            f"data: {json.dumps({'type': 'error', 'error': 'This is an MCP-protocol runtime. Use POST /api/test-mcp-runtime to discover and call its tools.'})}\n\n",
            media_type="text/event-stream",
        )

    from app.services import step_clients

    target_event = _deployment_target_event(deployment_state, region)
    region = target_event["target_region"]
    target_session = step_clients.session_for_event(target_event)

    # This is the browser's primary invoke route, so it must participate in the
    # same lazy policy promotion/reconciliation as the sync route. Previously
    # only POST /api/test-runtime did, even though the UI normally calls here.
    _maybe_promote_policy(deployment_state, region)

    # Bug 190 — HARNESS mode must route to the data-plane invoke_harness, NOT
    # invoke_agent_runtime. The frontend uses THIS streaming route for harness
    # tests too (DeployPanel calls /api/test-runtime-stream regardless of mode),
    # so without this branch a harness test calls invoke_agent_runtime with the
    # HARNESS arn and fails with "No endpoint or agent found with qualifier
    # 'DEFAULT' for agent arn:...:harness/...". Mirror the sync handle_test_runtime
    # harness path (and stream_handler.py's branch) here.
    if deployment_state.get("deployment_mode") == "harness":
        from fastapi.responses import PlainTextResponse

        harness_arn = deployment_state.get("harness_arn", "")
        if not harness_arn:
            return PlainTextResponse(
                f"data: {json.dumps({'type': 'error', 'error': 'Harness ARN not found for this deployment'})}\n\n",
                media_type="text/event-stream",
            )
        result = invoke_harness(
            region,
            harness_arn,
            prompt,
            request.session_id or runtime_id,
            agentcore_data_client=target_session.client(
                "bedrock-agentcore",
                region_name=region,
            ),
        )
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
        lines.append(
            f"data: {json.dumps({'type': 'done', 'session_id': request.session_id or runtime_id, 'full_response': out, 'trace_id': result.get('trace_id')})}\n\n"
        )
        return PlainTextResponse("".join(lines), media_type="text/event-stream")

    # F-56: same gate as the sync route, before anything is invoked. An error frame, not a
    # status, for the same reason as the tenant refusal above.
    try:
        memory_identity = memory_invocation_identity(deployment_state, request.session_id, caller_sub)
    except InvocationIdentityError as exc:
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(
            f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n",
            media_type="text/event-stream",
        )
    session_id = memory_identity.session_id if memory_identity else request.session_id

    runtime_arn = deployment_state.get("runtime_arn", "")
    if not runtime_arn:
        # Safe only because an owned record for this id exists — see above.
        try:
            sts = target_session.client("sts", region_name=region)
            account_id = sts.get_caller_identity()["Account"]
            runtime_arn = f"arn:aws:bedrock-agentcore:{region}:{account_id}:runtime/{runtime_id}"
        except Exception:
            logger.exception("Cannot resolve runtime ARN")
            from fastapi.responses import PlainTextResponse

            return PlainTextResponse(
                f"data: {json.dumps({'type': 'error', 'error': 'Cannot resolve runtime ARN'})}\n\n",
                media_type="text/event-stream",
            )

    try:
        from botocore.config import Config as _BotoConfig

        agentcore_client = target_session.client(
            "bedrock-agentcore",
            region_name=region,
            config=_BotoConfig(
                read_timeout=25,
                connect_timeout=5,
                retries={"max_attempts": 0},
            ),
        )
        invoke_params = {
            "agentRuntimeArn": runtime_arn,
            "payload": json.dumps(_runtime_payload(prompt, session_id, memory_identity)),
        }
        if session_id:
            invoke_params["runtimeSessionId"] = session_id

        resp = agentcore_client.invoke_agent_runtime(**invoke_params)
        if not memory_identity:
            session_id = resp.get("runtimeSessionId") or resp.get("sessionId")

        raw_response = resp.get("response", "") or resp.get("body", b"")
        if hasattr(raw_response, "read"):
            raw_response = raw_response.read()
        if isinstance(raw_response, bytes):
            raw_response = raw_response.decode("utf-8", errors="replace")

        parsed = _parse_response_body(str(raw_response))
        tool_receipts = parse_tool_receipts(raw_response)

        # Build SSE events — word-by-word tokens + final done event
        lines = []
        words = parsed.split(" ")
        for i, word in enumerate(words):
            token = word + (" " if i < len(words) - 1 else "")
            lines.append(f"data: {json.dumps({'type': 'token', 'token': token})}\n\n")
        done: dict = {"type": "done", "session_id": session_id, "full_response": parsed}
        if tool_receipts is not None:
            done["tool_receipts"] = tool_receipts
        lines.append(f"data: {json.dumps(done)}\n\n")

        from fastapi.responses import PlainTextResponse

        return PlainTextResponse("".join(lines), media_type="text/event-stream")

    except Exception:
        logger.exception("Runtime invocation failed")
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(
            f"data: {json.dumps({'type': 'error', 'error': 'Internal error'})}\n\n",
            media_type="text/event-stream",
        )


def _runtime_payload(
    prompt: str,
    session_id: str | None,
    memory_identity: InvocationIdentity | None,
    warmup: bool = False,
) -> dict[str, str | bool]:
    """The invoke payload a generated agent reads.

    Session goes in the payload as well as ``runtimeSessionId`` because the memory agent
    reads ``payload["session_id"]``, not the AgentCore context (tasks/lessons.md Bug 29).
    ``actor_id`` is sent only with Memory; without it the agent defaults to ``"user"``,
    which is one namespace for every tenant (F-56).

    ``warmup`` marks the deploy-time ping. A generated Memory agent returns on it before
    Memory or the model, so the ping is not recorded as a turn in the owner's stream;
    an agent without Memory ignores the key and answers the ping as it always has.
    """
    return build_runtime_payload(
        prompt,
        session_id,
        memory_identity,
        warmup=warmup,
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
    return parse_verified_response_body(body)


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


def _is_memory_role_name(role_name: str, deployment_record: dict | None) -> bool:
    """Whether a manifest IAM role is a memory execution role of this deployment."""
    if not role_name:
        return False
    recorded = ((deployment_record or {}).get("memory_result") or {}).get("memory_role_name")
    return role_name == recorded or role_name.startswith("AgentCoreMemory-")


def _shared_pool_children_client(pool_id: str, res_region: str, target_boto3):
    """The Cognito client that may act on a gateway's client or scope in *pool_id*.

    The platform's shared gateway-auth pool lives in the PLATFORM account, whatever
    account the deployment targeted, so its children go through platform credentials
    in the pool's own region. The row's ``account`` names the gateway's account, not
    the pool's. Any other pool stays on the resource's target session.
    """
    from app.services.gateway_deployer import (
        _create_platform_cognito_client,
        _pool_region,
        is_platform_owned_user_pool,
    )

    if is_platform_owned_user_pool(pool_id):
        return _create_platform_cognito_client(_pool_region(pool_id))
    return target_boto3.client("cognito-idp", region_name=res_region)


def _revoke_client_on_kept_gateways(client_id: str, gateways: list[dict], client_for) -> tuple[list[str], bool]:
    """Take *client_id* out of each handed-off gateway's ``allowedClients``.

    F-66b: a teardown that hands a shared gateway to the deployment still on it
    deleted its own app client but left the id in the gateway's authorizer (seen
    live). The gateway validates JWTs offline, so a token minted before the delete
    can stay accepted until it expires. Deleting the client stops new tokens;
    only this stops the ones already issued.

    Returns ``(messages, failed)``. A gateway that is gone, or that no longer lists
    the client, needs nothing. A list that would become empty is never sent: an
    authorizer with no allowed clients stops pinning clients at all, which widens
    the gateway to every client in the pool. When ours is the only client, the list
    becomes ``NO_CLIENT_ALLOWED`` instead (F-66d): leaving the id listed kept a token
    minted earlier accepted, and no survivor loses anything, because none of their
    clients was allowed. Success is the read-back, not the update's 200: the gateway
    must be READY *and* no longer list the client. Errors carry the code only,
    because the message echoes the authorizer configuration.
    """
    messages: list[str] = []
    failed = False
    for gw in gateways:
        gw_id = str(gw.get("id") or "")
        gw_region = str(gw.get("region") or "")
        try:
            ctrl = client_for(gw)
            # F-66e: under the gateway's write lock from the read to the READY read-back,
            # so no update computed before this revoke can land after it.
            with gateway_mutation_lock(ctrl, gw_region, gw_id) as gw_lock:
                assert_agentcore_resource_owned(ctrl, "gateway", gw_id, gw_region)
                detail = gw_lock.read()
                auth = dict((detail or {}).get("authorizerConfiguration") or {})
                jwt = dict(auth.get("customJWTAuthorizer") or {})
                clients = list(jwt.get("allowedClients") or [])
                if client_id not in clients:
                    continue
                remaining = [c for c in clients if c != client_id] or [NO_CLIENT_ALLOWED]

                def _revoked(readback: dict) -> bool:
                    listed = allowed_clients(readback)
                    return bool(listed) and client_id not in listed

                gw_lock.update(
                    preserving_gateway_update(
                        detail,
                        gw_id,
                        overrides={
                            "authorizerConfiguration": {
                                **auth,
                                "customJWTAuthorizer": {**jwt, "allowedClients": remaining},
                            }
                        },
                    ),
                    _revoked,
                )
            messages.append(f"[manifest] gateway {gw_id} no longer allows client {client_id}")
        except Exception as exc:  # noqa: BLE001
            if resource_is_missing(exc):
                continue
            code = exc.response.get("Error", {}).get("Code") if isinstance(exc, ClientError) else type(exc).__name__
            messages.append(f"[manifest] gateway {gw_id} still allows client {client_id}: {code}")
            failed = True
    return messages, failed


def _delete_managed_resource(
    res: dict,
    region: str,
    deployment_id: str | None = None,
    target_role_arn: str | None = None,
    target_session=None,
    owner_sub: str | None = None,
    sidecar_failures: list[str] | None = None,
) -> str:
    """Delete one resource from a deployment's created_resources[] manifest.

    Type-dispatched + idempotent (NotFound is treated as success). Returns a
    human log line, or "" for an unknown type (so older/foreign entries no-op
    rather than fail the whole teardown).

    Phase 7 (opt-in) cross-account teardown: when the manifest recorded an
    ``account`` for this resource, we assume the same cross-account deployment
    role to delete it. All the ``boto3.client(...)`` calls below transparently
    route through that session because ``boto3`` is rebound to a target-aware
    shim. Same-account resources (no recorded account) use the default session —
    unchanged behavior.
    """

    rtype = res.get("type", "")
    rid = res.get("id") or res.get("name") or ""
    rname = res.get("name") or ""
    res_region = res.get("region") or region
    res_account = res.get("account")

    class _TargetBoto3:
        """Minimal boto3 shim: routes .client() through the resource's target."""

        def __init__(self, account, region_, role_arn, session):
            self._event = (
                {
                    "target_account_id": account,
                    "target_region": region_,
                    "target_role_arn": role_arn,
                }
                if account
                else {}
            )
            self._session = session

        def client(self, service, **kwargs):
            """Routing invariant (documented, tested):
            1. a supplied target_session wins -- EVERY production cross-account delete arrives with one (the caller
               resolves it from the deployment record before this function runs);
            2. an account WITH a role but no session resolves through step_clients (assumes the role);
            3. an account with NEITHER session nor role is unreachable from the production caller: it is a direct call
               (probes, operators). It must not resolve a target through the settings table -- a NotFound from THAT
               read was once classified as "the resource is already gone" -- so it uses the default session's client;
            4. rows without an account (same-account deployments) use step_clients' home-account path.
            """
            from app.services import step_clients

            if self._session is not None:
                return self._session.client(service, **kwargs)
            if self._event.get("target_account_id") and not self._event.get("target_role_arn"):
                import boto3 as _module_boto3

                return _module_boto3.client(service, **kwargs)
            return step_clients.client(self._event, service, **kwargs)

    boto3 = _TargetBoto3(res_account, res_region, target_role_arn, target_session)

    def _gone(e: Exception) -> bool:
        # A failure while RESOLVING the target (settings-table GetItem/Query/Scan, role assumption) is not a read of
        # the managed resource: measured, a NotFound from the settings table reported a still-existing policy as
        # "already gone". Only the resource's own service may prove absence.
        op = str(getattr(e, "operation_name", "") or "")
        if op in {"GetItem", "Query", "Scan", "BatchGetItem", "AssumeRole"} or type(e).__name__ == "TargetError":
            return False
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

    def _pool_is_gone(pool_id: str, cognito_client) -> bool:
        """True only when Cognito itself says the pool does not exist.

        Asked only after ownership could not be proven, so an owned pool costs no extra
        call. A pool that is gone takes its app clients and resource servers with it, and
        its ownership can no longer be proven, so the rows below used to report "skipped
        (protected)", a retention, on every retry. Measured live 2026-10-01: nine older
        mcp-server-gateway-target versions stayed delete_retained for a pool an earlier
        attempt had already deleted. Any other describe failure raises, so the teardown
        is retried instead of guessed.
        """
        try:
            cognito_client.describe_user_pool(UserPoolId=pool_id)
        except Exception as exc:  # noqa: BLE001
            if _gone(exc):
                return True
            raise
        return False

    try:
        if rtype == "agent_runtime":
            runtime_ctrl = boto3.client(
                "bedrock-agentcore-control",
                region_name=res_region,
            )
            assert_agentcore_resource_owned(
                runtime_ctrl,
                "agent_runtime",
                rid,
                res_region,
            )
            destroy_kwargs = {
                "client_factory": boto3.client,
                # Target-account runtimes and hosted MCP servers use the stable
                # pre-provisioned Runtime role. It belongs to the target
                # onboarding contract, not to this deployment's manifest.
                "delete_execution_role": not bool(res_account),
            }
            # F-81: the manifest's ``name`` is deliberately NOT passed as ``runtime_name``.
            #
            # ``destroy_runtime`` uses that value to enumerate a TriggersTable partition -- which
            # is keyed by friendly name and is NOT owner-scoped -- and then deletes the Scheduler
            # schedules, EventBridge rules and targets, Lambda function-URL configs and webhook
            # secrets it finds there. So it needs a PROVEN name, and this field is not one: the
            # writers record ``friendly_runtime_name or runtime_id`` (step_handlers/
            # runtime_launch_step.py:62) and ``friendly_runtime_name or runtime_name``
            # (runtime_configure_step.py:391), so when the canvas carried no friendly name the
            # manifest holds a canonical id or an ``<friendly>_<8hex>`` AgentCore name instead --
            # exactly the unproven shape ``_resolve_runtime_name_for_cleanup`` now refuses to
            # synthesise. A tenant whose friendly name happens to equal that string would have its
            # triggers deleted by another tenant's teardown.
            #
            # Omitting it makes ``destroy_runtime`` resolve the name from AgentVersions, which is
            # proof: one unambiguous row mapping this exact runtime id. If no row proves it the
            # triggers leak, which costs money and is visible and fixable.
            # F-08: inside the async teardown the shared confirmation deadline bounds the
            # get_agent_runtime poll; inline there is none and the helper's own bound applies.
            destroy_kwargs.update(_runtime_confirmation_budget())
            r = destroy_runtime(rid, res_region, **destroy_kwargs)
            if r.get("retained"):
                # Not confirmed (still DELETING at the end of the budget, or unreadable): a
                # retention, exactly like the harness arm below, so the row stays a retry handle
                # and is never written "deleted".
                raise ResourceDeletionRefused(r.get("message") or "runtime deletion was not confirmed")
            if not r.get("success", True):
                raise RuntimeError(r.get("message") or "runtime destroy failed")
            # The runtime is gone; a sidecar it could not remove (dashboard, evaluation config,
            # eval log group, eval role) is reported to the caller's failure list instead of
            # failing this row -- the row's resource IS deleted -- and the verdict goes red.
            if sidecar_failures is not None:
                sidecar_failures.extend(f"runtime_sidecar:{item}" for item in (r.get("sidecar_failures") or []))
            return f"[manifest] runtime {rid}: {r.get('message', 'deleted')}"
        if rtype == "harness":
            harness_ctrl = boto3.client(
                "bedrock-agentcore-control",
                region_name=res_region,
            )
            assert_agentcore_resource_owned(
                harness_ctrl,
                "harness",
                rid,
                res_region,
            )
            r = destroy_harness(
                rid,
                res_region,
                agentcore_ctrl=harness_ctrl,
            )
            if r.get("retained"):
                raise ResourceDeletionRefused(r.get("note") or "harness deletion was not confirmed")
            if not r.get("success", True):
                raise RuntimeError(r.get("note") or r.get("error") or "harness destroy failed")
            return f"[manifest] harness {rid}: {r.get('note', 'deleted')}"
        if rtype == "memory":
            # F-56: DeleteMemory answers DELETING, which is not deletion. The confirmed
            # protocol re-checks stack AND caller ownership, deletes idempotently and
            # polls GetMemory to absence; anything short of that raises, so the caller
            # retains the memory (and, below, its execution role) instead of reporting it
            # gone.
            ctrl = boto3.client(
                "bedrock-agentcore-control",
                region_name=res_region,
            )
            delete_memory_confirmed(ctrl, rid, region=res_region, owner_sub=owner_sub, **_memory_confirmation_budget())
            return f"[manifest] memory {rid} confirmed deleted"
        if rtype == "gateway":
            # Bug 187 — delete_gateway FAILS if the gateway still has targets
            # ("...has targets associated with it. Delete all targets before
            # deleting the gateway."). Delete every target first, then the
            # gateway. Without this the teardown leaked the gateway + targets on
            # EVERY gateway-bearing deployment (the error was mis-swallowed as
            # "already gone" — see _gone). Best-effort per target so one stuck
            # target doesn't block the rest.
            _ctrl = boto3.client("bedrock-agentcore-control", region_name=res_region)
            _targets_deleted: list[str] = []

            def _confirm_absent() -> None:
                wait_until_absent(
                    resource_label=f"gateway {rid}",
                    read=lambda: _ctrl.get_gateway(gatewayIdentifier=rid),
                    max_attempts=8,
                    delay_seconds=1.5,
                )

            try:
                # F-66e: under the gateway's write lock from the ownership read to the
                # proof of absence, so no update computed before the delete lands after it.
                with gateway_mutation_lock(_ctrl, res_region, rid) as _gw_lock:
                    assert_agentcore_resource_owned(
                        _ctrl,
                        "gateway",
                        rid,
                        res_region,
                    )
                    # Targets delete asynchronously. Re-list on every gateway retry so
                    # a transient target-delete failure or a target visible on a later
                    # page can still converge.
                    for _attempt in range(8):
                        try:
                            _tgts = list_all(
                                _ctrl,
                                "list_gateway_targets",
                                item_keys=("items", "gatewayTargetSummaries"),
                                request={"gatewayIdentifier": rid, "maxResults": 100},
                            )
                        except Exception as _list_exc:  # noqa: BLE001
                            if _gone(_list_exc):
                                return f"[manifest] gateway {rid} already absent"
                            raise
                        for _t in _tgts:
                            _tid = _t.get("targetId")
                            if not _tid:
                                continue
                            try:
                                _ctrl.delete_gateway_target(
                                    gatewayIdentifier=rid,
                                    targetId=_tid,
                                )
                                _targets_deleted.append(str(_tid))
                            except Exception as _te:  # noqa: BLE001
                                if not _gone(_te):
                                    logger.warning(
                                        "gateway %s target %s delete: %s",
                                        rid,
                                        _tid,
                                        str(_te)[:160],
                                    )
                        try:
                            _gw_lock.delete(_confirm_absent, terminal=(DeletionFailedAfterAccept,))
                            break
                        except ClientError as _ge:
                            if _gone(_ge):
                                break
                            if "target" in str(_ge).lower() and _attempt < 7:
                                time.sleep(5)
                                continue
                            raise
            except Exception as _gw_exc:
                # A gateway missing some targets is a partial teardown, and the caller
                # must say so rather than read one error as "untouched" -- including when
                # the delete was accepted and the gateway then never went away.
                if not _gone(_gw_exc):
                    note_gateway_targets_deleted(_gw_exc, _targets_deleted)
                raise
            # The name claim is settled by _run_delete_cleanup once the whole graph is (F-66f).
            return f"[manifest] gateway {rid} deleted"
        if rtype in ("oauth2_credential_provider", "api_key_credential_provider"):
            provider_name = rname or rid
            deleted = delete_owned_credential_provider(
                boto3.client(
                    "bedrock-agentcore-control",
                    region_name=res_region,
                ),
                provider_name,
                res_region,
            )
            if not deleted:
                return f"[manifest] credential provider {provider_name} already absent"
            return f"[manifest] credential provider {provider_name} deleted from " + ", ".join(deleted)
        if rtype == "online_evaluation_config":
            # Recorded by evaluation_step at create time; deletion is by exact id, never by
            # matching a name (the name can be user-supplied and shared). The per-config
            # eval-results log group belongs to it. Absence is confirmed, not assumed.
            _ctrl = boto3.client("bedrock-agentcore-control", region_name=res_region)
            try:
                _ctrl.delete_online_evaluation_config(onlineEvaluationConfigId=rid)
            except Exception as _ee:  # noqa: BLE001
                if not _gone(_ee):
                    raise
            wait_until_absent(
                resource_label=f"online evaluation config {rid}",
                read=lambda: _ctrl.get_online_evaluation_config(onlineEvaluationConfigId=rid),
                max_attempts=8,
                delay_seconds=1.5,
            )
            _logs = boto3.client("logs", region_name=res_region)
            try:
                _logs.delete_log_group(logGroupName=f"/aws/bedrock-agentcore/evaluations/results/{rid}")
            except Exception as _le:  # noqa: BLE001
                if not _gone(_le):
                    raise
            return f"[manifest] online evaluation config {rid} deleted"
        if rtype == "secret":
            deleted = delete_deployment_bound_secret(
                region=res_region,
                deployment_id=deployment_id or "",
                secret_ref=rid,
                secrets_client=boto3.client("secretsmanager", region_name=res_region),
            )
            return f"[manifest] secret {rid} {'deleted' if deleted else 'already absent'}"
        if rtype == "s3_object":
            # rid is an s3://bucket/key URI for a staged connector OpenAPI spec.
            if rid.startswith("s3://"):
                _b, _, _k = rid[5:].partition("/")
                if _b and _k:
                    s3 = boto3.client(
                        "s3",
                        region_name=res_region,
                    )
                    try:
                        # F-60: every version by id, each proven by its own tags; a bare
                        # delete_object on a versioned bucket only writes a marker.
                        _removed = delete_owned_s3_object(
                            s3,
                            _b,
                            _k,
                            region=res_region,
                            deployment_id=deployment_id,
                            expected_bucket_owner=(str(res_account) if res_account else None),
                        )
                    except Exception as _own_exc:  # noqa: BLE001
                        # A retry after a partial teardown finds the object an earlier pass
                        # already removed. Only a conclusive miss is absence; every other
                        # read failure stays a refusal, and nothing is deleted either way.
                        if resource_is_missing(_own_exc):
                            return f"[manifest] s3 object {rid} already absent"
                        raise
                    if not _removed:
                        return f"[manifest] s3 object {rid} already absent"
                    return f"[manifest] s3 object {rid} deleted ({_removed} version(s))"
            raise ValueError(f"Malformed s3_object manifest identity: {rid!r}")
        if rtype == "iam_role":
            iam = boto3.client("iam")
            role_name = rname or rid
            # F-7d: a tool role is its function's paired resource: exact binding, same lock.
            _required = tool_binding_requirement(role_name, res.get("tool_scope"), deployment_id)
            with shared_lambda_lock(res_region, res.get("paired_function") or role_name):
                _binding_refusal = assert_role_binding(iam, role_name, _required)
                if _binding_refusal:
                    return f"[manifest] iam role {role_name} {_binding_refusal}"
                delete_owned_iam_role(iam, role_name, res_region)
            return f"[manifest] iam role {role_name} deleted"
        if rtype == "lambda":
            _fn = rname or rid
            _lam = boto3.client("lambda", region_name=res_region)
            # Defect C (manifest path): a SHARED singleton tool Lambda
            # (AgentCoreDynamicTools / AgentCoreCustomerSupportTools) is reused by
            # every gateway — release it by reference count (drop only this
            # gateway's invoke grant; delete only when no grants remain) instead
            # of hard-deleting it out from under other live gateways.
            if is_shared_tool_function(_fn, res_region):
                return f"[manifest] {_release_shared_tool_lambda(_lam, _fn, res.get('gateway_role'), res_region)}"
            # F-7c: the manifest says this deployment created a function by this name.
            # It is not evidence about whatever holds the name now, and a delete is not
            # reversible, so the ownership tag decides.
            # F-7d: exact binding (KB: DeploymentId; custom: the row's ToolScope) on top of the
            # stack gate, under the function's lock.
            _required = tool_binding_requirement(_fn, res.get("tool_scope"), deployment_id)
            with shared_lambda_lock(res_region, _fn):
                _refusal = _authorize_tool_function_deletion(_lam, _fn, res_region, required_tags=_required)
                if _refusal:
                    return f"[manifest] lambda {_fn} {_refusal}"
            _lam.delete_function(FunctionName=_fn)
            return f"[manifest] lambda {_fn} deleted"
        if rtype == "policy":
            # F-G09-003: a Cedar policy this deployment created on an engine it may not own. Deleted by exact
            # engine + policy id and proven absent; ordered before the engine. An adopted/shared child (recorded
            # with created_by_deployment=False after an in-place reconcile) is never deleted here -- its creator's
            # teardown, or the engine-wide confirmed deleter of the last adopter, reclaims it. Co-residency with
            # another live deployment on the same policy OR its parent engine is refused upstream
            # (manifest_delete_refusal) before this arm runs.
            engine = res.get("engine_id") or res.get("policy_engine_id") or ""
            if not engine or not rid:
                raise ResourceDeletionRefused(
                    f"policy row {rid or '?'} lacks its engine id; deletion cannot be addressed exactly"
                )
            if res.get("created_by_deployment") is not True:
                return (
                    f"[manifest] policy {engine}/{rid} left in place (adopted/shared: not created by this "
                    "deployment; its owner's teardown reclaims it)"
                )
            ctrl = boto3.client("bedrock-agentcore-control", region_name=res_region)
            # The parent engine must be proven THIS stack's immediately before the child is mutated: a policy this
            # deployment created on a customer-owned/imported engine is not ours to remove.
            assert_agentcore_resource_owned(ctrl, "policy_engine", engine, res_region)
            delete_policy_confirmed(ctrl, engine, rid)
            return f"[manifest] policy {engine}/{rid} deleted (confirmed absent)"
        if rtype == "policy_engine":
            ctrl = boto3.client("bedrock-agentcore-control", region_name=res_region)
            assert_agentcore_resource_owned(
                ctrl,
                "policy_engine",
                rid,
                res_region,
            )
            delete_policy_engine_confirmed(ctrl, rid)
            return f"[manifest] policy engine {rid} deleted"
        if rtype == "guardrail":
            bedrock = boto3.client("bedrock", region_name=res_region)
            assert_guardrail_owned(bedrock, rid, res_region)
            bedrock.delete_guardrail(guardrailIdentifier=rid)
            return f"[manifest] guardrail {rid} deleted"
        if rtype == "knowledge_base":
            # Bug 167: delete the KB and WAIT for it to reach a terminal deleted
            # state BEFORE the manifest reclaims the s3_vectors_bucket + KB role
            # (priority ordering guarantees this type runs first). Deleting a KB
            # with dataDeletionPolicy=DELETE makes Bedrock delete the underlying
            # vector data, which needs the store + a role it can assume — both
            # must still exist at KB-delete time.
            ba = boto3.client("bedrock-agent", region_name=res_region)
            assert_knowledge_base_owned(ba, rid, res_region)
            try:
                for ds in list_all(
                    ba,
                    "list_data_sources",
                    item_keys=("dataSourceSummaries",),
                    request={"knowledgeBaseId": rid, "maxResults": 100},
                ):
                    try:
                        ba.delete_data_source(knowledgeBaseId=rid, dataSourceId=ds["dataSourceId"])
                    except Exception:  # noqa: BLE001 — cascade delete below removes remaining data sources
                        logger.debug("delete_data_source on KB %s failed", rid, exc_info=True)
            except Exception:  # noqa: BLE001 — best-effort pre-clean; delete_knowledge_base surfaces real failures
                logger.debug("list_data_sources on KB %s failed", rid, exc_info=True)
            ba.delete_knowledge_base(knowledgeBaseId=rid)
            wait_until_absent(
                resource_label=f"knowledge base {rid}",
                read=lambda: ba.get_knowledge_base(knowledgeBaseId=rid),
                max_attempts=24,
                delay_seconds=5,
            )
            return f"[manifest] knowledge base {rid} deleted"
        if rtype == "s3_vectors_bucket":
            # Auto-provisioned S3 Vectors bucket backing a managed KB (Bug 145).
            # Indexes must be deleted before the bucket.
            s3v = boto3.client("s3vectors", region_name=res_region)
            bname = rname or rid
            assert_vector_bucket_owned(s3v, bname, res_region)
            delete_vector_bucket_confirmed(s3v, bname)
            return f"[manifest] s3 vectors bucket {bname} deleted"
        if rtype == "oss_collection":
            # Auto-provisioned OpenSearch Serverless collection backing a managed KB.
            # This is a STANDING billable resource — deleting it is critical to avoid
            # ~$350/mo orphans. delete_collection removes its indexes; then remove the
            # three security/access policies we created (named <coll>-enc/-net/-acc).
            aoss = boto3.client("opensearchserverless", region_name=res_region)
            cname = rname or rid
            detail = get_owned_aoss_collection(
                aoss,
                cname,
                res_region,
                deployment_id,
            )
            if detail:
                aoss.delete_collection(id=detail["id"])
                wait_until_absent(
                    resource_label=f"OpenSearch Serverless collection {cname}",
                    read=lambda: aoss.batch_get_collection(names=[cname]),
                    absent_response=lambda response: not (response.get("collectionDetails") or []),
                    max_attempts=60,
                    delay_seconds=5,
                )
            retained_policies: list[str] = []
            for suffix, ptype in (
                (f"{cname}-acc"[:32], "data"),
                (f"{cname}-net"[:32], "network"),
                (f"{cname}-enc"[:32], "encryption"),
            ):
                try:
                    assert_aoss_policy_owned(
                        aoss,
                        suffix,
                        ptype,
                        res_region,
                        deployment_id,
                    )
                    if ptype == "data":
                        aoss.delete_access_policy(name=suffix, type=ptype)
                        read_policy = partial(
                            aoss.get_access_policy,
                            name=suffix,
                            type=ptype,
                        )
                    else:
                        aoss.delete_security_policy(name=suffix, type=ptype)
                        read_policy = partial(
                            aoss.get_security_policy,
                            name=suffix,
                            type=ptype,
                        )
                    wait_until_absent(
                        resource_label=(f"OpenSearch Serverless {ptype} policy {suffix}"),
                        read=read_policy,
                        max_attempts=15,
                        delay_seconds=2,
                    )
                except ResourceDeletionRefused:
                    retained_policies.append(suffix)
                except Exception as exc:  # noqa: BLE001
                    if not _gone(exc):
                        raise
            if retained_policies:
                raise ResourceDeletionRefused(
                    "OpenSearch Serverless collection cleanup retained policies "
                    + ", ".join(retained_policies)
                    + " because exact ownership could not be proven."
                )
            return f"[manifest] oss collection {cname} + policies deleted"
        if rtype == "cognito_app_client":
            # One gateway's app client inside the SHARED platform pool. This row only
            # exists for the shared-pool path: when a deployment owns its pool, the
            # pool row below deletes the clients with it.
            #
            # A client id alone is not authority to delete. Two checks, both cheap:
            # the pool must be one the platform owns (shared-exact or stack-owned —
            # a pool we cannot classify reaches neither), and the delete is scoped to
            # the single client id this deployment's own manifest recorded. Same
            # trust model as the pool row: verify the container against the live
            # resource, then act only on the id we wrote.
            from app.services.gateway_deployer import (
                POOL_OWNED_BY_STACK,
                POOL_SHARED_EXACT,
                classify_user_pool,
            )

            _pool = str(res.get("pool_id") or "")
            if not _pool:
                return f"[manifest] cognito app client {rid} has no pool_id — skipped"
            cog = _shared_pool_children_client(_pool, res_region, boto3)
            if classify_user_pool(_pool, cognito_client=cog) not in (POOL_SHARED_EXACT, POOL_OWNED_BY_STACK):
                if _pool_is_gone(_pool, cog):
                    return f"[manifest] cognito app client {rid} already gone with its pool {_pool}"
                return f"[manifest] cognito app client {rid}: pool ownership could not be proven — skipped (protected)"
            try:
                cog.delete_user_pool_client(UserPoolId=_pool, ClientId=rid)
            except Exception as e:  # noqa: BLE001
                if not _gone(e):
                    raise
                return f"[manifest] cognito app client {rid} already gone"
            return f"[manifest] cognito app client {rid} deleted"
        if rtype == "cognito_resource_server":
            # The per-gateway scope definition inside the SHARED pool. Deleted after
            # the app client (priority 9 vs 8) because the safety check is "no client
            # can still be using this scope", which is only true once ours is gone.
            #
            # Its identifier is derived from the gateway NAME, so two deployments that
            # picked the same name share it — see resource_server_is_unused for why the
            # co-residency check reads client NAMES rather than scopes, and why it
            # fails closed.
            from app.services.gateway_deployer import (
                POOL_OWNED_BY_STACK,
                POOL_SHARED_EXACT,
                classify_user_pool,
                resource_server_is_unused,
            )

            _pool = str(res.get("pool_id") or "")
            if not _pool:
                return f"[manifest] cognito resource server {rid} has no pool_id — skipped"
            cog = _shared_pool_children_client(_pool, res_region, boto3)
            if classify_user_pool(_pool, cognito_client=cog) not in (POOL_SHARED_EXACT, POOL_OWNED_BY_STACK):
                if _pool_is_gone(_pool, cog):
                    return f"[manifest] cognito resource server {rid} already gone with its pool {_pool}"
                return f"[manifest] cognito resource server {rid}: pool ownership could not be proven — skipped (protected)"
            if not resource_server_is_unused(_pool, rid, cog):
                return f"[manifest] cognito resource server {rid} still has a client — skipped (protected)"
            try:
                cog.delete_resource_server(UserPoolId=_pool, Identifier=rid)
            except Exception as e:  # noqa: BLE001
                if not _gone(e):
                    raise
                return f"[manifest] cognito resource server {rid} already gone"
            return f"[manifest] cognito resource server {rid} deleted"
        if rtype == "cognito_user_pool":
            # Defence in depth. gateway_step never records the shared platform
            # gateway-auth pool, but a manifest row written by an older build (or a
            # future recorder) must still not be able to delete it: that pool holds
            # every deployed gateway's app client, and its hosted domain takes >381s
            # to reprovision. And "not the shared pool" is not "ours" — a manifest row
            # is persisted data, so it must be re-verified against the live resource
            # before a delete, not trusted. See gateway_deployer.classify_user_pool.
            from app.services.gateway_deployer import (
                POOL_OWNED_BY_STACK,
                classify_user_pool,
                is_platform_owned_user_pool,
            )

            # The shared-pool check is a pure env/id comparison, so it runs BEFORE any
            # client is constructed — the one pool that must never be touched is
            # refused without an AWS call at all.
            if is_platform_owned_user_pool(rid):
                return f"[manifest] cognito pool {rid} is the shared platform gateway-auth pool — skipped"
            # Then prove ownership, reading tags through the same client — and therefore
            # the same account and region — that the delete below would use.
            cog = boto3.client("cognito-idp", region_name=res_region)
            if classify_user_pool(rid, cognito_client=cog) != POOL_OWNED_BY_STACK:
                if _pool_is_gone(rid, cog):
                    return f"[manifest] cognito pool {rid} already gone"
                return f"[manifest] cognito pool {rid} ownership could not be proven — skipped (protected)"
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
                        except Exception:  # noqa: BLE001 — describe failure = treat domain as gone
                            still = None
                        if not still:
                            break
                        time.sleep(5)
            except Exception:  # noqa: BLE001 — pool delete below retries + surfaces real failures
                logger.debug("Cognito domain pre-delete for pool %s failed", rid, exc_info=True)
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
        if rtype == "litellm_gateway":
            # Informational only. A LiteLLM gateway is the CUSTOMER's proxy — we
            # created no AWS resource for it and must never try to delete it. The
            # one resource that deploy DID create, the virtual-key secret, is
            # recorded separately as type "secret" and torn down by that arm.
            # This arm exists so the row reads as deliberate rather than as an
            # unrecognized type falling through the default below.
            return f"[manifest] litellm gateway {rid} is external — nothing to delete"
        if rtype == "gateway_target":
            # F-74b. The row exists to record WHICH deployment asked for this target, so a
            # later deploy can tell a target of ours from one it must not touch. Deleting
            # it is the "gateway" arm's job: that arm removes every target on the gateway
            # before deleting the gateway, and a gateway retained for a co-resident
            # deployment keeps its targets by design.
            #
            # This arm is not decoration. Without it the row falls to the unknown-type
            # branch below, which logs an ERROR and tells the operator a resource is "still
            # in the account ... delete it by hand" — on EVERY teardown of EVERY
            # gateway-bearing deployment, for targets that were in fact deleted. A false
            # leak report on every teardown trains an operator to ignore the true ones.
            #
            # Removing only this deployment's own targets when the gateway is retained is
            # the next slice, and it needs a reference count over these rows: a delete
            # written before the rows exist in the population would, on its first run, see
            # no co-resident reference and remove the co-resident's tool.
            return f"[manifest] gateway target {rid} is deleted with its gateway — nothing to delete here"
        # Unknown type: still a no-op, because failing the whole teardown over one
        # unrecognised row would strand every resource after it. But SAY SO. F-29 fixed
        # exactly this silence on the *other* dispatcher (`status_update_step.
        # _cleanup_resource` raises `_NoDeleterFor`, logs an error, and refuses to count
        # the row as cleaned) and left this half unfixed, so the two paths disagreed
        # about honesty rather than about coverage: the caller does
        # `if _msg: cleanup_messages.append(_msg)`, so returning "" put the row in
        # neither `cleanup_messages` nor `cleanup_failures` and the user was told the
        # teardown succeeded with no mention that anything was skipped. The
        # handled-type sets are held equal by
        # ``test_the_two_teardown_dispatchers_handle_exactly_the_same_types``, so
        # reaching here means an OLD manifest row whose type predates this dispatcher,
        # or a row that was never ours — both of which are precisely the cases where
        # the operator needs to be told a resource is still in the account.
        logger.error(
            "Manifest teardown has NO deleter for type %r (%s) — left in the account",
            rtype,
            rid or rname,
        )
        return (
            f"[manifest] {rtype} {rid or rname} SKIPPED (no deleter for this type — it is "
            f"still in the account and nothing tried to delete it; delete it by hand)"
        )
    except Exception as e:  # noqa: BLE001
        if _gone(e):
            return f"[manifest] {rtype} {rid or rname} already gone"
        raise


@deployment_app.post("/api/runtime/import", status_code=201, dependencies=[Depends(require_scopes("agent:write"))])
async def handle_import_runtime(request: ImportRuntimeRequest, raw_request: Request) -> dict:
    """Import (adopt) an externally-built AgentCore Runtime into the platform.

    Loom-study 1.5. Describes the runtime by ARN and records it as a SUCCEEDED,
    caller-owned deployment WITHOUT any codegen/deploy — so a team can bring a
    pre-existing runtime under the platform's observability/cost/registry
    governance. Does NOT create AWS resources.

    Teardown goes through the same DELETE path, and the opt-in this docstring promised
    now actually exists: DELETE leaves an imported runtime running unless the caller
    passes ``?destroy=true``. Before that, importing a runtime and deleting it destroyed
    a runtime this platform never built — the record is marked ``imported`` so the
    delete path can tell.
    """
    arn = request.runtime_arn
    m = re.match(r"^arn:aws:bedrock-agentcore:([a-z0-9-]+):(\d{12}):runtime/([A-Za-z0-9_-]+)$", arn)
    if not m:
        raise HTTPException(status_code=400, detail="Invalid AgentCore Runtime ARN")
    region = request.aws_region or m.group(1)
    runtime_id = m.group(3)
    user_id = _get_user_id(raw_request)

    try:
        ctrl = boto3.client("bedrock-agentcore-control", region_name=region)
        rt = ctrl.get_agent_runtime(agentRuntimeId=runtime_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Import: could not describe runtime %s in %s: %s", runtime_id, region, exc)
        raise HTTPException(status_code=404, detail="Runtime not found or not describable") from exc

    store = _get_state_store()
    # Idempotency + tenant safety: if this runtime is already recorded by another
    # owner, refuse (mirrors the deploy-time cross-tenant guard).
    existing = _scan_for_runtime(store._table, runtime_id)
    if existing and existing.get("user_id") and existing.get("user_id") != user_id:
        raise HTTPException(status_code=409, detail="Runtime already imported by another user")

    now = datetime.now(timezone.utc)
    deployment_id = str(uuid.uuid4())
    endpoint = f"{arn}/runtime-endpoint/DEFAULT"
    state = DeploymentState(
        deployment_id=deployment_id,
        workflow_id=f"imported-{runtime_id[:32]}",
        user_id=user_id,
        status=DeploymentStatusEnum.SUCCEEDED,
        started_at=now,
        completed_at=now,
        runtime_id=runtime_id,
        runtime_arn=arn,
        runtime_endpoint=endpoint,
        agentcore_runtime_name=rt.get("agentRuntimeName") or runtime_id,
        deployment_mode="runtime",
        # The delete path reads this to decide whether teardown may destroy the AWS
        # runtime. Without it the only marker is the workflow_id prefix below.
        imported=True,
        resource_manifest_version=1,
        resource_manifest_complete=True,
        resource_manifest_error=False,
    )
    store.create(state)
    logger.info("Imported external runtime %s as deployment %s", runtime_id, deployment_id)
    return {
        "deploymentId": deployment_id,
        "runtimeId": runtime_id,
        "runtimeArn": arn,
        "status": rt.get("status", "READY"),
        "imported": True,
    }


# Resource types whose teardown is slow enough to blow past API Gateway's 29s
# integration cap (live-verified in the matrix run): a managed KB delete polls
# to a terminal state (~2 min worst case) BEFORE its backing S3-Vectors bucket /
# OSS collection can be reclaimed. Deployments carrying any of these are torn
# down in a background self-invoke (_async_delete) instead of inline.
#
# Memory joined the set with F-56: its delete is now polled to absence
# (delete_memory_confirmed, up to ~2 min) before its execution role may go, where it
# used to be fire-and-forget. Inline, that wait turns a working delete into a 503
# with the claim held until its lease expires.
_SLOW_DELETE_RESOURCE_TYPES = {
    "harness",
    "knowledge_base",
    "memory",
    "oss_collection",
    "policy_engine",
    "s3_vectors_bucket",
}


def _lookup_deployment_record(runtime_id: str) -> dict | None:
    """Resolve the deployment record for *runtime_id* (or a deployment_id).

    Tries the runtime_id GSI/scan first, then falls back to treating the value
    as a deployment_id (frontend fallback for partial-failed deploys where the
    agent runtime was never created but gateway/MCP server were).
    """
    store = _get_state_store()
    record = _scan_for_runtime(store._table, runtime_id)
    if not record:
        direct = store.get(runtime_id)
        if direct:
            record = direct.model_dump(mode="json")
    return record


def _is_imported_record(deployment_record: dict | None) -> bool:
    """Whether this deployment ADOPTED an existing runtime rather than creating one.

    Two markers, because the field came second. ``imported`` is authoritative for
    anything imported after it existed; the ``imported-<id>`` workflow_id written by
    /api/runtime/import is the only marker older records carry, and those are exactly
    the ones at risk — they were adopted before any opt-in existed.
    """
    if not deployment_record:
        return False
    if deployment_record.get("imported"):
        return True
    return str(deployment_record.get("workflow_id") or "").startswith("imported-")


def _wants_destroy(raw_request: Request) -> bool:
    """Whether the caller explicitly opted in to destroying an adopted runtime.

    Off unless the query string says ``destroy=true``. Deleting the platform's record
    of something is reversible; deleting someone else's runtime is not, so the
    irreversible half has to be asked for.
    """
    return str(raw_request.query_params.get("destroy", "")).lower() in {"1", "true", "yes"}


def _forget_imported_runtime(runtime_id: str, deployment_record: dict) -> DeleteResponse:
    """Drop the platform's record of an adopted runtime; destroy nothing in AWS."""
    dep_id = deployment_record.get("deployment_id", "")
    if dep_id:
        _set_delete_status(
            dep_id,
            "deleted",
            "Imported runtime released from the platform; the runtime itself was left running.",
        )
    logger.info("Released imported runtime %s (deployment %s) without destroying it", runtime_id, dep_id)
    return DeleteResponse(
        success=True,
        message=(
            f"Runtime {runtime_id} was imported, not created by this platform, so it was "
            "released from the platform without being destroyed. To destroy the AWS runtime "
            "too, repeat this call with ?destroy=true."
        ),
    )


def _is_slow_delete(deployment_record: dict | None) -> bool:
    """Whether this deployment's teardown belongs to the SLOW class.

    Slow = it carries a flow-created Knowledge Base (KB delete polls to a
    terminal state before the backing store can go) or any manifest entry of a
    KB-adjacent type. Everything else is the FAST class and stays inline,
    preserving the existing synchronous behavior for the 95% case.
    """
    if not deployment_record:
        return False
    kb_result = deployment_record.get("knowledge_base_result") or {}
    if kb_result.get("created_by_flow"):
        return True
    # A pre-manifest record names its Memory only here; the legacy teardown confirms
    # its deletion too, so it is just as slow.
    memory_result = deployment_record.get("memory_result") or {}
    if isinstance(memory_result, dict) and memory_result.get("memory_id"):
        return True
    for res in deployment_record.get("created_resources") or []:
        if str(res.get("type")) in _SLOW_DELETE_RESOURCE_TYPES:
            return True
    return False


# Longer than the deployment Lambda's 600-second timeout. Once this deadline is
# past, the worker that acquired it cannot still be executing in Lambda, so a
# retry can safely reclaim the row without a fencing token.
_DELETE_CLAIM_LEASE_SECONDS = 15 * 60


class ActiveFinalizerConflict(RuntimeError):
    """A teardown claim was refused because a deploy finalizer still holds the barrier.

    Distinct from the ordinary lost race (``_claim_delete_status`` returning
    ``False``, meaning another teardown worker owns it) because the two need
    opposite reports: a rival teardown is progress, whereas an active finalizer
    means the caller must come back later. The public route turns this into 409
    and stack cleanup into a retryable no-op; neither performs destructive work.
    """


def _finalizer_lease_is_live(deployment_id: str) -> bool:
    """Whether a deploy finalizer held the barrier when a delete claim was refused.

    REPORTING ONLY. The atomic ConditionExpression in ``_claim_delete_status``
    has already made the decision, and no destructive work runs on either
    branch, so this strongly-consistent read cannot reintroduce the TOCTOU it
    exists to describe -- it only chooses between "an active deploy" and
    "another teardown worker". ARCC cnt_4mD5f0eLH0RCDK: the check that counts is
    the one inside the conditional write, surfaced at use time as an exception.

    Fails toward the safer report. An unreadable row is described as an active
    finalizer, so the caller retries instead of destroying.
    """
    from app.services.deployment_state_store import FINALIZER_LEASE_ATTR

    try:
        response = _get_state_store()._table.get_item(
            Key={"deployment_id": deployment_id},
            ConsistentRead=True,
        )
    except Exception as exc:  # noqa: BLE001
        # Type only: a botocore message echoes the request that produced it.
        logger.warning(
            "Could not resolve why the delete claim on %s was refused (%s); reporting an active "
            "deploy so nothing destructive runs.",
            deployment_id,
            type(exc).__name__,
        )
        return True
    item = response.get("Item") or {}
    expires = item.get(FINALIZER_LEASE_ATTR)
    if expires is None:
        return False
    try:
        return int(expires) > int(datetime.now(timezone.utc).timestamp())
    except (TypeError, ValueError):
        # A malformed lease is not evidence that no finalizer is running.
        return True


def _set_delete_status(deployment_id: str, delete_status: str, delete_message: str | None = None) -> None:
    """Write delete_status (+ optional delete_message, DELETE_MESSAGE_MAX_CHARS cap) onto the
    deployment record so GET /api/deploy/{deployment_id} can surface async
    teardown progress. Only a successfully deleted tombstone receives a TTL;
    every nonterminal or retained/failed state remains durable for retry and
    ownership proof. Best-effort: never fails the delete itself."""
    try:
        from app.services.deployment_state_store import DELETE_MESSAGE_MAX_CHARS, _update_item

        set_parts = ["delete_status = :ds"]
        expr_values: dict = {":ds": delete_status}
        if delete_message is not None:
            set_parts.append("delete_message = :dm")
            expr_values[":dm"] = delete_message[:DELETE_MESSAGE_MAX_CHARS]
        expr_names = {
            "#t": "ttl",
            "#claim": "delete_claim_expires_at",
        }
        if delete_status == "deleted":
            set_parts.append("#t = :ttl")
            expr_values[":ttl"] = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
            update_expr = "SET " + ", ".join(set_parts) + " REMOVE #claim"
        else:
            update_expr = "SET " + ", ".join(set_parts) + " REMOVE #t, #claim"
        _update_item(
            _get_state_store()._table,
            key={"deployment_id": deployment_id},
            update_expr=update_expr,
            expr_values=expr_values,
            expr_names=expr_names,
            condition_expr="attribute_exists(deployment_id)",
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not write delete_status=%s for %s",
            delete_status,
            deployment_id,
            exc_info=True,
        )


#: What _deleted_is_final found for a deployment already "deleted".
DELETED_FINAL = "final"
DELETED_REOPENED = "reopened"
DELETED_UNVERIFIED = "unverified"
_DELETED_UNVERIFIED_MESSAGE = (
    "This deployment was deleted, but its gateway recovery record could not be verified; try again shortly."
)


def _deleted_is_final(deployment_id: str) -> str:
    """Whether a deployment already "deleted" may be reported so again.

    A teardown writes "deleted" only with its recovery pointer reclaimed, and nothing
    retries a "deleted" one. A promote landing after that teardown read leaves this
    deployment's evidence on a claim: a gateway the teardown never saw. So only an
    absent or marked pointer is FINAL. Live evidence (or a promote racing this check)
    REOPENS the deployment as delete_failed, by a write conditioned on it still being
    "deleted", so the caller's own claim then runs the teardown again, which finds
    it. Anything else (a failed read or write, a malformed pointer, a reopen that did
    not land) is UNVERIFIED, and the caller reports a retryable failure, never success.
    """
    try:
        outcome = reclaim_recovery_pointer(deployment_id=deployment_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Recovery pointer of deleted %s could not be checked: %s", deployment_id, type(exc).__name__)
        return DELETED_UNVERIFIED
    if outcome in (POINTER_ABSENT, POINTER_MARKED):
        return DELETED_FINAL
    if outcome not in (POINTER_ACTIVE, POINTER_RACED):
        return DELETED_UNVERIFIED
    logger.error("Deleted deployment %s still records a gateway on its name claim; reopening", deployment_id)
    try:
        from app.services.deployment_state_store import _update_item

        _update_item(
            _get_state_store()._table,
            key={"deployment_id": deployment_id},
            update_expr="SET #ds = :failed, delete_message = :message REMOVE #t",
            expr_values={
                ":failed": "delete_failed",
                ":deleted": "deleted",
                ":message": "A gateway recorded only on its name claim was found after deletion.",
            },
            expr_names={"#ds": "delete_status", "#t": "ttl"},
            condition_expr="#ds = :deleted",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Deleted deployment %s could not be reopened: %s", deployment_id, type(exc).__name__)
        return DELETED_UNVERIFIED
    return DELETED_REOPENED


def _claim_delete_status(deployment_id: str) -> bool:
    """Atomically become the sole teardown worker for one deployment.

    Returns ``False`` when another worker already claimed or completed the
    delete. Raises ``ActiveFinalizerConflict`` when the refusal was a deploy
    finalizer still writing, which needs the opposite report. Retrying a
    previously failed/safety-retained cleanup is allowed. Any storage error other
    than the expected conditional race propagates so a caller never performs
    destructive work without a durable claim.
    """
    from app.services.deployment_state_store import (
        FINALIZER_LEASE_ATTR,
        FINALIZER_TOKEN_ATTR,
        _update_item,
    )

    def _attempt() -> bool:
        """One atomic claim: ``True`` on success, ``False`` on a conditional refusal.

        ``now`` is recomputed per attempt so a retry evaluates the leases against
        the current clock rather than the first attempt's.
        """
        now = int(datetime.now(timezone.utc).timestamp())
        lease_expires = now + _DELETE_CLAIM_LEASE_SECONDS
        try:
            _update_item(
                _get_state_store()._table,
                key={"deployment_id": deployment_id},
                # REMOVE the finalizer lease AND its token in the same atomic
                # write that grants the claim. The condition below only lets this
                # through when the lease is absent or EXPIRED, and an expired
                # lease's token would otherwise still match -- so a finalizer
                # that woke up late would pass every fenced write's condition and
                # go on appending manifest rows to a row this teardown now owns.
                # Revoking it here is what makes the fence a real fencing token
                # rather than a liveness hint: ownership changes hands once, in
                # one operation, with no window in between.
                update_expr=("SET #ds = :deleting, delete_message = :message, #claim = :lease REMOVE #t, #fin, #ftok"),
                expr_values={
                    ":deleting": "deleting",
                    ":failed": "delete_failed",
                    ":retained": "delete_retained",
                    ":message": "Deletion is in progress.",
                    ":now": now,
                    ":lease": lease_expires,
                },
                expr_names={
                    "#ds": "delete_status",
                    "#t": "ttl",
                    "#claim": "delete_claim_expires_at",
                    "#fin": FINALIZER_LEASE_ATTR,
                    "#ftok": FINALIZER_TOKEN_ATTR,
                },
                condition_expr=(
                    "attribute_exists(deployment_id) AND "
                    # THE DEPLOY-VS-DELETE BARRIER. A finalizer that still holds a
                    # live lease is mid-write: it has already committed the terminal
                    # status but not yet the version row, the slot pointer, the
                    # registry record or its last manifest appends. Claiming here
                    # let teardown snapshot `created_resources`, the finalizer
                    # append one more row, and teardown then write the FINAL
                    # `deleted` over a row no deleter had ever seen -- a permanent
                    # leak that DELETE reported as success.
                    #
                    # This term, not a pre-read, is the check: it is evaluated
                    # inside the same conditional write that grants the claim, so
                    # there is no window between deciding and acting (ARCC
                    # cnt_4mD5f0eLH0RCDK, and cnt_vBC0kXE8PNHqrW on making the
                    # authorization decision at a time when it is still true).
                    # Bounded by expiry so a crashed finalizer cannot strand a row.
                    #
                    # `<= :now` so that "expired" here is the exact complement of
                    # "live" in `_finalizer_lease_is_live` (`expires > now`). At
                    # equality the two must not disagree, or a refusal gets
                    # classified as a rival teardown when nothing owns the row.
                    #
                    # Deliberately NOT gated on the deployment's own `status`: a
                    # finalizer's post-work runs while status is already
                    # `succeeded`, so status cannot see this window at all, and an
                    # aborted execution sits at `in_progress` forever -- refusing
                    # that would make those rows permanently undeletable.
                    "(attribute_not_exists(#fin) OR #fin <= :now) AND "
                    "(attribute_not_exists(#ds) OR #ds IN (:failed, :retained) OR "
                    "(#ds = :deleting AND "
                    "(attribute_not_exists(#claim) OR #claim < :now)))"
                ),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                return False
            raise

    if _attempt():
        return True
    if _finalizer_lease_is_live(deployment_id):
        raise ActiveFinalizerConflict(f"Deployment '{deployment_id}' is still being finalized by its deploy") from None
    # Refused, yet no live finalizer. Two possibilities, and they need opposite
    # reports: a rival teardown owns the row, or the finalizer lease expired in
    # the microseconds between the conditional write and the read above. In the
    # second case the row IS claimable now, and reporting a conflict would falsely
    # tell the public route "still deploying" and leave stack cleanup unable to
    # take an already-expired barrier. One more atomic attempt settles it -- the
    # retry is itself a conditional write, so this adds no check-then-act window.
    if _attempt():
        return True
    # Still refused. Re-classify: a NEW finalizer may have taken the lease between
    # the two attempts, which is a genuine conflict rather than a rival teardown.
    if _finalizer_lease_is_live(deployment_id):
        raise ActiveFinalizerConflict(f"Deployment '{deployment_id}' is still being finalized by its deploy") from None
    return False


@deployment_app.delete(
    "/api/runtime/{runtime_id}",
    response_model=DeleteResponse,
    response_model_by_alias=True,
    dependencies=[Depends(require_scopes("agent:write"))],
)
async def handle_delete_runtime(runtime_id: str, raw_request: Request) -> DeleteResponse:
    """Delete a runtime and clean up all associated resources. Caller must own it.

    Thin dispatcher: KB-backed teardowns (KB cascade waits + backing-store
    deletes) exceed API Gateway's 29s integration cap and used to 503 even
    though the Lambda finished — those are dispatched to a background
    self-invoke (mirrors the _async_generate/_async_test pattern) and tracked
    via delete_status on the deployment record. Everything else runs inline in
    _run_delete_cleanup exactly as before.
    """
    runtime_id = _validate_runtime_id(runtime_id)
    caller_sub = _get_user_id(raw_request)

    # Fetch the deployment record to classify fast vs slow and pre-validate
    # ownership. The lookup is cheap; _run_delete_cleanup re-does it internally
    # so the cleanup body works identically from the async path.
    try:
        deployment_record = _lookup_deployment_record(runtime_id)
    except Exception as exc:
        logger.warning("Delete dispatch: state lookup failed for %s: %s", runtime_id, exc)
        # F-9, and this is the destructive sink: a lookup that failed used to leave
        # the record None, which turned the ownership check off and let the cleanup
        # below call destroy_runtime() on the caller-supplied id. Never guess here.
        raise HTTPException(
            status_code=503,
            detail="Could not verify this runtime right now. Try again shortly.",
        ) from exc

    # Tenant isolation: caller must own the deployment.
    # See tasks/lessons.md Bug 37. A record is REQUIRED: with none there is nothing
    # this platform recorded creating, so "delete" could only mean destroying a
    # runtime by a name the caller supplied — including a foreign one.
    owner = (deployment_record or {}).get("user_id")
    if not deployment_record or (owner and owner != caller_sub):
        raise HTTPException(status_code=404, detail="Runtime not found")

    # An IMPORTED runtime was adopted, not created here. /api/runtime/import's own
    # docstring claimed "the caller opts in there" for teardown; no opt-in existed, so
    # import-then-delete destroyed a runtime this platform never built. The opt-in is
    # ``?destroy=true`` and it defaults to off: forget the record, leave the runtime.
    if _is_imported_record(deployment_record) and not _wants_destroy(raw_request):
        return _forget_imported_runtime(runtime_id, deployment_record)

    dep_id = deployment_record.get("deployment_id", "")
    if dep_id:
        existing_delete_status = str(deployment_record.get("delete_status") or "")
        if existing_delete_status == "deleted":
            verdict = _deleted_is_final(dep_id)
            if verdict == DELETED_FINAL:
                return DeleteResponse(
                    success=True,
                    message="This deployment has already been deleted.",
                )
            if verdict == DELETED_UNVERIFIED:
                return DeleteResponse(success=False, message=_DELETED_UNVERIFIED_MESSAGE)
            # Reopened: the claim below takes it, and the teardown runs again.
        try:
            claimed = _claim_delete_status(dep_id)
        except ActiveFinalizerConflict:
            # 409, not 503: the request was well-formed and the state is
            # legitimate, it is simply not this caller's turn. Nothing was
            # claimed, so nothing was deleted.
            raise HTTPException(
                status_code=409,
                detail=(
                    "This deployment is still finishing its deployment, so nothing was deleted. "
                    "Wait for it to finish and try again."
                ),
            ) from None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Delete dispatch: could not claim deployment %s",
                dep_id,
                exc_info=True,
            )
            raise HTTPException(
                status_code=503,
                detail="Could not safely start deletion right now. Try again shortly.",
            ) from exc
        if not claimed:
            return DeleteResponse(
                success=True,
                message="Deletion is already in progress or complete.",
            )

    if _is_slow_delete(deployment_record):
        try:
            lambda_client = boto3.client("lambda", region_name=config.aws_region)
            function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
            lambda_client.invoke(
                FunctionName=function_name,
                InvocationType="Event",
                Payload=json.dumps(
                    {
                        "_async_delete": True,
                        "runtime_id": runtime_id,
                        "caller_sub": caller_sub,
                        # The opt-in has to travel with the dispatch: the background
                        # invocation has no query string to re-read it from.
                        "allow_imported_destroy": _wants_destroy(raw_request),
                    }
                ).encode(),
            )
            return DeleteResponse(
                success=True,
                message=(
                    "Deletion started — teardown continues in the background while slow "
                    "resources (a Knowledge Base, Memory) are confirmed deleted; "
                    f"poll GET /api/deploy/{dep_id} for delete_status."
                ),
            )
        except Exception:  # noqa: BLE001
            # Better slow than dropped: if the Event self-invoke itself fails
            # (missing IAM, local dev without a function name), fall back to
            # the inline cleanup and accept the possible API GW timeout.
            logger.warning(
                "Async delete dispatch failed for %s; falling back to inline cleanup",
                runtime_id,
                exc_info=True,
            )

    try:
        result = _run_delete_cleanup(
            runtime_id,
            caller_sub,
            allow_imported_destroy=_wants_destroy(raw_request),
        )
    except Exception as exc:
        if dep_id:
            _set_delete_status(dep_id, "delete_failed", str(exc))
        raise

    if dep_id:
        final_status = "deleted" if result.success else "delete_retained" if result.retained else "delete_failed"
        _set_delete_status(
            dep_id,
            final_status,
            result.message,
        )
    return result


# F-56: how long the async teardown may wait for a Memory to finish deleting. Measured
# live 2026-09-22, AgentCore took ~175 s after DeleteMemory; the helper's default 60 x 2 s
# recorded every such delete as retained. The deadline is absolute from the start of the
# invoke, and the steps ordered after Memory (credential providers, vector stores, roles,
# Cognito) keep a reserve, so KB + Memory together cannot run into the Lambda timeout.
_ASYNC_MEMORY_CONFIRM_SECONDS = 360.0
_ASYNC_DELETE_RESERVE_SECONDS = 180.0
#: How many further background invocations an accepted-but-unconfirmed teardown may run before it
#: records a retention: with the budget above, about 30 minutes of confirmation in all.
_MAX_CONFIRMATION_CONTINUATIONS = 4
_MEMORY_CONFIRM_DELAY_SECONDS = 2.0
_memory_confirm_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "_memory_confirm_deadline", default=None
)


def _memory_confirmation_budget() -> dict:
    """Extra ``delete_memory_confirmed`` kwargs inside an async teardown, else none.

    Inline deletes answer inside API Gateway's 29 s cap and keep the helper's default.
    """
    deadline = _memory_confirm_deadline.get()
    if deadline is None:
        return {}
    remaining = max(0.0, deadline - time.monotonic())
    return {
        "deadline_monotonic": deadline,
        # The deadline, not the attempt count, is the bound.
        "confirmation_attempts": int(remaining // _MEMORY_CONFIRM_DELAY_SECONDS) + 2,
        "delay_seconds": _MEMORY_CONFIRM_DELAY_SECONDS,
    }


_RUNTIME_CONFIRM_DELAY_SECONDS = 5.0


def _runtime_confirmation_budget() -> dict:
    """Extra ``destroy_runtime`` kwargs inside an async teardown, else none (F-08).

    Same shared deadline as the Memory budget above: the async invoke's absolute
    ``time.monotonic()`` bound, so a slow runtime delete cannot run the Lambda into its
    timeout, and the attempt count is derived from it rather than the other way round.
    """
    deadline = _memory_confirm_deadline.get()
    if deadline is None:
        return {}
    remaining = max(0.0, deadline - time.monotonic())
    return {
        "confirmation_deadline": deadline,
        "confirmation_attempts": int(remaining // _RUNTIME_CONFIRM_DELAY_SECONDS) + 2,
        "confirmation_interval": _RUNTIME_CONFIRM_DELAY_SECONDS,
    }


def _handle_async_delete(event: dict, context=None):
    """Background (slow-class) teardown — dispatched by handle_delete_runtime.

    Runs the exact same _run_delete_cleanup body without the API Gateway 29s
    cap, then records the outcome on the deployment record (delete_status =
    "deleted" | "delete_retained" | "delete_failed", delete_message = the cleanup
    summary) so the frontend can poll GET /api/deploy/{deployment_id}. A teardown
    whose only open item is a delete the service accepted but had not finished
    inside this invocation's confirmation budget is handed to the next invocation
    instead (_continue_async_delete); the record stays "deleting" meanwhile.
    """
    remaining = (
        context.get_remaining_time_in_millis() / 1000.0
        if context is not None
        else _ASYNC_MEMORY_CONFIRM_SECONDS + _ASYNC_DELETE_RESERVE_SECONDS
    )
    budget = max(0.0, min(_ASYNC_MEMORY_CONFIRM_SECONDS, remaining - _ASYNC_DELETE_RESERVE_SECONDS))
    token = _memory_confirm_deadline.set(time.monotonic() + budget)
    try:
        return _run_async_delete(event)
    finally:
        _memory_confirm_deadline.reset(token)


def _note_delete_progress(deployment_id: str, message: str) -> None:
    """Record an interim teardown message and renew the claim lease, without ending the claim.

    _set_delete_status drops the claim with every write (a terminal state releases the worker);
    an in-progress note must keep it, or a concurrent DELETE would start a second teardown while
    this one is still confirming. Conditioned on the record still being "deleting", so a note can
    never land on a terminal state."""
    from app.services.deployment_state_store import DELETE_MESSAGE_MAX_CHARS, _update_item

    now = int(datetime.now(timezone.utc).timestamp())
    _update_item(
        _get_state_store()._table,
        key={"deployment_id": deployment_id},
        update_expr="SET delete_message = :dm, #claim = :lease",
        expr_values={
            ":dm": message[:DELETE_MESSAGE_MAX_CHARS],
            ":lease": now + _DELETE_CLAIM_LEASE_SECONDS,
            ":deleting": "deleting",
        },
        expr_names={"#claim": "delete_claim_expires_at", "#ds": "delete_status"},
        condition_expr="attribute_exists(deployment_id) AND #ds = :deleting",
    )


def _continue_async_delete(deployment_id: str, event: dict, message: str) -> int | None:
    """Hand an accepted-but-unconfirmed teardown to the next background invocation.

    Measured live 2026-10-02: a harness's managed Memory was still DELETING when the ~6 min
    confirmation budget of one invocation ran out, so the teardown recorded delete_retained
    ("Resources retained by deletion-authority policy: iam_role, memory") although the API had
    promised to confirm slow resources in the background. The Memory was gone minutes later and
    a hand retry converged in 12 s. The same teardown now runs again in a fresh invocation, up to
    _MAX_CONFIRMATION_CONTINUATIONS times, each pass re-reading what is left (the retry path the
    hand retry already proved). The claim lease is renewed with every hand-off, so a concurrent
    DELETE still reads "already in progress". Returns the next hop, or None when the chain is
    exhausted or the hand-off could not be made; the caller then records the retention, as before.
    """
    hop = int(event.get("confirmation_hop") or 0)
    if hop >= _MAX_CONFIRMATION_CONTINUATIONS:
        return None
    try:
        _note_delete_progress(
            deployment_id,
            "Deletion accepted; confirming in the background "
            f"(pass {hop + 2} of {_MAX_CONFIRMATION_CONTINUATIONS + 1}): {message}",
        )
        boto3.client("lambda", region_name=config.aws_region).invoke(
            FunctionName=os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""),
            InvocationType="Event",
            Payload=json.dumps({**event, "confirmation_hop": hop + 1}).encode(),
        )
    except Exception:  # noqa: BLE001 -- the retention is then recorded, exactly as before
        logger.warning("Async delete: could not continue confirming %s", deployment_id, exc_info=True)
        return None
    return hop + 1


def _run_async_delete(event: dict):
    runtime_id = event["runtime_id"]
    caller_sub = event.get("caller_sub")

    dep_id = ""
    try:
        record = _lookup_deployment_record(runtime_id)
        dep_id = (record or {}).get("deployment_id", "")
    except Exception:  # noqa: BLE001
        logger.warning("Async delete: record lookup failed for %s", runtime_id, exc_info=True)

    try:
        result = _run_delete_cleanup(
            runtime_id,
            caller_sub,
            allow_imported_destroy=bool(event.get("allow_imported_destroy")),
        )
        if dep_id and result.confirmation_pending:
            next_hop = _continue_async_delete(dep_id, event, result.message)
            if next_hop is not None:
                return {"success": False, "confirmation_pending": True, "confirmation_hop": next_hop}
        if dep_id:
            final_status = "deleted" if result.success else "delete_retained" if result.retained else "delete_failed"
            _set_delete_status(
                dep_id,
                final_status,
                result.message,
            )
        return {"success": result.success}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Async delete failed for %s", runtime_id)
        if dep_id:
            _set_delete_status(dep_id, "delete_failed", str(exc))
        return {"success": False}


def _handle_stack_cleanup_delete(event: dict) -> dict:
    """Run one guarded deployment teardown for ``scripts/cleanup.sh``.

    The cleanup script previously reimplemented teardown in bash by trusting
    legacy ``*_result`` ids.  That path had none of the manifest,
    co-residency, target-account, live-ownership, or terminal-state checks in
    :func:`_run_delete_cleanup`.  A stale row could therefore authorize a raw
    delete of a resource that now belonged to another deployment.

    This direct-invoke event is deliberately narrow:

    * Lambda Invoke IAM is the outer authorization boundary.
    * The caller must name this function's exact stack identity.
    * Only canonical UUID deployment keys in this function's own table enter.
    * A durable atomic claim is acquired before destructive work.
    * The tenant owner comes from the record, never from the event.
    * Imported runtimes retain the public API's non-destructive default.

    Any retained, failed, in-flight, or unconfirmed result makes the shell
    script stop before CDK removes the Lambda and deployment table required for
    a safe retry.
    """
    expected_owner = str(event.get("expected_stack_owner") or "")
    try:
        actual_owner = stack_id(config.aws_region)
    except OwnershipConfigurationError as exc:
        logger.error("Stack cleanup refused: deployment identity is not configured")
        return {
            "success": False,
            "message": f"Stack cleanup identity is unavailable ({type(exc).__name__}).",
        }
    if not expected_owner or not hmac.compare_digest(expected_owner, actual_owner):
        logger.warning("Stack cleanup refused: expected owner does not match this stack")
        return {
            "success": False,
            "message": "Stack cleanup was sent to a different stack identity.",
        }

    raw_deployment_id = str(event.get("deployment_id") or "")
    try:
        deployment_id = str(uuid.UUID(raw_deployment_id))
    except (ValueError, AttributeError):
        return {
            "success": False,
            "message": "Stack cleanup accepts only canonical deployment UUIDs.",
        }
    if deployment_id != raw_deployment_id:
        return {
            "success": False,
            "message": "Stack cleanup accepts only canonical deployment UUIDs.",
        }

    try:
        state = _get_state_store().get(deployment_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Stack cleanup could not read deployment %s",
            deployment_id,
            exc_info=True,
        )
        return {
            "success": False,
            "message": f"Deployment state could not be verified ({type(exc).__name__}).",
        }
    if state is None:
        # The row can disappear between the script's scan and this invoke. With
        # no record there is no deletion authority, so touch nothing; this
        # particular record is already in the desired state.
        return {
            "success": True,
            "message": "Deployment record was already absent; no resources were touched.",
        }

    record = state.model_dump(mode="json", exclude_none=True)
    observed_status = str(record.get("delete_status") or "")
    if observed_status == "deleted":
        verdict = _deleted_is_final(deployment_id)
        if verdict == DELETED_FINAL:
            return {
                "success": True,
                "message": "Deployment was already deleted.",
            }
        if verdict == DELETED_UNVERIFIED:
            return {"success": False, "retryable": True, "message": _DELETED_UNVERIFIED_MESSAGE}
        # Reopened: the claim below takes it, and the teardown runs again.
    try:
        claimed = _claim_delete_status(deployment_id)
    except ActiveFinalizerConflict:
        # Retryable and NOT destructive. The stack destroy must not proceed: the
        # finalizer is still appending manifest rows, and tearing down against a
        # snapshot taken now is exactly the leak the barrier exists to stop.
        logger.warning(
            "Stack cleanup deferred for deployment %s: its deploy finalizer still holds the "
            "barrier. No destructive work was performed.",
            deployment_id,
        )
        return {
            "success": False,
            "retryable": True,
            "message": "The deployment is still being finalized, so deletion was not started.",
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Stack cleanup could not claim deployment %s",
            deployment_id,
            exc_info=True,
        )
        return {
            "success": False,
            "retryable": True,
            "message": f"Deletion could not be safely claimed ({type(exc).__name__}).",
        }
    if not claimed:
        # Resolve the race once. A concurrent worker that already completed is
        # success; every other state must stop the stack destroy.
        try:
            current = _get_state_store().get(deployment_id)
            current_status = str((current.delete_status if current else "") or "")
        except Exception:  # noqa: BLE001
            current_status = ""
        if current_status == "deleted" and _deleted_is_final(deployment_id) == DELETED_FINAL:
            return {
                "success": True,
                "message": "A concurrent deletion already completed.",
            }
        return {
            "success": False,
            "retryable": True,
            "message": "Another deletion owns the durable cleanup claim.",
        }

    cleanup_identifier = str(record.get("runtime_id") or deployment_id)
    caller_sub = record.get("user_id")
    try:
        result = _run_delete_cleanup(
            cleanup_identifier,
            caller_sub,
            allow_imported_destroy=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Stack cleanup failed for deployment %s", deployment_id)
        _set_delete_status(deployment_id, "delete_failed", str(exc))
        return {
            "success": False,
            "message": f"Deployment cleanup failed ({type(exc).__name__}).",
        }

    final_status = "deleted" if result.success else "delete_retained" if result.retained else "delete_failed"
    _set_delete_status(deployment_id, final_status, result.message)
    return {
        "success": bool(result.success),
        "retained": bool(result.retained),
        "message": result.message,
    }


def _resolve_friendly_runtime_name(deployment_record: dict) -> tuple[str | None, bool]:
    """Resolve the EXACT AgentVersionsTable / RuntimeSlotsTable partition key for a deployment.

    Returns ``(name, proven)``. ``proven`` is True only when the name came from a value this
    deployment itself recorded -- the persisted field, or a suffix that correlates with this
    record's own version id. It is False for the sanitized-canvas-id fallback, which is an
    inference: a caller about to DELETE something reachable from the name must require it.

    F-81, measured live 2026-09-24. Two delete-path callers used to inline this as
    ``record.get("friendly_runtime_name") or record.get("node_id") or record.get("workflow_id")``
    and the first term was never a field on the record, so both always fell through to the raw
    canvas ``node_id``. A node id is not a runtime name: ``sanitize_runtime_name`` rewrites a
    hyphen to an underscore, so ``f81lock-1790236860`` became the lookup key for rows stored
    under ``f81lock_1790236860``. Neither caller errored -- the versions query returned no rows
    and the slots get returned None -- so the name release reported nothing to release and the
    friendly name stayed locked against every other tenant for good, which is the exact
    customer-visible 409 the release was added to prevent.

    Order of preference, and each term is here for a different reason:

    1. ``friendly_runtime_name`` -- now persisted at create time, and the only term that stays
       correct for a name long enough to be truncated below.
    2. Nothing at all for an IMPORTED runtime. Import persists the AWS ``agentRuntimeName``
       verbatim (``:3681``) and writes no versions or slots row, so there is no partition key to
       resolve -- but the name is arbitrary and frequently contains an underscore, so term 3 would
       have happily turned an adopted ``my_agent`` into the key ``my``. Returning None is not a
       degradation here: the one consumer that needs a name for an imported runtime passes it to
       ``destroy_runtime``, where None lets ``_resolve_runtime_name_for_cleanup`` do the lookup
       properly instead of being short-circuited by a guess.
    3. ``agentcore_runtime_name`` minus its version suffix, and ONLY when that suffix is provably
       the one this platform appended: it must equal ``short_version_suffix(version_id)`` for the
       version this very record names. An 8-hex-looking tail is not enough on its own -- real
       adopted runtimes in this account are named like ``Omar1_8fb9892d``, which satisfies the
       shape and means nothing. Correlating with the record's own version id is what makes the
       strip a derivation rather than a guess. A friendly name of 39+ chars is cut before the
       suffix is appended and cannot be recovered from this value; that is the same truncation
       that lets two versions collide on one name, tracked separately, and it is why term 1 had
       to start being persisted.
    4. ``node_id`` / ``workflow_id``, SANITIZED. Sanitizing is the fix to the original bug, not a
       flourish: unsanitized these are raw canvas ids that cannot be table keys. ``workflow_id``
       holds a node id only on pre-F-55 rows; on current rows it is a flow id, which is why it
       comes last and why term 3 must be tried before it.

    Returns None when nothing can be PROVEN, so a caller skips rather than querying -- or worse,
    deleting -- under a key belonging to someone else. Terms 2 and 3 were tightened after a peer
    review session pointed out the unconditional strip; the imported-record shape was then
    confirmed by reading the import path rather than assumed.
    """
    exact = deployment_record.get("friendly_runtime_name")
    if exact:
        return str(exact), True

    from app.services.agent_versions_store import short_version_suffix
    from app.services.runtime_deployer import sanitize_runtime_name

    # ``_is_imported_record`` rather than the ``imported`` flag alone: it also recognises the
    # legacy shape, a ``workflow_id`` beginning ``imported-``, which is how adopted runtimes were
    # marked before the flag existed. Reading the flag directly made this term miss exactly the
    # oldest adopted runtimes -- the ones most likely to carry an underscore in an AWS-chosen
    # name and so the ones term 3 would have mis-derived.
    if _is_imported_record(deployment_record):
        return None, False

    acn = str(deployment_record.get("agentcore_runtime_name") or "")
    version_id = str(deployment_record.get("version_id") or "")
    if "_" in acn and version_id:
        head, _, suffix = acn.rpartition("_")
        if head and suffix and suffix == short_version_suffix(version_id):
            return head, True

    for raw in (deployment_record.get("node_id"), deployment_record.get("workflow_id")):
        if raw:
            cleaned = sanitize_runtime_name(str(raw))
            if cleaned:
                return cleaned, False

    return None, False


def _proven_runtime_name_for_destroy(deployment_record: dict) -> str | None:
    """The friendly name to hand ``destroy_runtime``, or None to let it resolve one itself.

    There is no such thing as a read-only consumer of this name. ``destroy_runtime`` uses it to
    enumerate ``TriggersTable`` by runtime name -- a partition that is NOT owner-scoped -- and
    then DELETES EventBridge Scheduler schedules, EventBridge rules and their targets, Lambda
    function-URL configs, webhook secrets in Secrets Manager and the trigger rows themselves
    (services/runtime_deployer.py:1455-1560). An inferred name there is not a missed trigger, it
    is someone else's triggers deleted; an earlier revision of this function said otherwise in
    its own docstring, which is how the unproven value came to be passed in the first place.

    So: pass the name ONLY when ``_resolve_friendly_runtime_name`` could prove it came from
    something this deployment recorded. Otherwise pass None, which is strictly better than a
    guess -- ``destroy_runtime`` then calls ``_resolve_runtime_name_for_cleanup``, which looks the
    name up from the runtime id it is actually deleting.
    """
    name, proven = _resolve_friendly_runtime_name(deployment_record)
    if name and not proven:
        logger.info(
            "Not passing an inferred runtime name to destroy_runtime for deployment %s; "
            "letting the destroy resolve it from the runtime id",
            deployment_record.get("deployment_id"),
        )
        return None
    return name


def _release_runtime_name_claim(
    deployment_record: dict | None,
    caller_sub: str | None,
    *,
    runtime_may_still_live: bool,
    trigger_cleanup_unconfirmed: bool = False,
    outcome: dict | None = None,
) -> list[str]:
    """Release the friendly runtime NAME this deployment held; see ``_release_runtime_name_claim_messages``.

    ``outcome``, when given, receives ``released`` (the claim is gone) and ``kept_locked`` (the name
    survives as a retry handle or because the release could not be proven safe). A caller that
    reports a teardown verdict MUST read it: a kept name is a retained resource, not a success.
    """
    messages = _release_runtime_name_claim_messages(
        deployment_record,
        caller_sub,
        runtime_may_still_live=runtime_may_still_live,
        trigger_cleanup_unconfirmed=trigger_cleanup_unconfirmed,
    )
    if outcome is not None:
        kept = any(
            ("kept locked" in m) or ("not released" in m) or ("release skipped" in m) or ("release failed" in m)
            for m in messages
        )
        outcome["kept_locked"] = kept
        outcome["released"] = not kept
    return messages


def _release_runtime_name_claim_messages(
    deployment_record: dict | None,
    caller_sub: str | None,
    *,
    runtime_may_still_live: bool,
    trigger_cleanup_unconfirmed: bool = False,
) -> list[str]:
    """Release the friendly runtime NAME this deployment held, and nothing else.

    Extracted from ``_run_delete_cleanup`` so it can be tested against the real stores instead of
    a re-implementation. That matters more than usual here: the reason F-81 shipped at all is that
    the only test covering this logic was a hand-written mirror in
    tests/test_versions_cross_tenant.py which is HANDED the friendly name, so no amount of cases
    could exercise the code that decides what the name is.

    Returns messages for the caller to append to its cleanup report. Raises nothing it can help;
    the caller treats a failure here as non-fatal, because this frees a DynamoDB name lock rather
    than an AWS resource.

    ``runtime_may_still_live`` is decided by the caller from the destroy result and the manifest
    retentions, because only the caller knows them.
    """
    messages: list[str] = []
    # F-81: the same derivation the cross-account destroy uses. The order used to put the raw
    # ``node_id`` ahead of ``agentcore_runtime_name``, and because the first term was a field
    # nobody ever wrote, every release queried a sanitized-away key and found nothing --
    # so this whole block has been reporting success while releasing nothing.
    friendly, _friendly_is_proven = (
        _resolve_friendly_runtime_name(deployment_record) if deployment_record else (None, False)
    )

    # F-81d — a claim may not be released while the thing it protects is still running.
    # Everything above already decided whether the AWS runtime survived: a destroy failure or
    # a manifest/legacy retention leaves it alive. Releasing the name then hands it to another
    # tenant while our runtime is still serving, and simultaneously strips the slot+version
    # rows that `resolve_owned_runtime_target` needs, so the surviving runtime becomes
    # unmanageable by its own owner. A locked name is recoverable; that is not.
    #
    # The exception is a deployment that never recorded ANY name-claim consumer -- no runtime, no
    # harness, no MCP server runtime: there is nothing alive to protect, and its own pending row
    # is exactly the permanent lock F-82 is about, so a retention flag from some other resource
    # type must not keep that name locked forever. The caller decides that, because it is the
    # only place that knows which consumers were recorded and which of them survived; this
    # function used to re-derive it from ``runtime_id`` alone and so ignored the harness and
    # MCP-server modes, which claim the same name through the same two tables.
    _recorded_consumers = [
        (deployment_record or {}).get(field) for field in ("runtime_id", "harness_id", "mcp_server_runtime_id")
    ]
    if friendly and runtime_may_still_live and any(_recorded_consumers):
        logger.info(
            "Not releasing runtime name %s: the AWS runtime was retained or failed to destroy",
            friendly,
        )
        messages.append(f"Runtime name '{friendly}' kept locked (its runtime was not destroyed)")
        friendly = None

    # F-81f — the same rule for the OTHER thing this name protects.
    #
    # The trigger rows, the schedules and the webhook secrets are keyed by the friendly name, and
    # the tenant's own trigger API resolves ownership through the production slot. So releasing the
    # name while a trigger row survives deletes the only handle that could remove it: a peer session
    # measured the owner's retry of the trigger DELETE answering 404 afterwards. Unlike the runtime
    # term above, this one is NOT gated on which consumers the record happens to list -- a trigger
    # exists under the name whether or not this deployment recorded a runtime id.
    if friendly and trigger_cleanup_unconfirmed:
        logger.warning(
            "Not releasing runtime name %s: trigger cleanup was not confirmed, and the slot is the "
            "owner's only handle on what is left",
            friendly,
        )
        messages.append(f"Runtime name '{friendly}' kept locked (trigger cleanup was not confirmed)")
        friendly = None

    if friendly:
        from app.services.agent_versions_store import (
            NameClaimConflict,
            NameClaimReleaseTooLarge,
            get_slots_store,
            get_versions_store,
            release_name_claim_atomically,
            slot_pointer_pairs,
        )

        vstore = get_versions_store()
        # F-81e/F-83 — one bounded, generation-fenced partition snapshot. A single
        # strongly consistent Query is not a transaction-wide snapshot once it
        # paginates, so ``snapshot_for_name_release`` reads the sentinel before
        # and after the complete query and retries on churn. The transaction
        # below then pins that exact generation instead of adding one
        # ConditionCheck per historical version (which hit DynamoDB's 100-action
        # limit and made old names permanently unreleasable).
        try:
            claim_snapshot = vstore.snapshot_for_name_release(friendly)
        except NameClaimConflict:
            logger.warning(
                "Runtime name %s changed while its release snapshot was read; released nothing",
                friendly,
            )
            messages.append(f"Runtime name '{friendly}' not released (changed during teardown)")
            return messages
        rows = list(claim_snapshot.versions)

        # F-81b — scope the release to THIS deployment's own row.
        #
        # This block used to iterate every caller-owned row for the friendly name and delete
        # all of them, then delete the slot whenever it existed. That was survivable only
        # because the block was unreachable: the key it queried never matched anything (F-81).
        # Making the key correct makes the blast radius real, and multiple live versions under
        # one friendly name is the DESIGNED model -- that is what production_version_id,
        # previous_production_version_id and rollback exist for. So deleting v1 would have
        # taken live v2's row and the slot with it, and `resolve_owned_runtime_target` requires
        # slot + version + deployment to all agree before it will build a client: erasing
        # either one turns an untouched, running v2 into an uninvokable 404 for its tenant,
        # and every trigger and cost reconcile keyed off that slot stops resolving too.
        # Reported by the peer review session; confirmed by reading
        # services/runtime_target_context.py:129-174 before changing anything.
        #
        # Identity comes from AgentVersion.deployment_id, which is exactly "which deploy
        # created this row", cross-checked against version_id when the deployment record
        # carries one. Anything ambiguous deletes NOTHING: a name that stays locked is a 409
        # the tenant can work around, while a wrongly deleted row is a live agent nobody can
        # invoke and metadata that cannot be reconstructed.
        record_deployment_id = str((deployment_record or {}).get("deployment_id") or "")
        record_version_id = str((deployment_record or {}).get("version_id") or "")

        def _row_is_this_deployment(v) -> bool:  # noqa: ANN001 - AgentVersion, imported lazily
            # POSITIVE identity only. An earlier draft accepted a version-id match when either
            # side's deployment id was missing, on the theory that such a row predates the
            # field -- but ``AgentVersion.deployment_id`` has no default and the record's own
            # deployment id is its primary key, so "missing" is not a legacy shape, it is a
            # corrupt or foreign row. Treating absence as agreement is how a delete reaches
            # something it cannot identify.
            row_deployment = str(v.deployment_id or "")
            row_version = str(v.version_id or "")
            if not row_deployment or not record_deployment_id:
                return False
            if row_deployment != record_deployment_id:
                return False
            # BOTH terms must be present and agree. This used to read
            # ``not record_version_id or row_version == record_version_id``, so a record with no
            # version_id matched on the deployment id alone -- one identity term instead of two,
            # and the absent term silently counted as agreement. Any deployment that has a
            # version row also persisted its version_id (both are written together at :1831), so
            # requiring it costs nothing real and closes the shape where a corrupt or partially
            # written record authorises a delete.
            return bool(record_version_id) and row_version == record_version_id

        candidates = [v for v in rows if _row_is_this_deployment(v)]
        # EXPLICIT ownership. A row with no owner_sub is not "safely ours" -- and it locks
        # nobody out either: both halves of the deploy guard skip a row or slot whose
        # owner_sub is empty (see the H-1 checks above), so never deleting one costs no
        # functionality at all, while deleting one destroys a row we cannot attribute.
        owned = [v for v in candidates if v.owner_sub and v.owner_sub == (caller_sub or "")]
        target = owned[0] if len(candidates) == 1 and len(owned) == 1 else None
        deleted_version_ids: set[str] = {str(target.version_id)} if target else set()
        if target is None and candidates:
            # Two rows claiming one deployment, or a row this caller does not own. Neither is
            # a state this path may resolve by guessing.
            logger.warning(
                "Runtime name release: %d ambiguous version row(s) for deployment under name %s; deleting none",
                len(candidates),
                friendly,
            )
            messages.append("Runtime name release skipped (this deployment's version row is ambiguous)")

        # What still HOLDS the name, by the same definition the deploy guard uses. Ownership
        # is deliberately not a filter here: a foreign live row means hands off the slot.
        live_remaining = [
            v
            for v in rows
            if str(v.version_id) not in deleted_version_ids and _version_claim_is_live(v.status, v.created_at)
        ]

        sstore = get_slots_store()
        slot = sstore.get(friendly, consistent=True)
        # Explicit ownership AND this deployment's own version row, identified above. The name
        # alone may not authorize a slot mutation even when it is proven: a proven name says "this
        # key is the one I recorded", not "this slot row is the one I wrote". A deploy writes the
        # slot and the version row as separate operations, so a same-tenant deploy that has
        # upserted its slot and not yet landed its version row presents exactly as an orphan slot
        # -- and deleting it leaves that deploy with no slot at all, which is what
        # ``resolve_owned_runtime_target`` and every trigger resolve through.
        #
        # The cost is deliberate and known: an orphan slot with no version row and no pointers is
        # NOT auto-released, so a name orphaned by some earlier partial delete stays locked until
        # an operator clears it. That is the recoverable direction, and the message below says so
        # rather than reporting a release that did not happen. ``friendly_is_proven`` is therefore
        # no longer a term here -- finding an owned row under the exact key subsumes it -- but it
        # is still what keeps an inferred name out of ``destroy_runtime``.
        slot_is_ours = (
            slot is not None and bool(slot.owner_sub) and slot.owner_sub == (caller_sub or "") and target is not None
        )
        # A deployment with NO version row under the name, while other live versions hold it, has
        # no claim of its own left: an earlier attempt of this same teardown released its row (the
        # row is written synchronously at admission, before any AWS work, so "not landed yet" is
        # not a teardown-reachable state). The slot belongs to the versions that hold the name and
        # stays untouched either way. Reporting "kept locked" there made the teardown
        # delete_retained on every retry, forever -- measured live 2026-10-01 on nine older
        # mcp-server-gateway-target versions -- and a delete_retained tombstone is a live
        # reference that pins every shared row it lists. With NO live version under the name the
        # slot is an orphan only an operator can clear, and a slot pointer still naming this
        # deployment's version is a dangling pointer of ours: both stay retentions.
        # "No row" means no row at all names this deployment -- by deployment id alone, not the
        # two-term identity above. A row that carries our deployment id but cannot be proven by both
        # terms (a record missing its version id, say) is still ours, and stays a retention.
        holds_no_row = bool(record_deployment_id) and not any(
            str(v.deployment_id or "") == record_deployment_id for v in rows
        )
        slot_names_this_version = slot is not None and any(
            value and str(value) == record_version_id for _field, value in slot_pointer_pairs(slot)
        )
        nothing_left_to_release = holds_no_row and bool(live_remaining) and not slot_names_this_version
        if slot is not None and not slot_is_ours:
            if nothing_left_to_release:
                # No count: the live versions may be another tenant's, and how many they run is theirs.
                messages.append(
                    f"Runtime name '{friendly}': this deployment holds no version row under it; "
                    "nothing of it remains to release (live versions still hold the name)"
                )
            else:
                logger.info(
                    "Leaving slot row for runtime name %s untouched (no owned version row for this deployment)",
                    friendly,
                )
                messages.append(
                    f"Runtime name '{friendly}' kept locked (no version row proves this deployment owns it)"
                )

        # A pointer naming a version row we did not read is EVIDENCE OF A ROW WE CANNOT SEE.
        # Even with a consistent read that is reachable: a deploy writes the slot and the version
        # row as two operations, so a slot can legitimately point at a version that has not landed
        # yet. Releasing the name there would delete the claim of a deploy that is still running.
        # It is also the shape of a pointer left dangling by some earlier partial delete -- also
        # not something a teardown may resolve by guessing. Either way: keep the name locked and
        # say so, because a lock is recoverable and a stolen name is not.
        known_version_ids = {str(v.version_id) for v in rows}
        # (name, value) pairs rather than getattr over the name tuple: the name set has to stay
        # fixed at import time, see ``slot_pointer_pairs``.
        pointer_pairs = slot_pointer_pairs(slot) if slot is not None else ()
        dangling_pointers = sorted(
            field for field, value in pointer_pairs if value and str(value) not in known_version_ids
        )

        # Only the pointers naming the version being deleted are cleared -- never repointed at a
        # surviving version, because the slot decides where invocations and triggers resolve, and
        # silently promoting a version the tenant did not promote is a traffic change disguised as
        # cleanup. Cleared reads as "no production version", which is the truth, and
        # promote/rollback can set it.
        cleared = (
            [field for field, value in pointer_pairs if value and str(value) in deleted_version_ids]
            if slot_is_ours
            else []
        )

        slot_action = "none"
        if target is not None:
            if slot is None:
                # Absence is state too. A late finalizer that creates the first
                # slot after this read must cancel the target deletion rather
                # than leave its new pointer dangling.
                slot_action = "assert_absent"
            elif slot_is_ours:
                if live_remaining:
                    # Even when the target is not named by a pointer, pin the
                    # exact slot. A concurrent repoint to the target must cancel
                    # its deletion rather than leave a dangling alias.
                    slot_action = "clear" if cleared else "check"
                elif dangling_pointers:
                    logger.warning(
                        "Runtime name %s: slot pointer(s) %s name no version row we can see; keeping the name locked",
                        friendly,
                        ",".join(dangling_pointers),
                    )
                    messages.append(
                        f"Runtime name '{friendly}' kept locked (slot points at {len(dangling_pointers)} "
                        "version(s) this teardown cannot account for)"
                    )
                    slot_action = "check"
                else:
                    # Nothing live is left under this name, so the lock comes off: the slot row
                    # alone is enough to 409 another tenant, which is the customer case Bug 192
                    # was filed for. Reaching this line already required the slot to carry THIS
                    # caller's sub, an owned version row for THIS deployment (``slot_is_ours``
                    # includes ``target is not None``), and every pointer on the slot to be
                    # accounted for.
                    slot_action = "delete"
            else:
                # A delayed teardown from an earlier tenant may find its retained
                # historical row under a name another tenant has reacquired.
                # Pin the exact current slot and let the store's owner-aware
                # snapshot reject the whole transaction; never delete the old
                # row while advancing or deleting the new tenant's sentinel.
                slot_action = "check"

        # F-81f — the triggers partition, read AFTER the slot, and only when the slot is about to move.
        #
        # ``destroy_runtime`` already enumerated this partition and the caller turned anything short
        # of "confirmed" into ``trigger_cleanup_unconfirmed`` above. But that enumeration ran before
        # the AWS deletes, seconds to minutes ago, and the owner's trigger API was live the whole
        # time: a create that landed in that window is a row this release is about to orphan, since
        # ``routers/triggers._resolve_owned_runtime`` answers through the slot being settled here.
        # So the evidence for "nothing keyed by this name is left" is taken again, strongly
        # consistent, after the slot read. A create that lands after THIS read is the transaction's
        # problem: it moves the slot's ``trigger_fence`` inside its own transaction, and the release
        # below pins the fence it read, so one of the two is cancelled.
        #
        # A row is residue unless it is POSITIVELY somebody else's -- another owner AND a target that
        # names a different runtime -- by the same three-way classification the destroy uses. The
        # slot at this point carries THIS caller's sub, so an unattributable row under it is far more
        # likely a legacy row of the caller's than a stranger's, and treating it as foreign here is
        # exactly the unretryable orphan being prevented. A read failure keeps the name locked too:
        # a locked name is a 409 the tenant can work around, an orphaned trigger is not.
        if slot_action != "none":
            try:
                from app.services.runtime_deployer import _classify_trigger_target
                from app.services.trigger_store import get_trigger_store

                trigger_rows = get_trigger_store().list_for_runtime(friendly, consistent=True)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Runtime name %s: could not read the triggers table before releasing; keeping the name locked",
                    friendly,
                    exc_info=True,
                )
                messages.append(f"Runtime name '{friendly}' kept locked (trigger rows could not be read)")
                return messages
            our_runtime_id = str((target.runtime_id if target is not None else "") or "")
            # F-81f protects the release that REMOVES the name, after which nothing keyed by it could
            # be deleted. While other live versions keep the name in a slot this caller owns, only
            # this deployment's own row goes and the slot stays, so a row whose target is positively
            # ANOTHER runtime is that version's trigger, still reachable through the slot. Counting
            # it kept the name locked and the teardown delete_retained until somebody deleted
            # another version's triggers -- measured live 2026-10-01: two older
            # strands_gateway_agent versions held by the four triggers a live version owns. A row
            # aimed at this runtime, or at a target nobody can attribute, still keeps it locked.
            name_stays_claimed = slot_is_ours and bool(live_remaining)
            residue = [
                t
                for t in trigger_rows
                if not (
                    (name_stays_claimed or (t.owner_sub and t.owner_sub != (caller_sub or "")))
                    and _classify_trigger_target(t.target_runtime_arn, our_runtime_id) == "foreign"
                )
            ]
            if residue:
                logger.warning(
                    "Runtime name %s: %d trigger row(s) still keyed by it; keeping the name locked so they stay deletable",
                    friendly,
                    len(residue),
                )
                messages.append(
                    f"Runtime name '{friendly}' kept locked ({len(residue)} trigger row(s) still registered under it)"
                )
                return messages

        # ONE transaction for both tables, conditioned on exactly the rows that were read.
        #
        # The order used to be: delete the version row, then upsert or delete the slot. A failure
        # between the two -- a throttle, a Lambda timeout, an expired credential -- left a slot
        # pointing at a version row that no longer exists, and ``resolve_owned_runtime_target``
        # needs slot + version + deployment to agree, so the tenant's own production alias stopped
        # resolving: a live agent made uninvokable by a cleanup step. The reverse residue (slot
        # settled, version row still there) only blocks a redeploy of the same name. A transaction
        # removes the ordering question entirely, and the conditions make it a compare-and-set, so
        # a deploy or a promote that moved either row after the reads cancels the release instead
        # of overwriting it.
        release_entire_name = bool(
            target is not None and not live_remaining and slot_action in {"delete", "assert_absent"}
        )
        if target is not None or slot_action != "none":
            try:
                release_name_claim_atomically(
                    friendly,
                    delete_version=(
                        (
                            str(target.version_id),
                            str(target.owner_sub),
                            str(target.deployment_id),
                            str(target.status or "pending"),
                            # None, not "", when the row carried no timestamp: the condition has to
                            # be attribute_not_exists for that shape, and str(None) would pin the
                            # literal "None".
                            str(target.created_at) if target.created_at else None,
                        )
                        if target is not None
                        else None
                    ),
                    claim_snapshot=claim_snapshot,
                    release_name=release_entire_name,
                    slot_action=slot_action,
                    slot_expected=slot if slot_action in {"check", "delete", "clear"} else None,
                    slot_clear_fields=tuple(cleared) if slot_action == "clear" else (),
                )
            except NameClaimReleaseTooLarge:
                # A different cause with the same outcome, and it must not be reported as a lost
                # race: no retry will ever clear it. The name has accumulated more version rows
                # than one DynamoDB transaction can condition on, so it needs an operator (or
                # F-83's bounded claim row) rather than another teardown.
                logger.error(
                    "Runtime name %s cannot be released in one transaction; it has too many "
                    "version rows. Released nothing",
                    friendly,
                )
                messages.append(
                    f"Runtime name '{friendly}' not released (too many version rows to release "
                    "atomically; needs an operator)"
                )
                return messages
            except NameClaimConflict:
                # Nothing was written -- that is the point of the transaction. Losing this race
                # means someone else is using the name right now, so refusing is the outcome we
                # want; report it as not released rather than pretending either way.
                logger.warning("Runtime name %s changed during teardown; released nothing", friendly)
                messages.append(f"Runtime name '{friendly}' not released (changed during teardown)")
                return messages

        if release_entire_name and slot_action == "delete":
            messages.append(f"Released runtime name '{friendly}' (slots/versions)")
        elif release_entire_name:
            messages.append(f"Released runtime name '{friendly}' (versions; slot already absent)")
        elif slot is not None and slot_is_ours and live_remaining:
            messages.append(
                f"Runtime name '{friendly}' still has {len(live_remaining)} live version(s); "
                f"kept slots{' (cleared ' + ', '.join(cleared) + ')' if cleared else ''}"
            )
    return messages


def _run_delete_cleanup(
    runtime_id: str,
    caller_sub: str | None,
    allow_imported_destroy: bool = False,
) -> DeleteResponse:
    """Synchronous teardown body shared by the inline (fast) and background
    (slow / _async_delete) delete paths. Re-does the record lookup internally
    (cheap) so both callers behave identically. The tenant-isolation check
    below still raises HTTPException — fine for the sync path; the async path
    pre-validates ownership before dispatching.

    ``allow_imported_destroy`` defaults to False so that this function — the one that
    actually calls destroy_runtime — is safe for an adopted runtime no matter which
    caller reaches it, rather than relying on the dispatcher having checked.
    """
    cleanup_messages: list[str] = []
    # Audit #11 (tasks/lessons.md Bug 106): track per-step cleanup failures.
    # Bug 44 only flipped the success flag for runtime-destroy; gateway / KB /
    # memory / guardrail / policy-engine / mcp-server cleanups still swallowed
    # exceptions silently into cleanup_messages while returning success=True.
    # Now: any failure to delete an AWS RESOURCE flips overall_success to False so
    # the caller gets an honest signal that resources may have leaked. The one
    # deliberate exception is the slots/versions name release near the end — it
    # frees a DynamoDB name lock, not an AWS resource, and its own comment explains
    # why it must never fail a teardown.
    cleanup_failures: list[str] = []
    cleanup_retained_types: set[str] = set()
    region = config.aws_region

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
        # Tenant isolation: caller must own the deployment.
        # See tasks/lessons.md Bug 37. F-9: a record is REQUIRED here as well, not just
        # in the dispatcher. This body is reachable from the async self-invoke, and
        # everything below it — including destroy_runtime(runtime_id) — used to run
        # against a caller-supplied id when the lookup came back empty.
        owner = (deployment_record or {}).get("user_id")
        if not deployment_record or (owner and owner != caller_sub):
            raise HTTPException(status_code=404, detail="Runtime not found")
        gateway_result = deployment_record.get("gateway_result")
        if gateway_result:
            gateway_config = gateway_result
    except HTTPException:
        raise
    except Exception as exc:
        # A lookup that FAILED must not fall through into the teardown below with
        # deployment_record=None; that was the other half of the bypass.
        logger.warning("Delete: state lookup failed for %s: %s", runtime_id, exc)
        raise HTTPException(
            status_code=503,
            detail="Could not verify this runtime right now. Try again shortly.",
        ) from exc

    # Adopted runtime, no opt-in: release the record and destroy nothing. Checked here
    # too, not only in the dispatcher, because this is the function that calls
    # destroy_runtime.
    if _is_imported_record(deployment_record) and not allow_imported_destroy:
        return _forget_imported_runtime(runtime_id, deployment_record)

    # Every AWS mutation in this teardown must land in the account/region that
    # created the resource. Build one session from the persisted deployment
    # target and reuse it: this avoids both wrong-account fallbacks and a fresh
    # STS AssumeRole call for every individual client.
    from app.services import step_clients

    region = deployment_record.get("target_region") or region
    _target_event = {
        "target_account_id": deployment_record.get("target_account_id"),
        "target_region": region,
        "target_role_arn": deployment_record.get("target_role_arn"),
    }
    _target_session = step_clients.session_for_event(_target_event)

    def _target_client(service: str, **kwargs):
        return _target_session.client(service, **kwargs)

    _cleanup_deployment_id = str(deployment_record.get("deployment_id") or runtime_id)

    def _legacy_manifest_refusal(
        resource_type: str,
        resource_id: str | None,
        *,
        name: str | None = None,
        resource_region: str | None = None,
        pool_id: str | None = None,
    ) -> str | None:
        """Apply the manifest co-residency gate to a legacy result-field fallback.

        An unsealed manifest intentionally leaves the old ``*_result`` cleanup
        paths enabled so a failed manifest append cannot leak a resource.  Those
        paths must still consult the same cross-deployment reference snapshot:
        otherwise a row retained above because a newer deployment adopted the
        resource is immediately deleted below using only stack-level AWS tags.
        """
        identity = str(resource_id or name or "")
        if not identity:
            return None
        resource = {
            "type": resource_type,
            "id": identity,
            "region": resource_region or region,
        }
        if name:
            resource["name"] = str(name)
        if pool_id:
            resource["pool_id"] = str(pool_id)
        return manifest_delete_refusal(
            store,
            _cleanup_deployment_id,
            resource,
            target_account_id=deployment_record.get("target_account_id"),
            target_region=resource_region or region,
        )

    # F-66f: hold every gateway name this teardown may delete, before anything below
    # deletes or decides co-residency, and until the end. A deploy still creating on
    # the name has no manifest row for the co-residency check to see; the lease is the
    # one record it has. Refused means nothing was deleted, and the retry decides again.
    _gateway_rows = list(deployment_record.get("created_resources") or [])
    _legacy_gateway = gateway_config if isinstance(gateway_config, dict) else {}
    if _legacy_gateway.get("gateway_id") and deployment_record.get("resource_manifest_complete") is not True:
        # The legacy result-field cleanup below deletes this one too.
        _gateway_rows.append({"type": "gateway", "id": _legacy_gateway["gateway_id"], "region": region})
    try:
        # A gateway whose every record failed but its name claim. Its row joins the
        # manifest loop below too, which is what deletes it.
        _recovered_rows = recovered_gateway_rows(
            deployment_id=_cleanup_deployment_id,
            owner_sub=owner or caller_sub,
            account_for=lambda: claim_account(deployment_record.get("target_account_id"), _target_client("sts")),
            region=region,
        )
        _gateway_rows = with_recovered_rows(_gateway_rows, _recovered_rows)
        _name_hold = hold_gateway_names_for_teardown(
            _gateway_rows,
            owner_sub=owner or caller_sub,
            deployment_id=_cleanup_deployment_id,
            default_account=deployment_record.get("target_account_id"),
            default_region=region,
            sts_client_for=lambda: _target_client("sts"),
            ctrl_for=lambda r: _target_client("bedrock-agentcore-control", region_name=r),
            prove_owned=lambda r, gid: assert_agentcore_resource_owned(
                _target_client("bedrock-agentcore-control", region_name=r), "gateway", gid, r
            ),
            fallback=_legacy_gateway,
        )
    except GatewayNameClaimRefused as exc:
        return DeleteResponse(success=False, message=str(exc), retained=False)
    except Exception as exc:  # noqa: BLE001
        # Type only: a botocore message can echo the table and key.
        return DeleteResponse(
            success=False,
            message=f"Could not hold the gateway name for this teardown ({type(exc).__name__}); nothing was deleted.",
            retained=False,
        )

    # Loom-study 0.7: delete the AWS Agent Registry record this deploy auto-created
    # (0.4), so teardown doesn't leave a stale/orphaned governance record pointing
    # at a runtime that no longer exists. Best-effort — never blocks teardown.
    _aws_rec_id = (deployment_record or {}).get("aws_registry_record_id")
    if _aws_rec_id:
        # The record id on the deployment row is the retry handle; it is never cleared here, so a
        # failed delete can be retried by a later teardown. Every non-success is a cleanup
        # FAILURE: an unconfigured registry cannot have created this id, so None means the
        # environment lost the registry, and False means the control plane refused.
        try:
            from app.services.aws_agent_registry import get_registry

            _reg = get_registry()
            if _reg is None:
                cleanup_messages.append(f"AWS registry record {_aws_rec_id} not deleted (registry not configured)")
                cleanup_failures.append("aws_registry_record")
            elif _reg.delete(_aws_rec_id):
                cleanup_messages.append(f"AWS registry record {_aws_rec_id} deleted")
            else:
                cleanup_messages.append(f"AWS registry record {_aws_rec_id} not deleted (control plane refused)")
                cleanup_failures.append("aws_registry_record")
        except Exception as exc:  # noqa: BLE001
            # Type only: a botocore message can echo the registry id and record.
            cleanup_messages.append(f"AWS registry record delete error ({type(exc).__name__})")
            cleanup_failures.append("aws_registry_record")

    # Step 0a: GENERIC manifest-driven teardown. Iterate created_resources[] and
    # delete every recorded sub-resource by type. This is the primary teardown
    # path that makes cleanup complete-by-construction (no orphans when a new
    # component is added). The per-component *_result cleanups below remain as a
    # fallback for older records that predate the manifest, and are idempotent
    # against anything this loop already deleted.
    # Only a manifest explicitly sealed complete by the final status step is
    # authoritative. A partial/legacy manifest is still useful inventory, but it
    # must not suppress the live-ownership-gated *_result fallbacks; doing so
    # turns one failed append into a permanent orphan.
    _recorded_rows = with_recovered_rows(
        list((deployment_record or {}).get("created_resources") or []), _recovered_rows
    )
    # A secret whose CreateSecret response was lost, or whose step was killed before
    # the row, has no row; its own tags are the record that survives. The discovered
    # rows go through the same refusal and ownership checks as recorded ones.
    from app.services.gateway_deployer import unrecorded_deployment_secret_rows

    _discovered_rows, _discovery_failures = unrecorded_deployment_secret_rows(
        deployment_id=str(deployment_record.get("deployment_id") or ""),
        recorded_rows=_recorded_rows,
        region=region,
        secrets_client_for=lambda _r: _target_session.client("secretsmanager", region_name=_r),
    )
    _in_gateway_graph = gateway_graph_membership(_recorded_rows)
    # A secret found by its tags could be the gateway's: its producer is unknown.
    _discovered_rows = [{**_r, GATEWAY_GRAPH_FIELD: True} for _r in _discovered_rows]
    for _failed_region, _failed_type in _discovery_failures:
        cleanup_messages.append(
            f"[manifest] secret discovery in {_failed_region} failed ({_failed_type}): "
            "only recorded secrets were deleted (skipped)"
        )
        cleanup_retained_types.add("secret_discovery")
    manifest_used = bool(_recorded_rows or _discovered_rows)
    manifest_authoritative = bool(deployment_record and deployment_record.get("resource_manifest_complete") is True)
    # Track what the manifest ACTUALLY deleted versus what it retained. Row
    # presence alone is not enough: treating a protected runtime row as "handled"
    # both suppressed an honest result and made the post-manifest fallback claim a
    # deletion that never happened.
    manifest_deleted_types: set[str] = set()
    manifest_retained_types: set[str] = set()
    # Why each retention happened, one entry per retained row: "unconfirmed" (the service accepted
    # the delete and this invocation's confirmation budget ran out), "memory-role-deferred" (a role
    # kept for a memory that is not confirmed gone) or "other" (every real refusal). The async
    # teardown continues in a later invocation only when nothing was refused (confirmation_pending).
    manifest_retention_reasons: list[str] = []
    manifest_unconfirmed_types: set[str] = set()

    def _retained(resource_type: str, reason: str = "other") -> None:
        manifest_retained_types.add(resource_type)
        manifest_retention_reasons.append(reason)

    # F-66c: rows kept only because another deployment still lists them. Counting
    # these as retained made this tombstone delete_retained, which protects the row
    # back, so the other deployment's teardown kept it too, and every retry of
    # either repeated that forever. Resolved at the end of the teardown.
    manifest_handoff_types: set[str] = set()
    # (type, id) of every handed-off row: whether a hand-off is this deployment's OWN runtime or
    # harness, or a secondary shared with another deployment, decides what it means below.
    manifest_handoff_keys: set[tuple[str, str]] = set()
    _handed_off_gateways: list[dict] = []
    if manifest_used:
        seen_mres: set[tuple[str, str, str, str]] = set()
        # Bug 167: tear down in DEPENDENCY order. A "primary" resource whose
        # delete cascades into a backing store or needs its exec role (KB ->
        # S3 Vectors store + role; runtime/gateway -> their child resources)
        # MUST be deleted (and reach a terminal state) BEFORE the secondaries it
        # depends on. Lower priority number = deleted earlier. Unlisted types
        # default to the middle band, then backing-stores/roles/secrets last.
        _late_cleanup_priority = 9
        _DELETE_PRIORITY = {
            "knowledge_base": 0,
            "agent_runtime": 1,
            "harness": 1,
            "gateway": 2,
            # Informational row for a customer-run LiteLLM proxy; deletes nothing.
            # Sits in the gateway band purely for symmetry with the arm above.
            "litellm_gateway": 2,
            # F-74b: provenance row, deletes nothing. Ordered before the gateway (2) anyway,
            # because that is where a refcounted per-target delete will have to run -- a
            # target must go before the gateway it is attached to, never after. Same value as
            # status_update_step's map, deliberately.
            "gateway_target": 1,
            # F-G09-003: a policy child goes before its engine (a shared engine outlives it).
            "policy": 1,
            "policy_engine": 2,
            "lambda": 5,
            "memory": 6,
            "guardrail": 6,
            "oauth2_credential_provider": 7,
            "api_key_credential_provider": 7,
            # Secondaries that must OUTLIVE the primaries above:
            "s3_vectors_bucket": 8,
            "oss_collection": 8,
            "iam_role": 9,
            # Before the pool (9) so a stack-owned pool's clients are gone first, and
            # after the gateway (2) whose customJWTAuthorizer pins this client id.
            "cognito_app_client": 8,
            # After the client (8): the resource-server delete is gated on "no client
            # can still be using this scope", which is only true once ours is deleted.
            "cognito_resource_server": 9,
            "cognito_user_pool": 9,
            "secret": _late_cleanup_priority,
            "s3_object": 9,
        }
        _ordered = sorted(
            collapse_secret_intent_rows(
                _recorded_rows + _discovered_rows,
                default_account=deployment_record.get("target_account_id"),
                default_region=deployment_record.get("target_region") or region,
            ),
            key=lambda r: _DELETE_PRIORITY.get(str(r.get("type")), 4),
        )
        store.reset_manifest_reference_cache(str((deployment_record or {}).get("deployment_id") or runtime_id))
        # Phase 7 (opt-in) — if this deploy targeted another account, inject that
        # account onto each manifest resource that didn't record its own, so
        # _delete_managed_resource assumes the same cross-account role to delete.
        _dep_target_account = deployment_record.get("target_account_id")
        _dep_target_region = deployment_record.get("target_region") or region
        _reused_keys = {
            manifest_resource_key(
                _res,
                default_account=_dep_target_account,
                default_region=_dep_target_region,
            )
            for _res in _ordered
            if _res.get("created_by_deployment") is False
        }
        # A gateway that survived its delete (retained, refused, or failed): nothing in its graph is touched after it, since its
        # authorizer pins the client and its targets call the Lambdas and providers.
        # The rows stay in the manifest, so a retry finishes the graph with it.
        _frozen_gateway: str | None = None

        def _report_targets_deleted(gateway_id: str, exc: BaseException) -> None:
            # A retained or failed gateway that already lost targets is a partial teardown.
            if gateway_targets_deleted(exc):
                cleanup_messages.append(
                    f"[manifest] {describe_gateway_targets_deleted(gateway_id, gateway_targets_deleted(exc))}"
                )

        for _res in _ordered:
            _this_gateway: str | None = None
            try:
                if _dep_target_account and not _res.get("account"):
                    _res = {**_res, "account": _dep_target_account}
                key = manifest_resource_key(
                    _res,
                    default_account=_dep_target_account,
                    default_region=_dep_target_region,
                )
                if key in seen_mres:
                    continue
                seen_mres.add(key)
                if key in _reused_keys and _res.get("created_by_deployment") is not False:
                    _res = {**_res, "created_by_deployment": False}
                if _frozen_gateway and _in_gateway_graph(_res):
                    cleanup_messages.append(
                        f"[manifest] retained {key[0]} {key[1]}: gateway {_frozen_gateway} was not deleted, "
                        "so its graph is left intact"
                    )
                    _retained(key[0])
                    continue
                if key[0] == "gateway":
                    # Cleared only by a reported delete or a hand-off.
                    _this_gateway = key[1]
                _refusal = manifest_delete_refusal(
                    store,
                    str((deployment_record or {}).get("deployment_id") or runtime_id),
                    _res,
                    target_account_id=(deployment_record or {}).get("target_account_id"),
                    target_region=_dep_target_region,
                )
                if _refusal == CO_RESIDENT_REFUSAL:
                    # Handed off, not retained: another deployment's teardown owns it,
                    # and this deployment's own client is what F-66b revokes below.
                    _this_gateway = None
                    cleanup_messages.append(f"[manifest] handed off {key[0]} {key[1]}: {_refusal}")
                    manifest_handoff_types.add(key[0])
                    manifest_handoff_keys.add((str(key[0]), str(key[1])))
                    if key[0] == "gateway":
                        # F-66b: band 2 runs before the clients (8), which revoke here.
                        _handed_off_gateways.append({**_res, "region": _res.get("region") or _dep_target_region})
                    continue
                if _refusal:
                    cleanup_messages.append(f"[manifest] retained {key[0]} {key[1]}: {_refusal}")
                    _retained(key[0])
                    continue
                # F-56: memory (band 6) runs before roles (band 9). A memory that was
                # retained or failed may still exist -- DELETING, or never touched -- and
                # deleting its execution role first strands it. The manifest carries no
                # role->memory link, so bind by this deployment's recorded role name
                # and by the AgentCoreMemory- prefix regional_iam_role_name preserves.
                # A handed-off memory still exists too.
                if (
                    _res.get("type") == "iam_role"
                    and (
                        "memory" in manifest_retained_types
                        or "memory" in manifest_handoff_types
                        or "memory" in cleanup_failures
                    )
                    and _is_memory_role_name(str(_res.get("name") or ""), deployment_record)
                ):
                    cleanup_messages.append(
                        f"[manifest] retained iam_role {_res.get('name')}: its memory is not confirmed deleted"
                    )
                    _retained("iam_role", "memory-role-deferred")
                    continue
                if _res.get("type") == "cognito_app_client" and _handed_off_gateways:
                    # Revoked before the delete, and deleted even if revoking failed:
                    # the delete still stops new tokens. The failure keeps the
                    # teardown delete_failed so a retry revokes again.
                    _revoke_msgs, _revoke_failed = _revoke_client_on_kept_gateways(
                        str(_res.get("id") or ""),
                        _handed_off_gateways,
                        lambda gw: _target_client("bedrock-agentcore-control", region_name=gw["region"]),
                    )
                    cleanup_messages.extend(_revoke_msgs)
                    if _revoke_failed:
                        cleanup_failures.append("gateway_allowed_clients")
                _msg = _delete_managed_resource(
                    _res,
                    region,
                    sidecar_failures=cleanup_failures,
                    deployment_id=(deployment_record or {}).get("deployment_id"),
                    target_role_arn=(deployment_record or {}).get("target_role_arn"),
                    target_session=_target_session,
                    owner_sub=owner or caller_sub,
                )
                if _msg:
                    cleanup_messages.append(_msg)
                    _msg_lower = _msg.lower()
                    if any(
                        marker in _msg_lower
                        for marker in (
                            "skipped",
                            "protected",
                            "left in place",
                        )
                    ):
                        _retained(key[0])
                    else:
                        manifest_deleted_types.add(key[0])
                        _this_gateway = None
                elif _this_gateway:
                    # No report is not proof of a delete.
                    _retained(key[0])
            except ResourceDeletionRefused as exc:
                cleanup_messages.append(f"[manifest] retained {key[0]} {key[1]}: {exc}")
                _report_targets_deleted(key[1], exc)
                if isinstance(exc, ConfirmationBudgetExhausted):
                    # Accepted by the service and not finished inside this invocation's budget:
                    # pending, not refused. The verdict below decides whether the teardown goes on.
                    manifest_unconfirmed_types.add(key[0])
                    _retained(key[0], "unconfirmed")
                else:
                    _retained(key[0])
            except Exception as exc:  # noqa: BLE001
                cleanup_messages.append(f"Manifest cleanup error ({_res.get('type')}): {exc}")
                _report_targets_deleted(key[1], exc)
                cleanup_failures.append(str(_res.get("type")))
            finally:
                if _this_gateway:
                    _frozen_gateway = _this_gateway

    manifest_deleted_runtime = "agent_runtime" in manifest_deleted_types
    # A handed-off runtime or harness still exists, so the legacy destroy below must skip it.
    manifest_retained_runtime = "agent_runtime" in manifest_retained_types or "agent_runtime" in manifest_handoff_types
    manifest_deleted_harness = "harness" in manifest_deleted_types
    manifest_retained_harness = "harness" in manifest_retained_types or "harness" in manifest_handoff_types

    # (type, identity) of every row the manifest loop above reached, whatever it decided.
    _manifest_decided = {key[:2] for key in seen_mres} if manifest_used else set()

    def _decided_by_manifest(*resources: dict) -> bool:
        """Whether the manifest loop already decided every one of these exact resources.

        The legacy fallbacks below are for a resource no manifest row records: an unsealed manifest
        keeps them enabled so a failed append cannot leak one. When rows do record the whole
        subject of a single-resource step, the loop's outcome (deleted, retained, handed off or
        failed) is the decision, and deciding again here can only disagree. Measured 2026-10-02:
        four failed mcp-server-gateway-target deploys shared one MCP server runtime. Each handed it
        off in the loop, then retained it here because another one still referenced it. The
        retention made each teardown delete_retained, each tombstone kept the others' references
        live, and the last two could never be deleted. The coarse steps (gateway, Knowledge Base)
        still run regardless: they also clean children an unsealed manifest may not record.
        """
        return bool(resources) and all(
            resource.get("id") and manifest_resource_key(resource)[:2] in _manifest_decided for resource in resources
        )

    # Step 0: Clean up MCP server runtime if one was deployed
    # (legacy fallback — skipped when the manifest already handled teardown)
    mcp_server_runtime_id = deployment_record.get("mcp_server_runtime_id") if deployment_record else None
    if (
        mcp_server_runtime_id
        and not manifest_authoritative
        and _decided_by_manifest({"type": "agent_runtime", "id": mcp_server_runtime_id})
    ):
        cleanup_messages.append(f"MCP server runtime {mcp_server_runtime_id}: decided by its manifest row")
    elif mcp_server_runtime_id and not manifest_authoritative:
        mcp_runtime_refusal = _legacy_manifest_refusal(
            "agent_runtime",
            mcp_server_runtime_id,
        )
        if mcp_runtime_refusal:
            cleanup_messages.append(
                f"MCP server runtime {mcp_server_runtime_id} left in place (protected): {mcp_runtime_refusal}"
            )
            cleanup_retained_types.add("mcp_server_runtime")
        else:
            try:
                mcp_destroy = destroy_runtime(
                    mcp_server_runtime_id,
                    region,
                    client_factory=_target_client,
                    delete_execution_role=not bool(_target_event.get("target_account_id")),
                )
                for _sidecar in mcp_destroy.get("sidecar_failures") or []:
                    cleanup_failures.append(f"mcp_runtime_sidecar:{_sidecar}")
                cleanup_messages.append(f"MCP server runtime destroyed: {mcp_destroy.get('message', 'ok')}")
                if not mcp_destroy.get("success", True):
                    if mcp_destroy.get("retained"):
                        cleanup_retained_types.add("mcp_server_runtime")
                    else:
                        cleanup_failures.append("mcp_server_runtime")
            except Exception as exc:
                cleanup_messages.append(f"MCP server runtime cleanup error: {exc}")
                cleanup_failures.append("mcp_server_runtime")

    # Step 0.5: Clean up policy engine if one was attached
    # Correct order: detach from gateway → delete policies → delete engine
    if deployment_record and not manifest_authoritative:
        policy_result = deployment_record.get("policy_result") or {}
        policy_engine_id = policy_result.get("engine_id")
        if policy_engine_id:
            policy_refusal = _legacy_manifest_refusal(
                "policy_engine",
                policy_engine_id,
            )
            if policy_refusal:
                cleanup_messages.append(f"Policy engine {policy_engine_id} left in place (protected): {policy_refusal}")
                cleanup_retained_types.add("policy_engine")
            else:
                try:
                    agentcore_ctrl = _target_client("bedrock-agentcore-control", region_name=region)
                    try:
                        assert_agentcore_resource_owned(
                            agentcore_ctrl,
                            "policy_engine",
                            policy_engine_id,
                            region,
                        )
                    except Exception as ownership_exc:  # noqa: BLE001
                        if resource_is_missing(ownership_exc):
                            cleanup_messages.append(f"Policy engine already gone: {policy_engine_id}")
                            policy_engine_id = None
                        else:
                            raise

                    # 1. Detach engine from gateway
                    gw_result = deployment_record.get("gateway_result") or {}
                    gw_id = gw_result.get("gateway_id")
                    if policy_engine_id and gw_id:
                        try:
                            gateway_refusal = _legacy_manifest_refusal(
                                "gateway",
                                gw_id,
                            )
                            if gateway_refusal:
                                raise ResourceDeletionRefused(
                                    "Policy engine retained because another live "
                                    "deployment still references its attached gateway"
                                )
                            # F-66e: the detach re-sends the authorizer it reads, so it
                            # reads and writes under the gateway's write lock.
                            with gateway_mutation_lock(agentcore_ctrl, region, gw_id) as gw_lock:
                                assert_agentcore_resource_owned(
                                    agentcore_ctrl,
                                    "gateway",
                                    gw_id,
                                    region,
                                )
                                # Detaching IS the update without
                                # policyEngineConfiguration. It is a full replace, so
                                # everything else the gateway holds is re-sent (F-62).
                                gw_lock.update(
                                    preserving_gateway_update(
                                        gw_lock.read(),
                                        gw_id,
                                        detach={"policyEngineConfiguration"},
                                    ),
                                    engine_detached,
                                )
                                cleanup_messages.append(f"Policy engine detached from gateway {gw_id}")
                        except Exception as detach_exc:  # noqa: BLE001
                            if resource_is_missing(detach_exc):
                                cleanup_messages.append(f"Gateway already gone before policy detach: {gw_id}")
                            else:
                                # Do not delete an engine still referenced by a
                                # gateway whose ownership or update result is unknown.
                                raise ResourceDeletionRefused(
                                    "Policy engine retained because its attached gateway could not be safely detached"
                                ) from detach_exc

                    # 2. Delete all policies attached to the engine
                    if policy_engine_id:
                        delete_policy_engine_confirmed(
                            agentcore_ctrl,
                            policy_engine_id,
                        )
                        cleanup_messages.append(f"Policy engine confirmed deleted: {policy_engine_id}")
                except ResourceDeletionRefused as exc:
                    cleanup_messages.append(
                        f"Policy engine {policy_result.get('engine_id')} left in place (protected): {exc}"
                    )
                    cleanup_retained_types.add("policy_engine")
                except Exception as exc:
                    cleanup_messages.append(f"Policy engine cleanup error: {exc}")
                    cleanup_failures.append("policy_engine")

    # Step 0.6: Clean up memory if one was created
    if deployment_record and not manifest_authoritative:
        memory_result = deployment_record.get("memory_result") or {}
        memory_id = memory_result.get("memory_id")
        # Bug 158: memory_step creates an AgentCoreMemory-<name> IAM role; the
        # delete path deleted the memory but ORPHANED the role (confirmed live).
        # Delete it too (best-effort, idempotent).
        memory_name = memory_result.get("memory_name")
        mem_role_name = memory_result.get("memory_role_name")
        if not mem_role_name and memory_name:
            # Compatibility with records created before memory_result
            # persisted the exact IAM role name.
            mem_role_name = f"AgentCoreMemory-{memory_name}"
        if memory_id and _decided_by_manifest(
            {"type": "memory", "id": memory_id},
            *([{"type": "iam_role", "id": mem_role_name, "name": mem_role_name}] if mem_role_name else []),
        ):
            cleanup_messages.append(f"Memory {memory_id}: decided by its manifest rows")
        elif memory_id:
            # F-56: the role below may only go once the memory is PROVEN absent. A
            # DELETING acknowledgement, a refusal or an error all leave it possibly
            # alive, and a memory whose execution role is gone is an orphan nobody can
            # operate.
            memory_gone = False
            memory_refusal = _legacy_manifest_refusal("memory", memory_id)
            if memory_refusal:
                cleanup_messages.append(f"Memory {memory_id} left in place (protected): {memory_refusal}")
                cleanup_retained_types.add("memory")
            else:
                try:
                    agentcore_ctrl = _target_client("bedrock-agentcore-control", region_name=region)
                    delete_memory_confirmed(
                        agentcore_ctrl,
                        memory_id,
                        region=region,
                        owner_sub=owner or caller_sub,
                        **_memory_confirmation_budget(),
                    )
                    memory_gone = True
                    cleanup_messages.append(f"Memory confirmed deleted: {memory_id}")
                except ResourceDeletionRefused as exc:
                    cleanup_messages.append(f"Memory {memory_id} left in place (protected): {exc}")
                    cleanup_retained_types.add("memory")
                except Exception as exc:
                    if resource_is_missing(exc):
                        memory_gone = True
                        cleanup_messages.append(f"Memory already gone: {memory_id}")
                    else:
                        cleanup_messages.append(f"Memory cleanup error: {exc}")
                        cleanup_failures.append("memory")
            if mem_role_name and not memory_gone:
                cleanup_messages.append(
                    f"Memory IAM role {mem_role_name} left in place: memory {memory_id} is not confirmed deleted"
                )
                cleanup_retained_types.add("memory_role")
            elif mem_role_name:
                role_refusal = memory_refusal or _legacy_manifest_refusal(
                    "iam_role",
                    mem_role_name,
                    name=mem_role_name,
                )
                if role_refusal:
                    cleanup_messages.append(
                        f"Memory IAM role {mem_role_name} left in place (protected): {role_refusal}"
                    )
                    cleanup_retained_types.add("memory_role")
                else:
                    try:
                        iam_c = _target_client("iam")
                        delete_owned_iam_role(iam_c, mem_role_name, region)
                        cleanup_messages.append(f"Memory IAM role deleted: {mem_role_name}")
                    except ResourceDeletionRefused as exc:
                        cleanup_messages.append(f"Memory IAM role {mem_role_name} left in place (protected): {exc}")
                        cleanup_retained_types.add("memory_role")
                    except Exception as exc:
                        if not is_error(exc, "NoSuchEntity", "NoSuchEntityException"):
                            cleanup_messages.append(f"Memory role cleanup error: {exc}")
                            cleanup_failures.append("memory_role")

    # Step 0.7: Clean up guardrail if we created it
    if deployment_record and not manifest_authoritative:
        guardrails_result = deployment_record.get("guardrails_result") or {}
        if guardrails_result.get("created_by_flow"):
            guardrail_id = guardrails_result.get("guardrail_id")
            if guardrail_id and _decided_by_manifest({"type": "guardrail", "id": guardrail_id}):
                cleanup_messages.append(f"Guardrail {guardrail_id}: decided by its manifest row")
            elif guardrail_id:
                guardrail_refusal = _legacy_manifest_refusal(
                    "guardrail",
                    guardrail_id,
                )
                if guardrail_refusal:
                    cleanup_messages.append(f"Guardrail {guardrail_id} left in place (protected): {guardrail_refusal}")
                    cleanup_retained_types.add("guardrail")
                else:
                    try:
                        bedrock_client = _target_client("bedrock", region_name=region)
                        assert_guardrail_owned(
                            bedrock_client,
                            guardrail_id,
                            region,
                        )
                        bedrock_client.delete_guardrail(guardrailIdentifier=guardrail_id)
                        cleanup_messages.append(f"Guardrail deleted: {guardrail_id}")
                    except ResourceDeletionRefused as exc:
                        cleanup_messages.append(f"Guardrail {guardrail_id} left in place (protected): {exc}")
                        cleanup_retained_types.add("guardrail")
                    except Exception as exc:
                        if resource_is_missing(exc):
                            cleanup_messages.append(f"Guardrail already gone: {guardrail_id}")
                        else:
                            cleanup_messages.append(f"Guardrail cleanup error: {exc}")
                            cleanup_failures.append("guardrail")

    # Step 1: Clean up gateway resources
    if gateway_config and not manifest_authoritative:
        gateway_id = gateway_config.get("gateway_id") if isinstance(gateway_config, dict) else None
        gateway_refusal = _legacy_manifest_refusal("gateway", gateway_id)
        if gateway_refusal:
            cleanup_messages.append(f"Gateway {gateway_id} left in place (protected): {gateway_refusal}")
            cleanup_retained_types.add("gateway")
        else:
            try:
                with gateway_aws_session(_target_session):
                    gw_log = cleanup_gateway_resources(
                        runtime_id=runtime_id,
                        region=region,
                        gateway_config=gateway_config,
                        deployment_id=(deployment_record or {}).get("deployment_id", ""),
                    )
                cleanup_messages.extend(gw_log)
                # cleanup_gateway_resources() never raises but reports per-resource
                # errors as " ... error:" lines in its log. Treat any of those as
                # a cleanup failure so we don't return success=True when a target
                # / pool / Lambda was actually leaked.
                if any(" error:" in line or " error " in line for line in gw_log):
                    cleanup_failures.append("gateway")
                if any(marker in line.lower() for line in gw_log for marker in ("(protected)", "left in place")):
                    cleanup_retained_types.add("gateway")
            except Exception as exc:
                cleanup_messages.append(f"Gateway cleanup error: {exc}")
                cleanup_failures.append("gateway")

    # Step 1.5: Clean up Knowledge Base resources
    kb_result = (deployment_record or {}).get("knowledge_base_result") or {}
    dep_id = (deployment_record or {}).get("deployment_id", "")
    # NOTE: run this EVEN WHEN manifest_used. The KB-as-gateway-tool Lambda and
    # its execution role (AgentCoreKBToolRole-<dep8>) are created by the gateway
    # deployer with deterministic names derived from the deployment id, but they
    # are NOT recorded in the teardown manifest — so manifest-driven teardown
    # would orphan the IAM role (observed live in the E2E matrix run). Deleting
    # by deterministic name is idempotent and safe in both paths.
    kb_lambda_suffix = dep_id[:8] if dep_id else ""
    if kb_lambda_suffix:
        # F-7d: KB tool functions and roles are stack-scoped by name now
        # (AgentCore-<token>-KBTool-<dep8>); records written before that used the unscoped
        # pair. Both are tried, each through the same ownership gate, so a legacy record still
        # cleans up and a scoped one is not orphaned. A name that does not exist is "already
        # gone" on either side.
        from app.services.naming import deployment_scope_suffix, scoped_function_name, scoped_role_name

        # The scoped pair uses a digest of the FULL deployment id (peer 82: two ids sharing
        # eight hex characters collapsed onto one function); the legacy pair keeps dep[:8].
        _scoped_suffix = deployment_scope_suffix(dep_id)
        _kb_name_pairs = (
            (
                scoped_function_name("KBTool", stack_id(region), _scoped_suffix),
                scoped_role_name("KBTool", stack_id(region), _scoped_suffix),
            ),
            (f"AgentCore-KBTool-{kb_lambda_suffix}", f"AgentCoreKBToolRole-{kb_lambda_suffix}"),
        )
        for index, (kb_fn_name, kb_role_name) in enumerate(_kb_name_pairs):
            # The scoped pair (index 0) is bound to the exact DeploymentId on both the function
            # and the role, and that binding is REQUIRED before either delete (peer 3c): a
            # same-stack digest collision is never deletion authority. The legacy pair carries
            # no such tag; its function is accepted only when its Description names this
            # deployment id in full (the create always wrote it), and its role only after
            # that function was verified -- fail-closed otherwise.
            _scoped_pair = index == 0
            _kb_required = {"DeploymentId": dep_id} if _scoped_pair else None
            # verified: the legacy function named this deployment and was deleted here;
            # absent: no function holds the legacy name (a deployment with no KB tool, or an
            # earlier pass removed it) -- the role delete then falls back to the stack-ownership
            # gate, which is what it always was, and a missing role is silently nothing;
            # mismatch: a function holds the name but is NOT this deployment's -> the role is
            # kept too. Only "mismatch" may produce a retained row (peer 5e: a deployment with
            # no KB must tear down clean, not invent a retained role).
            _legacy_function_state = "absent"
            try:
                lambda_client = _target_client("lambda", region_name=region)
                with shared_lambda_lock(region, kb_fn_name):  # F-7d: auth + delete of the pair, fenced
                    kb_lambda_manifest_refusal = _legacy_manifest_refusal(
                        "lambda",
                        kb_fn_name,
                        name=kb_fn_name,
                    )
                    if kb_lambda_manifest_refusal:
                        cleanup_messages.append(
                            f"KB Lambda {kb_fn_name} left in place (protected): {kb_lambda_manifest_refusal}"
                        )
                        cleanup_retained_types.add("kb_lambda")
                    else:
                        try:
                            # F-7c: this name is derived, not recorded -- the first 8 hex of a uuid4,
                            # which is 4.3e9 values and therefore a name that CAN be held by something
                            # else. "Deleting by deterministic name is idempotent and safe in both
                            # paths", above, is true about repetition and says nothing about ownership.
                            kb_refusal = _authorize_tool_function_deletion(
                                lambda_client, kb_fn_name, region, required_tags=_kb_required
                            )
                            if not kb_refusal and not _scoped_pair:
                                _desc = str(
                                    (
                                        lambda_client.get_function(FunctionName=kb_fn_name).get("Configuration") or {}
                                    ).get("Description", "")
                                )
                                if _desc != f"KB Query tool for deployment {dep_id}":
                                    kb_refusal = (
                                        "kept (legacy name derived from the first 8 hex of the deployment id, and its "
                                        "Description does not name this deployment exactly -- a prefix collision, not ours)"
                                    )
                                    _legacy_function_state = "mismatch"
                            if kb_refusal:
                                cleanup_messages.append(f"KB Lambda {kb_fn_name} {kb_refusal}")
                                cleanup_retained_types.add("kb_lambda")
                                if not _scoped_pair and _legacy_function_state != "mismatch":
                                    _legacy_function_state = "mismatch"  # kept for any reason: not proven ours
                            else:
                                _legacy_function_state = "verified"
                                try:
                                    lambda_client.delete_function(FunctionName=kb_fn_name)
                                    cleanup_messages.append(f"KB Lambda deleted: {kb_fn_name}")
                                except Exception as delete_exc:  # noqa: BLE001
                                    if resource_is_missing(delete_exc):
                                        cleanup_messages.append(f"KB Lambda already gone: {kb_fn_name}")
                                    else:
                                        raise
                        except Exception as lambda_exc:  # noqa: BLE001
                            if resource_is_missing(lambda_exc):
                                cleanup_messages.append(f"KB Lambda already gone: {kb_fn_name}")
                            else:
                                cleanup_messages.append(f"KB Lambda cleanup error: {type(lambda_exc).__name__}")
                                cleanup_failures.append("kb_lambda")
                    iam_client = _target_client("iam")
                    kb_role_refusal = kb_lambda_manifest_refusal or _legacy_manifest_refusal(
                        "iam_role",
                        kb_role_name,
                        name=kb_role_name,
                    )
                    if not kb_role_refusal and _scoped_pair:
                        try:
                            _role_tags = tag_map(iam_client.get_role(RoleName=kb_role_name)["Role"].get("Tags"))
                        except Exception as role_read_exc:  # noqa: BLE001
                            if is_error(role_read_exc, "NoSuchEntity", "NoSuchEntityException"):
                                _role_tags = None
                            else:
                                raise
                        if _role_tags is not None and _role_tags.get("DeploymentId") != dep_id:
                            kb_role_refusal = (
                                f"bound to DeploymentId={_role_tags.get('DeploymentId') or '<untagged>'}, not "
                                f"{dep_id}; a name collision is never deletion authority"
                            )
                    elif not kb_role_refusal and _legacy_function_state == "mismatch":
                        kb_role_refusal = "its legacy function was not verified as this deployment's in this pass"
                    if kb_role_refusal:
                        cleanup_messages.append(
                            f"KB Lambda role {kb_role_name} left in place (protected): {kb_role_refusal}"
                        )
                        cleanup_retained_types.add("kb_lambda_role")
                    else:
                        try:
                            delete_owned_iam_role(iam_client, kb_role_name, region)
                            cleanup_messages.append(f"KB Lambda role deleted: {kb_role_name}")
                        except ResourceDeletionRefused as role_exc:
                            cleanup_messages.append(
                                f"KB Lambda role {kb_role_name} left in place (protected): {role_exc}"
                            )
                            cleanup_retained_types.add("kb_lambda_role")
                        except Exception as role_exc:
                            if not is_error(role_exc, "NoSuchEntity", "NoSuchEntityException"):
                                cleanup_messages.append(f"KB Lambda role cleanup error: {role_exc}")
                                cleanup_failures.append("kb_lambda_role")
            except Exception as exc:
                cleanup_messages.append(f"KB Lambda cleanup error: {exc}")
                cleanup_failures.append("kb_lambda")
    # Legacy fallback for records that predate created_resources. Once any
    # manifest is present it is the deletion authority: re-running this block
    # after a manifest refusal would destroy a KB that a newer deployment still
    # references. Missing manifest rows must retain, not revive an ungated path.
    if kb_result.get("created_by_flow") and not manifest_authoritative:
        try:
            bedrock_agent = _target_client("bedrock-agent", region_name=region)
            kb_id = kb_result.get("kb_id")
            ds_id = kb_result.get("data_source_id")
            kb_protected = False
            if kb_id:
                kb_manifest_refusal = _legacy_manifest_refusal(
                    "knowledge_base",
                    kb_id,
                )
                if kb_manifest_refusal:
                    cleanup_messages.append(f"Knowledge Base {kb_id} left in place (protected): {kb_manifest_refusal}")
                    cleanup_retained_types.add("knowledge_base")
                    kb_protected = True
                else:
                    try:
                        assert_knowledge_base_owned(
                            bedrock_agent,
                            kb_id,
                            region,
                        )
                    except ResourceDeletionRefused as ownership_exc:
                        cleanup_messages.append(f"Knowledge Base {kb_id} left in place (protected): {ownership_exc}")
                        cleanup_retained_types.add("knowledge_base")
                        kb_protected = True
                    except Exception as ownership_exc:  # noqa: BLE001
                        if resource_is_missing(ownership_exc):
                            cleanup_messages.append(f"Knowledge Base already gone: {kb_id}")
                            kb_id = None
                        else:
                            raise
            if ds_id and kb_id and not kb_protected:
                # A KB delete with dataDeletionPolicy=DELETE cascades and removes
                # the data source first, so an explicit DeleteDataSource can race
                # to ResourceNotFoundException. That is SUCCESS (the DS is gone),
                # not a cleanup failure — swallow not-found so the KB leg isn't
                # falsely reported success=false while everything actually deleted.
                try:
                    bedrock_agent.delete_data_source(knowledgeBaseId=kb_id, dataSourceId=ds_id)
                except Exception as ds_exc:  # noqa: BLE001
                    # "does not exist" fallback kept: the KB-delete cascade race can
                    # also surface as a ValidationException with that message.
                    if (
                        not is_error(ds_exc, "ResourceNotFoundException")
                        and "does not exist" not in str(ds_exc).lower()
                    ):
                        raise
            if kb_id and not kb_protected:
                # Manifest teardown may have ALREADY deleted this KB (it also
                # carries a knowledge_base resource) — a second delete then
                # returns ResourceNotFoundException, which is SUCCESS (the KB is
                # gone), not a failure. Swallow not-found so the async delete
                # isn't falsely marked delete_failed while everything actually
                # deleted (production-readiness: false-negative teardown).
                try:
                    bedrock_agent.delete_knowledge_base(knowledgeBaseId=kb_id)
                    wait_until_absent(
                        resource_label=f"knowledge base {kb_id}",
                        read=lambda: bedrock_agent.get_knowledge_base(knowledgeBaseId=kb_id),
                        max_attempts=24,
                        delay_seconds=5,
                    )
                    cleanup_messages.append(f"Knowledge Base confirmed deleted: {kb_id}")
                except Exception as kb_exc:  # noqa: BLE001
                    if not is_error(kb_exc, "ResourceNotFoundException") and "not found" not in str(kb_exc).lower():
                        raise
                    cleanup_messages.append(f"Knowledge Base already gone: {kb_id}")
            # Delete KB IAM role (idempotent — manifest may have removed it)
            kb_role_arn = kb_result.get("kb_role_arn", "")
            if kb_role_arn and not kb_protected:
                kb_iam_role_name = kb_role_arn.split("/")[-1] if "/" in kb_role_arn else ""
                if kb_iam_role_name:
                    kb_role_refusal = _legacy_manifest_refusal(
                        "iam_role",
                        kb_iam_role_name,
                        name=kb_iam_role_name,
                    )
                    if kb_role_refusal:
                        cleanup_messages.append(
                            f"KB IAM role {kb_iam_role_name} left in place (protected): {kb_role_refusal}"
                        )
                        cleanup_retained_types.add("knowledge_base_role")
                    else:
                        iam_c = _target_client("iam")
                        try:
                            delete_owned_iam_role(iam_c, kb_iam_role_name, region)
                            cleanup_messages.append(f"KB IAM role deleted: {kb_iam_role_name}")
                        except iam_c.exceptions.NoSuchEntityException:
                            cleanup_messages.append(f"KB IAM role already gone: {kb_iam_role_name}")
                        except ResourceDeletionRefused as role_exc:
                            cleanup_messages.append(
                                f"KB IAM role {kb_iam_role_name} left in place (protected): {role_exc}"
                            )
                            cleanup_retained_types.add("knowledge_base_role")
        except ResourceDeletionRefused as exc:
            cleanup_messages.append(f"Knowledge Base cleanup retained resources: {exc}")
            cleanup_retained_types.add("knowledge_base")
        except Exception as exc:
            cleanup_messages.append(f"KB cleanup error: {exc}")
            cleanup_failures.append("knowledge_base")

    # Step 2: Destroy the runtime via boto3 — or the Harness (Phase B). HARNESS
    # mode targets the managed harness instead of an AgentCore Runtime; the
    # harness id was persisted on the record (fall back to runtime_id, which the
    # frontend sends as the deployment id for harness deploys).
    runtime_destroy_failed = False
    # Bound before the branch, not inferred after it: every arm below assigns it, but the two
    # ``except`` handlers can fire before their assignment, and the F-81f triggers verdict reads it.
    # A conditional local read behind an exception path is exactly how an earlier abort handler in
    # this file ended up reasoning about a variable that was never set.
    destroy_result: dict | None = None
    if deployment_record and deployment_record.get("deployment_mode") == "harness":
        if manifest_deleted_harness:
            logger.info(
                "Skipping duplicate post-manifest harness destroy for %s",
                runtime_id,
            )
            destroy_result = {"success": True, "note": "handled by manifest"}
        elif manifest_retained_harness:
            logger.warning(
                "Suppressing post-manifest harness destroy for %s because the manifest explicitly retained it",
                runtime_id,
            )
            destroy_result = {
                "success": False,
                "note": "retained by manifest deletion-authority policy",
            }
        else:
            harness_id = deployment_record.get("harness_id") or runtime_id
            harness_refusal = _legacy_manifest_refusal(
                "harness",
                harness_id,
            )
            if harness_refusal:
                cleanup_messages.append(f"Harness {harness_id} left in place (protected): {harness_refusal}")
                cleanup_retained_types.add("harness")
                destroy_result = {
                    "success": False,
                    "retained": True,
                    "note": harness_refusal,
                }
            else:
                try:
                    destroy_result = destroy_harness(
                        harness_id,
                        region,
                        agentcore_ctrl=_target_client(
                            "bedrock-agentcore-control",
                            region_name=region,
                        ),
                    )
                    cleanup_messages.append(
                        f"Harness destroy: {destroy_result.get('note', destroy_result.get('harness_id', 'ok'))}"
                    )
                    if not destroy_result.get("success", True):
                        if destroy_result.get("retained") or destroy_result.get("protected"):
                            cleanup_retained_types.add("harness")
                        else:
                            runtime_destroy_failed = True
                    # Tear down the OAuth2 credential provider registered so the harness
                    # could call its connected gateway (no orphan). destroy_harness
                    # reconstructs the normal deterministic name; this persisted value
                    # covers older records with a different name. Never remove it while
                    # the harness itself is still live.
                    _hr = deployment_record.get("harness_result") or {}
                    _gw_prov = _hr.get("gateway_outbound_provider_name") if isinstance(_hr, dict) else None
                    if _gw_prov and destroy_result.get("success", False):
                        provider_refusal = _legacy_manifest_refusal(
                            "oauth2_credential_provider",
                            _gw_prov,
                            name=_gw_prov,
                        )
                        if provider_refusal:
                            cleanup_messages.append(
                                f"Harness gateway OAuth provider {_gw_prov} "
                                f"left in place (protected): {provider_refusal}"
                            )
                            cleanup_retained_types.add("harness_oauth2_credential_provider")
                        else:
                            try:
                                _deleted_provider_types = delete_owned_credential_provider(
                                    _target_client(
                                        "bedrock-agentcore-control",
                                        region_name=region,
                                    ),
                                    _gw_prov,
                                    region,
                                )
                                if _deleted_provider_types:
                                    cleanup_messages.append(f"Harness gateway OAuth provider {_gw_prov} deleted")
                                else:
                                    cleanup_messages.append(f"Harness gateway OAuth provider {_gw_prov} already gone")
                            except ResourceDeletionRefused as _e:
                                cleanup_messages.append(
                                    f"Harness gateway OAuth provider {_gw_prov} left in place (protected): {_e}"
                                )
                                cleanup_retained_types.add("harness_oauth2_credential_provider")
                            except Exception as _e:  # noqa: BLE001
                                if not resource_is_missing(_e):
                                    cleanup_messages.append(
                                        f"Harness gateway OAuth provider delete error: {type(_e).__name__}"
                                    )
                                    cleanup_failures.append("harness_oauth2_credential_provider")
                except Exception:
                    logger.exception("Harness destroy error for %s", runtime_id)
                    cleanup_messages.append("Harness destroy error (check server logs)")
                    runtime_destroy_failed = True
    else:
        if manifest_deleted_runtime:
            logger.info(
                "Skipping duplicate post-manifest runtime destroy for %s",
                runtime_id,
            )
            destroy_result = {"success": True, "message": "handled by manifest"}
        elif manifest_retained_runtime:
            logger.warning(
                "Suppressing post-manifest runtime destroy for %s because the manifest explicitly retained it",
                runtime_id,
            )
            destroy_result = {
                "success": False,
                "message": "retained by manifest deletion-authority policy",
            }
        elif not deployment_record.get("runtime_id"):
            # The route also accepts a DEPLOYMENT id, so a failed deploy whose runtime
            # was never created can still be cleaned up. That id only located the
            # record; it names no runtime, and handing it to destroy_runtime would ask
            # AWS to delete whatever a caller-supplied string happens to match.
            destroy_result = {
                "success": True,
                "message": "No runtime was recorded for this deployment; nothing to destroy",
            }
        else:
            # Destroy the id this deployment PERSISTED, never the path parameter.
            recorded_runtime_id = str(deployment_record["runtime_id"])
            runtime_refusal = _legacy_manifest_refusal(
                "agent_runtime",
                recorded_runtime_id,
            )
            if runtime_refusal:
                cleanup_messages.append(f"Runtime {recorded_runtime_id} left in place (protected): {runtime_refusal}")
                cleanup_retained_types.add("agent_runtime")
                destroy_result = {
                    "success": False,
                    "retained": True,
                    "message": runtime_refusal,
                }
            else:
                try:
                    _target_account = _target_event.get("target_account_id")
                    if _target_account:
                        # F-81: this is passed to destroy_runtime as ``runtime_name``, where it
                        # SHORT-CIRCUITS ``_resolve_runtime_name_for_cleanup`` -- the resolver
                        # that reads the friendly name off the versions store and would have
                        # been right. Passing an unsanitized node id therefore did not merely
                        # fail, it replaced a working lookup with a broken one, and the
                        # triggers keyed by that name were never deleted. Only a PROVEN name
                        # goes in, because that name drives deletes across Scheduler,
                        # EventBridge, Lambda URLs and Secrets Manager (see the helper).
                        _friendly_runtime_name = _proven_runtime_name_for_destroy(deployment_record)
                        destroy_result = destroy_runtime(
                            recorded_runtime_id,
                            region,
                            client_factory=_target_client,
                            delete_execution_role=False,
                            runtime_name=_friendly_runtime_name,
                        )
                    else:
                        # Preserve the historical home-account call shape for
                        # callers/tests that replace destroy_runtime directly.
                        destroy_result = destroy_runtime(recorded_runtime_id, region)
                    # destroy_runtime returns success:false on AccessDenied / other
                    # errors; propagate that to the top-level response.
                    _destroy_ok = destroy_result.get("success", True)
                    if not _destroy_ok:
                        if destroy_result.get("retained"):
                            cleanup_retained_types.add("agent_runtime")
                        else:
                            runtime_destroy_failed = True
                    # Sidecars (dashboard, evaluation config/log group/role) are deleted by
                    # destroy_runtime best-effort *per item*, never silently: each one it could
                    # not remove is a cleanup failure here, so the verdict cannot be green while
                    # an evaluation config is still sampling a runtime that no longer exists.
                    for _sidecar in destroy_result.get("sidecar_failures") or []:
                        cleanup_failures.append(f"runtime_sidecar:{_sidecar}")
                    cleanup_messages.append(destroy_result.get("message", "Runtime destroy completed"))
                except Exception:
                    logger.exception("Runtime destroy error for %s", recorded_runtime_id)
                    cleanup_messages.append("Runtime destroy error (check server logs)")
                    runtime_destroy_failed = True

    # F-81f — the triggers verdict, and what it costs.
    #
    # ``destroy_runtime`` cleans up the schedules / rules / function URLs / webhook secrets keyed by
    # the friendly runtime NAME, and reports one of: confirmed (every row that targets this runtime
    # is gone), partial (a row survives as a retry handle), unresolved (the name could not be
    # proven, so it never even enumerated), refused, or error. Only "confirmed" proves nothing
    # name-keyed is left.
    #
    # Anything else must keep the name locked. A peer session measured why: with the name released,
    # the slot and version rows go with it, and ``routers/triggers._resolve_owned_runtime``
    # resolves the owner THROUGH the production slot -- so the tenant's own retry of
    # ``DELETE /api/runtimes/{name}/triggers/{id}`` answers 404 and the leaked schedule keeps firing
    # at a deleted ARN with no handle left anywhere. The residue is only "visible and fixable" if
    # the metadata that identifies it survives.
    #
    # An absent "triggers" key means the destroy never ran (nothing recorded, a policy retention, a
    # test double) and there is nothing to confirm -- not an unconfirmed cleanup.
    _trigger_state = destroy_result.get("triggers") if isinstance(destroy_result, dict) else None
    _trigger_outcome = str((_trigger_state or {}).get("outcome") or "")
    _triggers_unconfirmed = bool(_trigger_outcome) and _trigger_outcome != "confirmed"
    if _triggers_unconfirmed:
        # A cleanup FAILURE, not a retention: the deployment must stay retryable and must not be
        # marked "deleted", because "deleted" is never retried and this residue costs money and
        # invokes a dead ARN on a schedule.
        cleanup_failures.append("triggers")
        cleanup_messages.append(
            f"Trigger cleanup not confirmed ({_trigger_outcome}); "
            f"{int((_trigger_state or {}).get('kept') or 0)} trigger row(s) kept as a retry handle"
        )

    # Bug 192 — release the runtime NAME so it can be redeployed. The slots +
    # versions rows (AgentVersionsTable / RuntimeSlotsTable) are the cross-tenant
    # name lock used by the deploy guard (H-1). If teardown leaves them behind, the
    # friendly name stays permanently locked even after the AWS resource is gone,
    # and a later deploy of the same name fails with 409 "already in use by another
    # tenant" — exactly what a customer hit after a prior harness was torn down.
    # Best-effort: never fail the teardown on this. The logic lives in
    # ``_release_runtime_name_claim`` so it is reachable from a test.
    #
    # F-66c/F-81d: exactly ONE definition of "something that claims this name may still be
    # running", shared with the hand-off downgrade below. All three of these modes claim the
    # friendly name through the same two tables -- a harness deploy and an MCP-server deploy write
    # the same version and slot rows an agent-runtime deploy does -- so keying the release off
    # ``agent_runtime`` alone released the name of a harness or MCP server that was still alive.
    # A consumer this teardown retained counts, and so does a cleanup FAILURE on any of the
    # three: a failed delete is the case where the thing is most likely still there. So does a
    # hand-off of this deployment's OWN runtime or harness: another deployment shares it, and it
    # keeps running this deployment's agent.
    #
    # A hand-off of a SECONDARY consumer does not. The shared MCP server runtime behind an
    # mcp-server-gateway-target gateway (an agent_runtime row, adopted by every version) is
    # alive only because another live deployment still references it, and that deployment's
    # own rows protect what it needs. Counting it made every older version's tombstone
    # delete_retained, which protected the shared rows back, so the last version could never
    # reclaim the MCP runtime, gateway, target, provider, resource server and role (measured
    # live 2026-10-01).
    _consumer_types = {"agent_runtime", "harness", "mcp_server_runtime"}
    _own_consumer_ids = {
        str(value)
        for value in (
            runtime_id,
            (deployment_record or {}).get("runtime_id"),
            (deployment_record or {}).get("harness_id"),
        )
        if value
    }
    _own_consumer_handed_off = any(
        kind in _consumer_types and resource_id in _own_consumer_ids for kind, resource_id in manifest_handoff_keys
    )
    _consumer_may_live = (
        runtime_destroy_failed
        or _own_consumer_handed_off
        or bool(
            _consumer_types & (manifest_retained_types | cleanup_retained_types)
            or _consumer_types & set(cleanup_failures)
        )
    )
    # The release applies one more term itself -- whether this deployment recorded any consumer at
    # all -- because that is a property of the record it already holds, and keeping it there is
    # what makes it reachable from a test.
    _release_outcome: dict = {}
    try:
        cleanup_messages.extend(
            _release_runtime_name_claim(
                deployment_record,
                caller_sub,
                runtime_may_still_live=_consumer_may_live,
                trigger_cleanup_unconfirmed=_triggers_unconfirmed,
                outcome=_release_outcome,
            )
        )
    except Exception:  # noqa: BLE001
        # A release that raised is a teardown that did not finish: the name row (and the
        # version rows under it) are still there, so the verdict must say so rather than
        # report success with a note. The row stays as the retry handle.
        logger.warning("Slots/versions release failed for %s", runtime_id, exc_info=True)
        cleanup_messages.append("Runtime name release failed (check server logs)")
        cleanup_failures.append("runtime_name_release")
    else:
        if _release_outcome.get("kept_locked"):
            # Deliberately kept (runtime retained, trigger residue, unproven ownership...) --
            # still a resource this teardown left behind, and the caller must see it as one.
            cleanup_retained_types.add("runtime_name")

    # F-66c: a hand-off is only honest once this deployment has stopped consuming
    # what it handed off. While its runtime, harness or MCP server may still be
    # running, a hand-off is a plain retention: the row stays protective, and the
    # other deployment's teardown cannot delete a gateway this runtime still calls.
    if manifest_handoff_types and _consumer_may_live:
        manifest_retained_types |= manifest_handoff_types
        manifest_retention_reasons.extend("other" for _ in manifest_handoff_types)
        manifest_handoff_types = set()

    # Audit #11 (tasks/lessons.md Bug 106): overall_success is False if either
    # runtime-destroy failed (Bug 44) OR any other cleanup step (gateway, KB,
    # memory, guardrail, policy engine, MCP server) leaked. Failed steps are
    # listed in the response message so the caller can act on the leak.
    overall_success = (
        not runtime_destroy_failed
        and not cleanup_failures
        and not manifest_retained_types
        and not cleanup_retained_types
    )
    # The verdict goes FIRST: the stored delete_message is capped (DELETE_MESSAGE_MAX_CHARS), and a
    # gateway teardown's per-row lines alone overflow it, so a trailing summary was
    # always the part cut off.
    verdict: list[str] = []
    if cleanup_failures:
        verdict.append(f"Cleanup failures in: {', '.join(sorted(set(cleanup_failures)))}")
    if manifest_retained_types:
        verdict.append(f"Resources retained by deletion-authority policy: {', '.join(sorted(manifest_retained_types))}")
    if cleanup_retained_types:
        verdict.append(
            f"Legacy resources retained by live-ownership policy: {', '.join(sorted(cleanup_retained_types))}"
        )
    if manifest_handoff_types:
        verdict.append(
            "Left in place for another deployment that still uses them, and deleted with it: "
            f"{', '.join(sorted(manifest_handoff_types))}"
        )
    cleanup_messages = verdict + cleanup_messages
    summary = "; ".join(cleanup_messages) if cleanup_messages else "Cleanup completed"
    retained = bool(manifest_retained_types or cleanup_retained_types)
    # Pending, not retained: every retention is a delete the service accepted and had not finished
    # inside this invocation's confirmation budget (or a memory role kept for exactly such a memory),
    # and nothing failed or was refused. The async teardown then confirms again in a later
    # invocation instead of reporting a retention the user would have to retry by hand.
    confirmation_pending = (
        "unconfirmed" in manifest_retention_reasons
        and all(reason in ("unconfirmed", "memory-role-deferred") for reason in manifest_retention_reasons)
        and ("memory-role-deferred" not in manifest_retention_reasons or "memory" in manifest_unconfirmed_types)
        and not cleanup_retained_types
        and not cleanup_failures
        and not runtime_destroy_failed
    )
    # F-66f: only now, with the whole graph settled. The claim goes only when every
    # gateway is gone: a gateway retained or failed keeps it, and a gateway handed off
    # keeps only a claim that already existed (the survivor may be another owner).
    _name_hold.settle(
        erase=overall_success and "gateway" not in manifest_handoff_types,
        handed_off=[str(g.get("id")) for g in _handed_off_gateways] if "gateway" in manifest_handoff_types else [],
    )
    # "deleted" is never retried, so it may not strand recovery evidence or a pointer
    # no TTL will remove; a retained or failed teardown is retried anyway.
    unfinished = unfinished_recovery(_cleanup_deployment_id)
    if unfinished and overall_success:
        overall_success = False
        summary = f"{unfinished} {summary}"  # first: the stored message is capped
    return DeleteResponse(
        success=overall_success,
        message=summary,
        retained=retained and not runtime_destroy_failed and not cleanup_failures,
        confirmation_pending=confirmation_pending,
    )


# ---------------------------------------------------------------------------
# POST /api/generate-tool
# ---------------------------------------------------------------------------


@deployment_app.post("/api/generate-tool", dependencies=[Depends(require_scopes("agent:write"))])
async def handle_generate_tool(request: ToolGenerateRequest, raw_request: Request):
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

        # Stamped with the caller and given a TTL for the same reasons as the
        # test row below: this one holds the prompt and the generated tool source.
        table = _get_deploy_table()
        item = {
            "deployment_id": job_id,
            "status": "running",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ttl": _scratch_row_ttl(),
        }
        user_id = _get_user_id(raw_request)
        if user_id:
            item["user_id"] = user_id
        table.put_item(Item=item)

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


@deployment_app.get("/api/generate-tool/{job_id}", dependencies=[Depends(require_scopes("agent:read"))])
async def handle_get_generate_result(job_id: str, raw_request: Request):
    """Poll for async tool generation results. Caller must own the job."""
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

    _assert_may_read_scratch_row(item, raw_request, "Job not found")

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


# The generate/test rows are scratch: a client writes one, polls it for the ~60s
# the model or the sandbox Lambda takes, and never reads it again. They were
# written with no TTL at all, so every tool anyone ever generated -- prompt,
# generated source and test cases, i.e. customer content -- accumulated in the
# deployments table permanently. A day is generous for the poll and bounded.
# ``ttl`` is the attribute the table is configured with (infra/stacks/platform/
# tables.py:99). Live deployment-state rows deliberately omit it; only completed
# delete tombstones receive their own 30-day TTL.
_SCRATCH_ROW_TTL_DAYS = 1


def _scratch_row_ttl() -> int:
    """Unix-epoch expiry for an async generate/test row.

    Written on create AND on every completion update. The update is not redundant:
    a row created before the TTL shipped has no ``ttl`` attribute, so DynamoDB
    would keep it and the prompt and generated source inside it forever. Stamping
    it on completion means any legacy row the platform still touches acquires an
    expiry, which is the whole population that can still be in flight.
    """
    return int((datetime.now(timezone.utc) + timedelta(days=_SCRATCH_ROW_TTL_DAYS)).timestamp())


def _assert_may_read_scratch_row(item: dict, raw_request: Request, not_found_detail: str) -> None:
    """404 unless the caller owns *item*, for the async generate/test rows.

    SECURITY -- tenant isolation. Neither poll route had any: both fetched the row
    by id and returned it, so any authenticated caller holding an id read another
    tenant's generated tool source, prompt and test output. ARCC cnt_Yq9sVcaZyQniIv
    ("Prevent data leakage in generative AI systems") makes this explicit, both in
    its threat ("users may be able to view other users' content and session
    histories") and in its manual verification step: *test that the session state of
    a given user is not accessible to another user and that IDOR is not possible*.

    404 rather than 403, and the same wording as the not-found case, so the route is
    not an existence oracle -- matching handle_deploy_status / handle_test_runtime and
    the requirement in ARCC cnt_94E30Xo4RZHtSJ ("Handle all errors and return generic
    error messages") that an unauthorized caller must not be able to probe for the
    existence of a resource.

    **This fails closed on a row with no owner, unlike handle_deploy_status.** The
    first version of this helper copied that route's pre-tenancy carve-out
    (tasks/lessons.md Bug 37) and justified it as "narrow by construction: these rows
    expire within a day". That justification was false, and a reviewer caught it: the
    TTL is written by the *new* code, so a row created before this shipped has no
    ``ttl`` attribute at all. DynamoDB never expires it, and an absent ``user_id``
    made it readable by every tenant forever -- the exact opposite of narrow.

    Failing closed is cheap here in a way it is not on ``handle_deploy_status``. A
    scratch row lives for the length of one poll (seconds for a test, under two
    minutes for a generation), so the entire population of ownerless rows is
    whatever was in flight during the deploy; those callers get "not found" and
    retry. A deployment *state* row is long-lived and refusing it would lock users
    out of agents they own, which is why the carve-out is correct there and wrong
    here. Same shape, different lifetime, different answer.

    Written as two explicit checks rather than one ``!=``: with no caller identity and
    no owner on the row, ``None != None`` is False and a single comparison would let an
    unauthenticated caller read exactly the ownerless rows this change exists to
    refuse. An absent value must never compare equal to a missing one.
    """
    caller = _get_user_id(raw_request)
    owner = item.get("user_id")
    if not caller or not owner or owner != caller:
        raise HTTPException(status_code=404, detail=not_found_detail)


@deployment_app.post("/api/test-tool", dependencies=[Depends(require_scopes("agent:write"))])
async def handle_test_tool(request: ToolTestRequest, raw_request: Request):
    """Start an async tool test. Returns a testId for polling.

    The actual test runs in an async Lambda invocation to avoid the
    API Gateway 30s timeout. Poll GET /api/test-tool/{testId} for results.
    """
    try:
        test_id = f"test-{uuid.uuid4().hex[:12]}"

        # Store initial "running" state, stamped with the caller so the poll route
        # can refuse another tenant. This row previously recorded no tenant at all,
        # which made the poll route's ownership question unanswerable as well as
        # unasked -- and this is the row holding client-submitted Python.
        table = _get_deploy_table()
        item = {
            "deployment_id": test_id,
            "status": "running",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ttl": _scratch_row_ttl(),
        }
        user_id = _get_user_id(raw_request)
        if user_id:
            item["user_id"] = user_id
        table.put_item(Item=item)

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
        # SECURITY (ARCC cnt_94E30Xo4RZHtSJ, "return generic error messages"): log
        # the detail server-side, return none of it. This returned
        # f"{type(e).__name__}: {e}", which hands the caller the failing internal
        # component by name -- the sibling generate route already did it this way.
        logger.exception("handle_test_tool failed")
        raise HTTPException(
            status_code=500,
            detail={"error": "Tool test failed."},
        ) from e


@deployment_app.get("/api/test-tool/{test_id}", dependencies=[Depends(require_scopes("agent:read"))])
async def handle_get_test_result(test_id: str, raw_request: Request):
    """Poll for async test results. Caller must own the test."""
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

    _assert_may_read_scratch_row(item, raw_request, "Test not found")

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
        # The sandbox posture, and an explanation when the sandbox itself is what
        # failed the test. Without these a user whose tool calls an HTTP API sees a
        # connection timeout from correct code and rewrites it. Both are produced by
        # tool_tester and are about OUR configuration, not the caller's account, so
        # they are safe to publish -- unlike the row's ``error``, which is genericized
        # upstream (ARCC cnt_94E30Xo4RZHtSJ).
        "sandboxIsolated": item.get("sandbox_isolated"),
        "note": item.get("note"),
    }


# ---------------------------------------------------------------------------
# POST /api/generate-canvas — Phase 1 Gap 1E (NL agent generator)
# ---------------------------------------------------------------------------


@deployment_app.post(
    "/api/generate-canvas",
    response_model=AgentGenerateResponse,
    response_model_by_alias=True,
    dependencies=[Depends(require_scopes("agent:write"))],
)
async def handle_generate_canvas(request: AgentGenerateRequest, raw_request: Request) -> AgentGenerateResponse:
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


def _artifacts_s3_client(region: str):
    """An S3 client whose PRESIGNED URLs carry the regional endpoint.

    botocore resolves ``s3`` to the global ``s3.amazonaws.com`` host by default
    no matter what ``region_name`` says. Requests still succeed, because the
    region redirector transparently retries a 301 — but
    ``generate_presigned_url`` performs no request, so the wrong host ends up
    baked into a SigV4 signature that covers ``Host``. The browser then gets a
    307 to the regional host and a 403 SignatureDoesNotMatch on the retry.

    Invisible in us-east-1, where the global host IS the regional one; broken in
    every other region. Pinning the endpoint keeps the same virtual-hosted URL
    shape and is correct in us-east-1 too.
    """
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=f"https://s3.{region}.amazonaws.com",
        config=BotoConfig(s3={"addressing_style": "virtual"}, signature_version="s3v4"),
    )


#: The two download-bundle prefixes. Their objects are temporary platform artifacts
#: behind a one-hour presigned URL, and the bucket expires both prefixes after a day
#: (infra buckets.py). ``deployments/`` holds workload code and is a different class.
EXPORT_BUNDLE_PREFIXES = {"cfn-template": "cfn-templates", "python-export": "python-exports"}


def _export_owner_hash(raw_request: Request) -> str | None:
    """The caller's owner hash for a staged bundle, or ``None`` when nothing is staged.

    Called BEFORE the bundle is generated: a caller with no identity is refused with a
    401 before any generation, compression or S3 write, rather than filed under a
    shared placeholder. With no artifacts bucket the bundle is returned inline, so
    nothing is keyed by owner and no identity is needed.
    """
    if not os.environ.get("ARTIFACTS_BUCKET_NAME", ""):
        return None
    from app.services.resource_ownership import owner_sub_hash

    caller_sub = _get_user_id(raw_request)
    if not caller_sub:
        raise HTTPException(status_code=401, detail="An export needs an authenticated caller")
    return owner_sub_hash(caller_sub)


def _stage_export_bundle(owner_hash: str, artifact_type: str, name: str, body: bytes, download_name: str) -> str:
    """Put a download bundle under the caller's own prefix and return a 1-hour URL.

    The key carries the caller's owner HASH, never the raw sub, so bundles from two
    callers never share a prefix and the tenant identifier is not copied into object
    keys, access logs or inventory reports. The object carries a FIXED tag set
    (product, stack, owner hash, artifact type), never the workload's governance
    tags: S3 caps an object at 10 tags, so copying a workload's set would make any
    workload with more than 10 tags impossible to export, and these bundles are not
    the workload.
    """
    from app.services.resource_ownership import owner_tags

    tags = owner_tags(config.aws_region, extra={"OwnerSubHash": owner_hash, "ArtifactType": artifact_type})
    bucket = os.environ["ARTIFACTS_BUCKET_NAME"]
    # A full 128-bit suffix: PutObject overwrites silently, so a colliding 32-bit
    # suffix would replace another export's bundle under a live presigned URL.
    key = f"{EXPORT_BUNDLE_PREFIXES[artifact_type]}/{owner_hash}/{name}-{uuid.uuid4().hex}.zip"
    s3_client = _artifacts_s3_client(config.aws_region)
    s3_client.put_object(Bucket=bucket, Key=key, Body=body, Tagging=urllib.parse.urlencode(tags))
    # The browser saves a cross-origin download under the OBJECT's name and ignores the anchor's `download` attribute,
    # so the pre-signed URL itself must name the file: the same `<name>-<kind>.zip` the API reports as `filename`.
    return s3_client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ResponseContentDisposition": f'attachment; filename="{download_name}"',
            "ResponseContentType": "application/zip",
        },
        ExpiresIn=3600,
    )


# ---------------------------------------------------------------------------
# POST /api/generate-cfn-template
# ---------------------------------------------------------------------------


#: S3 allows 10 tags per object. Every workload governance tag goes on the exported
#: code.zip, so this is the most a CFN export may resolve. When P0-A adds fixed platform
#: tags to that object, lower this by their count; the staging test pins the value.
CODE_ZIP_TAG_LIMIT = 10


def _resolve_export_tags(request: DeployRequest) -> DeployRequest:
    """Collapse ``tag_profile`` into ``resource_tags`` for an export, or fail with a 400.

    The generator can emit ``resource_tags`` onto every taggable resource, but it has no
    business reading DynamoDB to find out what a named profile contains -- and a profile
    left unresolved is a dropped setting, which is the failure class the export guard
    exists to prevent. So the resolution happens here, against the same
    ``tag_policy_store`` and therefore with the same required-tag enforcement, as
    ``/api/deploy`` at the top of this module.

    Both paths now share ONE resolver (``_resolve_governance_or_refuse``) and therefore one
    posture: fail closed. The export always was fatal on a store failure, because the
    artifact this route returns is a file the customer keeps and deploys later, possibly
    after we are out of the loop, and handing them a stack missing the governance tags they
    asked for -- with a warning that only ever appeared in our own log -- is precisely the
    silent drop this work set out to remove. P0-B made the deploy path match it, so the
    "one deliberate difference" this docstring used to record is gone.

    The early return stays, with one addition: a request carrying a ``policy_revision`` is
    resolved even when it supplies no tags and no profile. Otherwise dropping the tag fields
    while keeping the revision would export an untagged template with a governance state
    attached that nothing ever checked.
    """
    if not (request.resource_tags or request.tag_profile or request.policy_revision):
        return request

    resolved = _resolve_governance_or_refuse(request, artifact="export")

    # The exported stack's AgentCodePackage writes a code.zip that must carry EVERY
    # workload governance tag (item 15), and S3 caps an object at 10 tags. A set that
    # cannot fit is refused HERE, before anything is generated or staged: the
    # alternatives were a template that is knowingly undeployable, or a partially tagged
    # workload artifact, and neither is a governance outcome a caller can see. The
    # download bundle itself carries a fixed 4-tag set and is unaffected (P0-C).
    if len(resolved) > CODE_ZIP_TAG_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{len(resolved)} resource tags resolved for this export, but the exported "
                f"agent code object can carry at most {CODE_ZIP_TAG_LIMIT} (the S3 per-object "
                "limit). Remove tags or choose a smaller tag profile and export again; the "
                "export was stopped before anything was staged."
            ),
        )

    # model_copy rather than a re-validating round trip: both values written here are a
    # plain dict and None, so no validator is being skipped that has anything to check.
    # tag_profile is cleared because it has now been *consumed* -- leaving it set would
    # trip the generator's refusal guard, which is correct in general and wrong here.
    return request.model_copy(
        update={
            "resource_tags": resolved or None,
            "tag_profile": None,
            # Consumed together with the profile: both tokens have now been verified against
            # the store, and a leftover timestamp with no profile name is a state the resolver
            # would (correctly) refuse if this request were ever resolved a second time.
            "policy_revision": None,
            "tag_profile_updated_at": None,
        }
    )


@deployment_app.post("/api/generate-cfn-template", dependencies=[Depends(require_scopes("agent:write"))])
async def handle_generate_cfn_template(request: DeployRequest, raw_request: Request):
    """Generate a downloadable CloudFormation template bundle.

    Returns a presigned S3 URL to download the zip, or the zip bytes
    directly if no S3 bucket is configured.
    """
    # Imported outside the try on purpose: the `except CfnExportUnsupportedError`
    # below has to resolve that name, so a failed import here would turn into a
    # NameError raised from the handler rather than the ImportError that happened.
    from app.services.cfn_template_generator import (
        CfnExportUnsupportedError,
        CfnTemplateGenerator,
    )

    # Identity first: a caller with no sub must not reach the tag-policy store either.
    owner_hash = _export_owner_hash(raw_request)
    request = _resolve_export_tags(request)

    try:
        generator = CfnTemplateGenerator()
        bundle = generator.generate(request)
        zip_bytes = bundle.to_zip()

        # Try to upload to S3 and return presigned URL
        if owner_hash:
            url = _stage_export_bundle(
                owner_hash, "cfn-template", bundle.deployment_name, zip_bytes, f"{bundle.deployment_name}-cfn.zip"
            )
            return {"download_url": url, "filename": f"{bundle.deployment_name}-cfn.zip"}

        # Fallback: return base64-encoded zip
        import base64

        return {
            "zip_base64": base64.b64encode(zip_bytes).decode(),
            "filename": f"{bundle.deployment_name}-cfn.zip",
        }

    except CfnExportUnsupportedError as exc:
        # Not a server fault and not a secret: the generator refused a canvas it
        # cannot represent, and its message names the workaround. Pass it through
        # verbatim — collapsing it into the generic 500 below is what made the
        # LiteLLM gap invisible in the first place.
        logger.warning("CFN export refused: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    except HTTPException:
        raise

    except Exception as exc:
        logger.exception("CFN template generation failed")
        raise HTTPException(status_code=500, detail="Internal server error") from exc


# ---------------------------------------------------------------------------
# POST /api/export-python  (Phase 3 Gap 3G — eject standalone Python project)
# ---------------------------------------------------------------------------


def _reject_tags_on_python_export(request: DeployRequest) -> None:
    """Refuse governance tags on the Python export instead of dropping them.

    A standalone Python project creates no AWS resources, so there is nothing to put
    ``resourceTags``/``tagProfile`` on. Accepting them and returning a 200 would tell
    the caller their tags were applied when they were thrown away.
    """
    for wire, value in (("resourceTags", request.resource_tags), ("tagProfile", request.tag_profile)):
        if value:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{wire} cannot be applied to a standalone Python export, which creates no "
                    f"AWS resources. Remove {wire}, or download the CloudFormation bundle or "
                    "deploy to the platform to have the resources tagged."
                ),
            )


@deployment_app.post("/api/export-python", dependencies=[Depends(require_scopes("agent:write"))])
async def handle_export_python(request: DeployRequest, raw_request: Request):
    """Export a downloadable, standalone Python agent project.

    Mirrors handle_generate_cfn_template: build the project bundle, zip it,
    upload to the artifacts bucket and return a 3600s presigned URL (base64
    fallback when no bucket). Both exports key the object by the caller's owner hash.
    """
    _reject_cfn_only_naming_profile(request, "a standalone Python export")
    _reject_tags_on_python_export(request)
    owner_hash = _export_owner_hash(raw_request)

    try:
        from app.services.python_exporter import build_and_zip

        zip_bytes, deployment_name = build_and_zip(request)

        if owner_hash:
            url = _stage_export_bundle(
                owner_hash, "python-export", deployment_name, zip_bytes, f"{deployment_name}-python.zip"
            )
            return {"download_url": url, "filename": f"{deployment_name}-python.zip"}

        import base64

        return {
            "zip_base64": base64.b64encode(zip_bytes).decode(),
            "filename": f"{deployment_name}-python.zip",
        }

    except HTTPException:
        raise

    except Exception as exc:
        logger.exception("Python export failed")
        raise HTTPException(status_code=500, detail="Internal server error") from exc


# ---------------------------------------------------------------------------
# Mangum handler (Lambda entry point)
# ---------------------------------------------------------------------------

_mangum_handler = Mangum(deployment_app, lifespan="off")


def handler(event, context):
    """Lambda entry point. Intercepts async events before Mangum."""
    if isinstance(event, dict):
        records = event.get("Records")
        if (
            isinstance(records, list)
            and records
            and all(isinstance(record, dict) and record.get("eventSource") == "aws:sqs" for record in records)
        ):
            from app.services.trigger_runtime import dispatch_sqs_batch

            return dispatch_sqs_batch(event)
        if event.get("_async_test"):
            return _handle_async_test(event)
        if event.get("_async_harness_warmup"):
            return _handle_async_harness_warmup(event)
        if event.get("_async_generate"):
            return _handle_async_generate(event)
        # Slow-class (KB-backed) runtime teardown — dispatched by
        # handle_delete_runtime so the API response stays under API Gateway's
        # 29s cap while the KB cascade + backing-store deletes finish here.
        if event.get("_async_delete"):
            return _handle_async_delete(event, context)
        # IAM-authorized direct invoke used by scripts/cleanup.sh. It delegates
        # stack teardown to the same guarded manifest cleanup as the API.
        if event.get("_stack_cleanup_delete"):
            return _handle_stack_cleanup_delete(event)
        # EventBridge-scheduled Cedar-ENFORCE promotion sweep (Loom-study 0.6):
        # self-drives pending permits to ACTIVE without a user touchpoint.
        if event.get("policy_sweep") or (
            event.get("source") == "aws.events" and event.get("detail-type") == "policy-sweep"
        ):
            from app.step_handlers.policy_sweep_step import handler as _sweep

            return _sweep(event, context)
        # EventBridge-scheduled FinOps cost reconciliation (Loom-study 5.3):
        # self-drives budget-breach detection for idle-but-overspending agents
        # that no human has opened the cost panel for.
        if event.get("cost_reconcile") or (
            event.get("source") == "aws.events" and event.get("detail-type") == "cost-reconcile"
        ):
            from app.step_handlers.cost_reconcile_step import handler as _reconcile

            return _reconcile(event, context)

    # Normal API Gateway request → Mangum/FastAPI
    return _mangum_handler(event, context)


#: The deploy-time warm-up of a harness, on a session of its own (>= 33 characters, the runtime-session minimum).
HARNESS_WARMUP_SESSION_PREFIX = "platform-warmup-"


def _start_harness_warmup(deployment_state: dict, region: str, harness_arn: str) -> None:
    """Hand a harness's deploy-time ping to this function asynchronously (InvocationType=Event, as
    the tool-test route does), so the HTTP API answers at once. The payload carries only the owner-
    checked harness ARN and the deployment's frozen target context, never a prompt or a credential."""
    boto3.client("lambda", region_name=config.aws_region).invoke(
        FunctionName=os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""),
        InvocationType="Event",
        Payload=json.dumps(
            {
                "_async_harness_warmup": True,
                "harness_arn": harness_arn,
                "target_event": _deployment_target_event(deployment_state, region),
            }
        ).encode(),
    )


def _handle_async_harness_warmup(event: dict) -> dict:
    """Warm a harness off the API's 30 s path: one turn in the deployment's target account and
    region, on a session no conversation uses."""
    from app.services import step_clients

    target_event = event.get("target_event") or {}
    region = target_event.get("target_region") or config.aws_region
    result = invoke_harness(
        region,
        event["harness_arn"],
        "ping",
        f"{HARNESS_WARMUP_SESSION_PREFIX}{uuid.uuid4()}",
        agentcore_data_client=step_clients.session_for_event(target_event).client(
            "bedrock-agentcore", region_name=region
        ),
    )
    warmed = bool(result.get("success"))
    logger.info("Harness warm-up %s", "completed" if warmed else "failed")
    return {"warmed": warmed}


def _handle_async_test(event: dict):
    """Run tool test and store results in DynamoDB."""
    test_id = event["test_id"]
    table = _get_deploy_table()

    try:
        result = test_tool(
            lambda_code=event["lambda_code"],
            test_cases=event["test_cases"],
            # The publisher always sets "region"; fall back to THIS deployment's
            # region rather than a literal, so a missing key cannot silently run
            # the Bedrock/Lambda call in the wrong region.
            region=event.get("region") or config.aws_region,
        )

        table.update_item(
            Key={"deployment_id": test_id},
            UpdateExpression=(
                "SET #s = :s, success = :ok, all_passed = :ap, "
                "results_json = :rj, #e = :e, #ttl = :ttl, "
                "sandbox_isolated = :iso, #note = :note"
            ),
            # ``note`` goes through an expression attribute name rather than being
            # written literally. The DynamoDB reserved-word list is long and the
            # failure mode is a ValidationException on the completion update only --
            # the test would run, and its result would never be recorded.
            ExpressionAttributeNames={"#s": "status", "#e": "error", "#ttl": "ttl", "#note": "note"},
            ExpressionAttributeValues={
                ":s": "completed",
                ":ok": result.get("success", False),
                ":ap": result.get("allPassed", False),
                ":rj": json.dumps(result.get("results", [])),
                ":e": result.get("error"),
                ":ttl": _scratch_row_ttl(),
                # Persisted, not just returned: the route that shows this to the user
                # is a later poll against the row, so a value that only existed in
                # test_tool's return is a value the user never sees.
                ":iso": result.get("sandboxIsolated"),
                ":note": result.get("note"),
            },
        )
    except Exception as exc:
        logger.exception("Async tool test failed: %s", exc)
        # Generic, per ARCC cnt_94E30Xo4RZHtSJ. This stored str(exc) and the poll
        # route returns the row's ``error`` verbatim, so an unexpected internal
        # failure -- a boto3 ClientError naming a role or an account id, say --
        # was published to the caller. A refusal of the submitted code is NOT this
        # path: test_tool returns that as a result with its own actionable
        # "Code safety validation failed: ..." message, which still reaches the UI.
        table.update_item(
            Key={"deployment_id": test_id},
            UpdateExpression="SET #s = :s, #e = :e, #ttl = :ttl",
            ExpressionAttributeNames={"#s": "status", "#e": "error", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":s": "completed",
                ":e": "Tool test failed unexpectedly. Check the platform logs for details.",
                ":ttl": _scratch_row_ttl(),
            },
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
            # The publisher always sets "region"; fall back to THIS deployment's
            # region rather than a literal, so a missing key cannot silently run
            # the Bedrock/Lambda call in the wrong region.
            region=event.get("region") or config.aws_region,
        )

        tool_data = result.get("tool")
        test_cases = result.get("testCases", [])

        table.update_item(
            Key={"deployment_id": job_id},
            UpdateExpression=(
                "SET #s = :s, success = :ok, message = :msg, #e = :e, "
                "response_type = :rt, tool_json = :tj, test_cases_json = :tcj, #ttl = :ttl"
            ),
            ExpressionAttributeNames={"#s": "status", "#e": "error", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":s": "completed",
                ":ok": result.get("success", False),
                ":msg": result.get("message", ""),
                ":e": result.get("error"),
                ":rt": result.get("responseType", "generation"),
                ":tj": json.dumps(tool_data) if tool_data else None,
                ":tcj": json.dumps(test_cases) if test_cases else "[]",
                ":ttl": _scratch_row_ttl(),
            },
        )
    except Exception as exc:
        logger.exception("Async tool generation failed: %s", exc)
        # Generic for the same reason as the test path above (ARCC cnt_94E30Xo4RZHtSJ):
        # the poll route publishes this field to the caller verbatim.
        table.update_item(
            Key={"deployment_id": job_id},
            UpdateExpression="SET #s = :s, success = :ok, #e = :e, #ttl = :ttl",
            ExpressionAttributeNames={"#s": "status", "#e": "error", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":s": "completed",
                ":ok": False,
                ":e": "Tool generation failed unexpectedly. Check the platform logs for details.",
                ":ttl": _scratch_row_ttl(),
            },
        )
