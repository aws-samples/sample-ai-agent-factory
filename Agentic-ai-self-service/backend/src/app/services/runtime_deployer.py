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
import secrets
import time
import urllib.parse
import zipfile

import boto3

from app.services.aws_errors import is_error
from app.services.aws_pagination import list_all
from app.services.deletion_confirmation import DeletionFailedAfterAccept, wait_until_absent
from app.services.iam_boundary import create_role_kwargs, ensure_role_boundary
from app.services.naming import regional_iam_role_name
from app.services.resource_ownership import (
    ResourceDeletionRefused,
    assert_agentcore_resource_owned,
    assert_this_deployment_may_mutate,
    delete_owned_iam_role,
    owner_tags,
    resource_is_missing,
)
from app.services.resource_tagging import governed_tag_list, governed_tags

logger = logging.getLogger(__name__)

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07")
RUNTIME_LOG_GROUP_PREFIX = "/aws/bedrock-agentcore/runtimes/"
RUNTIME_LOG_RETENTION_DAYS = 30


def _list_all_agent_runtimes(agentcore_ctrl) -> list[dict]:
    return list_all(
        agentcore_ctrl,
        "list_agent_runtimes",
        item_keys=("agentRuntimes", "agentRuntimeSummaries"),
        request={"maxResults": 100},
    )


def _list_all_runtime_endpoints(agentcore_ctrl, runtime_id: str) -> list[dict]:
    return list_all(
        agentcore_ctrl,
        "list_agent_runtime_endpoints",
        item_keys=("runtimeEndpoints", "agentRuntimeEndpoints"),
        request={"agentRuntimeId": runtime_id, "maxResults": 100},
    )


def _list_all_online_evaluation_configs(agentcore_ctrl) -> list[dict]:
    return list_all(
        agentcore_ctrl,
        "list_online_evaluation_configs",
        item_keys=("onlineEvaluationConfigs", "items"),
        request={"maxResults": 50},
    )


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

    return sanitize_agentcore_name(name, style="underscore", prefix="agent", fallback="agent_default")


