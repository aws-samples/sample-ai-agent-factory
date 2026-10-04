"""AgentCore Harness deployment operations (parallel authoring path).

The Harness is AWS's managed, config-driven agent harness (GA, powered by
Strands): you DECLARE model + instructions + tools + memory and AgentCore runs
the orchestration loop — no code artifact, container, or dependency bundle. This
is the second authoring path alongside the code-generated AgentCore Runtime.

Control plane: ``bedrock-agentcore-control`` (create/get/update/delete_harness).
Data plane:    ``bedrock-agentcore``         (invoke_harness — streaming).

Uses pure boto3 APIs. Mirrors the conventions in ``runtime_deployer.py``
(transient-retry on create, name->id resolution, idempotent delete).
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid

import boto3

from app.services.aws_errors import is_error
from app.services.aws_pagination import list_all
from app.services.deletion_confirmation import (
    DeletionFailedAfterAccept,
    wait_until_absent,
)
from app.services.iam_boundary import create_role_kwargs, ensure_role_boundary
from app.services.naming import regional_iam_role_name
from app.services.resource_ownership import (
    ResourceDeletionRefused,
    assert_agentcore_resource_owned,
    assert_this_deployment_may_mutate,
)
from app.services.resource_tagging import governed_tag_list, governed_tags

logger = logging.getLogger(__name__)

# A Harness runtime session id must be >= 33 chars (AgentCore requirement).
_MIN_SESSION_ID_LEN = 33


def _create_agentcore_control_client(region: str):
    return boto3.client("bedrock-agentcore-control", region_name=region)


def _create_agentcore_client(region: str):
    # InvokeHarness streams a full agent turn (model + tool round-trips), which can
    # exceed the 60s default read timeout on a cold first call that hits a tool.
    # Give the data-plane client a generous read timeout so a slow-but-valid
    # tool-grounded turn isn't cut off mid-stream (verified live: a connector
    # tool_use turn took >40s).
    from botocore.config import Config as _BotoConfig

    return boto3.client(
        "bedrock-agentcore",
        region_name=region,
        config=_BotoConfig(read_timeout=180, connect_timeout=15, retries={"max_attempts": 2}),
    )


def sanitize_harness_name(name: str) -> str:
    """Sanitize a friendly name for the Harness name constraints.

    AgentCore enforces ``[a-zA-Z][a-zA-Z0-9_]{0,39}`` (verified live): must start
    with a letter, only letters/digits/UNDERSCORE (NO hyphens), max 40 chars.

    Thin wrapper over the shared ``naming.sanitize_agentcore_name`` (underscore
    style, capped at 40 for the harness). Kept as a named function because
    harness_step / tests import it.
    """
    from app.services.naming import sanitize_agentcore_name

    return sanitize_agentcore_name(name, style="underscore", max_len=40, prefix="h", fallback="agentcore_harness")


def pad_session_id(session_id: str) -> str:
    """Ensure a runtime session id satisfies the >= 33 char requirement."""
    if len(session_id) >= _MIN_SESSION_ID_LEN:
        return session_id
    return (session_id + "-" + "0" * _MIN_SESSION_ID_LEN)[:_MIN_SESSION_ID_LEN]


# ---------------------------------------------------------------------------
# IAM execution role
# ---------------------------------------------------------------------------


def _model_arn_pattern(model_id: str) -> str | None:
    """Best-effort Bedrock foundation-model ARN pattern from a model id.

    Scopes InvokeModel to the model FAMILY rather than ``*``. Bedrock model ids
    look like ``us.anthropic.claude-sonnet-5`` (an inference
    profile) — we scope to the provider+family prefix across regions/accounts so
    cross-region inference profiles still resolve, while excluding unrelated
    providers. Returns None when we can't parse one (caller falls back to ``*``).
    """
    if not model_id:
        return None
    base = model_id.split("/")[-1]
    parts = base.split(".")
    # strip a leading cross-region inference-profile prefix (us./eu./apac./global.)
    _XREGION = {"us", "eu", "apac", "global", "apse", "use", "usw"}
    if len(parts) >= 3 and parts[0].lower() in _XREGION:
        parts = parts[1:]
    if len(parts) < 2:
        return None
    provider = parts[0]
    # Scope to the provider + first model-family token (e.g. anthropic.claude),
    # broad enough to cover dated variants/inference profiles, narrow enough to
    # exclude unrelated providers. Strip any trailing ":N" version suffix.
    family = parts[1].split(":")[0]
    return f"arn:aws:bedrock:*::foundation-model/{provider}.{family}*"


def _model_resource_arns(model_id: str) -> list | None:
    """Bedrock resources the exec role must allow for *model_id*.

    For a cross-region inference profile (id begins ``us.``/``eu.``/``apac.``/
    ``global.``) Bedrock's ConverseStream/InvokeModelWithResponseStream is
    evaluated against the INFERENCE-PROFILE ARN as well as the underlying
    foundation-model ARNs. Granting only the foundation-model pattern (Bug 146)
    yields `AccessDeniedException ... not authorized to perform
    bedrock:InvokeModelWithResponseStream on resource: arn:...:inference-profile/
    us.anthropic...`. So when the id is an inference profile we return BOTH the
    foundation-model family pattern and a matching inference-profile ARN pattern.
    Returns None when we can't parse one (caller falls back to ``*``).
    """
    fm = _model_arn_pattern(model_id)
    if not fm:
        return None
    resources = [fm]
    base = model_id.split("/")[-1]
    first = base.split(".")[0].lower()
    _XREGION = {"us", "eu", "apac", "global", "apse", "use", "usw"}
    if first in _XREGION:
        # The inference-profile id is the full model id (e.g.
        # us.anthropic.claude-sonnet-5). Scope across regions/accounts.
        resources.append(f"arn:aws:bedrock:*:*:inference-profile/{base}")
        # System-defined inference profiles are also referenced without an
        # account; include that form too for safety.
        resources.append(f"arn:aws:bedrock:*::inference-profile/{base}")
    return resources


def create_harness_iam_role(
    iam_client,
    role_name: str,
    *,
    harness_name: str,
    model_id: str | None = None,
    memory_arn: str | None = None,
    gateway_arn: str | None = None,
    region: str | None = None,
    return_provenance: bool = False,
    resource_tags: dict | None = None,
) -> str | tuple[str, bool]:
    """Create or reuse an execution role the Harness can assume.

    The Harness needs to invoke Bedrock models, read/write AgentCore Memory,
    invoke connected Gateways, and emit observability — but, unlike a Runtime, it
    has NO S3 code artifact to read. Returns the role ARN.

    Least-privilege (Holmes IAM findings): when *model_id* / *memory_arn* /
    *gateway_arn* are supplied the corresponding statements are scoped to those
    ARNs (model family for InvokeModel; the specific memory/gateway ARNs for the
    agentcore actions). When NO memory/gateway is connected the scoped statement
    is omitted entirely (no ``Resource: "*"`` fallback) — the harness-owned
    default memory and account-level discovery verbs have their own dedicated,
    tightly-scoped statements, so a bare harness still works.

    *harness_name* is REQUIRED, and required rather than optional on purpose. It
    is the only way to scope the auto-provisioned default memory (see the
    ``AgentCoreHarnessOwnedMemory`` statement below), and neither fallback for a
    missing value is acceptable: ``memory/*`` would let any harness read every
    other agent's conversation memory in the account (ARCC ``cnt_L4ZLZgjrCctfxl``,
    least privilege), and omitting the statement would silently reproduce the
    outage that statement exists to prevent. A missing argument is therefore a
    TypeError at the call site instead of a quiet grant or a quiet denial.
    """
    import json

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
    # `governed_tag_list`, not a bare ManagedBy: that names the PRODUCT, so a role
    # carrying only it is indistinguishable from another deployment's role in the
    # same account, and the already-exists branch below has to be able to tell. The
    # governance half is additive to that pair, never a substitute for it -- the
    # already-exists branch still matches on ManagedBy + AgentCoreStack, so a tag
    # policy cannot change what this role is allowed to reuse (ARCC cnt_6gBImtb08AJqCB
    # is why the tags are here; ownership is deliberately not one of them).
    managed_tag = governed_tag_list(region, resource_tags)
    try:
        resp = iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description=f"Execution role for AgentCore Harness {role_name}",
            Tags=managed_tag,
            **create_role_kwargs(),
        )
        role_arn = resp["Role"]["Arn"]
        role_created = True
        logger.info("Created harness IAM role: %s", role_arn)
    except iam_client.exceptions.EntityAlreadyExistsException:
        # Prove it is ours before tagging it and overwriting its inline policy below.
        _existing = iam_client.get_role(RoleName=role_name)["Role"]
        assert_this_deployment_may_mutate(
            f"IAM role {role_name}",
            _existing.get("Tags"),
            region,
        )
        role_arn = _existing["Arn"]
        role_created = False
        logger.info("Reusing existing harness IAM role: %s", role_arn)
        # F-06: retrofit the permissions boundary once ownership is proven.
        ensure_role_boundary(iam_client, role_name, role=_existing)
        try:
            iam_client.tag_role(RoleName=role_name, Tags=managed_tag)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not tag reused harness role %s: %s", role_name, e)

    # Scope InvokeModel to the model FAMILY when we know the model id; else "*".
    # For cross-region inference profiles this also includes the inference-profile
    # ARN (Bug 146) so ConverseStream/InvokeModelWithResponseStream is authorized.
    model_resource = _model_resource_arns(model_id) or "*"

    # Resource-scopable agentcore actions: scope to the connected memory + gateway
    # ARNs when supplied (least privilege). ListGateways has no resource form, so
    # it stays in the account-level statement below.
    scoped_agentcore = [
        "bedrock-agentcore:GetMemory",
        "bedrock-agentcore:CreateEvent",
        "bedrock-agentcore:ListEvents",
        "bedrock-agentcore:RetrieveMemoryRecords",
        "bedrock-agentcore:InvokeAgentRuntime",
        "bedrock-agentcore:InvokeGateway",
        "bedrock-agentcore:GetGateway",
        "bedrock-agentcore:ListGatewayTargets",
    ]
    scoped_resources = [a for a in (memory_arn, gateway_arn) if a]
    # Gateway ARNs also need their sub-resource (targets/tools) — add a wildcard
    # suffix so InvokeGateway/ListGatewayTargets resolve on the gateway's children.
    if gateway_arn:
        scoped_resources.append(f"{gateway_arn}/*")
    if memory_arn:
        scoped_resources.append(f"{memory_arn}/*")

    statements: list[dict] = [
        {
            "Sid": "BedrockModelAccess",
            "Effect": "Allow",
            "Action": [
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
            ],
            "Resource": model_resource,
        },
    ]
    # Memory/gateway data-plane actions, scoped to the connected resource
    # ARNs. Both callers (harness_step + services/deployment) thread the
    # gateway/memory ARNs they wire, so when NONE are supplied there is no
    # connected resource to reach and the statement is OMITTED entirely
    # (previously it fell back to Resource "*" — Holmes IAM HIGH finding).
    # Account-level discovery (ListGateways) + the harness's auto-provisioned
    # default memory are covered by the dedicated statements below.
    if scoped_resources:
        statements.append(
            {
                # InvokeGateway is REQUIRED for the harness to call a connected
                # gateway's tools at runtime (mirrors the runtime exec role's
                # GatewayAccess Sid). GetGateway/ListGatewayTargets cover
                # discovery + target enumeration on the connected gateway.
                "Sid": "AgentCoreMemoryAndGateway",
                "Effect": "Allow",
                "Action": scoped_agentcore,
                "Resource": scoped_resources,
            }
        )
    # Sanitized here rather than trusting the caller: this string is interpolated
    # into an IAM Resource pattern, and ``sanitize_harness_name`` restricts it to
    # ``[a-zA-Z][a-zA-Z0-9_]{0,39}`` — no ``*`` or ``?``, so a hostile agent name
    # cannot widen the pattern. It is also idempotent, so the callers that already
    # sanitized (harness_step.py:90) get the identical name CreateHarness receives,
    # which is what makes the prefix match at all.
    _harness_memory_prefix = sanitize_harness_name(harness_name)
    statements += [
        {
            # CreateHarness ALWAYS auto-provisions a DEFAULT AgentCore Memory for
            # the harness session, whose ARN is NOT known when this role policy is
            # built (it is minted later by the harness service). Without memory
            # data-plane perms on it, the first InvokeHarness fails on ListEvents.
            #
            # The memory is named ``<harnessName>-<10 alnum>`` -- the harness's own
            # name with a random tail, the same shape as the harnessId. It is NOT
            # under any ``harness_`` prefix. This statement used to say
            # ``memory/harness_*``, which matches only a harness a user happened to
            # name ``harness_*``; the harness name is the user's agent name run
            # through ``sanitize_harness_name``, so for every normally-named agent
            # this granted nothing and harness authoring mode was dead on first
            # invoke. The old "verified live" note was true of a probe whose name
            # began with ``harness_`` -- a self-fulfilling sample. Measured live
            # 2026-09-24 on acfe2e-p0920, deployment aa3f6767, via the product's own
            # POST /api/test-runtime:
            #   User: .../assumed-role/AgentCoreHarness-p0bharn1790232124_dda45e47/
            #   BedrockAgentCore-0f0aa378-... is not authorized to perform:
            #   bedrock-agentcore:ListEvents on resource:
            #   arn:aws:bedrock-agentcore:us-east-1:...:memory/
            #   p0bharn1790232124_dda45e47-sfd0kpCXwL
            # The route returns a generic "Harness invocation failed" (CodeQL
            # py/stack-trace-exposure), so this is recoverable from the deployment
            # Lambda's log group and nowhere else -- which is how it shipped.
            #
            # ``<harnessName>-*`` is safe at the MAXIMUM name length, which is the
            # one case that could have broken it: the name caps at 40 and the memory
            # id would be 51, so truncation of the name portion would make this
            # pattern miss. Measured with a deliberately 40-char name
            # (deployment e641c575): harnessName 40 chars, memory id 51 chars,
            # ``p0bharnlongnamemeasuringtruncation40chr_-Sit25JGw75`` -- the name is
            # carried whole and only the tail is appended. No truncation.
            "Sid": "AgentCoreHarnessOwnedMemory",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:GetMemory",
                "bedrock-agentcore:CreateEvent",
                "bedrock-agentcore:ListEvents",
                "bedrock-agentcore:GetEvent",
                "bedrock-agentcore:ListSessions",
                "bedrock-agentcore:RetrieveMemoryRecords",
                "bedrock-agentcore:ListMemoryRecords",
            ],
            "Resource": [
                f"arn:aws:bedrock-agentcore:*:*:memory/{_harness_memory_prefix}-*",
                f"arn:aws:bedrock-agentcore:*:*:memory/{_harness_memory_prefix}-*/*",
            ],
        },
        {
            # Account-level agentcore actions with no resource-ARN form:
            # ListGateways (collection discovery) + the token-vault token/key
            # fetches GetResourceOauth2Token/GetResourceApiKey (needed to load
            # the outbound OAuth token for a CUSTOM_JWT gateway — verified
            # live). Resource "*" is required: these verbs are not resource-
            # scopable (token-vault/default is account-singleton).
            "Sid": "AgentCoreAccountLevel",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:ListGateways",
                "bedrock-agentcore:GetResourceOauth2Token",
                "bedrock-agentcore:GetResourceApiKey",
            ],
            "Resource": "*",
        },
        {
            # GetResourceOauth2Token internally reads the AgentCore-managed
            # token-vault secret (name prefix bedrock-agentcore-identity!) that
            # holds the outbound OAuth token. Without this the token fetch fails
            # with "Access denied when retrieving secret ...!default/oauth2/..."
            # (verified live, the layer beneath GetResourceOauth2Token).
            "Sid": "AgentCoreIdentityVaultSecrets",
            "Effect": "Allow",
            "Action": ["secretsmanager:GetSecretValue"],
            "Resource": "arn:aws:secretsmanager:*:*:secret:bedrock-agentcore-identity!*",
        },
        {
            # Scoped to the AgentCore-managed log-group namespace (harness
            # runtimes emit under /aws/bedrock-agentcore/...) instead of "*".
            "Sid": "CloudWatchLogs",
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
            ],
            "Resource": [
                "arn:aws:logs:*:*:log-group:/aws/bedrock-agentcore/*",
                "arn:aws:logs:*:*:log-group:/aws/bedrock-agentcore/*:log-stream:*",
            ],
        },
    ]
    policy = {"Version": "2012-10-17", "Statement": statements}
    iam_client.put_role_policy(
        RoleName=role_name,
        PolicyName="HarnessExecutionPolicy",
        PolicyDocument=json.dumps(policy),
    )
    if return_provenance:
        return role_arn, role_created
    return role_arn


