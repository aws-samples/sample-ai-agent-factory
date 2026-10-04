"""Step handler: Deploy MCP Server Runtime before Gateway.

Generates FastMCP server code, uploads to S3 with dependency bundle,
creates IAM role, sets up Cognito OAuth for gateway-to-runtime auth,
creates the runtime with MCP protocol + JWT authorizer, and waits
for it to reach READY status.

Returns the runtime ARN and OAuth credentials so the gateway step
can create the MCP target with proper OAUTH credential provider.

Requirements: MCP Server as Gateway Target pattern
"""

# ruff: noqa: I001

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import json
import logging
import os
import re
import time

import app.services._otel_platform  # noqa: F401
import boto3
from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services import authoritative_dns
from app.services import step_clients
from app.services.deployment import generate_mcp_server_code
from app.services.deployment_state_store import DeploymentStateStore
from app.services.gateway_deployer import (
    ConnectorSecretDeletionRefused,
    bind_connector_secret_for_deployment,
    connector_identity_mode,
    manifest_secret_journal,
    secret_intent_journal,
    delete_deployment_bound_secret,
)
from app.services.naming import (
    regional_iam_role_name,
    scoped_mcp_code_s3_key,
)
from app.services.resource_tagging import governed_tags
from app.services.runtime_deployer import (
    create_agent_runtime,
    create_runtime_iam_role,
    govern_default_runtime_log_group,
    sanitize_runtime_name,
    upload_code_to_s3,
    wait_for_default_endpoint_ready,
    wait_for_runtime_ready,
)

logger = logging.getLogger(__name__)

_LAMBDA_TIMEOUT_FALLBACK_SECONDS = 600.0
_READINESS_COMPLETION_RESERVE_SECONDS = 30.0
_MAX_MCP_PREWARM_RESPONSE_BYTES = 1024 * 1024
#: Stop extending pre-warm transport retries when this little of the readiness deadline is left.
_PREWARM_DEADLINE_RESERVE_SECONDS = 20.0


#: Describe failures that no amount of waiting cures: the caller is not allowed to ask.
#: When no authoritative nameserver can be reached at all, accept Cognito ACTIVE after this settle window.
_DOMAIN_SETTLE_SECONDS_WHEN_UNVERIFIABLE = 90.0
_DOMAIN_DESCRIBE_FATAL_CODES = frozenset({"AccessDeniedException", "UnauthorizedException", "NotAuthorizedException"})