def default_runtime_log_group_name(runtime_id: str) -> str:
    """Return the CloudWatch group AgentCore uses for the DEFAULT endpoint.

    AgentCore creates this group outside our CloudFormation stack.  Validate the
    service-returned identifier before interpolating it into an AWS resource name
    so a malformed response cannot redirect a governance call to another group.
    """
    value = str(runtime_id or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("AgentCore returned an invalid runtime id for log governance")
    name = f"{RUNTIME_LOG_GROUP_PREFIX}{value}-DEFAULT"
    if len(name) > 512:
        raise ValueError("AgentCore runtime log group name exceeds the CloudWatch limit")
    return name


def govern_default_runtime_log_group(logs_client, runtime_id: str) -> str:
    """Create-or-adopt a runtime's DEFAULT log group and apply retention.

    The service-owned group otherwise has no retention policy, so conversation
    content is retained indefinitely.  This is intentionally fail-closed: once a
    runtime has been recorded in the deployment manifest, a deployment must not
    report success while its conversation log is unbounded.

    The group is not tagged here.  The step role is shared across deployments and
    the runtime id is not known when its IAM policy is synthesized; granting
    ``logs:TagResource`` on the whole AgentCore runtime prefix would let that role
    relabel unrelated runtime groups.  The deployment manifest remains the
    ownership authority, and teardown deliberately leaves these logs to expire.
    """
    log_group_name = default_runtime_log_group_name(runtime_id)
    try:
        logs_client.create_log_group(logGroupName=log_group_name)
    except Exception as exc:  # noqa: BLE001 -- narrowed to one AWS error code
        if not is_error(exc, "ResourceAlreadyExistsException"):
            raise
    logs_client.put_retention_policy(
        logGroupName=log_group_name,
        retentionInDays=RUNTIME_LOG_RETENTION_DAYS,
    )
    logger.info(
        "AgentCore runtime log group %s governed with %d-day retention",
        log_group_name,
        RUNTIME_LOG_RETENTION_DAYS,
    )
    return log_group_name


def _merge_deps_into_zip(
    target_zf: zipfile.ZipFile,
    bundle_bytes: bytes,
    seen: set[str] | None = None,
) -> None:
    """Extract dependency bundle contents into the target zip, excluding cache files.

    Reads *bundle_bytes* as an in-memory zip and copies every entry into
    *target_zf* **except** paths that contain ``__pycache__`` or end with
    ``.pyc``.  This keeps the final code zip free of stale bytecode that
    could conflict with the AgentCore Runtime's Python version.

    *seen* makes the merge idempotent across MORE THAN ONE bundle, which is what a
    provider-extras bundle needs: it is a delta on top of the base bundle and the two
    still share ``strands-agents``' own dist-info and any package pip pulled into
    both. ``zipfile`` does not reject a duplicate name — it appends a second member
    with the same path, warns, and leaves the reader to take whichever it finds — so
    without this the zip would carry two copies of every shared file and which one
    gets extracted would be an implementation detail of the unzip. Pass the same set
    to every call for one zip.

    Requirements: 4.4, 4.5, 5.5
    """
    if seen is None:
        seen = set()
    with zipfile.ZipFile(io.BytesIO(bundle_bytes), "r") as bundle_zf:
        for item in bundle_zf.namelist():
            if "__pycache__" in item or item.endswith(".pyc"):
                continue
            if item in seen:
                continue
            seen.add(item)
            data = bundle_zf.read(item)
            target_zf.writestr(item, data)


def _create_code_zip(
    agent_code: str,
    requirements_txt: str,
    entrypoint: str,
    deps_bundle: bytes | None = None,
    extra_bundles: list[bytes] | None = None,
) -> bytes:
    """Create in-memory zip with agent code and optionally bundled deps.

    If *deps_bundle* is provided its contents are merged into the zip root
    via ``_merge_deps_into_zip``, giving the AgentCore Runtime all
    dependencies without a ``pip install`` phase.

    *extra_bundles* are merged AFTER it, in order, sharing one dedupe set. They carry
    the model-provider SDK a non-Bedrock agent needs (``strands-agents[openai]``'s
    delta over plain ``strands-agents``, and so on). They are separate zips rather
    than a fatter base bundle because AgentCore gives a container 30 seconds to reach
    a listening state and every byte in this zip is paid for on every cold start —
    the same reason ``mcp-lean.zip`` exists. The base bundle wins any path collision,
    which is the order that keeps a provider bundle from downgrading ``strands`` itself.

    ``requirements.txt`` is only written when *requirements_txt* contains
    non-whitespace content. It normally is not: nothing pip-installs at container
    start, and per ARCC cnt_Vsqr5LAdJVd1Il third-party packages must be served from
    infrastructure we control rather than fetched from PyPI at deploy or run time,
    which is what these pre-built bundles are.

    Requirements: 5.1, 5.2, 5.3, 5.4
    """
    buf = io.BytesIO()
    seen: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(entrypoint, agent_code)
        seen.add(entrypoint)
        if requirements_txt.strip():
            zf.writestr("requirements.txt", requirements_txt)
            seen.add("requirements.txt")
        if deps_bundle:
            _merge_deps_into_zip(zf, deps_bundle, seen)
        for extra in extra_bundles or []:
            if extra:
                _merge_deps_into_zip(zf, extra, seen)
    buf.seek(0)
    return buf.read()


def upload_code_to_s3(
    s3_client,
    bucket: str,
    key: str,
    agent_code: str,
    requirements_txt: str,
    entrypoint: str = "agent.py",
    deps_bundle: bytes | None = None,
    extra_bundles: list[bytes] | None = None,
    expected_bucket_owner: str | None = None,
    region: str | None = None,
    deployment_id: str | None = None,
) -> str:
    """Upload agent code zip to S3, optionally with bundled dependencies.

    Returns the S3 URI.
    """
    zip_bytes = _create_code_zip(agent_code, requirements_txt, entrypoint, deps_bundle, extra_bundles)
    put_kwargs = {"Bucket": bucket, "Key": key, "Body": zip_bytes}
    if expected_bucket_owner:
        put_kwargs["ExpectedBucketOwner"] = expected_bucket_owner
    if deployment_id:
        put_kwargs["Tagging"] = urllib.parse.urlencode(
            owner_tags(
                region,
                extra={"DeploymentId": str(deployment_id)},
            )
        )
    s3_client.put_object(**put_kwargs)
    logger.info("Uploaded code to s3://%s/%s (%d bytes)", bucket, key, len(zip_bytes))
    return f"s3://{bucket}/{key}"


def client_secret_grant_targets(
    client_info: dict | None,
    region: str,
    account_id: str,
) -> tuple[str | None, str | None]:
    """``(user_pool_arn, client_secret_arn)`` for a gateway's ``client_info``.

    One helper because three deploy paths need the same two ARNs and a mismatch
    between them is invisible until an agent cannot mint a token: the per-agent role
    (iam_step), the legacy per-deploy role (iam_step) and the direct deploy
    (services/deployment.py).

    A ``client_secret_ref`` is a Secrets Manager NAME, not an ARN (see
    ``models/components.py``), and Secrets Manager appends a random six-character
    suffix to the ARN, so naming a secret by name in a policy requires the trailing
    wildcard. An ARN that was passed anyway is used as-is.

    THE TWO ARE MUTUALLY EXCLUSIVE, and the secret wins. Returning both would hand a
    per-agent role a ``cognito-idp:DescribeUserPoolClient`` grant it does not need,
    and that grant cannot be narrowed to the one client the agent owns: Cognito's IAM
    resource type is ``userpool``, so it authorizes reading the secret of every app
    client in the pool. In ``shared`` identity mode that is every deployed gateway's
    client. Since every gateway created after
    ``gateway_deployer._mint_client_secret_ref`` carries a reference, the pool ARN is
    returned only for a gateway that predates it, where it is the sole way the agent
    can resolve its secret at all. That is what makes ``per_agent`` mode genuinely
    isolating rather than isolating-except-for-the-credential.
    """
    ci = client_info or {}
    ref = ci.get("client_secret_ref") or ci.get("clientSecretRef") or ""
    if ref.startswith("arn:"):
        client_secret_arn: str | None = ref
    elif ref:
        client_secret_arn = f"arn:aws:secretsmanager:{region}:{account_id}:secret:{ref}-*"
    else:
        client_secret_arn = None
    if client_secret_arn:
        return None, client_secret_arn
    pool_id = ci.get("user_pool_id") or ""
    user_pool_arn = f"arn:aws:cognito-idp:{region}:{account_id}:userpool/{pool_id}" if pool_id else None
    return user_pool_arn, client_secret_arn


def _cfg_get(config, *names, default=None):
    """Read ``names`` off a config that may be a Pydantic model OR a plain dict.

    The SFN event carries the runtime config as a model in some steps and as the
    raw dict in others (``iam_step`` never constructs the model), so every helper
    that both paths share has to accept either.
    """
    for name in names:
        if isinstance(config, dict):
            if name in config and config[name] is not None:
                return config[name]
        else:
            value = getattr(config, name, None)
            if value is not None:
                return value
    return default


def _provider_name(value) -> str:
    """A provider's NAME, whether it arrived as a string or as a ``StrandsModelProvider``."""
    return str(getattr(value, "value", value) or "").strip()


def canvas_model_providers(config) -> list[str]:
    """Every model provider the GENERATED code will instantiate, parent first.

    Not just ``model_provider``: ``code_generator`` builds one model per sub-agent
    from ``multi_agent_config["agents"][*]["modelProvider"]``, defaulting to the
    parent's. So a Bedrock parent with one OpenAI sub-agent really does need a
    provider API key, and the gate that only looked at the parent silently denied it
    one — an agent that deploys green and whose sub-agent's first model call 401s.
    Mirrors ``RuntimeConfig._check_model_id``, which already walks the same list.

    Returns provider NAMES, never enum members. ``deployment_models.RuntimeConfig``
    declares ``model_provider`` as a ``str`` and ``components.RuntimeConfiguration``
    declares it as ``StrandsModelProvider``, and the two deploy paths pass one each. A
    plain ``str()`` over the second yields ``"StrandsModelProvider.OPENAI"`` — because
    ``StrandsModelProvider`` is a ``str, Enum`` and not a ``StrEnum``, so ``__str__`` is
    ``Enum``'s. Measured: that string matches no key in ``PROVIDER_STRANDS_EXTRA``, so
    the direct path selected no model-provider SDK bundle at all while
    ``needs_provider_api_key`` still said True — a green deploy, a granted key, and a
    container that cannot import. Normalize here rather than at each caller, because a
    caller that forgets gets silence, not an error.
    """
    parent = _provider_name(_cfg_get(config, "model_provider", "modelProvider", default=""))
    model_cfg = _cfg_get(config, "model", default=None)
    if not parent:
        parent = _provider_name(_cfg_get(model_cfg, "provider", default=""))
    parent = parent or "bedrock"

    providers = [parent]
    multi = _cfg_get(config, "multi_agent_config", "multiAgentConfig", default=None)
    if isinstance(multi, dict):
        for ag in multi.get("agents") or []:
            if isinstance(ag, dict):
                providers.append(_provider_name(ag.get("modelProvider")) or parent)
    return providers


def needs_provider_api_key(config) -> bool:
    """True when some model in this canvas is from a provider that takes an API key.

    Bedrock and SageMaker authenticate with the runtime's IAM role, and Ollama is
    local, so those providers need no key, must be handed no
    ``PROVIDER_API_KEY_SECRET_ARN`` and must be granted no read.
    """
    keyless = {"bedrock", "sagemaker", "ollama", ""}
    return any(str(provider).lower() not in keyless for provider in canvas_model_providers(config))


def runtime_key_grant_targets(config, gateway_result: dict | None) -> tuple[str | None, str | None]:
    """``(provider_key_secret_arn, gateway_key_secret_arn)`` for a runtime's role.

    The sibling of ``client_secret_grant_targets``, and for the same reason: neither
    the model provider's API key nor a LiteLLM gateway's virtual key travels as an
    environment variable any more (``GetAgentRuntime`` returns those verbatim, ARCC
    cnt_dAiE0OyXKvfeow), so the agent dereferences the ARN itself and the exec role
    must be able to read exactly those secrets and nothing else.

    This function is the single decision shared by the step that INJECTS the ARNs
    (``runtime_configure_step``) and the step that GRANTS them (``iam_step``). They
    must not each decide: a grant without an injection is a dead statement, but an
    injection without a grant is an agent that deploys green and raises
    AccessDeniedException on its first model call. Returns ``None`` for either side
    that has no secret, which is the correct answer for a Bedrock agent on an
    AgentCore gateway — it is granted nothing.

    Both ARNs arrive already constrained. On the Step Functions path,
    ``provider_api_key_ref`` is a deployment-bound ``agentcore-connector/`` copy of
    a live-validated ``agentcore-provider/`` source; the direct legacy path scopes
    its per-deploy role to the exact source ARN. A LiteLLM ``api_key_ref`` is either
    platform-minted or rejected by ``litellm_gateway_deployer``
    (``is_own_connector_secret``).
    """
    provider_arn = None
    if needs_provider_api_key(config):
        ref = _cfg_get(config, "provider_api_key_ref", "providerApiKeyRef", default="")
        provider_arn = str(ref) if ref else None

    client_info = (gateway_result or {}).get("client_info") or {}
    gateway_arn = None
    if str(client_info.get("provider") or "") == "litellm":
        ref = client_info.get("api_key_ref")
        gateway_arn = str(ref) if ref else None
    return provider_arn, gateway_arn


def create_runtime_iam_role(
    iam_client,
    role_name: str,
    account_id: str,
    region: str,
    connected_tools: list | None = None,
    otel_secret_arn: str | None = None,
    resource_tags: dict | None = None,
    user_pool_arn: str | None = None,
    client_secret_arn: str | None = None,
    provider_key_secret_arn: str | None = None,
    gateway_key_secret_arn: str | None = None,
    return_provenance: bool = False,
    model_free: bool = False,
) -> str | tuple[str, bool]:
    """Create or reuse an IAM execution role for an AgentCore runtime.

    ``resource_tags`` (Phase 2 governance tagging) are merged onto the role
    alongside the mandatory ManagedBy tag. Returns the role ARN.

    ``user_pool_arn`` / ``client_secret_arn`` grant the ONE read the agent needs to
    resolve its gateway OAuth client secret at the moment of use, because no deploy
    path injects that secret as an environment variable any more (GetAgentRuntime
    returns those in plaintext). Omit both and nothing is granted — an agent with no
    gateway must not be able to read either.

    ``provider_key_secret_arn`` / ``gateway_key_secret_arn`` are the same contract for
    the model provider's API key and a LiteLLM gateway's virtual key, which stopped
    being injected as plaintext ``PROVIDER_API_KEY`` / ``GATEWAY_API_KEY`` env vars for
    exactly that reason. Each is scoped to its one ARN; omit them and nothing is
    granted. Kept in sync with ``per_agent_identity.build_scoped_runtime_policy`` and
    the CDK shared role (``infra/stacks/platform/lambdas.build_shared_runtime_role``,
    which can only grant the two namespaces because it has no per-deploy ARN).
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
    # Also carries AgentCoreStack={project}-{env}-{region}: ManagedBy identifies
    # the PRODUCT, so two deployments of it in one account are indistinguishable,
    # and IAM role names are account-global. cleanup.sh gates role deletion on
    # the stack tag — see services/resource_ownership.py.
    # P0-B: via governed_tag_list, not owner_tag_list(extra=...), so the governance set is
    # VALIDATED before it reaches IAM. It was not: a tag key designating credential material
    # was written verbatim onto this role, while the CloudFormation export refused the very
    # same tag set. One validation, both paths -- see services/resource_tagging.
    _managed_tag = governed_tag_list(region, resource_tags)
    try:
        resp = iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description=f"Execution role for AgentCore runtime {role_name}",
            Tags=_managed_tag,
            **create_role_kwargs(),
        )
        role_arn = resp["Role"]["Arn"]
        role_created = True
        logger.info("Created runtime IAM role: %s", role_arn)
    except iam_client.exceptions.EntityAlreadyExistsException:
        # Prove the existing role is this deployment's before tagging it or
        # overwriting its inline policy below. `get_role` returns `Role.Tags`, so the
        # check reuses a call and a grant that were already here. See
        # `resource_ownership.can_this_deployment_mutate` for why the CDK
        # Project/Environment pair counts as proof: the platform's own shared runtime
        # role carries those and no AgentCoreStack tag.
        _existing = iam_client.get_role(RoleName=role_name)["Role"]
        assert_this_deployment_may_mutate(f"IAM role {role_name}", _existing.get("Tags"), region)
        role_arn = _existing["Arn"]
        role_created = False
        logger.info("Reusing existing runtime IAM role: %s", role_arn)
        # F-06: retrofit the permissions boundary once ownership is proven, before the
        # inline policy below replaces whatever the role carried.
        ensure_role_boundary(iam_client, role_name, role=_existing)
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

    # A protocol-only FastMCP server never invokes a model, so its execution role
    # must NOT carry bedrock model access. Reusing the model-capable statement for
    # a tool-only runtime is the model-free contract violation the dedicated MCP
    # role guards against.
    _model_access_statements = (
        []
        if model_free
        else [
            {
                "Sid": "BedrockModelAccess",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                "Resource": "*",
            }
        ]
    )
    core_policy = {
        "Version": "2012-10-17",
        "Statement": [
            *_model_access_statements,
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
                        # RetrieveMemoryRecords, not RetrieveMemories, and there is no
                        # GetLastKTurns action at all -- turn history is read through
                        # ListEvents. Both were inert. See the AgentCore IAM prefix
                        # note in services/per_agent_identity.py.
                        "bedrock-agentcore:RetrieveMemoryRecords",
                        "bedrock-agentcore:ListSessions",
                        "bedrock-agentcore:ListActors",
                        "bedrock-agentcore:ListEvents",
                        "bedrock-agentcore:GetMemory",
                        "bedrock-agentcore:ListMemories",
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
                        "bedrock-agentcore:CreateOnlineEvaluationConfig",
                        "bedrock-agentcore:GetOnlineEvaluationConfig",
                        "bedrock-agentcore:ListOnlineEvaluationConfigs",
                        "bedrock-agentcore:ListEvaluators",
                        "bedrock-agentcore:GetEvaluator",
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
                        "bedrock-agentcore:CreatePolicyEngine",
                        "bedrock-agentcore:GetPolicyEngine",
                        "bedrock-agentcore:ListPolicyEngines",
                        "bedrock-agentcore:CreatePolicy",
                        "bedrock-agentcore:GetPolicy",
                        "bedrock-agentcore:ListPolicies",
                        "bedrock-agentcore:UpdateGateway",
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

    # The gateway's OAuth client secret, resolved at the moment of use rather than
    # injected as an env var. Exactly one of these two is ever set: a Cognito pool
    # the platform created, or an external IDP's secret. ARCC cnt_LuG2TKuO0errRp.
    if user_pool_arn:
        core_policy["Statement"].append(
            {
                "Sid": "GatewayClientSecretFromUserPool",
                "Effect": "Allow",
                "Action": ["cognito-idp:DescribeUserPoolClient"],
                "Resource": [user_pool_arn],
            }
        )
    if client_secret_arn:
        core_policy["Statement"].append(
            {
                "Sid": "ExternalIdpClientSecret",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": [client_secret_arn],
            }
        )

    # The model provider's API key and a LiteLLM gateway's virtual key, both read by
    # the agent at the moment of use because neither travels as an environment
    # variable any more (GetAgentRuntime returns those in plaintext). One statement
    # per secret, scoped to the one ARN. Omitted -> nothing granted, which is correct
    # for a Bedrock agent with an AgentCore gateway. Bug 9 reminder above applies:
    # keep in sync with the SFN path and the CDK shared role.
    if provider_key_secret_arn:
        core_policy["Statement"].append(
            {
                "Sid": "ModelProviderApiKey",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": [provider_key_secret_arn],
            }
        )
    if gateway_key_secret_arn:
        core_policy["Statement"].append(
            {
                "Sid": "LiteLLMGatewayVirtualKey",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": [gateway_key_secret_arn],
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
    if return_provenance:
        return role_arn, role_created
    return role_arn


def _build_network_configuration(vpc_config: dict | None) -> dict:
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
    env_vars: dict | None = None,
    authorizer_config: dict | None = None,
    vpc_config: dict | None = None,
    region: str | None = None,
    resource_tags: dict | None = None,
) -> dict:
    """Create an AgentCore runtime using the boto3 control API.

    ``resource_tags`` (P0-B governance) are merged onto the runtime itself. Before this they
    reached only ``create_runtime_iam_role`` -- the runtime's EXEC ROLE -- which is the one
    resource in a deployment that cannot appear in a cost report, while the runtime, the thing
    that actually bills, carried ownership tags only. Measured live on ``acfe2e-p0920``: a
    governed deploy produced a runtime tagged ``{ManagedBy, AgentCoreStack}`` and nothing else,
    with the three platform tags sitting in the deployment record AND in every Step Functions
    state input. The parameter simply did not exist, so no call site could notice it was absent.

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
        # governed_tags validates the set before the create call: an illegal character, an
        # over-long key, a reserved ``aws:`` prefix or a key designating credential material
        # raises here rather than being rejected by AgentCore partway through the deploy.
        "tags": governed_tags(region, resource_tags),
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
            "Moved Permanently",  # S3 region cache miss
            "Status Code: 301",  # S3 region cache miss
        )
        last_err = None
        attempts = 8
        for attempt in range(attempts):
            try:
                return agentcore_ctrl.create_agent_runtime(**create_params)
            except Exception as e:
                err_str = str(e)
                if is_error(e, "ValidationException") and any(m in err_str for m in retryable_markers):
                    last_err = e
                    logger.info(
                        "create_agent_runtime transient (attempt %d/%d): %s",
                        attempt + 1,
                        attempts,
                        err_str[:200],
                    )
                    time.sleep(5)
                    continue
                raise
        raise last_err if last_err else RuntimeError("create_agent_runtime failed")

    try:
        resp = _create_with_transient_retry()
    except Exception as e:
        # "already exists" fallback kept: conflicts can surface as a
        # ValidationException whose message says "already exists".
        if is_error(e, "ConflictException") or "already exists" in str(e):
            # Find existing runtime by paginating through all runtimes
            logger.info("Runtime '%s' already exists, searching to update...", runtime_name)
            found_id = None
            found_arn = ""
            for rt in _list_all_agent_runtimes(agentcore_ctrl):
                if rt.get("agentRuntimeName") == runtime_name:
                    found_id = rt.get("agentRuntimeId", "")
                    found_arn = rt.get("agentRuntimeArn", "")
                    break

            if found_id:
                logger.info("Found existing runtime: %s, updating...", found_id)
                # A name collision is not permission to repoint another
                # installation's runtime at our role, code bundle, environment,
                # or authorizer. Re-read the exact live owner tag before the
                # update; list results are inventory only.
                assert_agentcore_resource_owned(
                    agentcore_ctrl,
                    "agent_runtime",
                    found_id,
                    region,
                )
                if not found_arn:
                    raise RuntimeError(
                        f"runtime '{runtime_name}' was found by name (id {found_id}) but the "
                        "list response carried no agentRuntimeArn, so it can be neither tagged "
                        "nor returned to the manifest. Refusing rather than deploying a runtime "
                        "this deployment cannot address afterwards."
                    ) from e
                # Re-stamp the governance tags on the ADOPTED runtime.
                #
                # ``update_agent_runtime`` has no ``tags`` parameter, so every conflict
                # recovery silently dropped ``create_params["tags"]`` -- the adopted runtime
                # kept whatever set the deploy that FIRST created it sent. The visible effect
                # is the one this workstream exists to close: an admin changes a tag policy,
                # redeploys, the deploy reports success, and the billing resource is unchanged.
                # A redeploy is the only way an existing runtime's tags can ever be corrected,
                # so this was also the only path by which a stale ABAC tag could be fixed.
                #
                # The FULL governed set is sent, not just the governance keys, and that is
                # what makes the existing IAM grant sufficient with no policy change: the step
                # roles' ``bedrock-agentcore:TagResource`` statement pins
                # ``aws:RequestTag/ManagedBy`` and ``aws:RequestTag/AgentCoreStack`` with
                # StringEquals, and a StringEquals on an ABSENT request tag does not match --
                # a request carrying only ``platform:*``/``org:*`` keys would be denied on the
                # missing pinned keys. Passing ``create_params["tags"]`` verbatim also keeps
                # the two paths from drifting: the create and the retag cannot disagree about
                # what this deployment stamps.
                #
                # BEFORE the update, deliberately. Tagging is the call that can be denied
                # (the grant is conditioned and the key namespace is checked), so doing it
                # first means a denial fails the deploy having mutated nothing. Reversed, a
                # denial would leave the new code live under the PREVIOUS deploy's ABAC tags,
                # which is the worse of the two states.
                #
                # Raised rather than logged, for symmetry with the create path -- where a tag
                # denial fails the create outright on the principle that refusing to create an
                # untagged resource beats creating one quietly -- and because ARCC
                # cnt_SaTYaDCgBBJTcv treats a stack that cannot tag what it manages as a
                # security failure rather than a reporting one (cnt_6gBImtb08AJqCB: tags carry
                # ABAC decisions). A swallowed failure here would be invisible, which is
                # exactly how the dropped-tags defect above survived.
                #
                # KNOWN LIMIT, stated because it is bounded and not fixed here: tag_resource
                # is ADDITIVE. A key REMOVED from the tag policy stays on an adopted runtime.
                # Reconciling removals needs ListTagsForResource plus UntagResource, and an
                # UntagResource grant on a wildcard resource is precisely the "strip another
                # deployment's ownership" escalation the grant's own comment refuses. The ARN
                # is not mutated (ARCC cnt_4uCsExIwIeSUub) -- only tags are added.
                try:
                    agentcore_ctrl.tag_resource(resourceArn=found_arn, tags=create_params["tags"])
                except Exception as tag_err:
                    raise RuntimeError(
                        "An owned runtime with this name already exists, but this "
                        "deployment's governance tags could not be applied to it "
                        f"({type(tag_err).__name__}). Nothing was changed: the runtime still "
                        "serves its previous code and configuration. Continuing would deploy "
                        "new code onto a resource carrying another deployment's cost and "
                        "access-control tags."
                    ) from tag_err
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
                    # UpdateAgentRuntime REPLACES the runtime's configuration: an authorizer
                    # left out of the update is removed, not kept. Measured live (2026-09-28):
                    # a redeploy of the customJWTAuthorizer MCP server runtime came back with
                    # authorizerConfiguration = null, accepted plain SigV4, and rejected the
                    # bearer token the gateway target and the pre-warm authenticate with, so
                    # every pre-warm attempt failed and the deployment died. The update must
                    # carry exactly what the create would.
                    if authorizer_config:
                        update_params["authorizerConfiguration"] = authorizer_config
                    agentcore_ctrl.update_agent_runtime(**update_params)
                except Exception as update_err:
                    raise RuntimeError(
                        "An owned runtime with this name already exists, but it "
                        "could not be updated to the requested code and "
                        f"configuration ({type(update_err).__name__}). Returning "
                        "the previous runtime would silently deploy stale code."
                    ) from update_err
                return {
                    "runtime_id": found_id,
                    "arn": found_arn,
                    "status": "UPDATING",
                    # A conflict recovery is adoption, not creation.  Manifest
                    # writers carry this bit into teardown so a failed redeploy
                    # cannot delete the runtime that was already serving.
                    "created_by_deployment": False,
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
        "created_by_deployment": True,
    }