# ---------------------------------------------------------------------------
# Harness lifecycle
# ---------------------------------------------------------------------------


def build_harness_tools(
    gateway_arn: str | None = None,
    *,
    gateway_outbound_provider_arn: str | None = None,
    gateway_scopes: list | None = None,
) -> list:
    """Build the Harness ``tools`` list from connected components.

    Wires a connected AgentCore Gateway (which fronts the agent's
    tools/connectors). A gateway created by this platform uses CUSTOM_JWT
    (Cognito) auth, so the harness MUST present an outbound OAuth credential to
    call it — otherwise the harness gets ``401 Unauthorized`` loading the tool
    (verified live). When *gateway_outbound_provider_arn* is supplied, attach
    ``outboundAuth.oauth`` (client-credentials); otherwise fall back to no
    outbound auth (only valid for an unauthenticated gateway).
    """
    tools: list = []
    if gateway_arn:
        gw_cfg: dict = {"gatewayArn": gateway_arn}
        if gateway_outbound_provider_arn:
            gw_cfg["outboundAuth"] = {
                "oauth": {
                    "providerArn": gateway_outbound_provider_arn,
                    "scopes": gateway_scopes or [],
                    "grantType": "CLIENT_CREDENTIALS",
                }
            }
        tools.append(
            {
                "type": "agentcore_gateway",
                "name": "gateway_tools",
                "config": {"agentCoreGateway": gw_cfg},
            }
        )
    return tools