def _wait_for_cognito_domain(cognito, domain: str, region: str, *, deadline_monotonic: float) -> float:
    """Block until a freshly created Cognito auth domain is ACTIVE and its hostname resolves.

    Measured live (2026-09-28, run 13): the domain was created, the client minted a token from it
    ~30 s later from this Lambda, and the AgentCore service STILL failed the gateway target update
    with "Please check the OAuth setup. Failed to resolve hostname: <domain>.auth.<region>.
    amazoncognito.com" -- a hosted-UI domain takes up to about a minute to propagate and a
    resolver that looked too early keeps a negative answer. Nothing downstream may be pointed at
    the domain before it is both reported ACTIVE by Cognito and resolvable from here. Bounded by
    the step's deadline; a domain that never becomes usable is a failed deployment, not a warning.
    Returns the seconds waited (for the log).
    """
    zone = f"auth.{region}.amazoncognito.com"
    host = f"{domain}.{zone}"
    started = time.monotonic()
    attempt = 0
    active_since: float | None = None
    while True:
        attempt += 1
        status = ""
        try:
            described = cognito.describe_user_pool_domain(Domain=domain)
            status = str(((described or {}).get("DomainDescription") or {}).get("Status") or "")
        except Exception as exc:  # noqa: BLE001 -- transient describe failures are retried below
            code = str((getattr(exc, "response", None) or {}).get("Error", {}).get("Code") or "")
            if code in _DOMAIN_DESCRIBE_FATAL_CODES:
                # Live, matrix run 14 (2026-09-28): the step role lacked
                # cognito-idp:DescribeUserPoolDomain, every attempt was AccessDenied, and this
                # loop retried a permanent denial for nine minutes before blaming the deadline
                # with "status unknown". A permission denial is never transient.
                raise RuntimeError(
                    f"MCP auth domain {host}: describe_user_pool_domain was refused ({code}); the step role "
                    "needs cognito-idp:DescribeUserPoolDomain (no resource type: Resource '*'). "
                    "Nothing was pointed at the domain."
                ) from exc
            logger.warning("MCP auth domain describe attempt %d: %s", attempt, code or type(exc).__name__)
        resolvable = False
        if status == "ACTIVE":
            # Ask the zone's AUTHORITATIVE nameservers, never this Lambda's resolver: the first
            # lookup here runs before the record exists and the resolver caches that NXDOMAIN for
            # the zone's 900 s negative TTL -- longer than this whole budget (live, 2026-09-29:
            # 180 getaddrinfo attempts over nine minutes, all NXDOMAIN, record long published).
            verdict = authoritative_dns.authoritative_answer(host, zone)
            if verdict == authoritative_dns.PRESENT:
                resolvable = True
            elif verdict == authoritative_dns.UNKNOWN:
                # No authority reachable (egress policy or DNS outage). Do not fall back to the
                # poisoned local resolver; fall back to Cognito's own ACTIVE plus a settle window.
                if active_since is None:
                    active_since = time.monotonic()
                if time.monotonic() - active_since >= _DOMAIN_SETTLE_SECONDS_WHEN_UNVERIFIABLE:
                    logger.warning(
                        "MCP auth domain %s: no authoritative nameserver reachable; proceeding on Cognito "
                        "ACTIVE + %.0f s settle without DNS confirmation",
                        host,
                        _DOMAIN_SETTLE_SECONDS_WHEN_UNVERIFIABLE,
                    )
                    resolvable = True
            else:
                logger.warning(
                    "MCP auth domain %s not published at the zone's authorities yet (attempt %d)", host, attempt
                )
        if resolvable:
            waited = time.monotonic() - started
            if attempt > 1:
                logger.warning("MCP auth domain %s became usable after %.0f s (%d attempts)", host, waited, attempt)
            return waited
        if time.monotonic() + 3.0 >= deadline_monotonic:
            raise RuntimeError(
                f"MCP auth domain {host} did not become ACTIVE and resolvable before the deployment deadline "
                f"(last Cognito status {status or 'unknown'}); nothing was pointed at it."
            )
        time.sleep(3.0)


def _lambda_bounded_readiness_deadline(context) -> float:
    """Leave room for the handler to raise and the orchestrator to persist failure."""
    remaining_seconds = _LAMBDA_TIMEOUT_FALLBACK_SECONDS
    remaining_reader = getattr(context, "get_remaining_time_in_millis", None)
    if callable(remaining_reader):
        try:
            remaining_ms = remaining_reader()
        except Exception:  # noqa: BLE001 — a test/fallback context may not implement it
            remaining_ms = None
        if isinstance(remaining_ms, (int, float)) and not isinstance(remaining_ms, bool) and remaining_ms >= 0:
            remaining_seconds = remaining_ms / 1000.0

    readiness_budget = max(
        0.0,
        remaining_seconds - _READINESS_COMPLETION_RESERVE_SECONDS,
    )
    return time.monotonic() + readiness_budget


def _read_bounded_mcp_response(response) -> bytes | None:
    """Read one MCP response without accepting an unbounded payload."""
    try:
        body = response.read(_MAX_MCP_PREWARM_RESPONSE_BYTES + 1)
    except TypeError:
        # Small test doubles and a few file-like wrappers expose read() without
        # the optional size argument. Real urllib responses take the bounded path.
        body = response.read()
    if not isinstance(body, (bytes, bytearray)):
        return None
    body = bytes(body)
    if len(body) > _MAX_MCP_PREWARM_RESPONSE_BYTES:
        return None
    return body


