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
import time
from typing import Optional

import boto3

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

    return sanitize_agentcore_name(
        name, style="underscore", max_len=40, prefix="h", fallback="agentcore_harness"
    )


def pad_session_id(session_id: str) -> str:
    """Ensure a runtime session id satisfies the >= 33 char requirement."""
    if len(session_id) >= _MIN_SESSION_ID_LEN:
        return session_id
    return (session_id + "-" + "0" * _MIN_SESSION_ID_LEN)[:_MIN_SESSION_ID_LEN]


# ---------------------------------------------------------------------------
# IAM execution role
# ---------------------------------------------------------------------------


def _model_arn_pattern(model_id: str) -> Optional[str]:
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


def _model_resource_arns(model_id: str) -> Optional[list]:
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
    model_id: Optional[str] = None,
    memory_arn: Optional[str] = None,
    gateway_arn: Optional[str] = None,
) -> str:
    """Create or reuse an execution role the Harness can assume.

    The Harness needs to invoke Bedrock models, read/write AgentCore Memory,
    invoke connected Gateways, and emit observability — but, unlike a Runtime, it
    has NO S3 code artifact to read. Returns the role ARN.

    Least-privilege (Holmes IAM findings): when *model_id* / *memory_arn* /
    *gateway_arn* are supplied the corresponding statements are scoped to those
    ARNs (model family for InvokeModel; the specific memory/gateway ARNs for the
    agentcore actions) instead of ``Resource: "*"``. We fall back to ``*`` only
    when an ARN is unavailable, so callers that don't pass them keep working.
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
    managed_tag = [{"Key": "ManagedBy", "Value": "agentcore-flows"}]
    try:
        resp = iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description=f"Execution role for AgentCore Harness {role_name}",
            Tags=managed_tag,
        )
        role_arn = resp["Role"]["Arn"]
        logger.info("Created harness IAM role: %s", role_arn)
    except iam_client.exceptions.EntityAlreadyExistsException:
        role_arn = iam_client.get_role(RoleName=role_name)["Role"]["Arn"]
        logger.info("Reusing existing harness IAM role: %s", role_arn)
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

    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "BedrockModelAccess",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                "Resource": model_resource,
            },
            {
                # Memory/gateway data-plane actions. Scoped to the connected
                # resource ARNs when known (Holmes IAM finding), else "*".
                # InvokeGateway is REQUIRED for the harness to call a connected
                # gateway's tools at runtime (mirrors the runtime exec role's
                # GatewayAccess Sid). ListGateways/GetGateway/ListGatewayTargets
                # cover discovery + target enumeration.
                "Sid": "AgentCoreMemoryAndGateway",
                "Effect": "Allow",
                "Action": scoped_agentcore,
                "Resource": scoped_resources or "*",
            },
            {
                # CreateHarness ALWAYS auto-provisions a DEFAULT AgentCore Memory
                # for the harness session (memory/harness_<name>_*), whose ARN is
                # NOT known when this role policy is built (it's minted later by the
                # harness service). Without memory data-plane perms on that ARN, the
                # first InvokeHarness fails with AccessDenied on ListEvents
                # (verified live: "not authorized to perform bedrock-agentcore:
                # ListEvents on resource memory/harness_<name>_..."). Scope the
                # memory data-plane verbs to the harness-owned memory name prefix.
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
                    "arn:aws:bedrock-agentcore:*:*:memory/harness_*",
                    "arn:aws:bedrock-agentcore:*:*:memory/harness_*/*",
                ],
            },
            {
                # Account-level agentcore actions with no resource-ARN form:
                # ListGateways (collection) + the token-vault token/key fetches
                # GetResourceOauth2Token/GetResourceApiKey (needed to load the
                # outbound OAuth token for a CUSTOM_JWT gateway — verified live).
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
    iam_client.put_role_policy(
        RoleName=role_name,
        PolicyName="HarnessExecutionPolicy",
        PolicyDocument=json.dumps(policy),
    )
    return role_arn


# ---------------------------------------------------------------------------
# Harness lifecycle
# ---------------------------------------------------------------------------


def build_harness_tools(
    gateway_arn: Optional[str] = None,
    *,
    gateway_outbound_provider_arn: Optional[str] = None,
    gateway_scopes: Optional[list] = None,
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
    agentcore_ctrl, harness_name: str, gateway_client_info: dict
) -> tuple[Optional[str], list]:
    """Register an OAuth2 credential provider so a harness can call a CUSTOM_JWT
    gateway, derived from that gateway's Cognito client_info.

    Returns (provider_arn, scopes). Returns (None, []) if client_info lacks the
    fields needed (then the caller wires the gateway with no outbound auth).
    Mirrors the internal-MCP CustomOauth2 registration in gateway_deployer.
    """
    import re

    discovery_url = gateway_client_info.get("discovery_url")
    client_id = gateway_client_info.get("client_id")
    client_secret = gateway_client_info.get("client_secret")
    scope = gateway_client_info.get("scope", "")
    user_pool_id = gateway_client_info.get("user_pool_id")
    region = gateway_client_info.get("region")

    # Cognito gateways created by deploy_gateway expose user_pool_id but derive the
    # discovery URL from it; reconstruct if absent.
    if not discovery_url and user_pool_id:
        # region is embedded in the pool id prefix (e.g. us-west-2_abc); fall back
        # to the provided region or parse from the token endpoint.
        reg = region
        if not reg:
            te = gateway_client_info.get("token_endpoint", "")
            m = re.search(r"\.auth\.([a-z0-9-]+)\.amazoncognito", te)
            reg = m.group(1) if m else "us-east-1"
        discovery_url = (
            f"https://cognito-idp.{reg}.amazonaws.com/{user_pool_id}/.well-known/openid-configuration"
        )

    if not (discovery_url and client_id and client_secret):
        logger.warning(
            "Gateway client_info lacks discovery_url/client_id/client_secret; "
            "harness gateway tool will have NO outbound auth (401 likely if gateway is CUSTOM_JWT)"
        )
        return None, []

    provider_name = (re.sub(r"[^a-zA-Z0-9_-]", "-", f"harness-gw-{harness_name}")[:60]) or "harness-gw-cred"
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
        )
        provider_arn = resp["credentialProviderArn"]
        logger.info("Created harness gateway outbound OAuth provider %s", provider_name)
    except Exception as e:  # noqa: BLE001
        if "ConflictException" in str(e) or "already exists" in str(e):
            try:
                got = agentcore_ctrl.get_oauth2_credential_provider(name=provider_name)
                provider_arn = got.get("credentialProviderArn", "")
            except Exception:  # noqa: BLE001
                return None, []
        else:
            raise
    scopes = [scope] if scope else []
    return provider_arn, scopes


def create_harness(
    agentcore_ctrl,
    harness_name: str,
    role_arn: str,
    *,
    model_id: Optional[str] = None,
    system_prompt: Optional[str] = None,
    gateway_arn: Optional[str] = None,
    gateway_outbound_provider_arn: Optional[str] = None,
    gateway_scopes: Optional[list] = None,
    memory_arn: Optional[str] = None,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    env_vars: Optional[dict] = None,
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
        create_params["memory"] = {
            "agentCoreMemoryConfiguration": {"arn": memory_arn, "messagesCount": 20}
        }
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
                if "ValidationException" in err and any(m in err for m in retryable):
                    last_err = e
                    logger.info(
                        "create_harness transient role/cache race (attempt %d/%d): %s",
                        attempt + 1, attempts, err[:200],
                    )
                    time.sleep(10)
                    continue
                raise
        raise last_err if last_err else RuntimeError("create_harness failed")

    try:
        resp = _create_with_transient_retry()
    except Exception as e:  # noqa: BLE001
        if "ConflictException" in str(e) or "already exists" in str(e):
            logger.info("Harness '%s' already exists, looking up", harness_name)
            existing = _find_harness_by_name(agentcore_ctrl, harness_name)
            if existing:
                return existing
        raise

    # CreateHarness/GetHarness wrap the resource in a "harness" envelope (verified
    # live); the ARN field is "arn" (not "harnessArn").
    h = resp.get("harness", resp)
    harness_id = h.get("harnessId", "")
    arn = h.get("arn", h.get("harnessArn", ""))
    logger.info("Created harness: id=%s, arn=%s", harness_id, arn)
    return {"harness_id": harness_id, "arn": arn, "status": h.get("status", "CREATING")}


def _find_harness_by_name(agentcore_ctrl, harness_name: str) -> Optional[dict]:
    """Paginate list_harnesses to find one by name. Returns {harness_id,arn,status}."""
    next_token = None
    for _ in range(20):
        kwargs = {}
        if next_token:
            kwargs["nextToken"] = next_token
        try:
            resp = agentcore_ctrl.list_harnesses(**kwargs)
        except Exception as e:  # noqa: BLE001
            logger.warning("list_harnesses failed: %s", e)
            return None
        items = resp.get("harnesses", resp.get("harnessSummaries", resp.get("items", [])))
        for h in items:
            if h.get("harnessName") == harness_name:
                return {
                    "harness_id": h.get("harnessId", ""),
                    "arn": h.get("arn", h.get("harnessArn", "")),
                    "status": h.get("status", ""),
                }
        next_token = resp.get("nextToken")
        if not next_token:
            break
    return None


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
                return {
                    "success": True,
                    "harness_id": harness_id,
                    "arn": h.get("arn", h.get("harnessArn", "")),
                    "status": status,
                }
            if "FAILED" in status:
                return {
                    "success": False,
                    "harness_id": harness_id,
                    "status": status,
                    "error": f"Harness entered {status}",
                }
        except Exception as e:  # noqa: BLE001
            logger.warning("Error checking harness status: %s", e)
        time.sleep(15)
    return {
        "success": False,
        "harness_id": harness_id,
        "error": f"Harness did not become READY within {timeout}s",
    }


def invoke_harness(
    region: str,
    harness_arn: str,
    prompt: str,
    session_id: str,
    *,
    timeout_seconds: Optional[int] = None,
) -> dict:
    """Invoke a Harness (data plane) and collect the streamed response.

    Returns {success, output, stop_reason, tool_calls, error}. The session id is
    padded to the >= 33 char requirement. Reuse the same session id to continue a
    conversation in the same environment (memory continuity).
    """
    data = _create_agentcore_client(region)
    params: dict = {
        "harnessArn": harness_arn,
        "runtimeSessionId": pad_session_id(session_id),
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
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
        return {"success": False, "error": "Harness invocation failed", "output": "", "stop_reason": "", "tool_calls": []}

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
    }


def _harness_approval_check(region: str, tool_calls: list, session_id: str) -> list:
    """Match invoked harness tools against org approval policies; record PENDING
    rows for "require"-mode matches. Returns the list of matched tool names."""
    import os
    import fnmatch

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
    import time
    import secrets

    try:
        import boto3
        ms = int(time.time() * 1000)
        boto3.resource("dynamodb", region_name=region).Table(hitl_table).put_item(Item={
            "runtime_id": os.environ.get("HITL_RUNTIME_ID", "harness"),
            "request_id": "%012x%s" % (ms, secrets.token_hex(10)),
            "owner_sub": os.environ.get("RUNTIME_OWNER_SUB", ""),
            "status": "PENDING",
            "action": ("harness-tool:" + str(tool_name))[:2000],
            "reason": ("session:" + str(session_id))[:2000],
            "created_at": ms,
            "ttl": int(time.time()) + 24 * 60 * 60,
        })
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


def destroy_harness(harness_id: str, region: str) -> dict:
    """Delete a Harness and its harness->gateway outbound OAuth provider. Idempotent.

    The outbound provider is named deterministically (``harness-gw-<harness_name>``)
    by ``ensure_gateway_outbound_provider``, so we can always reconstruct and delete
    it here WITHOUT relying on a persisted harness_result (which status_update does
    not store) — this closes the orphan gap caught in the live customer test.

    Note: this boto3/service build exposes only Create/Get/List/Update/Delete
    Harness — there are no separate harness-endpoint operations.
    """
    agentcore_ctrl = _create_agentcore_control_client(region)
    resolved = _resolve_harness_identifier(agentcore_ctrl, harness_id)
    harness_name = _harness_name_from_id(resolved)

    result: dict
    try:
        agentcore_ctrl.delete_harness(harnessId=resolved)
        logger.info("Deleted harness %s", resolved)
        result = {"success": True, "harness_id": resolved}
    except Exception as e:  # noqa: BLE001
        err = str(e)
        if "ResourceNotFound" in err or "NotFound" in err:
            result = {"success": True, "harness_id": resolved, "note": "already gone"}
        else:
            result = {"success": False, "harness_id": resolved, "error": err}

    # Best-effort delete of the outbound OAuth provider (no orphan).
    provider_name = f"harness-gw-{harness_name}"[:60]
    try:
        agentcore_ctrl.delete_oauth2_credential_provider(name=provider_name)
        logger.info("Deleted harness gateway outbound provider %s", provider_name)
        result["outbound_provider_deleted"] = provider_name
    except Exception as e:  # noqa: BLE001
        if "ResourceNotFound" not in str(e) and "NotFound" not in str(e):
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
    model_id: Optional[str] = None,
    memory_arn: Optional[str] = None,
    gateway_arn: Optional[str] = None,
) -> str:
    """Return a harness execution role ARN.

    Prefers a pre-created shared role (env SHARED_HARNESS_ROLE_ARN, mirroring the
    runtime shared-role strategy that dodges the IAM-cache race / Bug 60). Falls
    back to a per-harness role, scoped to the connected model/memory/gateway ARNs
    for least privilege when those are known (Holmes IAM findings).
    """
    shared = os.environ.get("SHARED_HARNESS_ROLE_ARN", "")
    if shared:
        return shared
    return create_harness_iam_role(
        iam_client,
        f"AgentCoreHarness-{sanitize_harness_name(harness_name)}",
        model_id=model_id,
        memory_arn=memory_arn,
        gateway_arn=gateway_arn,
    )