def ensure_gateway_outbound_provider(
    agentcore_ctrl,
    harness_name: str,
    gateway_client_info: dict,
    *,
    secrets_client=None,
    cognito_client=None,
    return_provenance: bool = False,
    region: str | None = None,
    resource_tags: dict | None = None,
) -> tuple[str | None, list] | tuple[str | None, list, bool]:
    """Register an OAuth2 credential provider so a harness can call a CUSTOM_JWT
    gateway, derived from that gateway's Cognito client_info.

    Returns ``(provider_arn, scopes)`` by default.  With
    ``return_provenance=True`` it returns
    ``(provider_arn, scopes, created_by_deployment)`` so the manifest does not
    turn a conflict-recovered provider into deletion authority. Returns an empty
    ARN/scopes (and ``False`` provenance) if client_info lacks the fields needed.
    Mirrors the internal-MCP CustomOauth2 registration in gateway_deployer.
    """
    import re

    from app.services.gateway_deployer import _pool_region, resolve_client_secret

    def _result(
        provider_arn: str | None,
        scopes: list,
        created_by_deployment: bool,
    ):
        if return_provenance:
            return provider_arn, scopes, created_by_deployment
        return provider_arn, scopes

    discovery_url = gateway_client_info.get("discovery_url")
    client_id = gateway_client_info.get("client_id")
    # `client_info` no longer carries the secret itself — it is re-read from
    # Cognito (or dereferenced from Secrets Manager) at the moment of use, because
    # client_info travels through the Step Functions execution history, the
    # DynamoDB deployment item and GET /api/deploy/{id}. See resolve_client_secret.
    client_secret = resolve_client_secret(
        gateway_client_info,
        secrets_client=secrets_client,
        cognito_client=cognito_client,
    )
    scope = gateway_client_info.get("scope", "")
    user_pool_id = gateway_client_info.get("user_pool_id")
    region = region or gateway_client_info.get("region")

    # Cognito gateways created by deploy_gateway expose user_pool_id but derive the
    # discovery URL from it; reconstruct if absent.
    if not discovery_url and user_pool_id:
        # region is embedded in the pool id prefix (e.g. us-west-2_abc); fall back
        # to the provided region or parse from the token endpoint.
        reg = gateway_client_info.get("user_pool_region") or _pool_region(user_pool_id)
        if not reg:
            te = gateway_client_info.get("token_endpoint", "")
            m = re.search(r"\.auth\.([a-z0-9-]+)\.amazoncognito", te)
            reg = m.group(1) if m else "us-east-1"
        discovery_url = f"https://cognito-idp.{reg}.amazonaws.com/{user_pool_id}/.well-known/openid-configuration"

    if not (discovery_url and client_id and client_secret):
        logger.warning(
            "Gateway client_info lacks discovery_url/client_id/client_secret; "
            "harness gateway tool will have NO outbound auth (401 likely if gateway is CUSTOM_JWT)"
        )
        return _result(None, [], False)

    provider_name = (re.sub(r"[^a-zA-Z0-9_-]", "-", f"harness-gw-{harness_name}")[:60]) or "harness-gw-cred"
    provider_created = False
    try:
        resp = agentcore_ctrl.create_oauth2_credential_provider(
            name=provider_name,
            credentialProviderVendor="CustomOauth2",
            oauth2ProviderConfigInput={
                "customOauth2ProviderConfig": {
                    "oauthDiscovery": {"discoveryUrl": discovery_url},
                    "clientId": client_id,
                    "clientSecret": client_secret,
                }
            },
            tags=governed_tags(region, resource_tags),
        )
        provider_arn = resp["credentialProviderArn"]
        provider_created = True
        logger.info("Created harness gateway outbound OAuth provider %s", provider_name)
    except Exception as e:  # noqa: BLE001
        # "already exists" fallback kept: conflicts can surface as a
        # ValidationException whose message says "already exists".
        if is_error(e, "ConflictException") or "already exists" in str(e):
            try:
                got = agentcore_ctrl.get_oauth2_credential_provider(name=provider_name)
                assert_agentcore_resource_owned(
                    agentcore_ctrl,
                    "oauth2_credential_provider",
                    provider_name,
                    region,
                )
                provider_arn = got.get("credentialProviderArn", "")
            except Exception as lookup_exc:  # noqa: BLE001
                raise RuntimeError(
                    "A harness outbound OAuth provider with this name already "
                    "exists, but its ownership and ARN could not be verified. "
                    "Refusing to create a harness that would silently omit or "
                    "reuse an untrusted gateway credential."
                ) from lookup_exc
        else:
            raise
    scopes = [scope] if scope else []
    return _result(provider_arn, scopes, provider_created)