def _mcp_initialize_response_succeeded(body: bytes) -> bool:
    """Require a matching JSON-RPC initialize result from JSON or SSE."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return False

    candidates: list[str] = []
    stripped = text.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)

    data_lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line == "":
            if data_lines:
                candidates.append("\n".join(data_lines))
                data_lines = []
            continue
        if line.startswith("data:"):
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            data_lines.append(value)
    if data_lines:
        candidates.append("\n".join(data_lines))

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("jsonrpc") != "2.0" or payload.get("id") != 1:
            continue
        if "error" in payload:
            return False
        if isinstance(payload.get("result"), dict):
            return True
    return False


def _prewarm_mcp_runtime(
    region: str,
    runtime_arn: str,
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    scope: str,
    attempts: int = 4,
    *,
    deadline_monotonic: float | None = None,
) -> bool:
    """Force the MCP server runtime container to fully initialize (Bug 171).

    The Gateway's MCP-target tool-discovery probe has a hard ~30s init ceiling on
    the AgentCore side; a cold MCP container (loading the strands-mcp bundle)
    exceeds it on first contact, so the target lands FAILED ("Runtime
    initialization time exceeded ... 30s") and the gateway serves 0 tools. We
    cannot change that probe limit, so we send a real MCP request HERE first —
    once the container is warm, the gateway's later probe completes well under
    30s.

    This runtime has ``customJWTAuthorizer`` enabled. The SigV4
    ``invoke_agent_runtime`` SDK path therefore cannot authenticate it; OAuth
    runtimes must be called over HTTPS with a bearer token. Mint the same
    client-credentials token the Gateway will use, then POST JSON-RPC
    ``initialize`` to the runtime data plane. Best-effort — never raises to the
    caller and never logs the client secret or access token. When an absolute
    deadline is supplied, every network timeout and retry sleep is bounded by
    the time still available to this Lambda invocation.
    """
    import base64 as _base64
    import json as _json
    import urllib.parse as _urlparse
    import urllib.request as _urlrequest
    import urllib.error as _urlerror

    arn_parts = runtime_arn.split(":", 5)
    try:
        token_url = _urlparse.urlsplit(token_endpoint)
        token_port = token_url.port
    except ValueError:
        token_url = None
        token_port = None
    region_is_valid = bool(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+-\d+", region))
    runtime_is_valid = bool(
        len(arn_parts) == 6
        and arn_parts[0] == "arn"
        and arn_parts[2] == "bedrock-agentcore"
        and arn_parts[3] == region
        and arn_parts[5].startswith("runtime/")
    )
    token_host = (token_url.hostname or "").lower() if token_url else ""
    token_suffixes = (
        f".auth.{region}.amazoncognito.com",
        f".auth.{region}.amazoncognito.com.cn",
    )
    token_is_valid = bool(
        token_url
        and token_url.scheme == "https"
        and token_url.username is None
        and token_url.password is None
        and token_port in (None, 443)
        and token_url.path == "/oauth2/token"
        and not token_url.query
        and not token_url.fragment
        and any(token_host.endswith(suffix) and len(token_host) > len(suffix) for suffix in token_suffixes)
    )
    if not (region_is_valid and runtime_is_valid and token_is_valid):
        logger.warning("MCP runtime pre-warm refused a malformed AgentCore or Cognito service endpoint")
        return False

    encoded_arn = _urlparse.quote(runtime_arn, safe="")
    runtime_url = (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
    )
    handshake = _json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "agentcore-flows-prewarm", "version": "1.0"},
            },
        }
    ).encode()

    def _network_timeout(default_seconds: float) -> float | None:
        if deadline_monotonic is None:
            return default_seconds
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            return None
        return min(default_seconds, remaining)

    i = -1
    while True:
        i += 1
        token_timeout = _network_timeout(30.0)
        if token_timeout is None:
            return False
        try:
            token_body = _urlparse.urlencode(
                {
                    "grant_type": "client_credentials",
                    "scope": scope,
                }
            ).encode()
            basic = _base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            token_request = _urlrequest.Request(
                token_endpoint,
                data=token_body,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            with _urlrequest.urlopen(
                token_request,
                timeout=token_timeout,
            ) as response:  # nosec B310  # noqa: S310
                access_token = _json.loads(response.read().decode())["access_token"]

            invoke_timeout = _network_timeout(120.0)
            if invoke_timeout is None:
                return False
            invoke_request = _urlrequest.Request(
                runtime_url,
                data=handshake,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            with _urlrequest.urlopen(
                invoke_request,
                timeout=invoke_timeout,
            ) as response:  # nosec B310  # noqa: S310
                response_body = _read_bounded_mcp_response(response)
            if response_body is None or not _mcp_initialize_response_succeeded(response_body):
                raise ValueError("MCP initialize response did not prove successful initialization")
            logger.info("MCP runtime pre-warm succeeded on attempt %d", i + 1)
            return True
        except Exception as e:  # noqa: BLE001
            # WARNING, not INFO: this module's logger sits below the Lambda root's WARNING
            # threshold, so at INFO the reason for every rejected attempt was invisible and a
            # stripped authorizer surfaced only as "did not succeed" (live, 2026-09-28). The
            # reason is bounded and never carries the token or the secret (neither is
            # interpolated into the exception messages raised above).
            logger.warning(
                "MCP runtime pre-warm attempt %d/%d rejected: %s",
                i + 1,
                attempts,
                str(e)[:160],
            )
            # Live, 2026-09-29 (run 18): four attempts in fifteen seconds all died at the socket
            # layer ("[Errno 99] Cannot assign requested address", "[Errno 16] Device or resource
            # busy") with four minutes of deadline left, and the deployment failed. A transport
            # error is not the runtime rejecting the warm-up; when a deadline is known, keep
            # trying until it is nearly spent. A real rejection (HTTP 400/401/403: authorizer,
            # token, scope) is permanent and still stops after ``attempts``.
            permanent = isinstance(e, _urlerror.HTTPError) and e.code in (400, 401, 403)
            remaining = None if deadline_monotonic is None else deadline_monotonic - time.monotonic()
            extend = not permanent and remaining is not None and remaining > _PREWARM_DEADLINE_RESERVE_SECONDS
            if i + 1 >= attempts and not extend:
                logger.warning("MCP runtime pre-warm exhausted %d attempts", i + 1)
                break
            if i + 1 >= attempts:
                logger.warning(
                    "MCP runtime pre-warm: transport error, retrying until the deadline (%.0f s left)", remaining
                )
            sleep_seconds = 5.0
            if remaining is not None:
                if remaining <= 0:
                    break
                sleep_seconds = min(sleep_seconds, remaining)
            time.sleep(sleep_seconds)
    return False


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_deployment_store() -> DeploymentStateStore:
    return DeploymentStateStore(
        table_name=_get_env("DEPLOYMENT_TABLE_NAME", "DeploymentState"),
        region=_get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")),
    )


def handler(event: dict, context) -> dict:
    deployment_id = event.get("deployment_id", "")

    try:
        store = _get_deployment_store()
        store.update_step(
            deployment_id,
            DeploymentStepName.MCP_SERVER,
            DeploymentStatusEnum.IN_PROGRESS,
        )

        mcp_server_config = event.get("mcp_server_config", {})
        region = event.get("target_region") or _get_env(
            "APP_AWS_REGION",
            _get_env("AWS_REGION", "us-east-1"),
        )
        platform_bucket = _get_env("ARTIFACTS_BUCKET_NAME", "")
        bucket = step_clients.artifacts_bucket_for_event(
            event,
            platform_bucket=platform_bucket,
        )

        mcp_name = mcp_server_config.get("name", "mcp-server")
        mcp_tools = mcp_server_config.get("tools", [])
        mcp_system_prompt = mcp_server_config.get("systemPrompt", "")

        logger.info("Deploying MCP Server Runtime: %s (tools=%s)", mcp_name, mcp_tools)

        # 1. Generate MCP server code
        mcp_code = generate_mcp_server_code(
            server_name=mcp_name,
            tools=mcp_tools if mcp_tools else None,
            system_prompt=mcp_system_prompt,
        )
        logger.info("Generated MCP server code (%d bytes)", len(mcp_code))

        # 2. Download deps bundle and upload code zip to S3.
        # Stable prefix keyed on runtime name (Bug 61) — see codegen_step
        # for rationale (AgentCore IAM cache is keyed on (role, S3 prefix)).
        primary_runtime_name = (
            event.get("agentcore_runtime_name")
            or event.get("friendly_runtime_name")
            or (event.get("config") or {}).get("name")
            or "agent_default"
        )
        mcp_s3_key = scoped_mcp_code_s3_key(
            primary_runtime_name,
            mcp_name,
        )
        if bucket:
            upload_s3 = step_clients.client(event, "s3")
            deps_s3 = boto3.client(
                "s3",
                region_name=_get_env(
                    "APP_AWS_REGION",
                    _get_env("AWS_REGION", "us-east-1"),
                ),
            )

            # Bug 171: use the LEAN mcp-only bundle (mcp + bedrock-agentcore +
            # boto3, NO strands/otel). The generated MCP server only imports
            # FastMCP, and the heavy strands-mcp.zip made the container cold-start
            # blow past the Gateway's 30s tool-discovery probe (target FAILED, 0
            # tools). The heavy strands-mcp bundle is deliberately NOT a fallback:
            # a model-free FastMCP server must never ship the strands/model stack,
            # and silently substituting it reintroduces the cold-start deadline
            # miss it was removed to fix. A missing lean bundle is a hard failure.
            _lean_key = "agentcore-deps/mcp-lean.zip"
            deps_bundle = None
            if platform_bucket:
                try:
                    resp = deps_s3.get_object(Bucket=platform_bucket, Key=_lean_key)
                    deps_bundle = resp["Body"].read()
                    logger.info("Downloaded MCP deps bundle %s (%d bytes)", _lean_key, len(deps_bundle))
                except Exception as e:  # noqa: BLE001
                    logger.warning("MCP deps bundle %s unavailable: %s", _lean_key, str(e)[:120])
            if not deps_bundle:
                raise RuntimeError(
                    "The platform artifacts bucket does not contain a readable "
                    "agentcore-deps/mcp-lean.zip. Deploying the generated MCP server "
                    "without the lean FastMCP bundle would either fail to import FastMCP "
                    "or force the heavy Strands bundle, which misses the Gateway's "
                    "tool-discovery deadline."
                )

            upload_code_to_s3(
                upload_s3,
                bucket,
                mcp_s3_key,
                mcp_code,
                "",
                "agent.py",
                deps_bundle=deps_bundle,
                expected_bucket_owner=(str(event["target_account_id"]) if event.get("target_account_id") else None),
                region=region,
                deployment_id=deployment_id,
            )
            logger.info("Uploaded MCP server code to s3://%s/%s", bucket, mcp_s3_key)
            # Same manifest gap as codegen_step's code.zip: the bundle was uploaded and
            # never recorded, so a failure in any later state of THIS step (the IAM role,
            # the Cognito pool, the runtime) left it behind. Recorded with the key that
            # actually ran — `sanitize_runtime_name` truncates, so a key reconstructed at
            # delete time would not necessarily match, and delete_object on a wrong key
            # succeeds while deleting nothing.
            object_row = {
                "type": "s3_object",
                "id": f"s3://{bucket}/{mcp_s3_key}",
                "region": region,
                "created_by_deployment": True,
            }
            if event.get("target_account_id"):
                object_row["account"] = str(event["target_account_id"])
            store.record_resource(deployment_id, object_row)
        else:
            raise RuntimeError("No deployment artifacts bucket resolved")

        # 3. Resolve the MCP runtime execution role. Cross-account deployments
        # use a pre-provisioned STABLE role — minting one and immediately passing
        # it to AgentCore reintroduces the IAM propagation failure target
        # onboarding prevents. A hosted MCP server is model-free, so it uses the
        # distinct, model-free AgentCoreFlowsMCPRuntimeRole, never the
        # model-capable Runtime role.
        if event.get("target_account_id"):
            from app.services.deploy_target import (
                DEFAULT_TARGET_MCP_RUNTIME_ROLE_NAME,
                target_execution_role_arn,
            )

            mcp_role_arn = target_execution_role_arn(
                str(event["target_account_id"]),
                role_arn=event.get("target_mcp_runtime_role_arn"),
                default_role_name=DEFAULT_TARGET_MCP_RUNTIME_ROLE_NAME,
            )
            mcp_role_name = mcp_role_arn.rsplit("/", 1)[-1]
            logger.info(
                "Cross-account MCP server: using pre-provisioned model-free target MCP runtime role %s",
                mcp_role_arn,
            )
        else:
            sts = step_clients.client(event, "sts")
            account_id = sts.get_caller_identity()["Account"]
            iam_client = step_clients.client(event, "iam")
            # Role name must start with "AgentCore" so the step Lambda's
            # iam:CreateRole resource scope (arn:aws:iam::*:role/AgentCore*)
            # in platform_stack.py matches. See tasks/lessons.md Bug 71.
            mcp_role_name = regional_iam_role_name(
                f"AgentCoreMCP-{sanitize_runtime_name(mcp_name)}",
                region,
            )
            mcp_role_result = create_runtime_iam_role(
                iam_client,
                mcp_role_name,
                account_id,
                region,
                [],  # MCP server doesn't need extra tool permissions
                return_provenance=True,
                # A protocol-only FastMCP server never invokes a model; its role
                # must be model-free (no bedrock:InvokeModel).
                model_free=True,
            )
            if isinstance(mcp_role_result, tuple):
                mcp_role_arn, mcp_role_created = mcp_role_result
            else:
                # An older/mocked helper that does not return provenance cannot
                # authorize teardown of an account-global IAM role.
                mcp_role_arn, mcp_role_created = mcp_role_result, False
            logger.info("Created MCP server IAM role: %s", mcp_role_arn)
            # Only per-deployment roles belong in teardown. The stable target
            # role is an onboarding prerequisite shared by many deployments.
            store.record_resource(
                deployment_id,
                {
                    "type": "iam_role",
                    "name": mcp_role_name,
                    "region": region,
                    "created_by_deployment": mcp_role_created,
                },
            )

        # 4. Create Cognito pool for gateway-to-MCP-server OAuth auth
        gateway_name = event.get("gateway_config", {}).get("name", "mcp-gw")
        cognito = step_clients.client(event, "cognito-idp")

        pool_name = f"AgentCore-mcp-{gateway_name}"
        resource_id = f"agentcore-mcp-{gateway_name}"
        scope_name = "invoke"

        # Tagged for the same reason the gateway pool is: cleanup.sh sweeps pools
        # by the "AgentCore" name prefix and then refuses to delete any whose owner
        # tag is not this stack's. An untagged AgentCore-mcp-* pool is matched by
        # that sweep but skipped as foreign on every pass, so it leaks forever.
        pool_resp = cognito.create_user_pool(
            PoolName=pool_name,
            AutoVerifiedAttributes=[],
            UsernameAttributes=["email"],
            UserPoolTags=governed_tags(region, event.get("resource_tags")),
            Policies={
                "PasswordPolicy": {
                    "MinimumLength": 8,
                    "RequireUppercase": True,
                    "RequireLowercase": True,
                    "RequireNumbers": True,
                    "RequireSymbols": False,
                }
            },
        )
        pool_id = pool_resp["UserPool"]["Id"]
        logger.info("Created MCP Cognito pool: %s", pool_id)
        # Manifest: record the MCP Cognito user pool for generic teardown.
        store.record_resource(
            deployment_id,
            {
                "type": "cognito_user_pool",
                "id": pool_id,
                "region": region,
                "created_by_deployment": True,
            },
        )

        try:
            cognito.create_resource_server(
                UserPoolId=pool_id,
                Identifier=resource_id,
                Name=f"AgentCore MCP {gateway_name}",
                Scopes=[{"ScopeName": scope_name, "ScopeDescription": "Invoke MCP server"}],
            )
        except Exception as e:
            logger.warning("MCP resource server creation: %s", e)

        domain = f"ac-mcp-{gateway_name}-{pool_id.split('_')[-1][:8]}".lower()
        domain = re.sub(r"[^a-z0-9-]", "-", domain)[:63]
        try:
            cognito.create_user_pool_domain(Domain=domain, UserPoolId=pool_id)
        except Exception as e:
            # Fail closed: without this domain the token endpoint below is dead and every
            # OAuth-backed target would later fail "cannot resolve hostname" for a reason that
            # was known right here.
            raise RuntimeError(f"MCP auth domain {domain} could not be created ({type(e).__name__})") from e
        _wait_for_cognito_domain(
            cognito,
            domain,
            region,
            deadline_monotonic=_lambda_bounded_readiness_deadline(context),
        )

        full_scope = f"{resource_id}/{scope_name}"
        client_resp = cognito.create_user_pool_client(
            UserPoolId=pool_id,
            ClientName=f"mcp-{gateway_name}-client",
            GenerateSecret=True,
            AllowedOAuthFlowsUserPoolClient=True,
            AllowedOAuthFlows=["client_credentials"],
            AllowedOAuthScopes=[full_scope],
            SupportedIdentityProviders=["COGNITO"],
        )
        mcp_client_id = client_resp["UserPoolClient"]["ClientId"]
        mcp_client_secret = client_resp["UserPoolClient"]["ClientSecret"]
        discovery_url = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/openid-configuration"
        token_endpoint = f"https://{domain}.auth.{region}.amazoncognito.com/oauth2/token"
        logger.info("Created MCP OAuth client: %s, scope: %s", mcp_client_id, full_scope)

        # The generated client secret is plaintext only inside this Lambda and
        # only long enough to pre-warm the runtime. Bind it immediately to this
        # deployment, then durably record the ARN before creating more resources.
        secrets_client = step_clients.client(event, "secretsmanager", region_name=region)
        with (
            secret_intent_journal(manifest_secret_journal(store, deployment_id, event.get("target_account_id"))),
            connector_identity_mode(event.get("identity_config")),
        ):
            mcp_client_secret_ref, _created = bind_connector_secret_for_deployment(
                region=region,
                owner_sub=event.get("owner_sub") or "",
                deployment_id=deployment_id,
                payload_key="clientSecret",
                raw_value=mcp_client_secret,
                secrets_client=secrets_client,
                resource_tags=event.get("resource_tags"),
            )
        secret_row = {
            "type": "secret",
            "id": mcp_client_secret_ref,
            "region": region,
            "created_by_deployment": True,
        }
        if event.get("target_account_id"):
            secret_row["account"] = event["target_account_id"]
        try:
            store.record_resource_strict(deployment_id, secret_row)
        except Exception:
            try:
                delete_deployment_bound_secret(
                    region=region,
                    deployment_id=deployment_id,
                    secret_ref=mcp_client_secret_ref,
                    secrets_client=secrets_client,
                )
            except ConnectorSecretDeletionRefused:
                logger.error("MCP OAuth credential rollback refused because exact ownership was not proven")
            except Exception as cleanup_exc:  # noqa: BLE001
                logger.error("MCP OAuth rollback failed: %s", type(cleanup_exc).__name__)
            raise
        recorded_secret_arns = list(event.get("recorded_secret_arns") or [])
        if mcp_client_secret_ref not in recorded_secret_arns:
            recorded_secret_arns.append(mcp_client_secret_ref)

        # 5. Create MCP server runtime (protocol=MCP) with JWT authorizer
        agentcore_ctrl = step_clients.client(event, "bedrock-agentcore-control")
        authorizer_config = {
            "customJWTAuthorizer": {
                "discoveryUrl": discovery_url,
                "allowedClients": [mcp_client_id],
            }
        }
        mcp_runtime_result = create_agent_runtime(
            agentcore_ctrl=agentcore_ctrl,
            runtime_name=sanitize_runtime_name(mcp_name),
            role_arn=mcp_role_arn,
            s3_bucket=bucket,
            s3_key=mcp_s3_key,
            entrypoint="agent.py",
            python_runtime="PYTHON_3_13",
            protocol="MCP",
            env_vars={"AWS_REGION": region},
            authorizer_config=authorizer_config,
            region=region,
            # The MCP server runtime bills exactly like the agent runtime, so it carries the
            # same governance tags. Omitting it here would have made cost attribution
            # depend on which node the user happened to put on the canvas.
            resource_tags=event.get("resource_tags") or {},
        )
        mcp_runtime_id = mcp_runtime_result["runtime_id"]
        logger.info("Created MCP server runtime: %s", mcp_runtime_id)
        # Manifest: record the MCP server runtime for generic teardown right
        # after create (the readiness wait below can be killed mid-poll).
        store.record_resource(
            deployment_id,
            {
                "type": "agent_runtime",
                "id": mcp_runtime_id,
                "region": region,
                "created_by_deployment": (mcp_runtime_result.get("created_by_deployment") is True),
            },
        )
        # The runtime manifest row must exist before governance can fail, so the
        # failure path can still delete the exact runtime it created.
        govern_default_runtime_log_group(
            step_clients.client(event, "logs", region_name=region),
            mcp_runtime_id,
        )

        # 6. All readiness phases share one Lambda-bounded absolute deadline.
        # Independent 300s + 180s + pre-warm retry budgets can otherwise exceed
        # this function's 600s timeout and let the orchestrator retry while the
        # original resource-creating invocation is still alive.
        readiness_deadline = _lambda_bounded_readiness_deadline(context)
        mcp_launch = wait_for_runtime_ready(
            agentcore_ctrl,
            mcp_runtime_id,
            timeout=300,
            deadline_monotonic=readiness_deadline,
        )
        if not mcp_launch.get("success"):
            raise RuntimeError(f"MCP Server Runtime failed to become ready: {mcp_launch.get('error', 'unknown')}")
        logger.info("MCP Server Runtime is READY: %s", mcp_runtime_id)

        # 6a. Gate on the DEFAULT endpoint too (Bug 166) — control-plane READY
        # does not mean the data-plane endpoint is invokable yet.
        # An adopted runtime was UPDATED: its DEFAULT endpoint is READY on the old version until
        # the new one goes live. Read the runtime's current version and wait for the endpoint to
        # serve exactly that, or the pre-warm below warms the wrong container.
        expected_version = None
        if mcp_runtime_result.get("created_by_deployment") is not True:
            try:
                expected_version = (
                    str(
                        agentcore_ctrl.get_agent_runtime(agentRuntimeId=mcp_runtime_id).get("agentRuntimeVersion") or ""
                    )
                    or None
                )
            except Exception as exc:  # noqa: BLE001 -- the wait below still gates on READY
                logger.warning("Could not read the adopted MCP runtime's version (%s)", type(exc).__name__)
            logger.warning(
                "MCP runtime %s was adopted; waiting for its endpoint to serve version %s",
                mcp_runtime_id,
                expected_version or "?",
            )
        ep = wait_for_default_endpoint_ready(
            agentcore_ctrl,
            mcp_runtime_id,
            timeout=180,
            deadline_monotonic=readiness_deadline,
            expected_version=expected_version,
        )
        if not ep.get("success"):
            raise RuntimeError(f"MCP DEFAULT endpoint failed to become ready: {ep.get('error', 'unknown')}")

        # 6b. PRE-WARM the MCP runtime (Bug 171). The Gateway's MCP target
        # discovery probe ("fetch tools") has a HARD 30s init ceiling on the
        # AgentCore side; a COLD MCP container (loading the strands-mcp bundle)
        # blows past it on first contact, so the target lands FAILED with
        # "Runtime initialization time exceeded ... 30s" and the gateway serves 0
        # tools. We can't change the 30s probe limit, so we warm the container
        # FIRST by sending a real MCP request, so the gateway's probe hits an
        # already-initialized runtime. A failed warm-up is a deployment failure:
        # reporting success here creates a gateway target known to have no
        # discoverable tools.
        mcp_server_runtime_arn = mcp_runtime_result.get("arn", "")
        try:
            prewarm_succeeded = _prewarm_mcp_runtime(
                region,
                mcp_server_runtime_arn,
                token_endpoint,
                mcp_client_id,
                mcp_client_secret,
                full_scope,
                deadline_monotonic=readiness_deadline,
            )
        finally:
            # Prevent the plaintext value from being reused in the returned event.
            del mcp_client_secret
        if not prewarm_succeeded:
            # Two different failures used to share one message. Exhausted attempts with time
            # to spare mean the runtime REJECTED the warm-up (authorizer, token, endpoint); only
            # an expired deadline is a deadline.
            if time.monotonic() >= readiness_deadline:
                raise RuntimeError("MCP runtime pre-warm did not succeed before the deployment deadline")
            raise RuntimeError(
                "MCP runtime pre-warm was rejected on every attempt; the step log carries each attempt's reason"
            )

        # 7. Return runtime ARN + OAuth credentials for gateway step
        logger.info("MCP Server Runtime ARN: %s", mcp_server_runtime_arn)

        return {
            **event,
            "mcp_server_runtime_arn": mcp_server_runtime_arn,
            "mcp_server_runtime_id": mcp_runtime_id,
            "recorded_secret_arns": recorded_secret_arns,
            "mcp_oauth": {
                "discovery_url": discovery_url,
                "client_id": mcp_client_id,
                "client_secret_ref": mcp_client_secret_ref,
                "scope": full_scope,
                "pool_id": pool_id,
                "token_endpoint": token_endpoint,
            },
        }

    except Exception:
        logger.exception("MCP Server step failed for deployment %s", deployment_id)
        raise