def _bounded_poll_deadline(
    timeout: int | float,
    deadline_monotonic: float | None,
) -> tuple[float, float]:
    """Return the phase start and the earliest applicable monotonic deadline."""
    start = time.monotonic()
    phase_deadline = start + max(float(timeout), 0.0)
    if deadline_monotonic is not None:
        phase_deadline = min(phase_deadline, float(deadline_monotonic))
    return start, phase_deadline


def wait_for_runtime_ready(
    agentcore_ctrl,
    runtime_id: str,
    timeout: int = 600,
    *,
    deadline_monotonic: float | None = None,
) -> dict:
    """Poll until runtime is READY/ACTIVE or the phase/shared deadline."""
    start, deadline = _bounded_poll_deadline(timeout, deadline_monotonic)
    while time.monotonic() < deadline:
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

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(15.0, remaining))

    budget = max(0.0, deadline - start)
    return {
        "success": False,
        "runtime_id": runtime_id,
        "error": f"Runtime did not become READY within its {budget:g}s readiness budget",
    }


def wait_for_default_endpoint_ready(
    agentcore_ctrl,
    runtime_id: str,
    timeout: int = 180,
    *,
    deadline_monotonic: float | None = None,
    expected_version: str | None = None,
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

    ``expected_version``: after an ADOPT-UPDATE of an existing runtime the DEFAULT
    endpoint is already READY -- on the previous version -- and moves through
    UPDATING to the new ``liveVersion`` later. READY alone would let the pre-warm
    warm the old container while the gateway's 30 s discovery probe then meets the
    new one cold (redeploy audit 2026-09-28, row 1). When a version is given, READY
    counts only once ``liveVersion`` equals it and no ``targetVersion`` is pending.
    """
    start, deadline = _bounded_poll_deadline(timeout, deadline_monotonic)
    last_seen = ""
    last_live = ""
    while time.monotonic() < deadline:
        try:
            eps = _list_all_runtime_endpoints(agentcore_ctrl, runtime_id)
            for ep in eps:
                if ep.get("name") == "DEFAULT":
                    last_seen = ep.get("status", "")
                    if last_seen == "READY":
                        live = str(ep.get("liveVersion") or "")
                        pending = str(ep.get("targetVersion") or "")
                        if expected_version is not None and (live != str(expected_version) or pending):
                            if live != last_live:
                                logger.warning(
                                    "DEFAULT endpoint of %s is READY on version %s (pending %s); waiting for version %s",
                                    runtime_id,
                                    live or "?",
                                    pending or "none",
                                    expected_version,
                                )
                                last_live = live
                            break
                        return {
                            "success": True,
                            "endpoint_arn": ep.get("agentRuntimeEndpointArn", ""),
                            "status": "READY",
                            "live_version": live,
                        }
                    if "FAILED" in last_seen:
                        return {
                            "success": False,
                            "status": last_seen,
                            "error": f"DEFAULT endpoint entered {last_seen}",
                        }
        except Exception:  # noqa: BLE001 — transient list errors are retried
            logger.warning("Error listing endpoints for runtime %s (will retry)", runtime_id)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(5.0, remaining))

    budget = max(0.0, deadline - start)
    version_note = (
        f" on version {expected_version} (last live version {last_live or 'unknown'})"
        if expected_version is not None
        else ""
    )
    return {
        "success": False,
        "status": last_seen or "ABSENT",
        "error": (
            f"DEFAULT endpoint for runtime {runtime_id} did not become READY{version_note} "
            f"within its {budget:g}s readiness budget "
            f"(last status: {last_seen or 'not listed'})"
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
        for rt in _list_all_agent_runtimes(agentcore_ctrl):
            if rt.get("agentRuntimeName") == identifier:
                return rt.get("agentRuntimeId", identifier)
    except Exception as e:
        logger.warning("Could not resolve runtime name %s to id: %s", identifier, e)
    return identifier  # fall through; caller will see ResourceNotFound and treat as no-op


#: ``AgentRuntimeId`` and ``AgentRuntimeArn``, transcribed from the installed
#: ``bedrock-agentcore-control`` service model rather than guessed from examples. The service model
#: is the authority on what AgentCore can mint, so anything outside these is not an identifier this
#: platform could have recorded -- and the only use of these patterns is to decide whether a target
#: is READABLE at all, so a loose pattern turns an arbitrary string into positive proof.
#:
#: Note the name bound differs between the two on purpose: the id allows 100 name characters and the
#: ARN allows 48. Both are copied as-is; narrowing either one to "the stricter of the two" would
#: invent a rule the service does not have.
_AGENT_RUNTIME_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,99}-[a-zA-Z0-9]{10}$")
_AGENT_RUNTIME_ARN_RE = re.compile(
    r"^arn:aws(-[^:]+)?:bedrock-agentcore:[a-z0-9-]+:[0-9]{12}"
    r":runtime/(?P<runtime_id>[a-zA-Z][a-zA-Z0-9_]{0,47}-[a-zA-Z0-9]{10})"
    r"(?:/runtime-endpoint/[a-zA-Z][a-zA-Z0-9_]{0,47})?$"
)


def _runtime_id_from_target(target: str) -> str | None:
    """The AgentCore runtime id a trigger target NAMES, or None when the target is unreadable.

    Parsing is total and structural. Returning None is the load-bearing case: it is what lets the
    caller say "unknown" instead of guessing, and every shape that is not one a supported producer
    emits lands there. A peer session measured the cost of being loose about the ARN shape:
    ``arn:aws:s3:us-east-1:<acct>:runtime/<our-id>`` matched, because only the resource segment was
    checked, so an object key in an unrelated service's ARN read as a positive proof of ownership
    and authorized deleting that row's schedules. The service segment is checked here for that
    reason, not for tidiness.

    The three accepted shapes are the three a supported producer writes: ``runtime/<id>`` (the
    version's ``runtime_arn``), ``runtime/<id>/runtime-endpoint/<name>`` (the endpoint ARN an invoke
    path records), and a bare runtime id, which is the fallback in
    ``routers/triggers._resolve_owned_runtime`` when no ARN was recorded yet. Matching is against the
    service model's full patterns, anchored at both ends, so a truncated resource, an extra nested
    resource under the runtime, a wrong partition, an absent region or account, or a name shape
    AgentCore cannot mint are all None rather than a partial read.

    Region and account are required to be PRESENT and well-formed but are not compared against this
    deployment's own: the caller already holds a row from its own table partition and compares the
    returned id against its own canonical id, whose 10-character tail is minted per runtime, and
    demanding a region match would misclassify a legitimately recorded cross-region ARN as unknown
    and strand the row. Requiring the segments to exist is a different thing from comparing them --
    ``arn:aws:bedrock-agentcore:::runtime/<id>`` is not an ARN any producer emits, so reading an id
    out of it is reading an id out of a string somebody made up.
    """
    matched = _AGENT_RUNTIME_ARN_RE.match(target)
    if matched:
        return matched.group("runtime_id")
    if target.startswith("arn:"):
        return None  # an ARN that is not an AgentCore runtime ARN names no runtime id
    return target if _AGENT_RUNTIME_ID_RE.match(target) else None


def _classify_trigger_target(target_runtime_arn: str, canonical_id: str) -> str:
    """``"ours"`` / ``"foreign"`` / ``"unknown"`` for a trigger row's server-derived target.

    Three outcomes, not two, and the third is the point. ``"foreign"`` is a POSITIVE finding -- a
    well-formed target naming a different runtime -- and it lets the teardown release the friendly
    name over somebody else's row, which it must, or a name two tenants collide on could never be
    released. ``"unknown"`` is an absent, malformed or unreadable target: it may be a legacy row of
    ours, so it is neither deleted nor dismissed, and it keeps the name locked. Absence is never
    agreement; treating it as agreement in either direction is how a delete or a release reaches
    something nobody can attribute.

    A bool cannot carry this: with two outcomes, "not ours" has to stand in for both "provably
    somebody else's" and "I cannot tell", and whichever way that is spent is wrong for the other
    half -- either a malformed target authorizes releasing the name, or a colliding name can never
    be released at all.

    Exact equality against our own canonical id is accepted without the id grammar. The grammar
    exists to tell a different runtime's id from an arbitrary string, and a value equal to the id
    we are destroying needs no such test; requiring it would make a legacy or adopted runtime whose
    id does not fit the pattern permanently unresolvable, which keeps its name locked forever.
    """
    target = str(target_runtime_arn or "").strip()
    canonical = str(canonical_id or "").strip()
    # No id to compare against means nothing can be attributed, in EITHER direction: without this
    # every well-formed row would read as foreign and the teardown would release the name.
    if not target or not canonical:
        return "unknown"
    if target == canonical:
        return "ours"
    named = _runtime_id_from_target(target)
    if named is None:
        return "unknown"
    return "ours" if named == canonical else "foreign"


def _trigger_targets_runtime(target_runtime_arn: str, canonical_id: str) -> bool:
    """True when a trigger row's server-derived target names EXACTLY this runtime.

    F-81f — the per-row authorization. The friendly ``runtime_name`` selects a TriggersTable
    partition that is NOT owner-scoped, so even a proven name only says "this is the partition my
    deployment writes to"; it does not say that every row in it is mine. Two tenants that collide
    on a friendly name share the partition, and the old code deleted every row it found there.

    ``target_runtime_arn`` is derived server-side by ``routers/triggers._resolve_owned_runtime``
    (never from a request body) and is either the version's runtime ARN or, when no ARN was
    recorded yet, the canonical runtime id. So it is the row's own statement of which runtime it
    fires at, and matching it against the id being destroyed is a proof that survives the runtime
    itself being gone.

    This is the delete authorization specifically, so it must collapse "unknown" into False rather
    than restate the parse: it is the classifier's ``"ours"`` and nothing else.
    """
    return _classify_trigger_target(target_runtime_arn, canonical_id) == "ours"


def _resolve_runtime_name_for_cleanup(
    canonical_id: str,
    platform_region: str,
    live_agentcore_name: str | None = None,
) -> str | None:
    """Map an AgentCore canonical runtime id back to the friendly runtime_name.

    The TriggersTable is keyed by the human-friendly ``runtime_name`` (e.g.
    ``my_agent``), but ``destroy_runtime`` only has the canonical AgentCore id
    (``<agentcore_runtime_name>-<10hash>``). The deployer records the
    runtime_name<->runtime_id / agentcore_runtime_name mapping in the
    AgentVersions table, so scan there for a matching row.

    Returns the friendly name ONLY when the scan proves ONE unambiguous mapping, and None
    otherwise. Never raises; the caller's cleanup is best-effort.

    "Unambiguous" is three separate refusals, because this name selects a TriggersTable partition
    that is not owner-scoped:

    * ``runtime_id`` is the stronger match and wins outright. ``agentcore_runtime_name`` is only
      consulted when no row matched on the id, since it is a NAME (``friendly[:39]_<8hex>``) and
      two tenants can collide on it after truncation.
    * that second tier needs ``live_agentcore_name`` -- the ``agentRuntimeName`` off the
      ownership-proven ``get_agent_runtime`` response -- because the canonical id is
      ``<agentcore_runtime_name>-<10hash>`` and therefore NEVER equals a stored
      ``agentcore_runtime_name``. Comparing the id against that column, as this function used to,
      made the whole tier dead code for every real id. Reported by the Codex audit session.
    * two rows matching the same id with DIFFERENT friendly names is corrupt data, not a tie to
      break -- the old code returned whichever the scan happened to reach first.
    * a scan that hits the page cap with pages left is INCONCLUSIVE, not a miss: uniqueness cannot
      be claimed over rows that were never read.

    There used to be a fallback here: strip the canonical 10-char hash and return that. It ran
    both on a scan miss and inside the ``except``, so a throttle or an expired credential produced
    a name too. That value is unproven, and the caller does not merely look things up with it --
    it enumerates ``TriggersTable`` by that name, a partition that is NOT owner-scoped, and
    deletes EventBridge Scheduler schedules, EventBridge rules and targets, Lambda function-URL
    configs and webhook secrets from whatever it finds. It was also usually wrong: the strip
    leaves the version suffix on (``myagent_1a2b3c4d``), which equals the friendly name only in
    the legacy single-version path -- so the realistic outcomes were "delete nothing" or "delete
    the triggers of a tenant whose friendly name happens to be that string".

    So: no guess. A None here leaks schedules and rules, which costs money and is visible and
    fixable; the other direction silently deletes another tenant's triggers, which is not.
    """
    if not canonical_id:
        return None
    try:
        # The versions store keys rows by friendly runtime_name and stamps each
        # with runtime_id / agentcore_runtime_name. We only have the AgentCore
        # id here, so do a bounded scan of the AgentVersions table and match on
        # either field. The table is tenant-small in practice; cap pages.
        table_name = os.environ.get("AGENT_VERSIONS_TABLE_NAME", "AgentVersions")
        # AgentVersions is platform control-plane metadata. A runtime may live
        # in another account/region, but scanning that target region looks for a
        # table that is not there (and can use the wrong credentials entirely).
        table = boto3.resource("dynamodb", region_name=platform_region).Table(table_name)
        scan_kwargs: dict = {
            "ProjectionExpression": ("runtime_name, runtime_id, agentcore_runtime_name"),
            # STRONGLY consistent, on every page. An eventually consistent scan cannot prove
            # uniqueness: it can miss the row that maps this id -- turning a resolvable name into a
            # leak -- and worse, it can miss the SECOND, conflicting row and so report a unique
            # mapping that is not one. The result authorizes non-owner-scoped deletes, so the read
            # that produces it has to be the strong one. (Reported by the Codex audit session.)
            "ConsistentRead": True,
        }
        by_runtime_id: set[str] = set()
        by_agentcore_name: set[str] = set()
        truncated = False
        for _ in range(20):  # cap at 20 pages of scan
            resp = table.scan(**scan_kwargs)
            for item in resp.get("Items", []):
                name = item.get("runtime_name")
                if not name:
                    continue
                if item.get("runtime_id") == canonical_id:
                    by_runtime_id.add(str(name))
                elif live_agentcore_name and item.get("agentcore_runtime_name") == live_agentcore_name:
                    by_agentcore_name.add(str(name))
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_kwargs["ExclusiveStartKey"] = last_key
        else:
            # The loop ran all 20 pages without breaking, so ``last_key`` is still set and there
            # are rows nobody read. Do NOT return a match found on page 1: the thing being proven
            # is that exactly one friendly name maps to this id.
            truncated = True
        matches = by_runtime_id or by_agentcore_name
        if len(matches) == 1 and not truncated:
            return next(iter(matches))
        if truncated:
            logger.warning(
                "AgentVersions scan for %s hit the page cap; cannot prove a unique friendly "
                "runtime name, so skipping name-keyed trigger cleanup",
                canonical_id,
            )
        elif matches:
            logger.warning(
                "AgentVersions maps %s to %d different friendly runtime names; "
                "skipping name-keyed trigger cleanup rather than picking one",
                canonical_id,
                len(matches),
            )
        else:
            logger.warning(
                "No AgentVersions row maps %s to a friendly runtime name; "
                "skipping name-keyed trigger cleanup rather than guessing one",
                canonical_id,
            )
    except Exception as e:
        # Log the TYPE, not the botocore message: that message echoes the request, including
        # the table name and the caller-supplied id.
        logger.warning(
            "Could not resolve runtime_name for %s via versions store (%s); "
            "skipping name-keyed trigger cleanup rather than guessing one",
            canonical_id,
            type(e).__name__,
        )
    return None


def destroy_runtime(
    runtime_id: str,
    region: str,
    *,
    client_factory=None,
    delete_execution_role: bool = True,
    runtime_name: str | None = None,
    platform_client_factory=None,
    confirmation_attempts: int = 36,
    confirmation_interval: float = 5.0,
    confirmation_deadline: float | None = None,
) -> dict:
    """Delete an AgentCore runtime AND its execution role, confirming the first before the second.

    Order of operations matters: capture roleArn via get-agent-runtime BEFORE
    delete-agent-runtime — after deletion the get fails and the role is orphaned
    (verified live 2026-05-16 — the API DELETE path leaked roles before this
    fix; see tasks/lessons.md Bug 25 / Bug 27 — drift between cleanup.sh and
    runtime_deployer.destroy_runtime).

    The `runtime_id` arg may be either the canonical agentRuntimeId or the
    friendly agentRuntimeName — we resolve it (Bug 50).

    ``runtime_name`` is a CROSS-CHECK, not a source. Name-keyed trigger cleanup always resolves the
    friendly name from metadata (canonical id + the ownership-proven live AgentCore name); passing a
    name that disagrees with that mapping cancels the cleanup instead of selecting a partition.
    Passing None is the normal case.

    F-08: ``delete_agent_runtime`` returns an ACCEPTANCE. The runtime sits in ``DELETING`` and can
    end in ``DELETE_FAILED`` (the same accepted-then-failed shape measured for DeleteGateway at
    ``status_update_step._confirm_gateway_deleted``). This used to take the 200 as "gone", delete
    the execution role under a runtime that still existed, and return ``success: True`` -- which
    the teardown wrote as a terminal ``deleted`` row nothing ever retries. So, like
    ``destroy_harness`` and ``delete_memory_confirmed``, the delete is now polled with
    ``get_agent_runtime`` until not-found: a terminal ``*FAILED`` is ``success: False`` with the
    service's reason; an exhausted budget or an unreadable state is ``retained: True`` (unknown,
    never claimed either way); and only a confirmed absence goes on to the role and the sidecars.
    ``confirmation_deadline`` is the caller's absolute ``time.monotonic()`` budget (the async
    teardown shares one across every resource); the attempt count bounds an inline call.

    Idempotent on already-deleted runtimes/roles.
    """
    aws_client = client_factory or boto3.client
    platform_client = platform_client_factory or boto3.client
    platform_region = os.environ.get(
        "APP_AWS_REGION",
        os.environ.get("AWS_REGION", region),
    )
    agentcore_ctrl = aws_client("bedrock-agentcore-control", region_name=region)
    canonical_id = _resolve_runtime_identifier(agentcore_ctrl, runtime_id)

    # Only a conclusive not-found is idempotent success.  AccessDenied used to be
    # treated as "runtime is gone", which let a stale deployment record authorize
    # deletion of convention-derived side resources without ever proving that the
    # live runtime belonged to this stack.
    def _is_runtime_gone(err: Exception) -> bool:
        return resource_is_missing(err)

    # This helper is used by both manifest and legacy cleanup paths, so the
    # destructive boundary must defend itself.  A persisted id is inventory, not
    # authority over whichever resource currently occupies it.
    runtime_owned = False
    role_arn = ""
    # The LIVE AgentCore runtime name, taken only from the ownership-proven describe. It is the
    # second-tier key for the friendly-name lookup below, and it is read here because this is the
    # one place that has proof the resource is ours -- a name from anywhere else is a guess.
    live_agentcore_name = ""
    try:
        rt = assert_agentcore_resource_owned(
            agentcore_ctrl,
            "agent_runtime",
            canonical_id,
            region,
        )
        runtime_owned = True
        role_arn = rt.get("roleArn", "") or ""
        live_agentcore_name = str(rt.get("agentRuntimeName") or "")
    except Exception as e:
        if not _is_runtime_gone(e):
            logger.warning(
                "Runtime %s retained because live ownership could not be proven: %s",
                canonical_id,
                e,
            )
            return {
                "success": False,
                "retained": True,
                "message": (f"Runtime {runtime_id} retained: exact live stack ownership could not be proven"),
            }

    if runtime_owned:
        try:
            agentcore_ctrl.delete_agent_runtime(agentRuntimeId=canonical_id)
            logger.info("Delete accepted for runtime %s; confirming", canonical_id)
        except Exception as e:
            if not _is_runtime_gone(e):
                return {"success": False, "message": f"Runtime destroy error: {e}"}
            logger.info("Runtime %s already deleted during teardown", canonical_id)
        else:
            # F-08: accepted is not deleted. Nothing below -- the role least of all -- may run
            # until a live read proves the runtime absent. See the docstring.
            try:
                wait_until_absent(
                    resource_label=f"runtime {canonical_id}",
                    read=lambda: agentcore_ctrl.get_agent_runtime(agentRuntimeId=canonical_id),
                    max_attempts=confirmation_attempts,
                    delay_seconds=confirmation_interval,
                    deadline_monotonic=confirmation_deadline,
                )
                logger.info("Confirmed runtime %s deleted", canonical_id)
            except DeletionFailedAfterAccept as e:
                logger.warning("Runtime %s: %s", canonical_id, e)
                return {"success": False, "message": f"Runtime destroy error: {e}"}
            except ResourceDeletionRefused as e:
                logger.warning("Runtime %s retained: %s", canonical_id, e)
                return {"success": False, "retained": True, "message": str(e)}
    else:
        logger.info("Runtime %s already deleted (or never existed)", canonical_id)

    # Best-effort: delete the matched IAM execution role too. Cross-account
    # runtimes deliberately use a pre-provisioned stable role shared by every
    # deployment in that target account, so their caller sets
    # delete_execution_role=False. Deleting that role with one runtime would
    # break every other Runtime and hosted MCP server in the account.
    # When the runtime never existed, role_arn is empty (get_agent_runtime
    # returned AccessDenied); fall back to the conventional names used by
    # the SFN IAM step (`AgentCoreRuntime-{name}`) and the direct-deploy path
    # (`{name}-role`). See lessons Bug 57.
    candidate_role_names: list[str] = []
    if delete_execution_role and role_arn:
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
    # Built with the SAME function iam_step used to create it, not by re-spelling
    # f"AgentCoreRuntime-{name}" here. The two spellings are not equivalent:
    # build_per_agent_role_name truncates to IAM's 64-char role-name limit, and the
    # prefix is 17 chars while sanitize_runtime_name caps the runtime name at 48.
    # 17 + 48 = 65, so for any agent whose sanitized name reaches 48 characters the
    # created role was truncated to 64 and this candidate asked for 65. IAM answers
    # NoSuchEntityException, the `continue` below treats that as "not our role", and
    # the role plus its inline AgentCoreRuntimePolicy leak silently -- a policy that
    # still grants reads against live targets (the artifacts bucket, the gateway,
    # memory, the KB, and the shared Cognito pool). Verified by constructing both
    # names: "customer_support_escalation_triage_assistant_v2_prod" creates
    # ...assistant_v2 (64) and this loop used to look for ...assistant_v2_ (65).
    from app.services.per_agent_identity import build_per_agent_role_name

    # Derived names are a fallback only when AgentCore could not return the
    # exact role ARN (for example an already-deleted runtime). If an exact ARN
    # was captured, also probing convention-derived names can delete a second,
    # unrelated role that happens to share the runtime's friendly-name prefix.
    if delete_execution_role and not role_arn:
        for role_name in (
            build_per_agent_role_name(name_for_role, region=region),
            # Compatibility candidate for per-agent roles created before
            # regional/digest naming.  Derived only after the exact runtime role
            # ARN is unavailable, and still guarded by live ownership tags.
            f"AgentCoreRuntime-{name_for_role}"[:64],
            regional_iam_role_name(f"{name_for_role}-role", region),
            f"{name_for_role}-role",
        ):
            if role_name not in candidate_role_names:
                candidate_role_names.append(role_name)

    # Bug 60 introduced a stack-managed shared role. Bug 62: never delete
    # that role — every DELETE /api/runtime would nuke it, breaking every
    # other runtime in the stack (and DemoTriage). Compare role NAME (not
    # ARN) so this still works when the cleanup is via the name fallback.
    shared_role_arn = os.environ.get("SHARED_RUNTIME_ROLE_ARN", "")
    shared_role_name = shared_role_arn.rsplit("/", 1)[-1] if shared_role_arn else ""

    iam = aws_client("iam")
    deleted_any_role = False
    for role_name in candidate_role_names:
        if not role_name:
            continue
        # Skip stack-managed shared roles. Match exact (Bug 60's role) or any
        # role with `-shared` suffix as defense in depth against future shared
        # roles. See tasks/lessons.md Bug 62.
        if role_name == shared_role_name or role_name == "AgentCoreFlowsRuntimeRole" or role_name.endswith("-shared"):
            logger.info("Skipping shared role %s (Bug 62 guard)", role_name)
            continue
        try:
            delete_owned_iam_role(iam, role_name, region)
            logger.info("Deleted runtime execution role: %s", role_name)
            deleted_any_role = True
        except iam.exceptions.NoSuchEntityException:
            # Role doesn't exist for this candidate — try the next.
            continue
        except ResourceDeletionRefused as e:
            # Derived fallback names can legitimately collide with another role
            # after the runtime is gone. Refuse without touching it.
            logger.warning("%s", e)
        except Exception as e:
            if is_error(e, "AccessDenied", "AccessDeniedException"):
                logger.debug(
                    "Skipping role %s during cleanup: not an agentcore-managed role "
                    "(tag-scoped grant denied). Candidate name, not an orphan.",
                    role_name,
                )
            else:
                logger.warning("Runtime %s role cleanup (%s) failed: %s", runtime_id, role_name, e)

    # Phase 1 Gap 1D — best-effort dashboard cleanup. The dashboard was
    # created in runtime_launch_step.py with name `agentcore-{runtime_id}`.
    # A derived dashboard name has no independent ownership proof, so it may be
    # touched only while the live runtime itself supplied graph-level authority.
    # Every sidecar this function could not remove is named here and returned to the caller,
    # which records each as a cleanup failure. "Best-effort" means the runtime delete is not
    # blocked by a sidecar; it never meant the verdict may stay green.
    sidecar_failures: list[str] = []
    if runtime_owned:
        try:
            from app.services.observability_dashboard import delete_dashboard_for_runtime

            if not delete_dashboard_for_runtime(
                canonical_id,
                region,
                cloudwatch_client=aws_client("cloudwatch", region_name=region),
            ):
                sidecar_failures.append("dashboard")
        except Exception as e:
            logger.warning("Dashboard cleanup for %s failed: %s", canonical_id, e)
            sidecar_failures.append("dashboard")

    # Phase 1 Gap 1C cleanup (M-2 + real-tester finding 2026-05-28):
    # cascade-delete the AgentCore OnlineEvaluationConfig + its CloudWatch
    # eval-results log group + the AgentCoreEval-* IAM execution role.
    # evaluation_step.py names the config `eval_<sanitized_runtime_id>`
    # and the role `AgentCoreEval-<agent_id[:32]>`. Best-effort: failures
    # here don't fail the destroy. Same pattern as Bug 25/27. See Bug 124.
    try:
        if not runtime_owned:
            raise ResourceDeletionRefused(
                "runtime is already absent, so name-derived evaluation resources have no live graph ownership proof"
            )
        ctrl = aws_client("bedrock-agentcore-control", region_name=region)
        logs_client = aws_client("logs", region_name=region)
        normalised_runtime = re.sub(r"[^a-zA-Z0-9_]", "_", canonical_id)[:32]
        # Name-derived cleanup covers configs created before the manifest row existed, and ONLY
        # the default name evaluation_step derives from this runtime id: an exact match, never
        # a substring, because the name can be user-supplied and another runtime's default name
        # can contain ours. Manifest rows (type online_evaluation_config) are deleted by id.
        expected_default_name = re.sub(r"[^a-zA-Z0-9_]", "_", f"eval_{canonical_id}")[:48]
        configs = _list_all_online_evaluation_configs(ctrl)
        for cfg in configs:
            cfg_name = cfg.get("onlineEvaluationConfigName", "")
            if cfg_name != expected_default_name:
                continue
            cfg_id = cfg.get("onlineEvaluationConfigId", "")
            if not cfg_id:
                continue
            try:
                ctrl.delete_online_evaluation_config(onlineEvaluationConfigId=cfg_id)
                logger.info("Deleted OnlineEvaluationConfig %s", cfg_id)
            except Exception as e:
                logger.warning("Failed to delete eval config %s: %s", cfg_id, e)
                sidecar_failures.append(f"evaluation_config:{cfg_id}")
            # Eval results log group is per-config — see Bug 120.
            try:
                logs_client.delete_log_group(logGroupName=f"/aws/bedrock-agentcore/evaluations/results/{cfg_id}")
                logger.info("Deleted eval-results log group for config %s", cfg_id)
            except Exception as e:
                if not is_error(e, "ResourceNotFoundException"):
                    logger.warning("Failed to delete eval log group for %s: %s", cfg_id, e)
                    sidecar_failures.append(f"evaluation_log_group:{cfg_id}")
        # Bug 124: also delete the AgentCoreEval-* IAM exec role. evaluation_step
        # mints it as `AgentCoreEval-{agent_id[:32]}` where agent_id is the
        # AgentCore runtime_id. Match the same prefix here. Idempotent on
        # already-gone roles.
        for eval_role_name in dict.fromkeys(
            (
                regional_iam_role_name(
                    f"AgentCoreEval-{normalised_runtime}",
                    region,
                ),
                # Compatibility with roles created before regional naming.
                f"AgentCoreEval-{normalised_runtime}",
            )
        ):
            try:
                delete_owned_iam_role(iam, eval_role_name, region)
                logger.info("Deleted eval execution role %s", eval_role_name)
            except iam.exceptions.NoSuchEntityException:
                pass
            except ResourceDeletionRefused as e:
                logger.warning("%s", e)
                sidecar_failures.append(f"evaluation_role:{eval_role_name}")
            except Exception as e:
                logger.warning("Failed to delete eval role %s: %s", eval_role_name, e)
                sidecar_failures.append(f"evaluation_role:{eval_role_name}")
    except ResourceDeletionRefused as e:
        logger.info("Eval-config cleanup skipped for %s: %s", canonical_id, e)
    except Exception as e:
        logger.warning("Eval-config cleanup for %s failed: %s", canonical_id, e)
        sidecar_failures.append("evaluation")

    # Bug 124 — Phase 3 Gap 3F: tear down any scheduled / event triggers so a
    # destroyed runtime doesn't leave a live cron/webhook invoking a dead ARN.
    # destroy_runtime only has canonical_id; resolve the friendly runtime_name
    # (the triggers PK) from the versions/slots store, then for each trigger
    # delete the provisioned EventBridge Scheduler schedule / events.Rule /
    # Lambda Function URL + the webhook HMAC secret, and finally the DDB rows.
    # Best-effort: every failure is logged and never fails the destroy.
    trigger_outcome: dict[str, object] = {
        # "unresolved" is the pre-loop default on purpose: anything that stops this block before it
        # has enumerated a partition leaves the caller with "I do not know", never with silence.
        "outcome": "unresolved",
        "rows": 0,
        "deleted": 0,
        "kept": 0,
        "foreign": 0,
    }
    # F-81f — the outcome is STRUCTURED and returned, because the caller decides from it whether the
    # runtime NAME may be released. A peer session measured the old shape end to end: an unresolvable
    # name skipped the trigger cleanup, the release deleted the slot and version rows anyway, and the
    # owner's own ``DELETE /triggers/{id}`` retry then answered 404 -- ``_resolve_owned_runtime``
    # resolves through the production slot, so releasing the name destroys the only handle that could
    # ever have cleaned the leaked schedule up. "Best-effort" has to mean "reported", not "forgotten".
    try:
        from app.services.trigger_runtime import cleanup_trigger_resources
        from app.services.trigger_store import (
            TYPE_CRON,
            TYPE_EVENTBRIDGE,
            TYPE_S3,
            TriggerDeleteBusy,
            get_trigger_store,
        )

        # Resolve the friendly runtime_name that keys the TriggersTable. ALWAYS from metadata --
        # the canonical id plus the ownership-proven live AgentCore name -- never from the caller.
        #
        # ``runtime_name`` used to short-circuit this (``runtime_name or _resolve...``), which made
        # the proof bypassable by every caller that had a name to offer: the manifest teardown
        # (``res["name"]``, recorded as ``friendly_runtime_name or runtime_id``) and the legacy
        # cross-account path (``_proven_runtime_name_for_destroy``, whose "proven" means "this key
        # is the one the record names", NOT "this key maps to this runtime id"). A stale or corrupt
        # deployment record therefore still selected a TriggersTable partition, and that partition
        # is not owner-scoped. Reported by the Codex audit session.
        #
        # So the hint is now a VETO, not a source: it can only disagree with the resolved mapping
        # and cancel the cleanup. A disagreement means one of the two is stale, and neither is worth
        # a cross-tenant delete.
        resolved_trigger_name = _resolve_runtime_name_for_cleanup(
            canonical_id,
            platform_region,
            live_agentcore_name,
        )
        trigger_runtime_name = resolved_trigger_name
        if runtime_name and runtime_name != resolved_trigger_name:
            logger.warning(
                "Caller-supplied runtime name for %s does not match the name AgentVersions maps to "
                "it; skipping name-keyed trigger cleanup rather than trusting either",
                canonical_id,
            )
            trigger_runtime_name = None
        if not trigger_runtime_name:
            trigger_outcome["outcome"] = "unresolved"
        else:
            store = get_trigger_store()
            platform_scheduler = None
            platform_events = None
            platform_lambda = None
            platform_sm = None
            # STRONGLY consistent: this enumeration is the evidence for "nothing is left under this
            # name", and a stale page that misses a row turns into a released name plus a schedule
            # still firing at a deleted ARN.
            rows = store.list_for_runtime(trigger_runtime_name, consistent=True)
            trigger_outcome["rows"] = len(rows)
            ours_all_done = True
            for trig in rows:
                # F-81f — per-row authorization, and the reason this block no longer refuses when
                # the runtime is already absent. Reaching here with ``runtime_owned`` False means
                # ``_is_runtime_gone`` was true (any other ownership failure returned "retained"
                # above), so the graph proof is unavailable *because there is no graph* -- and
                # refusing then leaked every schedule of a runtime deleted out of band, forever.
                # The row's own server-derived target is the better proof anyway: it names one
                # runtime id, so it also stops the shared-partition delete the old code did, where
                # a friendly-name collision put another tenant's rows in reach.
                classification = _classify_trigger_target(trig.target_runtime_arn, canonical_id)
                if classification == "unknown":
                    # Absent, malformed, or otherwise unreadable. It may be a legacy row of ours,
                    # so it is neither deleted nor dismissed: it keeps the name locked, which is the
                    # recoverable direction, and survives as the owner's handle. Reported by the
                    # peer audit sessions, which measured the alternative: classifying it foreign
                    # leaves the row AND releases the name, recreating the unretryable orphan.
                    logger.warning(
                        "Trigger %s/%s has no readable target runtime; keeping it and the name lock",
                        trig.runtime_name,
                        trig.trigger_id,
                    )
                    trigger_outcome["kept"] = int(trigger_outcome["kept"]) + 1
                    ours_all_done = False
                    continue
                if classification == "foreign":
                    # Positively somebody else's. Reported, but it is not our residue -- counting it
                    # would make a friendly name shared with another tenant unreleasable forever.
                    logger.warning(
                        "Trigger %s/%s does not target %s; leaving it alone",
                        trig.runtime_name,
                        trig.trigger_id,
                        canonical_id,
                    )
                    trigger_outcome["foreign"] = int(trigger_outcome["foreign"]) + 1
                    continue

                # Claim the row before deleting any provisioned resource. This is
                # the same trigger-wide dispatch fence used by the public DELETE
                # route: a live invocation wins and keeps both the row and runtime
                # name retryable; once claimed, no new delivery can start.
                delete_token = secrets.token_hex(16)
                try:
                    claimed = store.claim_delete(
                        runtime_name=trig.runtime_name,
                        trigger_id=trig.trigger_id,
                        owner_sub=str(getattr(trig, "owner_sub", "") or ""),
                        delete_token=delete_token,
                    )
                except TriggerDeleteBusy:
                    logger.warning(
                        "Trigger %s/%s is still dispatching; preserving it for teardown retry",
                        trig.runtime_name,
                        trig.trigger_id,
                    )
                    trigger_outcome["kept"] = int(trigger_outcome["kept"]) + 1
                    ours_all_done = False
                    continue
                except Exception as e:
                    logger.warning(
                        "Trigger delete claim failed (%s)",
                        type(e).__name__,
                    )
                    trigger_outcome["kept"] = int(trigger_outcome["kept"]) + 1
                    ours_all_done = False
                    continue

                if claimed is None:
                    # Either another owner now holds this key or a concurrent
                    # same-owner delete already removed it. Only absence is a
                    # confirmed success; every surviving row keeps the name lock.
                    try:
                        current = store.get(
                            trig.runtime_name,
                            trig.trigger_id,
                            consistent=True,
                        )
                    except Exception:
                        current = trig
                    if current is None:
                        trigger_outcome["deleted"] = int(trigger_outcome["deleted"]) + 1
                    else:
                        trigger_outcome["kept"] = int(trigger_outcome["kept"]) + 1
                        ours_all_done = False
                    continue

                # Re-check the authoritative post-claim image before side
                # effects. Trigger rows have no supported target-mutation API,
                # but a direct or future writer must not turn the earlier scan
                # into stale delete authority.
                if (
                    _classify_trigger_target(
                        claimed.target_runtime_arn,
                        canonical_id,
                    )
                    != "ours"
                ):
                    logger.warning(
                        "Trigger %s/%s changed target while teardown claimed it; preserving the row",
                        claimed.runtime_name,
                        claimed.trigger_id,
                    )
                    trigger_outcome["kept"] = int(trigger_outcome["kept"]) + 1
                    ours_all_done = False
                    continue

                trigger_cleanup_ok = True
                if claimed.scheduler_name:
                    try:
                        platform_scheduler = platform_scheduler or platform_client(
                            "scheduler",
                            region_name=platform_region,
                        )
                        platform_scheduler.delete_schedule(Name=claimed.scheduler_name)
                    except Exception as e:
                        # A not-found is the DESIRED end state, not a failure. Without this, a
                        # teardown that deleted the schedule and then failed on a later step
                        # retained the row, and every retry re-failed on the already-gone
                        # schedule -- so the "recoverable" retention was permanent. Only the
                        # service's own conclusive code converts; everything else (AccessDenied,
                        # a throttle, an unclassified error) still preserves the row.
                        if not is_error(e, "ResourceNotFoundException"):
                            logger.warning("Trigger schedule cleanup failed: %s", type(e).__name__)
                            trigger_cleanup_ok = False
                if claimed.function_name:
                    try:
                        platform_lambda = platform_lambda or platform_client(
                            "lambda",
                            region_name=platform_region,
                        )
                        platform_lambda.delete_function_url_config(FunctionName=claimed.function_name)
                    except Exception as e:
                        if not is_error(e, "ResourceNotFoundException"):
                            logger.warning("Trigger function-url cleanup failed: %s", type(e).__name__)
                            trigger_cleanup_ok = False
                elif claimed.function_url:
                    # A URL does not encode the Lambda function name accepted by
                    # DeleteFunctionUrlConfig. Keep the row as an operator retry
                    # handle instead of issuing a guaranteed-invalid delete and
                    # then discarding the only evidence of the resource.
                    logger.warning(
                        "Trigger %s/%s has function_url but no function_name; preserving the row for manual cleanup",
                        claimed.runtime_name,
                        claimed.trigger_id,
                    )
                    trigger_cleanup_ok = False
                try:
                    # Rules and HMAC secrets are platform control-plane
                    # resources even when the runtime is cross-account. The
                    # shared cleanup helper validates the deterministic rule
                    # ARN and secret ownership tags before deleting either.
                    # Do not construct an EventBridge client for a webhook-only
                    # row: it has no rule to inspect or delete. A recorded rule
                    # ARN still wins over the type so malformed/legacy metadata
                    # cannot hide a resource from teardown.
                    if claimed.eventbridge_rule_arn or claimed.type in {TYPE_CRON, TYPE_EVENTBRIDGE, TYPE_S3}:
                        platform_events = platform_events or platform_client(
                            "events",
                            region_name=platform_region,
                        )
                    if claimed.webhook_secret_ref:
                        platform_sm = platform_sm or platform_client(
                            "secretsmanager",
                            region_name=platform_region,
                        )
                    cleanup_trigger_resources(
                        claimed,
                        events_client=platform_events,
                        secrets_client=platform_sm,
                    )
                except Exception as e:
                    logger.warning(
                        "Trigger managed-resource cleanup failed (%s)",
                        type(e).__name__,
                    )
                    trigger_cleanup_ok = False
                if trigger_cleanup_ok:
                    try:
                        if store.delete_claimed(
                            runtime_name=claimed.runtime_name,
                            trigger_id=claimed.trigger_id,
                            delete_token=delete_token,
                        ):
                            trigger_outcome["deleted"] = int(trigger_outcome["deleted"]) + 1
                        else:
                            current = store.get(
                                claimed.runtime_name,
                                claimed.trigger_id,
                                consistent=True,
                            )
                            if current is None:
                                trigger_outcome["deleted"] = int(trigger_outcome["deleted"]) + 1
                            else:
                                trigger_outcome["kept"] = int(trigger_outcome["kept"]) + 1
                                ours_all_done = False
                    except Exception as e:
                        logger.warning("Trigger row cleanup failed: %s", type(e).__name__)
                        trigger_outcome["kept"] = int(trigger_outcome["kept"]) + 1
                        ours_all_done = False
                else:
                    # The row survives on purpose -- it is the operator's and the OWNER's retry
                    # handle. That only means something if the name stays locked, which is what
                    # the "partial" outcome buys from the caller.
                    trigger_outcome["kept"] = int(trigger_outcome["kept"]) + 1
                    ours_all_done = False
            trigger_outcome["outcome"] = "confirmed" if ours_all_done else "partial"
    except ResourceDeletionRefused as e:
        trigger_outcome["outcome"] = "refused"
        logger.info("Triggers cleanup skipped for %s: %s", canonical_id, e)
    except Exception as e:
        trigger_outcome["outcome"] = "error"
        logger.warning("Triggers cleanup for %s failed: %s", canonical_id, type(e).__name__)

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
        return {
            "success": True,
            "message": f"Runtime {runtime_id} and execution role deleted",
            "triggers": trigger_outcome,
            "sidecar_failures": sidecar_failures,
        }
    return {
        "success": True,
        "message": f"Runtime {runtime_id} deleted",
        "triggers": trigger_outcome,
        "sidecar_failures": sidecar_failures,
    }