def create_harness(
    agentcore_ctrl,
    harness_name: str,
    role_arn: str,
    *,
    model_id: str | None = None,
    system_prompt: str | None = None,
    gateway_arn: str | None = None,
    gateway_outbound_provider_arn: str | None = None,
    gateway_scopes: list | None = None,
    memory_arn: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    env_vars: dict | None = None,
    region: str | None = None,
    resource_tags: dict | None = None,
) -> dict:
    """Create an AgentCore Harness. Returns {harness_id, arn, status}.

    If *model_id* is omitted the Harness defaults to Claude Sonnet 4.6 on Bedrock.
    A connected gateway is wired with outbound OAuth when
    *gateway_outbound_provider_arn* is supplied (required for CUSTOM_JWT gateways).
    Idempotent: on conflict, the existing harness is looked up and returned.
    """
    create_params: dict = {
        "harnessName": harness_name,
        "executionRoleArn": role_arn,
        "tags": governed_tags(region, resource_tags),
    }
    if model_id:
        model_config = {
            "modelId": model_id,
            "maxTokens": max_tokens,
        }
        # Claude Sonnet 5 and later models reject the temperature parameter
        # with ValidationException: "temperature is deprecated for this model".
        # Only include temperature for older models.
        if not any(m in model_id.lower() for m in ("claude-sonnet-5", "claude-opus-5")):
            model_config["temperature"] = temperature
        create_params["model"] = {"bedrockModelConfig": model_config}
    if system_prompt:
        create_params["systemPrompt"] = [{"text": system_prompt}]

    tools = build_harness_tools(
        gateway_arn,
        gateway_outbound_provider_arn=gateway_outbound_provider_arn,
        gateway_scopes=gateway_scopes,
    )
    if tools:
        create_params["tools"] = tools

    if memory_arn:
        create_params["memory"] = {"agentCoreMemoryConfiguration": {"arn": memory_arn, "messagesCount": 20}}
    if env_vars:
        create_params["environmentVariables"] = env_vars

    def _create_with_transient_retry():
        # Transient markers that warrant a retry:
        #  - S3 region cache 301 (Bug 63);
        #  - IAM-assume race (Bug 80/151): CreateHarness validates the exec role's
        #    trust policy SYNCHRONOUSLY, but a freshly-created role's trust policy
        #    lags IAM control-plane consistency, surfacing as
        #    "Role validation failed ... trust policy allows assumption" or
        #    "Access denied". The role IS correct; we just have to wait for the
        #    service-side IAM cache to catch up. Up to 12 x 10s = 120s.
        retryable = (
            "Access denied",
            "Moved Permanently",
            "Status Code: 301",
            "Role validation failed",
            "trust policy allows assumption",
            "not authorized to perform: sts:AssumeRole",
        )
        last_err = None
        attempts = 12
        for attempt in range(attempts):
            try:
                return agentcore_ctrl.create_harness(**create_params)
            except Exception as e:  # noqa: BLE001
                err = str(e)
                if is_error(e, "ValidationException") and any(m in err for m in retryable):
                    last_err = e
                    logger.info(
                        "create_harness transient role/cache race (attempt %d/%d): %s",
                        attempt + 1,
                        attempts,
                        err[:200],
                    )
                    time.sleep(10)
                    continue
                raise
        raise last_err if last_err else RuntimeError("create_harness failed")

    try:
        resp = _create_with_transient_retry()
    except Exception as e:  # noqa: BLE001
        # "already exists" fallback kept: conflicts can surface as a
        # ValidationException whose message says "already exists".
        if is_error(e, "ConflictException") or "already exists" in str(e):
            logger.info("Harness '%s' already exists, looking up", harness_name)
            existing = _find_harness_by_name(agentcore_ctrl, harness_name)
            if existing:
                assert_agentcore_resource_owned(
                    agentcore_ctrl,
                    "harness",
                    existing["harness_id"],
                    region,
                )
                existing["created_by_deployment"] = False
                return existing
        raise

    # CreateHarness/GetHarness wrap the resource in a "harness" envelope (verified
    # live); the ARN field is "arn" (not "harnessArn").
    h = resp.get("harness", resp)
    harness_id = h.get("harnessId", "")
    arn = h.get("arn", h.get("harnessArn", ""))
    logger.info("Created harness: id=%s, arn=%s", harness_id, arn)
    return {
        "harness_id": harness_id,
        "arn": arn,
        "status": h.get("status", "CREATING"),
        "created_by_deployment": True,
    }


def _find_harness_by_name(agentcore_ctrl, harness_name: str) -> dict | None:
    """Paginate list_harnesses to find one by name. Returns {harness_id,arn,status}."""
    try:
        items = list_all(
            agentcore_ctrl,
            "list_harnesses",
            item_keys=("harnesses", "harnessSummaries", "items"),
            request={"maxResults": 100},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("list_harnesses failed: %s", e)
        return None
    for h in items:
        if h.get("harnessName") == harness_name:
            return {
                "harness_id": h.get("harnessId", ""),
                "arn": h.get("arn", h.get("harnessArn", "")),
                "status": h.get("status", ""),
            }
    return None


#: Every field name AgentCore has been observed to report a lifecycle failure under, in the
#: order they are tried. Not a guess at one name: gateways use ``statusReasons`` (a list),
#: runtimes use ``failureReason``, and the harness shape is undocumented and unverified, so
#: the reader accepts all of them rather than betting on one.
_HARNESS_FAILURE_REASON_KEYS = (
    "failureReason",
    "statusReason",
    "statusReasons",
    "failureReasons",
)


def _harness_failure_reason(harness: dict) -> str:
    """Best-effort human-readable failure reason out of a get_harness/delete_harness body.

    Returns ``""`` when the body carries none, so the caller can fall back to the status
    alone rather than printing an empty separator. Values may be a string or a list of
    strings; a list is joined rather than indexed, because a truncated multi-reason failure
    is how the underlying cause gets hidden.

    Deliberately total: this runs on an already-failing path, so it must never be the thing
    that raises. A malformed body costs the reason, not the error.
    """
    if not isinstance(harness, dict):
        return ""
    parts: list[str] = []
    for key in _HARNESS_FAILURE_REASON_KEYS:
        value = harness.get(key)
        if not value:
            continue
        if isinstance(value, str):
            parts.append(value.strip())
        elif isinstance(value, (list, tuple)):
            parts.extend(str(v).strip() for v in value if v)
        else:
            parts.append(str(value).strip())
    # Deduplicate while preserving order: the same text often arrives under two keys, and
    # printing it twice reads like two separate failures.
    return "; ".join(dict.fromkeys(p for p in parts if p))


def wait_for_harness_ready(agentcore_ctrl, harness_id: str, timeout: int = 600) -> dict:
    """Poll get_harness until READY/ACTIVE or timeout."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = agentcore_ctrl.get_harness(harnessId=harness_id)
            h = resp.get("harness", resp)
            status = h.get("status", "")
            logger.info("Harness %s status: %s", harness_id, status)
            if status in ("READY", "ACTIVE"):
                environment = (h.get("environment") or {}).get("agentCoreRuntimeEnvironment") or {}
                return {
                    "success": True,
                    "harness_id": harness_id,
                    "arn": h.get("arn", h.get("harnessArn", "")),
                    "status": status,
                    # The runtime AgentCore created to host this harness. Its DEFAULT log group holds every
                    # conversation the harness serves (harness_step bounds it).
                    "backing_runtime_id": str(environment.get("agentRuntimeId") or ""),
                }
            if "FAILED" in status:
                # Carry the service's OWN reason, not just the status. Measured live
                # 2026-09-22 on acfe2e-p0920: a harness reached CREATE_FAILED because
                # CreateHarness asynchronously creates and tags a backing agent runtime
                # under the CALLER's role, and that role held no TagResource on runtime/*.
                # The 403 (Service: BedrockAgentcoreRuntimeControl, a different service
                # from the API that was called) appeared in NO log group -- a full sweep of
                # every step log group found nothing. It existed only in this response
                # body. Returning "Harness entered CREATE_FAILED" and dropping `h` on the
                # floor made a plain IAM denial look like an unexplained service failure,
                # and it is what made the grant take a second live round to find.
                #
                # Every key is tried because the field name is not contractual here and an
                # absent one must not mask a present one: AgentCore spells this
                # `statusReasons` (a list) on gateways and `failureReason` on runtimes, and
                # the harness shape is unverified. Unknown-but-present beats silent.
                reason = _harness_failure_reason(h)
                return {
                    "success": False,
                    "harness_id": harness_id,
                    "status": status,
                    "error": f"Harness entered {status}{f': {reason}' if reason else ''}",
                }
        except Exception as e:  # noqa: BLE001
            logger.warning("Error checking harness status: %s", e)
        time.sleep(15)
    return {
        "success": False,
        "harness_id": harness_id,
        "error": f"Harness did not become READY within {timeout}s",
    }


_W3C_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def normalize_trace_id(trace_id: str | None) -> str:
    """Return a W3C trace id (32 lowercase hex chars), minting one if needed.

    Accepts a W3C id, or an X-Ray root (``1-<8 hex>-<24 hex>``) which is folded
    into its 32-hex form. Anything else is replaced by a fresh id so the invoke
    never fails on a malformed caller value.
    """
    if trace_id:
        candidate = trace_id.strip().lower()
        if candidate.startswith("1-") and candidate.count("-") == 2:
            candidate = candidate.replace("-", "")[1:]
        if _W3C_TRACE_ID_RE.match(candidate):
            return candidate
        logger.warning("invoke_harness: ignoring malformed trace_id %r", trace_id[:64])
    return uuid.uuid4().hex


def invoke_harness(
    region: str,
    harness_arn: str,
    prompt: str,
    session_id: str,
    *,
    timeout_seconds: int | None = None,
    trace_id: str | None = None,
    agentcore_data_client=None,
) -> dict:
    """Invoke a Harness (data plane) and collect the streamed response.

    Returns {success, output, stop_reason, tool_calls, error, trace_id}. The
    session id is padded to the >= 33 char requirement. Reuse the same session id
    to continue a conversation in the same environment (memory continuity).

    Trace correlation (verified live 2026-09-14): InvokeHarness accepts
    ``traceId`` and ``traceParent``; only the W3C ``traceParent`` is honoured
    for propagation — with ``traceId`` alone the harness mints its own id. The
    trace id we send flows to every downstream AgentCore span, including the
    Memory ``CreateEvent``/``ListEvents`` spans in ``aws/spans``. Memory APPLICATION_LOGS never carry a trace id, so this is the
    only handle that ties a harness turn to its memory activity — we ALWAYS send
    one (caller-supplied or minted here) and hand it back as ``trace_id``. See
    ``services/memory_trace_lookup.py`` for the log -> span -> trace join.
    """
    data = agentcore_data_client or _create_agentcore_client(region)
    trace_id = normalize_trace_id(trace_id)
    params: dict = {
        "harnessArn": harness_arn,
        "runtimeSessionId": pad_session_id(session_id),
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "traceId": trace_id,
        "traceParent": f"00-{trace_id}-{uuid.uuid4().hex[:16]}-01",
    }
    if timeout_seconds:
        params["timeoutSeconds"] = timeout_seconds

    try:
        resp = data.invoke_harness(**params)
    except Exception as e:  # noqa: BLE001
        # SECURITY (CodeQL py/clear-text + stack-trace-exposure): keep the raw
        # exception text OUT of the returned dict (callers surface `error`/`output`
        # to clients). Log detail server-side; return a generic message.
        logger.warning("invoke_harness failed: %s", e)
        return {
            "success": False,
            "error": "Harness invocation failed",
            "output": "",
            "stop_reason": "",
            "tool_calls": [],
            "trace_id": trace_id,
        }

    text_parts: list[str] = []
    tool_calls: list[str] = []
    stop_reason = ""
    error = ""
    try:
        for event in resp.get("stream", []):
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                if "text" in delta:
                    text_parts.append(delta["text"])
            elif "contentBlockStart" in event:
                start = event["contentBlockStart"].get("start", {})
                tu = start.get("toolUse")
                if tu and tu.get("name"):
                    tool_calls.append(tu["name"])
            elif "messageStop" in event:
                stop_reason = event["messageStop"].get("stopReason", "")
            elif "runtimeClientError" in event:
                error = event["runtimeClientError"].get("message", "runtime client error")
    except Exception as e:  # noqa: BLE001
        # SECURITY: don't leak the raw exception text via the returned dict.
        logger.warning("invoke_harness stream read failed: %s", e)
        return {
            "success": False,
            "error": "Harness stream read failed",
            "output": "".join(text_parts),
            "stop_reason": stop_reason,
            "tool_calls": tool_calls,
            "trace_id": trace_id,
        }

    # Loom-study 2.4 — HITL for MANAGED (harness) agents. Direct-code agents get
    # a guaranteed BeforeToolInvocation gate (2.1), but a managed harness runs the
    # tool inside AWS's loop where we can't inject a hook. Our leverage is the
    # invoke boundary: inspect the streamed toolUse names against the org's
    # approval policies and, for a policy-matched "require" tool, RECORD a PENDING
    # approval + surface approval_required so an operator reviews it. (True mid-loop
    # blocking of a managed tool needs a gateway-side interceptor — noted in the
    # plan.) Best-effort; never fails the invoke.
    approval_required = _harness_approval_check(region, tool_calls, session_id)

    return {
        "success": not error,
        "output": "".join(text_parts),
        "stop_reason": stop_reason,
        "tool_calls": tool_calls,
        "approval_required": approval_required,
        "error": error,
        "trace_id": trace_id,
    }


def _harness_approval_check(region: str, tool_calls: list, session_id: str) -> list:
    """Match invoked harness tools against org approval policies; record PENDING
    rows for "require"-mode matches. Returns the list of matched tool names."""
    import fnmatch
    import os

    if not tool_calls:
        return []
    pol_table = os.environ.get("TAG_POLICY_TABLE_NAME", "")
    hitl_table = os.environ.get("HITL_REQUESTS_TABLE_NAME", "")
    if not pol_table:
        return []
    try:
        from app.services.approval_policy_store import ApprovalPolicyStore

        policies = ApprovalPolicyStore(pol_table, region).list("default")
    except Exception:  # noqa: BLE001
        return []
    matched: list = []
    for name in tool_calls:
        for p in policies:
            if not p.enabled:
                continue
            if any(fnmatch.fnmatch(name or "", pat) for pat in p.tool_match):
                matched.append(name)
                if p.mode == "require" and hitl_table:
                    _record_harness_pending(region, hitl_table, name, session_id)
                break
    return matched


def _record_harness_pending(region: str, hitl_table: str, tool_name: str, session_id: str) -> None:
    import os
    import secrets
    import time

    try:
        import boto3

        ms = int(time.time() * 1000)
        boto3.resource("dynamodb", region_name=region).Table(hitl_table).put_item(
            Item={
                "runtime_id": os.environ.get("HITL_RUNTIME_ID", "harness"),
                "request_id": f"{ms:012x}{secrets.token_hex(10)}",
                "owner_sub": os.environ.get("RUNTIME_OWNER_SUB", ""),
                "status": "PENDING",
                "action": ("harness-tool:" + str(tool_name))[:2000],
                "reason": ("session:" + str(session_id))[:2000],
                "created_at": ms,
                "ttl": int(time.time()) + 24 * 60 * 60,
            }
        )
    except Exception:  # noqa: BLE001
        logger.warning("harness HITL pending-record skipped")


def _resolve_harness_identifier(agentcore_ctrl, identifier: str) -> str:
    """Convert a harness NAME (or already-an-id) to the canonical harnessId.

    Like runtimes (Bug 50), delete/get accept only the canonical id. If the
    identifier already resolves via get_harness, use it; otherwise look it up by
    name.
    """
    try:
        agentcore_ctrl.get_harness(harnessId=identifier)
        return identifier
    except Exception:  # noqa: BLE001
        found = _find_harness_by_name(agentcore_ctrl, identifier)
        return found["harness_id"] if found and found.get("harness_id") else identifier


def _harness_name_from_id(harness_id: str) -> str:
    """Recover the harness NAME from its id (id == name + '-<10 char suffix>')."""
    if "-" in harness_id:
        head, _, tail = harness_id.rpartition("-")
        # The suffix AgentCore appends is ~10 alnum chars; only strip when it looks
        # like one (otherwise the name itself contained no suffix).
        if head and 6 <= len(tail) <= 16 and tail.isalnum():
            return head
    return harness_id


def destroy_harness(
    harness_id: str,
    region: str,
    *,
    agentcore_ctrl=None,
    confirmation_attempts: int = 70,
    confirmation_interval: float = 8.0,
    confirmation_deadline: float | None = None,
) -> dict:
    """Delete a Harness and its harness->gateway outbound OAuth provider. Idempotent.

    The outbound provider is named deterministically (``harness-gw-<harness_name>``)
    by ``ensure_gateway_outbound_provider``, so we can always reconstruct and delete
    it here WITHOUT relying on a persisted harness_result (which status_update does
    not store) — this closes the orphan gap caught in the live customer test.

    Note: this boto3/service build exposes only Create/Get/List/Update/Delete
    Harness — there are no separate harness-endpoint operations.
    """
    agentcore_ctrl = agentcore_ctrl or _create_agentcore_control_client(region)
    resolved = _resolve_harness_identifier(agentcore_ctrl, harness_id)
    harness_name = _harness_name_from_id(resolved)

    result: dict
    try:
        assert_agentcore_resource_owned(
            agentcore_ctrl,
            "harness",
            resolved,
            region,
        )
    except ResourceDeletionRefused as exc:
        return {
            "success": False,
            "harness_id": resolved,
            "protected": True,
            "retained": True,
            "note": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        if is_error(exc, "ResourceNotFoundException", "NotFoundException"):
            result = {
                "success": True,
                "harness_id": resolved,
                "note": "already gone",
            }
        else:
            return {
                "success": False,
                "harness_id": resolved,
                "error": f"Harness ownership read failed ({type(exc).__name__})",
            }
    else:
        result = {}

    if not result:
        try:
            agentcore_ctrl.delete_harness(harnessId=resolved)
            wait_until_absent(
                resource_label=f"harness {resolved}",
                read=lambda: agentcore_ctrl.get_harness(harnessId=resolved),
                max_attempts=confirmation_attempts,
                delay_seconds=confirmation_interval,
                deadline_monotonic=confirmation_deadline,
            )
            logger.info("Confirmed harness %s deleted", resolved)
            result = {"success": True, "harness_id": resolved}
        except DeletionFailedAfterAccept as e:
            result = {
                "success": False,
                "harness_id": resolved,
                "error": str(e),
            }
        except ResourceDeletionRefused as e:
            result = {
                "success": False,
                "harness_id": resolved,
                "retained": True,
                "note": str(e),
            }
        except Exception as e:  # noqa: BLE001
            if is_error(e, "ResourceNotFoundException", "NotFoundException"):
                result = {"success": True, "harness_id": resolved, "note": "already gone"}
            else:
                result = {"success": False, "harness_id": resolved, "error": str(e)}

    # The provider is part of the harness's attached graph.  If DeleteHarness
    # failed, removing its credential provider would leave a still-live harness
    # unable to call its gateway.  Only clean the provider after the harness is
    # conclusively deleted/already absent.
    if not result.get("success", False):
        return result

    # Best-effort delete of the outbound OAuth provider (no orphan).
    provider_name = f"harness-gw-{harness_name}"[:60]
    try:
        assert_agentcore_resource_owned(
            agentcore_ctrl,
            "oauth2_credential_provider",
            provider_name,
            region,
        )
        agentcore_ctrl.delete_oauth2_credential_provider(name=provider_name)
        logger.info("Deleted harness gateway outbound provider %s", provider_name)
        result["outbound_provider_deleted"] = provider_name
    except ResourceDeletionRefused as exc:
        logger.warning(
            "Harness outbound provider retained: %s",
            exc,
        )
        result["outbound_provider_retained"] = provider_name
    except Exception as e:  # noqa: BLE001
        if not is_error(e, "ResourceNotFoundException", "NotFoundException"):
            logger.warning("Harness outbound provider %s delete: %s", provider_name, str(e)[:120])

    # NOTE (Bug 188 investigation): CreateHarness auto-provisions a backing
    # AgentCore runtime named ``harness_<harness_id>`` (a Harness runs on top of a
    # runtime, Bug 151). That runtime is HARNESS-MANAGED — delete_agent_runtime on
    # it fails with "managed by harness ... Use DeleteHarness". delete_harness
    # DOES cascade-delete it (verified live across runs), so NO explicit runtime
    # delete is needed or even allowed here. A lingering ``harness_*`` runtime
    # means its delete_harness didn't actually run (e.g. a crashed test run that
    # never reached teardown), not a teardown-code gap.
    return result


def get_shared_or_new_harness_role(
    iam_client,
    harness_name: str,
    *,
    model_id: str | None = None,
    memory_arn: str | None = None,
    gateway_arn: str | None = None,
    region: str | None = None,
    return_provenance: bool = False,
    resource_tags: dict | None = None,
) -> str | tuple[str, bool]:
    """Return a harness execution role ARN.

    Prefers a pre-created shared role (env SHARED_HARNESS_ROLE_ARN, mirroring the
    runtime shared-role strategy that dodges the IAM-cache race / Bug 60). Falls
    back to a per-harness role, scoped to the connected model/memory/gateway ARNs
    for least privilege when those are known (Holmes IAM findings).
    """
    shared = os.environ.get("SHARED_HARNESS_ROLE_ARN", "")
    if shared:
        return (shared, False) if return_provenance else shared
    return create_harness_iam_role(
        iam_client,
        regional_iam_role_name(
            f"AgentCoreHarness-{sanitize_harness_name(harness_name)}",
            region,
        ),
        # The SAME name that becomes the harness name, so the exec role's memory
        # pattern matches the memory CreateHarness derives from it. Passing the role
        # name here instead would scope to ``memory/AgentCoreHarness-<name>-*`` and
        # silently restore the outage.
        harness_name=harness_name,
        model_id=model_id,
        memory_arn=memory_arn,
        gateway_arn=gateway_arn,
        region=region,
        return_provenance=return_provenance,
        resource_tags=resource_tags,
    )
