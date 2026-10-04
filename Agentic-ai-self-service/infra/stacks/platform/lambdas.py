"""IAM Roles + Lambda Functions (non-step Lambdas).

Audit #12: section banner — shared runtime role, workflow Lambda,
deployment Lambda and the streaming test Lambda live here; the per-step
roles + Lambdas live in step_lambdas.py.
"""

import hashlib
import os

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, RemovalPolicy
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_lambda_event_sources as lambda_event_sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm

from .buckets import deny_writes_to_agentcore_deps
from .config import (
    ATTACHABLE_MANAGED_POLICIES,
    DEPLOYMENT_STATE_MACHINE_TIMEOUT_MINUTES,
    GOVERNANCE_TAG_KEY_PREFIXES_ENV,
    PENDING_CLAIM_SLACK_SECONDS,
    PlatformConfig,
    governance_tag_key_globs,
    governance_tag_key_prefixes_env_value,
)
from .otel import OtelConfig
from .regional_artifact_bucket_grant import (
    READ_BUCKET_ACTIONS,
    READ_OBJECT_ACTIONS,
    grant_regional_artifact_buckets,
)
from .role_boundary import BOUNDARY_ARN_ENV, boundary_conditions, grant_boundary_retrofit
from .shared_role_guard import deny_mutating_the_shared_runtime_roles
from .step_lambdas import AGENTCORE_TYPE_ARN_TAIL
from .tables import RECOVERY_READ_ATTRIBUTES, Tables
from .tool_sandbox_net import ToolSandboxNetwork

#: The AgentCore resource types the DEPLOYMENT Lambda reads ownership tags from, derived by
#: tests/ownership_read_graph.py over src/app/deployment_handler.handler (164 functions
#: reached). It is every type the platform tags, because this one handler owns the whole
#: user-initiated cascade: DELETE /api/runtime/{id} plus the direct (non-Step-Functions)
#: deploy in services/deployment.py.
#:
#: Both credential-provider types are required even though no call site names the API-key
#: one literally -- delete_owned_credential_provider (resource_ownership.py:456) probes both
#: namespaces for back-compat with older manifests, deletes the OAuth one FIRST, and
#: re-raises on AccessDenied (only a MISSING resource is skipped). A role holding the read
#: for one type therefore tears down half the providers and then fails, which is worse than
#: failing closed. Shares AGENTCORE_TYPE_ARN_TAIL with the step roles so the container-vs-
#: child ARN fix cannot be applied to one surface and missed on the other -- which is
#: exactly how TagResource ended up correct for the delete verbs and wrong here.
DEPLOYMENT_LAMBDA_OWNERSHIP_READ_TYPES: tuple[str, ...] = (
    "runtime",
    "gateway",
    "memory",
    "policy-engine",
    "harness",
    "oauth2credentialprovider",
    "apikeycredentialprovider",
)

# Everything the backend Lambda asset must NOT carry. Exported rather than inlined so the
# test that bounds the bundle reads the same list the deploy uses; a test with its own copy
# would pass while the deploy shipped something else. See get_backend_code for the measured
# reason each of the last two groups is here.
BACKEND_ASSET_EXCLUDE: tuple[str, ...] = (
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".hypothesis",
    "tests",
    ".git",
    ".env",
    "build",
    "*.pyc",
    # Local test/build state. .coverage is git-ignored, which is why it went unnoticed.
    ".coverage",
    ".coverage.*",
    "htmlcov",
    "coverage.xml",
    ".ruff_cache",
    ".mypy_cache",
    "*.egg-info",
    ".DS_Store",
    # Dependency-resolution metadata is a build input, not something any
    # deployed handler reads. Keep it out of every Lambda asset.
    "uv.lock",
    # Read only by scripts/install-agentcore-deps.sh when it BUILDS the dependency
    # bundles, and only on a developer machine or in CI. Nothing in the deployed
    # handler opens it, and shipping it would put the exact pinned version of every
    # transitive dependency inside the asset for no purpose.
    "agentcore-deps-constraints.txt",
    # 138 MB of dependency bundles that reach the runtime through their OWN
    # BucketDeployment asset and are only ever read back as S3 keys. Shipping them here
    # uploaded them twice and spent 55% of Lambda's 250 MB unzipped limit on a copy
    # nothing opens.
    "agentcore-deps",
)


def get_backend_code() -> _lambda.Code:
    """Package the backend source as a Lambda code asset with bundled dependencies.

    Dependencies are pre-installed into backend/lib/ by the deploy script
    (pip install -r requirements-lambda.txt -t backend/lib/).
    The asset includes both src/ and lib/ directories.

    O-5. This packages a *directory of the working tree* minus a deny list, which means it
    ships whatever a developer happens to have left in ``backend/`` and nobody finds out
    until the artifact is read. Measured on the real deployed
    ``acfe2e-p0920-deployment`` bundle (155,176,077 bytes, 2026-09-21), ``unzip -l``:

    | top-level entry | uncompressed | ships? |
    |---|---|---|
    | ``agentcore-deps`` | 138,214,472 | **should not** — 75% of the bundle |
    | ``lib`` | 41,889,750 | yes, the runtime dependencies |
    | ``src`` | 2,976,308 | yes |
    | ``.coverage`` | 106,496 | **should not** — local test state |

    ``agentcore-deps`` is the expensive one and it is pure duplication: those zips reach the
    runtime through ``buckets.upload_agentcore_deps``, a *separate* ``BucketDeployment``
    asset, and every reference to them in ``backend/src`` is an **S3 key** fetched from the
    artifacts bucket at run time (``codegen_step``, ``mcp_server_step``, ``deployment``,
    ``cfn_template_generator``, ``code_generator``) — never a local path. So the same 138 MB
    was uploaded twice per deploy and the deployment Lambda never opened its copy. It also
    consumed the headroom that matters: 183 MB unzipped against Lambda's 250 MB hard limit,
    73% used, and the figure moves depending on whether the developer has run
    ``scripts/install-agentcore-deps.sh`` — so the limit could be crossed by a tree state
    rather than by a code change.

    ``.coverage`` holds file paths and line numbers, no credentials, so it is hygiene rather
    than a disclosure. It is in ``.gitignore``, which is exactly why nobody saw it.

    ARCC ``cnt_db6JTpAHC6jztZ`` (secure build process) asks for a *hermetic* build that
    "validate[s] and control[s] all inputs" — which a deny list over a working tree cannot
    do, because it can only exclude what someone already noticed. The list below is still a
    deny list, because ``Code.from_asset`` takes no include list; the bound is enforced
    instead by ``test_the_lambda_bundle_ships_only_what_it_needs``, which fails when a new
    top-level entry appears in ``backend/`` and is neither excluded nor explicitly allowed.
    """
    backend_path = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))
    return _lambda.Code.from_asset(
        backend_path,
        exclude=list(BACKEND_ASSET_EXCLUDE),
    )


def _bounded_trigger_resource_name(
    value: str,
    *,
    limit: int,
    punctuation: str = "._-",
) -> str:
    """Return a deterministic AWS-name-safe value no longer than ``limit``."""

    safe = "".join(char if char.isalnum() or char in punctuation else "-" for char in value).strip(punctuation)
    safe = safe or "agentcore"
    if len(safe) <= limit:
        return safe
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{safe[: limit - len(digest) - 1]}-{digest}"


def trigger_rule_prefix(cfg: PlatformConfig) -> str:
    """Prefix every runtime-created EventBridge rule with this stack identity.

    A trigger id is 32 characters and EventBridge rule names are capped at 64,
    leaving at most 31 characters for this prefix plus the separating hyphen.
    """

    return _bounded_trigger_resource_name(
        f"{cfg.project}-{cfg.env}-trigger",
        limit=31,
    )


def build_trigger_dispatch_queues(
    stack: cdk.Stack,
    cfg: PlatformConfig,
) -> tuple[sqs.Queue, sqs.Queue, str]:
    """Create the durable queue boundary for runtime-trigger delivery."""

    rule_prefix = trigger_rule_prefix(cfg)
    dead_letter_queue = sqs.Queue(
        stack,
        "TriggerDispatchDeadLetterQueue",
        queue_name=_bounded_trigger_resource_name(
            f"{cfg.project}-{cfg.env}-trigger-dispatch-dlq",
            limit=80,
            punctuation="_-",
        ),
        encryption=sqs.QueueEncryption.SQS_MANAGED,
        enforce_ssl=True,
        retention_period=Duration.days(14),
        removal_policy=cfg.removal_policy,
    )
    dispatch_queue = sqs.Queue(
        stack,
        "TriggerDispatchQueue",
        queue_name=_bounded_trigger_resource_name(
            f"{cfg.project}-{cfg.env}-trigger-dispatch",
            limit=80,
            punctuation="_-",
        ),
        encryption=sqs.QueueEncryption.SQS_MANAGED,
        enforce_ssl=True,
        # The deployment Lambda runs for at most 600 seconds. AWS recommends
        # at least six times that timeout for an SQS event source so a slow
        # invocation is not delivered concurrently to another worker.
        visibility_timeout=Duration.seconds(3600),
        retention_period=Duration.days(4),
        dead_letter_queue=sqs.DeadLetterQueue(
            max_receive_count=5,
            queue=dead_letter_queue,
        ),
        removal_policy=cfg.removal_policy,
    )
    dispatch_queue.add_to_resource_policy(
        iam.PolicyStatement(
            sid="AllowStackTriggerRules",
            effect=iam.Effect.ALLOW,
            principals=[iam.ServicePrincipal("events.amazonaws.com")],
            actions=["sqs:SendMessage"],
            resources=[dispatch_queue.queue_arn],
            conditions={
                "ArnLike": {
                    "aws:SourceArn": stack.format_arn(
                        service="events",
                        resource=f"rule/{rule_prefix}-*",
                    )
                },
                "StringEquals": {
                    "aws:SourceAccount": stack.account,
                },
            },
        )
    )
    return dispatch_queue, dead_letter_queue, rule_prefix


def build_shared_runtime_role(
    stack: cdk.Stack,
    cfg: PlatformConfig,
    otel: OtelConfig,
    *,
    artifacts_bucket: s3.Bucket,
    hitl_requests_table: dynamodb.Table,
) -> iam.Role:
    """Create ONE stable IAM execution role shared by every AgentCore runtime.

    NOTE: this builder deliberately takes NO ``gateway_auth_pool``. It briefly did, to
    grant DescribeUserPoolClient on the shared pool's exact ARN, and that grant is a
    cross-tenant credential break — see the long comment at the grant site below. An
    unused parameter named after the thing that must not be granted is an invitation to
    re-add it, so the parameter is gone rather than ignored.

    Why: AgentCore's service-side IAM cache for fresh roles can take 17-20
    minutes to propagate after put_role_policy in this account. Per-deploy
    roles fail with `ValidationException: Access denied when trying to
    retrieve zip file from S3` for that entire window. Creating one stable
    role at CDK stack init means propagation happens during stack creation,
    not at per-deploy time. See tasks/lessons.md Bug 60.

    Trade-off: every runtime in this stack shares the same role. Per-runtime
    least-privilege is sacrificed in exchange for a working deploy pipeline.
    Acceptable for a sample / demo platform; production deployments that
    need strict per-tenant IAM should override `RUNTIME_EXEC_ROLE_ARN` with
    a pre-existing role per agent.
    """
    # IAM role names are account-global, so the region has to be part of the
    # name for a second regional deployment to stand up alongside the first.
    # cfg.global_resource_name keeps the legacy name in us-east-1 (renaming a
    # role there would replace it, and AgentCore's IAM cache would need another
    # 17-20 min to propagate the replacement — see the Bug 60 note above).
    role = iam.Role(
        stack,
        "SharedRuntimeExecRole",
        role_name=f"AgentCoreRuntime-{cfg.global_resource_name(stack, 'shared')}",
        assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        description=(
            "Shared execution role used by every AgentCore runtime "
            "deployed by this stack. Pre-created so AgentCore's IAM "
            "cache has propagated by user-deploy time."
        ),
    )
    # Bedrock model access (Strands needs both InvokeModel + Stream)
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
                "bedrock:Converse",
                "bedrock:ConverseStream",
            ],
            resources=["*"],
        )
    )
    # Read the agent code zip from the artifacts bucket, and from the regional bucket
    # a non-home deploy stages into: AgentCore fetches it as this role (F-41b).
    # Explicit exact read actions rather than artifacts_bucket.grant_read(): the L2
    # helper emits wildcard ACTIONS (s3:GetObject*/GetBucket*/List*), which would make
    # this role's Nag suppression 'wildcard RESOURCES only, actions are exact' rationale
    # false and grant more object verbs than fetching a ZIP needs. Mirrors the exact
    # read set grant_regional_artifact_buckets applies, so both grants stay consistent.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=READ_BUCKET_ACTIONS,
            resources=[artifacts_bucket.bucket_arn],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=READ_OBJECT_ACTIONS,
            resources=[f"{artifacts_bucket.bucket_arn}/*"],
        )
    )
    grant_regional_artifact_buckets(role, stack, "read")
    # CloudWatch Logs (auto-instrumented by AgentCore Runtime)
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
            ],
            resources=["*"],
        )
    )
    # All AgentCore runtime tool integrations (browser, code interpreter,
    # gateway, memory, guardrails, evaluation, policy). Exact action lists
    # (verified against the bedrock-agentcore / bedrock-agentcore-control
    # botocore service models in backend/lib) instead of *Browser* /
    # *CodeInterpreter* / *Memory* action wildcards. Resource stays "*"
    # because the browser/code-interpreter sessions, memories, gateways and
    # KBs a runtime uses are created dynamically at deploy time (per-agent),
    # so their ARNs are unknowable when this stack-level shared role is
    # built — least privilege is enforced by the exact action list instead.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                # Browser tool (data plane sessions on the aws.browser.v1
                # built-in + any custom browser resource).
                "bedrock-agentcore:StartBrowserSession",
                "bedrock-agentcore:StopBrowserSession",
                "bedrock-agentcore:GetBrowserSession",
                "bedrock-agentcore:ListBrowserSessions",
                "bedrock-agentcore:UpdateBrowserStream",
                "bedrock-agentcore:InvokeBrowser",
                "bedrock-agentcore:ConnectBrowserAutomationStream",
                "bedrock-agentcore:GetBrowser",
                "bedrock-agentcore:ListBrowsers",
                # Code Interpreter tool (data plane sessions).
                "bedrock-agentcore:StartCodeInterpreterSession",
                "bedrock-agentcore:StopCodeInterpreterSession",
                "bedrock-agentcore:InvokeCodeInterpreter",
                "bedrock-agentcore:GetCodeInterpreterSession",
                "bedrock-agentcore:ListCodeInterpreterSessions",
                "bedrock-agentcore:GetCodeInterpreter",
                "bedrock-agentcore:ListCodeInterpreters",
                # Gateway tool plane.
                "bedrock-agentcore:InvokeGateway",
                "bedrock-agentcore:ListGateways",
                "bedrock-agentcore:GetGateway",
                # Memory (data plane events + records; GetMemory is the
                # control-plane read used to resolve the memory resource).
                "bedrock-agentcore:GetMemory",
                "bedrock-agentcore:CreateEvent",
                "bedrock-agentcore:GetEvent",
                "bedrock-agentcore:ListEvents",
                "bedrock-agentcore:DeleteEvent",
                "bedrock-agentcore:ListSessions",
                "bedrock-agentcore:ListActors",
                "bedrock-agentcore:RetrieveMemoryRecords",
                "bedrock-agentcore:GetMemoryRecord",
                "bedrock-agentcore:ListMemoryRecords",
                # No "legacy verbs kept for older SDK paths" here. GetLastKTurns
                # and RetrieveMemories used to sit on the two lines below this
                # comment and are not AgentCore IAM actions at all -- IAM accepts a
                # nonexistent action without complaint and authorizes nothing, so
                # they never did anything. The real verbs are RetrieveMemoryRecords
                # and ListEvents, both already above. Confirmed with IAM Access
                # Analyzer (INVALID_ACTION) and botocore's service model.
                "bedrock:ApplyGuardrail",
                "bedrock:GetGuardrail",
                # Knowledge Base retrieve (called by retrieve_from_kb tool
                # in agents that have a KB connected). See lessons Bug 87.
                "bedrock:Retrieve",
                "bedrock:RetrieveAndGenerate",
            ],
            # Resource-level scoping intentionally not applied: these
            # resources are created dynamically per-deploy — scoped by the
            # exact action list above instead.
            resources=["*"],
        )
    )
    # Optional OTEL auth-header secret (when platform OTEL is configured).
    if otel.enabled:
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[otel.auth_secret_arn],
            )
        )
    # Phase 2 Gap 2D — the injected human_approval @tool writes PENDING
    # approval rows. Scoped to the single HITL table; PutItem only.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["dynamodb:PutItem"],
            resources=[hitl_requests_table.table_arn],
        )
    )
    # The gateway's OAuth client secret, read at the moment of use.
    #
    # The deploy paths no longer inject COGNITO_CLIENT_SECRET: GetAgentRuntime
    # returns a runtime's environment variables in plaintext, a value that reaches a
    # CloudFormation resource's properties is copied into the stack's event stream
    # for 90 days, and every Task in the deployment state machine re-emits the whole
    # event into the execution history. So the agent is handed the POOL ID and calls
    # DescribeUserPoolClient itself (see _resolve_client_secret in
    # services/code_generator.py). Without this grant the change fails closed: the
    # deploy goes green and the agent then cannot mint a gateway token at all.
    #
    # This used to be ONE statement, scoped to userpool/* with a condition on
    # aws:ResourceTag/AgentCoreStack, under a comment asserting that a pool id is
    # "minted per-deploy and unknowable at synth time". That was true when it was
    # written and is now false: the shared gateway-auth pool is a CDK construct in
    # THIS stack (platform_stack.py), so its ARN is known at synth — and because it is
    # a CDK resource it carries CloudFormation's tags plus Project/Environment but NOT
    # AgentCoreStack, which the backend only stamps on pools it creates itself.
    # Measured on the live pool us-east-1_qiYLOs3Ij: tags were exactly Environment,
    # Project and three aws:cloudformation:* keys. So the condition was unsatisfiable
    # for the one pool the DEFAULT `shared` identity mode actually uses, and every
    # agent deployed in that mode silently exposed no gateway tools.
    #
    # The grant now comes from one helper shared with the gateway and harness step
    # roles, which had the opposite bug (an unconditioned userpool/*). See
    # cognito_client_secret_grant.py for the reasoning and the ARCC citations, and note
    # the explicit warning there against "fixing" this by tagging the RETAINed shared
    # pool with the teardown ownership marker.
    #
    # !! gateway_auth_pool IS DELIBERATELY *NOT* PASSED HERE. !!
    #
    # The obvious repair for the unsatisfiable condition above is to also grant this
    # role DescribeUserPoolClient on the shared pool's exact ARN. That is a cross-tenant
    # credential break, and it was measured on the live stack rather than argued:
    #
    #   * IAM for Cognito has NO resource granularity below the pool. The resource type
    #     is `userpool`, so a grant naming the pool authorizes reading the secret of
    #     EVERY app client in it -- and in `shared` mode the one pool holds every
    #     deployed gateway's client.
    #   * This same role already holds bedrock-agentcore:ListGateways + GetGateway on
    #     Resource=* with no condition. Verified live against agent-gateway-1kjpgafkwg:
    #     get-gateway returns authorizerConfiguration.customJWTAuthorizer.allowedClients
    #     (the app client id) and the discoveryUrl (the pool id).
    #   * Verified live: describe-user-pool-client returns ClientSecret (51 chars) for
    #     whatever client id it is handed.
    #   * Every gateway in the pool shares one token endpoint.
    #
    # Chained, that lets agent A enumerate agent B's gateway, read B's client id, fetch
    # B's client secret, mint a token for B's scope and invoke B's tools. Because the
    # role is SHARED, no per-deploy scoping can fix it -- and neither can identity mode
    # 'per_agent', since a per-agent role naming the same shared pool inherits the same
    # pool-wide read. That is what makes this different from the pre-existing shared-role
    # trade-offs: it removes the isolation that per_agent mode exists to provide.
    #
    # It is also the exact thing the comment on the next statement already forbids:
    # "granting this role every secret in the account would let any agent read ... every
    # other tenant's connector credentials."
    #
    # So the runtime never reads the client secret from Cognito. The gateway step -- a
    # control-plane role that legitimately holds this grant because it CREATED the
    # client -- reads the secret once at deploy time (out of create_user_pool_client's
    # own response), stores it in a per-deployment Secrets Manager secret under the
    # `agentcore-connector/` namespace, and hands the runtime OAUTH_CLIENT_SECRET_REF.
    # The generated agent already prefers that reference over DescribeUserPoolClient
    # (services/code_generator.py::_resolve_client_secret), and Secrets Manager DOES
    # scope per-resource-ARN -- so per_agent mode regains true isolation.
    # See gateway_deployer._mint_client_secret_ref.
    #
    # !! AND THE OWNER-TAG-CONDITIONED STATEMENT IS GONE TOO. !!
    #
    # It looked like the safe half of the grant, because its condition
    # (aws:ResourceTag/AgentCoreStack) reads as "only pools this deployment created".
    # It is not: the tag's VALUE is `{project}-{env}-{region}`, which identifies the
    # STACK, and every deployment in the stack stamps the identical value. So on the
    # dedicated-pool path it authorized reading the client secret of every OTHER
    # deployment's gateway pool in the same stack -- the same cross-tenant read as the
    # exact-ARN grant above, reached through a condition that merely looked narrow.
    # Cognito cannot express "the one client I own", so there is no version of this
    # grant that belongs on a tenant-facing role.
    #
    # BREAKING, and deliberately so: an agent deployed BEFORE the secret reference
    # existed has COGNITO_USER_POOL_ID in its environment and no ref, so it resolves
    # its secret through DescribeUserPoolClient and stops being able to mint a gateway
    # token once this role loses the grant. Redeploying the agent fixes it. Agents in
    # the default `shared` mode are unaffected, because for them the condition was
    # unsatisfiable and the tool plane was already dead. This is recorded in the
    # CHANGELOG as a migration step rather than papered over with a grant.
    #
    # `grant_client_secret_read` is still the single source of truth for the two step
    # roles that DO need the action (step_lambdas.py). It is not imported here -- an
    # import of the forbidden grant sitting unused in this module is how it comes back.
    # Every runtime credential now lands in the deployment-bound
    # `agentcore-connector/` namespace before SFN starts: external-IDP secrets,
    # LiteLLM virtual keys, provider API keys, and OTEL auth headers. Long-lived
    # `agentcore-provider/` / `agentcore-otel/` source secrets are read only by the
    # deployment control plane, never by this shared tenant-facing role.
    #
    # Region is wildcarded deliberately: a same-account deployment may select an
    # allowlisted target region, and its copied secret lives there while this IAM
    # role remains account-global.
    #
    # F-01 (signoff-g10 review): the prefix alone is every tenant of every stack of this
    # product in the account. The read is therefore pinned to the two ownership tags
    # `_put_connector_secret` stamps on every connector secret it mints
    # (governed_tag_list -> owner_tags: ManagedBy=agentcore-flows and
    # AgentCoreStack={project}-{env}-{region}). Both are supported on GetSecretValue as
    # aws:ResourceTag per the Service Reference feed. AgentCoreStack is matched as
    # `{project}-{env}-*` because the region segment is the SECRET's region: a same-account
    # deploy to an allow-listed target region stages its copy there, tagged with that
    # region, and this role is account-global.
    #
    # What this closes: reads of another stack's tenants' secrets, of untagged/legacy
    # secrets, and of anything a foreign writer parks under the prefix. What it cannot
    # close, said plainly: this is ONE principal for every tenant in `shared` identity
    # mode, AgentCore attaches no session tags, and GetSecretValue's request context
    # carries nothing that names the calling deployment -- no condition on this role can
    # tell tenant A's secret from tenant B's inside one stack. That isolation exists only
    # in `per_agent` mode (per_agent_identity.build_scoped_runtime_policy grants the exact
    # ARNs). A secret minted before the ownership tags existed stops being readable here;
    # redeploying the agent re-binds it into a tagged copy
    # (bind_connector_secret_for_deployment). ARCC cnt_SFJJhkOueCPRkd. Enforced by
    # infra/tests/test_f01_shared_runtime_role_connector_read_is_tag_bound.py.
    #
    # F-01 (b): `_put_connector_secret` also stamps IdentityMode=<shared|per_agent> at mint
    # time, so this role reads ONLY secrets bound to shared-mode deployments; a per_agent
    # deployment's secrets leave the shared role's reach entirely (its own minted role holds the
    # exact ARNs). This stays in a statement of its own: any other read that lacks the tag
    # would be denied by the same condition, and that outage would look like a tightening.
    # MIGRATION, intended and fail-closed: a connector secret minted before the IdentityMode tag
    # existed is unreadable by this role until its deployment is redeployed (the redeploy
    # re-binds it into a freshly tagged copy). That is the designed behaviour, not a regression.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=[f"arn:aws:secretsmanager:*:{stack.account}:secret:agentcore-connector/*"],
            conditions={
                "StringEquals": {
                    "aws:ResourceTag/ManagedBy": "agentcore-flows",
                    "aws:ResourceTag/IdentityMode": "shared",
                },
                "StringLike": {"aws:ResourceTag/AgentCoreStack": f"{cfg.project}-{cfg.env}-*"},
            },
        )
    )
    return role


def build_shared_mcp_runtime_role(
    stack: cdk.Stack,
    cfg: PlatformConfig,
    *,
    artifacts_bucket: s3.Bucket,
) -> iam.Role:
    """Create the stable, model-free execution role for standalone FastMCP runtimes.

    A standalone FastMCP server (``runtime_artifact_kind == "mcp"``) speaks the MCP
    protocol only; it must never be able to invoke a model. Reusing the ordinary
    ``build_shared_runtime_role`` — which deliberately carries bedrock:InvokeModel and
    the full AgentCore tool plane for Strands agents — would make such a runtime
    model-free in source code while its AWS authority stayed model-capable. The two
    must be SEPARATE roles: this one is pre-warmed alongside the model role (same Bug 60
    IAM-propagation reasoning) but grants ONLY what a protocol container needs to boot —
    read the staged runtime ZIP (AgentCore fetches it as the execution role) and write
    its own CloudWatch logs. No bedrock:* model actions, no bedrock-agentcore tool
    plane, no secrets, no HITL table.

    The ``-mcp-shared`` suffix is load-bearing: runtime teardown recognises stack-owned
    (never deployment-deletable) roles by the ``-shared`` suffix, so the name must keep
    it — see infra/tests/test_mcp_shared_runtime_role.py.
    """
    # Account-global role names carry the region via global_resource_name so a second
    # regional deployment can stand up alongside the first — same reasoning as the
    # model-capable shared role above.
    role = iam.Role(
        stack,
        "SharedMcpRuntimeExecRole",
        role_name=f"AgentCoreRuntime-{cfg.global_resource_name(stack, 'mcp-shared')}",
        assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        description=(
            "Model-free execution role shared by every standalone FastMCP "
            "runtime deployed by this stack. Pre-created so AgentCore's IAM "
            "cache has propagated by user-deploy time. Deliberately carries NO "
            "bedrock model or AgentCore tool permissions."
        ),
    )
    # Read the agent code zip from the artifacts bucket, and from the regional bucket
    # a non-home deploy stages into: AgentCore fetches it as this role (F-41b).
    # Explicit exact read actions rather than artifacts_bucket.grant_read(): the L2
    # helper emits wildcard ACTIONS (s3:GetObject*/GetBucket*/List*), which would make
    # this model-free role's Nag suppression 'wildcard resources only' rationale false
    # and grant more object verbs than fetching a ZIP needs. This mirrors the exact
    # read set grant_regional_artifact_buckets applies, so both grants stay consistent
    # and the only remaining wildcards are RESOURCE-level.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=READ_BUCKET_ACTIONS,
            resources=[artifacts_bucket.bucket_arn],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=READ_OBJECT_ACTIONS,
            resources=[f"{artifacts_bucket.bucket_arn}/*"],
        )
    )
    grant_regional_artifact_buckets(role, stack, "read")
    # CloudWatch Logs (auto-instrumented by AgentCore Runtime).
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
            ],
            resources=["*"],
        )
    )
    return role


def build_workflow_lambda(
    stack: cdk.Stack,
    cfg: PlatformConfig,
    otel: OtelConfig,
    *,
    backend_code: _lambda.Code,
    workflows_table: dynamodb.Table,
    flows_table: dynamodb.Table,
) -> _lambda.Function:
    """Create Workflow Lambda (FastAPI + Mangum) for CRUD operations.

    Requirements: 1.1, 1.5, 6.1
    """
    role = iam.Role(
        stack,
        "WorkflowLambdaRole",
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
        ],
    )
    # DynamoDB workflows table: read/write
    workflows_table.grant_read_write_data(role)
    # DynamoDB flows table: read/write
    flows_table.grant_read_write_data(role)
    # SSM read for app config
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "ssm:GetParameter",
                "ssm:GetParameters",
                "ssm:GetParametersByPath",
            ],
            resources=[f"arn:aws:ssm:{stack.region}:{stack.account}:parameter/agentcore-workflow/{cfg.env}/*"],
        )
    )
    # Secrets Manager for OTEL auth header and model-provider API-key storage.
    # The routers always name secrets under their two explicit namespaces.
    # CreateSecret historically required `*` because IAM Resource matching for
    # CreateSecret pre-2023 didn't support name patterns, but modern IAM does
    # via the secret-ARN path prefix. See tasks/lessons.md Bug 40.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["secretsmanager:CreateSecret"],
            resources=[
                f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:agentcore-otel/*",
                f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:agentcore-provider/*",
            ],
        )
    )
    # The two router namespaces get a tag grant EACH, not one shared statement, because
    # their writers send different key sets and a shared allowlist would be the union --
    # letting this role stamp ``OwnerSubHash`` on an OTEL secret and ``owner_sub`` on a
    # provider key for no reason either caller needs.
    #
    # Both writers build their tags with ``owner_tag_list``, which appends ManagedBy and
    # AgentCoreStack LAST and raises rather than defaulting when PROJECT_NAME/ENVIRONMENT
    # are unset, so both values are always present in the request and the pin below can be
    # a plain StringEquals. These prefixes are shared across every deployment in the
    # account, which is what the ownership pin bounds.
    for _prefix, _keys in (
        # routers/observability.py -> owner_tag_list(region, extra={Purpose, Provider,
        # owner_sub, created_at})
        ("agentcore-otel/", ["Purpose", "Provider", "owner_sub", "created_at"]),
        # routers/provider_credentials.py -> owner_tag_list(region, extra={Purpose,
        # Provider, OwnerSubHash})
        ("agentcore-provider/", ["Purpose", "Provider", "OwnerSubHash"]),
    ):
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:TagResource"],
                resources=[f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:{_prefix}*"],
                conditions={
                    "StringEquals": {
                        "aws:RequestTag/ManagedBy": "agentcore-flows",
                        "aws:RequestTag/AgentCoreStack": f"{cfg.project}-{cfg.env}-${{aws:RequestedRegion}}",
                    },
                    "ForAllValues:StringLike": {"aws:TagKeys": ["ManagedBy", "AgentCoreStack"] + _keys},
                },
            )
        )
    # Phase 3 Gap 3D GitOps: owner-scoped git PAT storage + retrieval. The
    # git_sync service names secrets `agentcore-git/{safe_owner}-{uuid}` and
    # reads them back at /api/workflows/{id}/git-sync time. Scoped to the
    # agentcore-git/ namespace only. (No new API GW route is needed —
    # /git-sync + /git-token are covered by the existing
    # /api/workflows/{proxy+} POST route.)
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                # TagResource is granted separately below. It cannot share a statement
                # with GetSecretValue/PutSecretValue: those send no request tags, and a
                # StringEquals against an absent aws:RequestTag does not match, so the
                # condition would deny every git-token read.
                "secretsmanager:CreateSecret",
                "secretsmanager:GetSecretValue",
                "secretsmanager:PutSecretValue",
            ],
            resources=[
                f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:agentcore-git/*",
            ],
        )
    )
    # ``git_sync._store_token`` sends ManagedBy, Purpose, owner_sub and created_at as a
    # plain literal -- NOT through owner_tag_list -- so there is no AgentCoreStack to pin,
    # exactly as with the webhook-trigger secret. What is pinnable is the two constants
    # plus a closed key set. ``owner_sub`` is per-user and this role is shared by every
    # caller of /api/workflows, so IAM cannot express the tenant boundary here; the router
    # does, by taking owner_sub from the authenticated caller. Recorded so the condition is
    # not misread as closing that.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["secretsmanager:TagResource"],
            resources=[
                f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:agentcore-git/*",
            ],
            conditions={
                "StringEquals": {
                    "aws:RequestTag/ManagedBy": "agentcore-flows",
                    "aws:RequestTag/Purpose": "git-sync-token",
                },
                "ForAllValues:StringLike": {
                    "aws:TagKeys": ["ManagedBy", "Purpose", "owner_sub", "created_at"],
                },
            },
        )
    )

    fn = _lambda.Function(
        stack,
        "WorkflowLambda",
        function_name=f"{cfg.project}-{cfg.env}-workflow",
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="src/app/lambda_handler.handler",
        code=backend_code,
        memory_size=512,
        timeout=Duration.seconds(30),
        role=role,
        tracing=_lambda.Tracing.ACTIVE,
        environment={
            "DYNAMODB_TABLE_NAME": workflows_table.table_name,
            "DYNAMODB_FLOWS_TABLE_NAME": flows_table.table_name,
            "ENVIRONMENT": cfg.env,
            # PROJECT_NAME + ENVIRONMENT + APP_AWS_REGION form the resource-owner tag
            # in services/resource_ownership.py; routers/observability.py stamps it on
            # every agentcore-otel/ secret it creates so cleanup.sh can tell this
            # deployment's secrets from a co-resident deployment's.
            "PROJECT_NAME": cfg.project,
            "APP_AWS_REGION": stack.region,
            "POWERTOOLS_SERVICE_NAME": "workflow",
            "PYTHONPATH": "/var/task/src:/var/task:/var/task/lib",
            # Scope-based RBAC (services/rbac.py). Enforcing by default: a caller
            # without the route's scope gets 403. The user provisioner puts every
            # user it creates in g-users-default + t-user, so they hold the scopes
            # a standard user needs. `-c rbac_enforce=false` is the advisory
            # escape hatch for an upgrade whose existing users have no group yet
            # (docs/RBAC_ROLLOUT.md).
            "RBAC_ENFORCE": stack.node.try_get_context("rbac_enforce") or "true",
        },
        log_group=logs.LogGroup(
            stack,
            "WorkflowLambdaLogGroup",
            log_group_name=f"/aws/lambda/{cfg.project}-{cfg.env}-workflow",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        ),
    )
    otel.apply(fn, "workflow")
    return fn


def build_deployment_lambda(
    stack: cdk.Stack,
    cfg: PlatformConfig,
    otel: OtelConfig,
    *,
    backend_code: _lambda.Code,
    tables: Tables,
    artifacts_bucket: s3.Bucket,
    shared_runtime_role: iam.Role,
    shared_mcp_runtime_role: iam.Role,
    role_boundary: iam.ManagedPolicy,
    sandbox_network: ToolSandboxNetwork,
    trigger_dispatch_queue: sqs.Queue,
    trigger_rule_prefix: str,
    gateway_auth_pool: cognito.IUserPool | None = None,
    gateway_auth_domain: str = "",
) -> _lambda.Function:
    """Create Deployment Lambda for deploy/status/test/delete operations.

    Requirements: 1.2, 6.2

    ``gateway_auth_pool`` is not used for a grant here — this Lambda never reads a
    client secret. It is needed because this Lambda RUNS A TEARDOWN PATH that must be
    able to recognize the shared pool and refuse to delete it. See the
    GATEWAY_SHARED_USER_POOL_ID entry in the environment below.
    """
    role = iam.Role(
        stack,
        "DeploymentLambdaRole",
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
        ],
    )
    # DynamoDB deployments table: read/write
    tables.deployments.grant_read_write_data(role)
    # The DELETE teardown's name hold (gateway_name_claim.hold_gateway_names_for_teardown,
    # F-66f): acquire, abandon and release are conditional UpdateItems, and the claim of a
    # gateway confirmed deleted is erased with a conditional DeleteItem. No read, no put.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["dynamodb:UpdateItem", "dynamodb:DeleteItem"],
            resources=[tables.gateway_name_claims.table_arn],
        )
    )
    # The same teardown finds a gateway recorded only on its claim
    # (gateway_name_claim.recovered_gateway_rows): strongly consistent GetItems of its
    # recovery pointer and the claims it lists, projected to the attributes the reader
    # checks, so never a claim's fence token (ARCC cnt_L4ZLZgjrCctfxl and
    # cnt_BBrFTwAEgWxA30: specific APIs, specific resources).
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["dynamodb:GetItem"],
            resources=[tables.gateway_name_claims.table_arn],
            conditions={
                "ForAllValues:StringEquals": {"dynamodb:Attributes": list(RECOVERY_READ_ATTRIBUTES)},
                "StringEquals": {"dynamodb:Select": "SPECIFIC_ATTRIBUTES"},
            },
        )
    )
    # F-66e: the teardown revoke/detach and the policy promoter take the gateway write
    # lock (services/gateway_mutation_lock): one conditional PutItem and one conditional
    # DeleteItem, on lock rows only (ARCC cnt_AGx9pUNpmdOVZB, least privilege).
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["dynamodb:PutItem"],
            resources=[tables.gateway_name_claims.table_arn],
            conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["gwlock#*"]}},
        )
    )
    # F-55: POST /api/deploy refuses a flowId the caller does not own, BEFORE any side effect
    # (deployment_handler._reject_unowned_flow). That is one GetItem by partition key, so this
    # is GetItem on the flows table only -- not grant_read_data, which would add Query, Scan and
    # BatchGetItem this Lambda has no use for (ARCC cnt_SFJJhkOueCPRkd, least privilege). The
    # table's AWS-managed encryption needs no KMS grant. Without this the owner check can only
    # ever fail closed with 503, i.e. every deploy that names a flow would be refused.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["dynamodb:GetItem"],
            resources=[tables.flows.table_arn],
        )
    )
    # Loom-study 1.6 — JIT IAM permission-request workflow. The router
    # (create/list/approve/reject) is on the deployment Lambda; on approve it
    # widens a managed role's inline policy, so grant PutRolePolicy scoped to
    # the platform's own AgentCore* managed roles (never arbitrary roles).
    tables.permission_requests.grant_read_write_data(role)
    # PutRolePolicy sits in a statement of its own so it can carry the iam:PermissionsBoundary
    # condition when enforcement is on (role_boundary.py, F-06): the key is absent from
    # GetRolePolicy's request context, and a StringEquals against an absent key denies the read.
    # The two shared runtime roles are excluded from this grant by the explicit Deny below
    # (shared_role_guard.py, F-05): widening the role every tenant runs as is not a JIT request.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["iam:PutRolePolicy"],
            resources=[f"arn:aws:iam::{stack.account}:role/AgentCore*"],
            conditions=boundary_conditions(stack, role_boundary),
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["iam:GetRolePolicy"],
            resources=[f"arn:aws:iam::{stack.account}:role/AgentCore*"],
        )
    )
    # Phase 1 Gap 1A — versions + slots tables. The deployment Lambda is
    # the read-write owner: handle_deploy() seeds the AgentVersion row,
    # and the versions router promotes/rolls back slots.
    tables.agent_versions.grant_read_write_data(role)
    tables.runtime_slots.grant_read_write_data(role)
    # Phase 2 Gap 2A — agent registry. The registry router (publish/
    # search/clone/update/delete) is mounted on the deployment Lambda.
    tables.agent_registry.grant_read_write_data(role)
    # Phase 2 Gap 2B — usage events table (optional write path). The
    # query-time cost path uses logs:StartQuery already granted below.
    tables.usage_events.grant_read_write_data(role)
    # Phase 2 Gap 2D — HITL approval queue. routers/hitl.py (mounted on the
    # deployment Lambda) reads the owner_sub GSI and decides requests.
    tables.hitl_requests.grant_read_write_data(role)
    # Phase 3 Gap 3F — triggers registry. routers/triggers.py (mounted on
    # the deployment Lambda) reads/writes this table + the owner_sub GSI.
    tables.triggers.grant_read_write_data(role)
    # Runtime-created rules can only live inside this stack's deterministic
    # prefix and can only target the one durable dispatch queue below.
    trigger_rule_arn = stack.format_arn(
        service="events",
        resource=f"rule/{trigger_rule_prefix}-*",
    )
    trigger_resource_tags = {
        "StringEquals": {
            "aws:ResourceTag/ManagedBy": "agentcore-flows",
            "aws:ResourceTag/Purpose": "runtime-trigger",
            "aws:ResourceTag/AgentCoreStack": (f"{cfg.project}-{cfg.env}-{stack.region}"),
        }
    }
    role.add_to_policy(
        iam.PolicyStatement(
            # PutRule is create-or-update and AWS exposes no create-only
            # condition. The application therefore proves live tags before an
            # update and verifies them again after the call.
            actions=["events:PutRule"],
            resources=[trigger_rule_arn],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["events:PutTargets"],
            resources=[trigger_rule_arn],
            conditions={
                **trigger_resource_tags,
                "ArnEquals": {
                    "events:TargetArn": trigger_dispatch_queue.queue_arn,
                },
            },
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["events:RemoveTargets", "events:DeleteRule"],
            resources=[trigger_rule_arn],
            conditions=trigger_resource_tags,
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["events:DescribeRule", "events:ListTagsForResource"],
            resources=[trigger_rule_arn],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["events:TagResource"],
            resources=[trigger_rule_arn],
            conditions={
                "StringEquals": {
                    "aws:RequestTag/ManagedBy": "agentcore-flows",
                    "aws:RequestTag/Purpose": "runtime-trigger",
                    "aws:RequestTag/AgentCoreStack": (f"{cfg.project}-{cfg.env}-{stack.region}"),
                },
                "ForAllValues:StringEquals": {
                    "aws:TagKeys": [
                        "ManagedBy",
                        "Purpose",
                        "AgentCoreStack",
                        "TriggerId",
                        "RuntimeName",
                    ]
                },
            },
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["sqs:SendMessage"],
            resources=[trigger_dispatch_queue.queue_arn],
        )
    )
    # Phase 3 Gap 3H — prompt library. routers/prompts.py (mounted on the
    # deployment Lambda) reads/writes this table + the owner_sub GSI.
    tables.prompt_library.grant_read_write_data(role)
    # Phase 2 (Loom) governance tagging. routers/tags.py + the deploy-time
    # tag resolver (services/tag_policy_store) read/write this table.
    tables.tag_policy.grant_read_write_data(role)
    # Phase 4 (Loom) FinOps — cost budgets (routers/cost.py budgets_router).
    tables.budget.grant_read_write_data(role)
    # Phase 5 (Loom) — audit trail (middleware writes, /api/admin/audit reads).
    tables.audit.grant_read_write_data(role)
    # states:StartExecution on the state machine (granted after SM creation)
    # SSM read
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "ssm:GetParameter",
                "ssm:GetParameters",
                "ssm:GetParametersByPath",
            ],
            resources=[f"arn:aws:ssm:{stack.region}:{stack.account}:parameter/agentcore-workflow/{cfg.env}/*"],
        )
    )
    # POST /api/deploy treats provider/OTEL ARNs as untrusted source inventory:
    # describe the live tags, then read once and copy into a deployment-bound
    # agentcore-connector/ secret. Read-only by design—the long-lived source
    # credential is never part of deployment teardown.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "secretsmanager:DescribeSecret",
                "secretsmanager:GetSecretValue",
            ],
            resources=[
                f"arn:aws:secretsmanager:*:{stack.account}:secret:agentcore-provider/*",
                f"arn:aws:secretsmanager:*:{stack.account}:secret:agentcore-otel/*",
            ],
        )
    )
    # Knowledge Base connector/RDS credential ARNs are customer inventory, not
    # authority.  The deployment handler may read an arbitrary source namespace
    # only when the resource owner explicitly opted it in.  It immediately copies
    # the value into an exact deployment-bound agentcore-connector/ secret; the
    # source ARN is never serialized into Step Functions history or granted to
    # Bedrock.  Keep this separate from the provider/OTEL namespace statement:
    # those prove caller+stack ownership, while this one requires explicit consent.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "secretsmanager:DescribeSecret",
                "secretsmanager:GetSecretValue",
            ],
            resources=[
                f"arn:aws:secretsmanager:*:{stack.account}:secret:*",
            ],
            conditions={
                "StringEquals": {
                    "aws:ResourceTag/AgentCoreFlowsAccess": "allow",
                }
            },
        )
    )
    # bedrock-agentcore for test-runtime invocation and runtime deletion.
    # IMPORTANT: AgentCore uses ONE IAM action prefix `bedrock-agentcore:`
    # for both control-plane (CreateAgentRuntime, etc) and data-plane
    # (InvokeAgentRuntime). The boto3 service name `bedrock-agentcore-control`
    # is misleading — see tasks/lessons.md Bug 43.
    # Also, observed live 2026-05-16: AgentCore does NOT honor
    # `bedrock-agentcore:*` wildcard authorization — explicit per-action
    # grants are required even though `iam:simulate-principal-policy`
    # claims `*` allows everything. See tasks/lessons.md Bug 47.
    # Bedrock model invocation + catalog. Resource "*" required: models are
    # user-selectable per flow and cross-region inference profiles resolve to
    # foundation-model ARNs in other regions; List* verbs are account-level.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
                # Loom-study 5.1 — live model catalog (/api/models) discovers
                # available Bedrock models + inference profiles at request time.
                "bedrock:ListFoundationModels",
                "bedrock:ListInferenceProfiles",
                # ListKnowledgeBases (account-level, no resource ARN form) is
                # used by the KB cleanup path to resolve ids.
                "bedrock:ListKnowledgeBases",
            ],
            resources=["*"],
        )
    )
    # KB cleanup on runtime delete (Bug 90). KB ids are service-generated, so
    # knowledge-base/* in this account+region is the tightest pattern.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "bedrock:GetKnowledgeBase",
                "bedrock:DeleteKnowledgeBase",
                "bedrock:DeleteDataSource",
                "bedrock:GetDataSource",
                "bedrock:ListDataSources",
            ],
            resources=[f"arn:aws:bedrock:*:{stack.account}:knowledge-base/*"],
        )
    )
    # Guardrail cleanup on runtime delete. The manifest delete path (and the
    # legacy guardrails_result fallback) run in THIS Lambda and call
    # DeleteGuardrail — without it the guardrail orphans with AccessDenied
    # (Bug 165). Guardrail ids are service-generated → guardrail/* pattern.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "bedrock:GetGuardrail",
                "bedrock:DeleteGuardrail",
            ],
            resources=[f"arn:aws:bedrock:*:{stack.account}:guardrail/*"],
        )
    )
    # S3 Vectors teardown (Bug 167): the KB step self-provisions a vector
    # bucket+index; the manifest delete path in THIS Lambda tears it down.
    # Scoped to vector-bucket ARNs in this account+region.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "s3vectors:GetVectorBucket",
                "s3vectors:DeleteVectorBucket",
                "s3vectors:ListIndexes",
                "s3vectors:GetIndex",
                "s3vectors:DeleteIndex",
            ],
            resources=[f"arn:aws:s3vectors:*:{stack.account}:bucket/*"],
        )
    )
    # ListVectorBuckets is account-level (no resource ARN form).
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["s3vectors:ListVectorBuckets"],
            resources=["*"],
        )
    )
    # OpenSearch Serverless teardown: collection verbs scope to collection
    # ARNs; security/access-policy deletes are account-level APIs with no
    # resource-level permission support (Resource must stay "*").
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["aoss:DeleteCollection"],
            resources=[f"arn:aws:aoss:*:{stack.account}:collection/*"],
        )
    )
    # BatchGetCollection is an account-level API — it fails AccessDenied when
    # scoped to collection ARNs (live-verified by the matrix run).
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["aoss:BatchGetCollection", "aoss:DeleteSecurityPolicy", "aoss:DeleteAccessPolicy"],
            resources=["*"],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                # Explicit AgentCore actions used by the deployment Lambda:
                # /api/test-runtime invokes; /api/runtime/{id} DELETE
                # cascades through Get/Delete on runtime + endpoint +
                # gateway + memory + policy resources.
                "bedrock-agentcore:InvokeAgentRuntime",
                # /api/test-mcp-runtime/* invokes WITH runtimeUserId (the caller's
                # identity is forwarded to the MCP server), and AgentCore authorises
                # that form as a distinct action. Measured live 2026-09-28: the MCP
                # runtime was READY, IAM simulation allowed InvokeAgentRuntime on both
                # ARN forms, and every discovery still died AccessDeniedException until
                # this grant. Same resources as InvokeAgentRuntime (service reference:
                # runtime, runtime-endpoint).
                "bedrock-agentcore:InvokeAgentRuntimeForUser",
                # Phase 1 (Loom-study 1.2) — OBO dry-run (routers/identity
                # test-obo) exchanges the caller's JWT for an on-behalf-of
                # downstream token to PROVE delegation runs as the user.
                "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                "bedrock-agentcore:GetResourceOauth2Token",
                "bedrock-agentcore:CreateAgentRuntime",
                "bedrock-agentcore:GetAgentRuntime",
                "bedrock-agentcore:UpdateAgentRuntime",
                "bedrock-agentcore:DeleteAgentRuntime",
                "bedrock-agentcore:ListAgentRuntimes",
                "bedrock-agentcore:CreateAgentRuntimeEndpoint",
                "bedrock-agentcore:GetAgentRuntimeEndpoint",
                "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
                "bedrock-agentcore:UpdateAgentRuntimeEndpoint",
                "bedrock-agentcore:ListAgentRuntimeEndpoints",
                "bedrock-agentcore:CreateGateway",
                "bedrock-agentcore:GetGateway",
                "bedrock-agentcore:UpdateGateway",
                "bedrock-agentcore:DeleteGateway",
                "bedrock-agentcore:ListGateways",
                "bedrock-agentcore:CreateGatewayTarget",
                "bedrock-agentcore:DeleteGatewayTarget",
                "bedrock-agentcore:ListGatewayTargets",
                "bedrock-agentcore:GetGatewayTarget",
                "bedrock-agentcore:UpdateGatewayTarget",  # Bug 171 retry parity
                # Phase A SaaS connectors: the direct-deploy path (services/
                # deployment.py -> deploy_gateway / cleanup_gateway_resources)
                # syncs targets and mints/reads/deletes BOTH api-key and
                # oauth2 credential providers — Bug 9 parity with the SFN
                # gateway step. Get* used by cleanup to confirm existence.
                "bedrock-agentcore:SynchronizeGatewayTargets",
                "bedrock-agentcore:CreateApiKeyCredentialProvider",
                "bedrock-agentcore:GetApiKeyCredentialProvider",
                "bedrock-agentcore:DeleteApiKeyCredentialProvider",
                "bedrock-agentcore:ListApiKeyCredentialProviders",
                "bedrock-agentcore:CreateOauth2CredentialProvider",
                "bedrock-agentcore:GetOauth2CredentialProvider",
                "bedrock-agentcore:DeleteOauth2CredentialProvider",
                "bedrock-agentcore:ListOauth2CredentialProviders",
                # Update*: reusing an existing provider by name must REPOINT it at
                # the current secret. Without these the "already exists" path
                # 403s and, worse, silently keeps sending the key the provider was
                # first created with — a rotation that never takes effect. See
                # gateway_deployer._ensure_api_key_credential_provider.
                "bedrock-agentcore:UpdateApiKeyCredentialProvider",
                "bedrock-agentcore:UpdateOauth2CredentialProvider",
                "bedrock-agentcore:CreateMemory",
                "bedrock-agentcore:GetMemory",
                "bedrock-agentcore:DeleteMemory",
                "bedrock-agentcore:ListMemories",
                "bedrock-agentcore:CreatePolicyEngine",
                "bedrock-agentcore:GetPolicyEngine",
                "bedrock-agentcore:DeletePolicyEngine",
                "bedrock-agentcore:ListPolicyEngines",
                # NOTE: Agent Registry actions are NOT here — at GA the Registry
                # became its own AWS service with an `agent-registry:` action
                # prefix. See the dedicated statement below.
                "bedrock-agentcore:CreatePolicy",
                "bedrock-agentcore:DeletePolicy",
                # UpdatePolicy: the lazy promoter recovers a CREATE_FAILED
                # permit by UPDATING it in place (race-free — no delete/create
                # name-collision window) once the gateway converges. Without
                # this action the update is silently AccessDenied and the
                # permit stays CREATE_FAILED forever, so ENFORCE never engages.
                "bedrock-agentcore:UpdatePolicy",
                "bedrock-agentcore:ListPolicies",
                "bedrock-agentcore:GetPolicy",
                # Bug 134: gateway-scoped policy create/delete is authorized as
                # ManageResourceScopedPolicy on the gateway ARN (not CreatePolicy).
                # Get/ListResourceScopedPolic* used to follow and were removed:
                # neither is a real AgentCore IAM action, so they granted nothing
                # and only implied capability. IAM Access Analyzer is what
                # established that (INVALID_ACTION). Note that Access Analyzer
                # alone is NOT sufficient to delete a grant — it also reports
                # CreateTokenVault as nonexistent, and a live deploy fails with
                # AccessDenied on exactly that action. Absence from the reference
                # PLUS no caller in the repo PLUS no implicit service-side
                # authorization is the bar. See the full oracle hierarchy in
                # platform/step_lambdas.py.
                "bedrock-agentcore:ManageResourceScopedPolicy",
                # AgentCore's DeleteAgentRuntime cascades into deleting
                # the runtime's auto-created workload-identity record;
                # the caller principal must hold this verb too. Verified
                # live 2026-05-16 — see tasks/lessons.md Bug 53.
                "bedrock-agentcore:GetWorkloadIdentity",
                "bedrock-agentcore:DeleteWorkloadIdentity",
                "bedrock-agentcore:ListWorkloadIdentities",
                # Phase 1 Gap 1C — eval results endpoint reads
                # OnlineEvaluationConfigs from the AgentCore control plane.
                "bedrock-agentcore:ListOnlineEvaluationConfigs",
                "bedrock-agentcore:GetOnlineEvaluationConfig",
                # M-2 (security review 2026-05-28): destroy_runtime
                # cascade-deletes orphaned eval configs to avoid PII /
                # billing residue under deleted runtimes. See Bug 123.
                "bedrock-agentcore:DeleteOnlineEvaluationConfig",
                # Phase B — AgentCore Harness (parallel deploy path).
                # The deployment Lambda owns the direct-deploy path
                # (services/deployment.py, Bug-9 parity with the SFN
                # harness step) plus test (InvokeHarness, DATA plane —
                # same action prefix, Bug 43) and delete (destroy_harness)
                # for deployment_mode=="harness". NO harness-endpoint verbs
                # exist.
                "bedrock-agentcore:CreateHarness",
                "bedrock-agentcore:GetHarness",
                "bedrock-agentcore:ListHarnesses",
                "bedrock-agentcore:UpdateHarness",
                "bedrock-agentcore:DeleteHarness",
                "bedrock-agentcore:InvokeHarness",
            ],
            # Least privilege: AgentCore control-plane resources (runtimes,
            # gateways, memories, harnesses, policies, registries, credential
            # providers, workload identities) are created dynamically per user
            # deploy — ARNs are unknowable at synth time, and AgentCore does
            # not honor `bedrock-agentcore:*` wildcards (Bug 47), so this is
            # scoped by the exact action list above instead of the resource.
            # (S3 Vectors / aoss / bedrock KB+guardrail verbs live in their own
            # ARN-scoped statements above.)
            resources=["*"],
        )
    )
    # SEPARATE, ARN-SCOPED STATEMENT: the ownership tag read that every delete on
    # this role's cascade depends on. This is the SAME defect fixed for the step
    # roles in platform/step_lambdas.py (see AGENTCORE_OWNERSHIP_READ_TYPES there)
    # and it is more severe here, because this is the NORMAL user-initiated
    # teardown — DELETE /api/runtime/{id} and the direct-deploy path — not a
    # rollback arm. deployment_handler.py calls assert_agentcore_resource_owned at
    # :2365, :2389, :2410, :2430, :2551, :3706, :3733, :3806 and
    # delete_owned_credential_provider at :2484 and :4171; every one of those ends
    # in list_tags_for_resource. The action was absent from the explicit AgentCore
    # list above, so each of those reads raised AccessDeniedException, which
    # resource_ownership turns into ResourceDeletionRefused: the delete is refused
    # (correctly — an unreadable owner is not proof of ownership) and the resource
    # is LEFT RUNNING AND BILLING in the customer's account.
    #
    # Deliberately NOT folded into the resources=["*"] statement above. That
    # statement is a wildcard because AgentCore mints ids at create time and does
    # not honor `bedrock-agentcore:*` (Bug 47), but a tag READ needs no unknowable
    # id: the account and the resource type are both known at synth time, exactly
    # as for InvokeGateway below. ARCC cnt_L4ZLZgjrCctfxl and cnt_a9beEXtEalSwBG:
    # enumerate the action and scope it, never widen to make an ownership check
    # pass — and note the check itself stays, this grant only lets it RUN.
    #
    # Region is wildcarded, not pinned to stack.region: this handler deletes
    # resources in the region they were deployed to (`res_region` throughout
    # deployment_handler), so a home-pinned ARN would fail closed on exactly the
    # cross-region teardown the multi-region work exists to support.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["bedrock-agentcore:ListTagsForResource"],
            resources=[
                f"arn:aws:bedrock-agentcore:*:{stack.account}:{tail}"
                for tail in dict.fromkeys(
                    t
                    for type_name in DEPLOYMENT_LAMBDA_OWNERSHIP_READ_TYPES
                    for t in AGENTCORE_TYPE_ARN_TAIL[type_name]
                )
            ],
        )
    )
    # SEPARATE, ARN-SCOPED STATEMENT: creating a gateway-scoped Cedar policy
    # requires being able to CALL the gateway, because AgentCore resolves the
    # gateway named in the statement AS THE CALLER. This role creates policies on
    # two paths — the direct (non-SFN) deploy in services/deployment.py and the
    # lazy promoter in services/policy_promoter.py, which is the component that
    # is supposed to RECOVER a permit that the SFN step left CREATE_FAILED. A
    # promoter without this action can never finish that recovery, so the
    # fail-closed engine stays deny-all forever.
    #
    # Same live proof and same reasoning as the policy step role in
    # platform/step_lambdas.py; see the comment there, including the controlled
    # two-role experiment that proved the wildcard gateway id below is honored
    # (ACTIVE with the statement, CREATE_FAILED without it, same engine, same
    # permit). ARCC cnt_AGx9pUNpmdOVZB:
    # InvokeGateway has a resource form and the ARN prefix is knowable at synth
    # time, so it does not belong in the `resources=["*"]` statement above.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeGateway"],
            # Per-deploy gateway ids are unknowable at synth time; the account,
            # region and type are not. An IAM `*` spans `/`, so a gateway's
            # targets are covered too.
            resources=[f"arn:aws:bedrock-agentcore:*:{stack.account}:gateway/*"],
        )
    )
    # Phase 6 (Loom) — AWS Agent Registry federation (opt-in).
    # SEPARATE STATEMENT, DIFFERENT ACTION PREFIX. Agent Registry graduated out
    # of AgentCore into its own AWS service at GA: the boto3 clients are
    # `agent-registry-control` / `agent-registry`, and BOTH planes authorize
    # under a single `agent-registry:` prefix (the control-plane model's
    # signingName is `agent-registry`, not `agent-registry-control`). Leaving
    # these as `bedrock-agentcore:*Registry*` silently AccessDenies every
    # federation call — and because the deploy-path auto-register is
    # best-effort, the denial shows up only as a skipped log line.
    # The data-plane search was also RENAMED: SearchRegistryRecords ->
    # SearchDiscoverableRegistryRecords.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                # control plane (agent-registry-control)
                "agent-registry:CreateRegistry",
                "agent-registry:GetRegistry",
                "agent-registry:UpdateRegistry",
                "agent-registry:DeleteRegistry",
                "agent-registry:ListRegistries",
                "agent-registry:CreateRegistryRecord",
                "agent-registry:GetRegistryRecord",
                "agent-registry:ListRegistryRecords",
                "agent-registry:UpdateRegistryRecord",
                "agent-registry:UpdateRegistryRecordStatus",
                "agent-registry:SubmitRegistryRecordForApproval",
                "agent-registry:DeleteRegistryRecord",
                # data plane (agent-registry) — discovery/search over APPROVED
                # records. Note the GA operation names.
                "agent-registry:SearchDiscoverableRegistryRecords",
                "agent-registry:ListDiscoverableRegistryRecords",
                "agent-registry:BatchGetDiscoverableRegistryRecord",
                # tagging: registries/records created by the platform are tagged
                # for cost attribution and teardown discovery.
                "agent-registry:TagResource",
                "agent-registry:UntagResource",
                "agent-registry:ListTagsForResource",
            ],
            # The registryId is supplied by an admin at runtime (opt-in feature),
            # so the registry/record ARNs are unknowable at synth time. Scoped by
            # the exact action list instead of the resource, matching the
            # AgentCore statement above.
            resources=["*"],
        )
    )
    # Phase 1 Gap 1C — CloudWatch Logs Insights query for evaluator scores.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "logs:StartQuery",
                "logs:GetQueryResults",
                "logs:StopQuery",
                "logs:DescribeLogGroups",
            ],
            resources=["*"],
        )
    )
    # M-2: also delete the eval-results log groups on destroy_runtime. F-04 (signoff-g10):
    # this verb used to share the Resource "*" above. Every delete_log_group reachable from
    # this Lambda names /aws/bedrock-agentcore/evaluations/results/{id}
    # (deployment_handler.py:3372, runtime_deployer.destroy_runtime:1649); the failure-path
    # step role already scoped the same verb to this prefix. DeleteLogGroup has no condition
    # keys, so the resource is the only lever (Service Reference feed). ARCC
    # cnt_SFJJhkOueCPRkd. Enforced by infra/tests/test_f04_deployment_lambda_log_deletion_is_scoped.py.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["logs:DeleteLogGroup"],
            resources=[f"arn:aws:logs:*:{stack.account}:log-group:/aws/bedrock-agentcore/evaluations/results/*"],
        )
    )
    # Phase 7 (opt-in) — cross-account deployment. The deploy path assumes a
    # target account's deployment role (services/deploy_target.session_for_
    # target). NAME-SCOPED to the agreed role name so this is NOT a blanket
    # AssumeRole: each target account must create a role named exactly
    # `AgentCoreFlowsDeploymentRole` trusting this platform account. Feature
    # is OFF by default (no target = no assume-role call is ever made).
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["sts:AssumeRole"],
            resources=["arn:aws:iam::*:role/AgentCoreFlowsDeploymentRole"],
        )
    )
    # Phase 1 Gap 1D — dashboard URL probe + cascade-delete on
    # destroy_runtime. CloudWatch dashboard IAM is account-level
    # (no resource ARN). DeleteDashboards is idempotent.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "cloudwatch:GetDashboard",
                "cloudwatch:DeleteDashboards",
                "cloudwatch:ListDashboards",
            ],
            resources=["*"],
        )
    )
    # Cleanup permissions: Cognito, Lambda, STS (needed by delete handler)
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "cognito-idp:DeleteUserPool",
                "cognito-idp:DeleteUserPoolClient",
                "cognito-idp:DeleteUserPoolDomain",
                "cognito-idp:DescribeUserPool",
                # A shared-pool gateway's own resource server (`agentcore-<gateway>`),
                # deleted from the manifest after its app client. This role had neither
                # action, so the delete path's resource-server cleanup was silently
                # inert — measured live on DeploymentLambdaRole. ListUserPoolClients is
                # the co-residency guard (two deploys sharing a gateway name share one
                # resource server) and returns names only, never a client secret; see
                # gateway_deployer.resource_server_is_unused for why that matters.
                "cognito-idp:DeleteResourceServer",
                "cognito-idp:ListUserPoolClients",
            ],
            resources=[f"arn:aws:cognito-idp:*:{stack.account}:userpool/*"],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["sts:GetCallerIdentity"],
            resources=["*"],
        )
    )
    # Tool tester + custom tool cleanup: create/invoke/delete Lambdas + IAM roles.
    # Also covers runtime_deployer.destroy_runtime which deletes the runtime's
    # execution role (Bug 27 fix — previously the API delete path leaked
    # AgentCoreRuntime-* roles).
    #
    # F-05 (signoff-g10): this used to be one statement carrying CreateRole, PassRole,
    # PutRolePolicy and TagRole on role/AgentCore*. Split by what each verb needs:
    #   * the read/detach/delete verbs stay on the wide prefix, unconditioned -- teardown
    #     names roles from manifests and legacy conventions;
    #   * iam:PutRolePolicy is GONE from this path. It was justified by "the direct-deploy
    #     harness path (create_harness_iam_role)", which this Lambda does not import: that
    #     function is reached only from harness_step. tool_tester attaches managed policies
    #     (AttachRolePolicy below) and writes no inline policy. The JIT approver's
    #     PutRolePolicy above is the one this role holds;
    #   * iam:CreateRole is scoped to the ONE role this Lambda creates, the tool sandbox
    #     (tool_tester.SANDBOX_ROLE_PREFIX = "AgentCore-ToolSandbox-"). runtime and harness
    #     roles are minted by step roles; nothing mounted here calls create_role otherwise;
    #   * iam:PassRole and iam:TagRole each get a statement of their own below, because
    #     their conditions (iam:PassedToService, aws:RequestTag) are absent from the other
    #     verbs' request contexts and would deny them.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "iam:GetRole",
                "iam:DetachRolePolicy",
                "iam:DeleteRole",
                "iam:ListAttachedRolePolicies",
                "iam:ListRolePolicies",
                "iam:DeleteRolePolicy",
            ],
            resources=[f"arn:aws:iam::{stack.account}:role/AgentCore*"],
        )
    )
    # iam:PermissionsBoundary is required here when enforcement is on (role_boundary.py, F-06).
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["iam:CreateRole"],
            resources=[f"arn:aws:iam::{stack.account}:role/AgentCore-ToolSandbox-*"],
            conditions=boundary_conditions(stack, role_boundary),
        )
    )
    # Retrofit path (F-06): the sandbox adopt branch (tool_tester._ensure_sandbox_role) and the
    # JIT router's target roles (role/AgentCore*, PutRolePolicy above) must carry the boundary
    # before enforcement is switched on; this Lambda puts it there. Pinned to the platform
    # boundary and never a Delete -- see grant_boundary_retrofit.
    grant_boundary_retrofit(
        role,
        stack,
        role_boundary,
        resources=[f"arn:aws:iam::{stack.account}:role/AgentCore*"],
    )
    # iam:PassRole, by the service the role is handed to (F-05). This Lambda passes a role
    # in exactly two places: tool_tester.create_function(Role=<sandbox role>) -- Lambda, on
    # the sandbox prefix only -- and gateway_deployer.configure_jwt_auth's UpdateAgentRuntime,
    # which must re-send the runtime's existing roleArn -- AgentCore, on the runtime-role
    # prefix. Without iam:PassedToService the wide grant let the sandbox function be created
    # under ANY AgentCore* role, the shared runtime role included.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["iam:PassRole"],
            resources=[f"arn:aws:iam::{stack.account}:role/AgentCore*"],
            conditions={"StringEquals": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}},
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["iam:PassRole"],
            resources=[f"arn:aws:iam::{stack.account}:role/AgentCore-ToolSandbox-*"],
            conditions={"StringEquals": {"iam:PassedToService": "lambda.amazonaws.com"}},
        )
    )
    # iam:TagRole for the sandbox role's create-with-Tags (IAM authorizes CreateRole's Tags
    # argument as TagRole on the new role). Pinned the same way every other tag grant on this
    # role is: the product value, the stack family, and an allowlist of keys -- tool_tester
    # sends owner_tag_list, i.e. exactly ManagedBy + AgentCoreStack. See the role/*-role
    # TagRole statement below for why a request-tag pin is the bound on VALUE, not RESOURCE.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["iam:TagRole"],
            resources=[f"arn:aws:iam::{stack.account}:role/AgentCore-ToolSandbox-*"],
            conditions={
                "StringEquals": {"aws:RequestTag/ManagedBy": "agentcore-flows"},
                "StringLike": {"aws:RequestTag/AgentCoreStack": f"{cfg.project}-{cfg.env}-*"},
                "ForAllValues:StringLike": {"aws:TagKeys": ["ManagedBy", "AgentCoreStack"]},
            },
        )
    )
    # AttachRolePolicy, split out to carry the iam:PolicyARN condition. This role
    # creates the tool-sandbox role that AI-generated code then runs as, so an
    # unconditioned attach here is the shortest path from "a tool did something
    # unexpected" to "a tool ran as an administrator". See
    # ATTACHABLE_MANAGED_POLICIES for the closed set and for why DetachRolePolicy
    # above is deliberately left unconditioned. Under boundary enforcement the target
    # role must also carry the platform boundary (F-06).
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["iam:AttachRolePolicy"],
            resources=[f"arn:aws:iam::{stack.account}:role/AgentCore*"],
            conditions={
                "ArnEquals": {"iam:PolicyARN": list(ATTACHABLE_MANAGED_POLICIES)},
                **(boundary_conditions(stack, role_boundary) or {}),
            },
        )
    )
    # F-05: whatever the grants above and below reach, the two roles every tenant's agent
    # runs as are never this Lambda's to rewrite. See shared_role_guard.py.
    deny_mutating_the_shared_runtime_roles(
        role,
        stack,
        shared_runtime_role=shared_runtime_role,
        shared_mcp_runtime_role=shared_mcp_runtime_role,
    )
    # Bug 138/139: runtime_deployer.destroy_runtime also cleans up the
    # direct-deploy execution-role convention `{runtime_name}-role` (Bug 57),
    # whose name does NOT start with "AgentCore". The first cut scoped this to
    # role/*-role, but that wildcard matches ANY account role ending in '-role'
    # (cdk-exec roles, customer lambda roles, etc.) — a least-privilege
    # regression. Bug 139 fix: runtime exec roles are now TAGGED
    # ManagedBy=agentcore-flows at creation, so we gate the cleanup-only verbs
    # (read/detach/delete — NOT CreateRole/PassRole) on that tag. A role we
    # didn't create can never carry the tag, so this can no longer touch
    # unrelated account roles. iam:TagRole below lets us stamp/repair the tag.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "iam:GetRole",
                "iam:DetachRolePolicy",
                "iam:DeleteRole",
                "iam:ListAttachedRolePolicies",
                "iam:ListRolePolicies",
                "iam:DeleteRolePolicy",
            ],
            resources=[f"arn:aws:iam::{stack.account}:role/*-role"],
            conditions={"StringEquals": {"aws:ResourceTag/ManagedBy": "agentcore-flows"}},
        )
    )
    # Repair the ownership tags on a runtime exec role this deployment ALREADY owns.
    #
    # F-03 (signoff-g10): this was an unconditioned iam:TagRole on the same role/*-role the
    # tag-gated cleanup statement above matches, which made that gate self-satisfiable --
    # TagRole(prod-app-role, ManagedBy=agentcore-flows) then DeleteRole(prod-app-role). The
    # comment said "a role we didn't create can never carry the tag"; this grant made it false.
    #
    # Three pins, all confirmed against the Service Reference feed (aws:RequestTag/aws:TagKeys
    # on the action; aws:ResourceTag on the role resource):
    #   * aws:ResourceTag/ManagedBy -- it may re-tag ONLY a role that already carries the
    #     product tag. The one live caller on this prefix is the reused-role branch of
    #     runtime_deployer.create_runtime_iam_role, which runs AFTER
    #     assert_this_deployment_may_mutate has read the tags, so the repair still works and an
    #     untagged role can never be claimed. Nothing on this Lambda CREATES a *-role role (its
    #     CreateRole is the sandbox prefix), so no create-with-tags path needs the untagged form.
    #   * aws:RequestTag/ManagedBy and /AgentCoreStack -- it may only write the product value it
    #     governs and this stack family's owner value, never relabel a role to another stack's.
    #   * aws:TagKeys -- exactly what governed_tag_list sends: the two ownership keys plus the
    #     namespaced governance keys (config.GOVERNANCE_TAG_KEY_PREFIXES).
    # TagRole is alone in the statement: a request-tag condition denies every action whose
    # request carries no tags. ARCC cnt_SFJJhkOueCPRkd. Enforced by
    # infra/tests/test_f03_tag_role_cannot_satisfy_its_own_gate.py.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["iam:TagRole"],
            resources=[f"arn:aws:iam::{stack.account}:role/*-role"],
            conditions={
                "StringEquals": {
                    "aws:ResourceTag/ManagedBy": "agentcore-flows",
                    "aws:RequestTag/ManagedBy": "agentcore-flows",
                },
                "StringLike": {"aws:RequestTag/AgentCoreStack": f"{cfg.project}-{cfg.env}-*"},
                "ForAllValues:StringLike": {
                    "aws:TagKeys": ["ManagedBy", "AgentCoreStack"] + governance_tag_key_globs()
                },
            },
        )
    )
    # Read/invoke/delete on every function this path may have to tear down. NOTE the
    # absence of lambda:CreateFunction: it used to be in this statement, on
    # function:AgentCore* + MCPServer*, and that was strictly more authority than the
    # deployment Lambda can use. Every create_function call site in the backend is
    # gateway_deployer's two (create_knowledge_base_lambda, _create_or_update_lambda,
    # both reached only from deploy_gateway on the GATEWAY STEP role) and
    # tool_tester's one, which creates AgentCore-ToolTest-<uuid> and nothing else.
    # deployment_handler never imports deploy_gateway -- it appears there in a comment
    # only -- so the broad create grant authorized names this role never creates.
    #
    # It was also an inconsistency that hid a real invariant: lambda:CreateFunction is a
    # privilege-escalation primitive (ARCC cnt_pXauQr9E6bKwke / cnt_L4ZLZgjrCctfxl -- the
    # new function runs with whatever role is passed to it), and create-with-Tags needs
    # lambda:TagResource on the SAME request. This role holds TagResource only on the
    # sandbox prefix, so on any wider name it could create a function it could not tag --
    # which, on the gateway path, is a hard failure with no retry-untagged fallback.
    # Narrowing the create to where the tag grant already reaches makes the pair exact.
    # Enforced by test_the_tool_lambda_ownership_grant.
    # test_every_role_that_creates_a_tool_lambda_can_also_tag_it.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "lambda:GetFunction",
                "lambda:DeleteFunction",
                # Required by _release_shared_tool_lambda (Defect C): the manifest
                # teardown ref-counts SHARED tool Lambdas (AgentCoreDynamicTools /
                # AgentCoreCustomerSupportTools) — it must read the resource policy
                # (GetPolicy) and drop this gateway's AllowAgentCoreInvoke-<role>
                # statement (RemovePermission). Without these the release helper
                # fail-safes to "kept": grants are never pruned and the Lambda
                # leaks at refcount zero.
                "lambda:GetPolicy",
                "lambda:RemovePermission",
                # The ownership READ every delete on this path now does first (F-7c):
                # the manifest teardown, the shared-singleton release, the per-gateway
                # tool function, the custom tool functions, and the KB tool function
                # whose name is DERIVED from the deployment id rather than recorded.
                # Without it the authorizer cannot prove ownership, refuses, and the
                # API's own delete leaks every function while still returning 200.
                "lambda:ListTags",
            ],
            # Legacy manifest rows may still name unscoped AgentCore* functions from builds
            # that predate the stack token (F-7d), so the read/delete grant keeps the wide
            # prefix; every delete on this path is ownership-gated in code. The
            # function:MCPServer* entry that used to sit here (Bug 175) is gone: no Lambda of
            # that name was ever created -- "MCPServerRuntime" is a gateway TARGET name, and
            # the manifest row that claimed a Lambda by it was a phantom (F-74). A grant
            # justified by a phantom is authority over any function whose name happens to
            # start with MCPServer, which is not this platform's to hold.
            resources=[f"arn:aws:lambda:*:{stack.account}:function:AgentCore*"],
        )
    )
    # Creating AND invoking the sandbox function -- the ONLY function this role creates,
    # and the only one it invokes other than itself.
    #
    # Both actions were moved here out of the broad AgentCore*/MCPServer* statement above
    # (2026-09-22); see that statement's comment for why the broad create was authority
    # this path cannot use, and for the create/tag pairing it broke.
    #
    # lambda:InvokeFunction followed for the same reason, established the same way: across
    # the WHOLE of backend/src/app exactly four call sites invoke a Lambda, and all four
    # are accounted for. deployment_handler's three (_is_slow_delete's async delete
    # dispatch, the async generate job, the async tool-test job) all pass
    # os.environ["AWS_LAMBDA_FUNCTION_NAME"], i.e. this very function, which has its own
    # exact-ARN self-invoke statement and does not need a prefix grant. tool_tester's one
    # invokes the AgentCore-ToolTest-<uuid4> sandbox it just created. Nothing in the
    # backend invokes an AgentCore* tool Lambda or an MCPServer* function from this role:
    # a tool Lambda is invoked by the AgentCore GATEWAY's own role through the function's
    # resource policy, never by the platform's control plane.
    #
    # Why it is worth narrowing rather than leaving: an invoke is arbitrary code execution
    # under the TARGET function's execution role, so a prefix-wide invoke grant is a
    # lateral-movement primitive over every tool Lambda in the account whose name starts
    # with AgentCore -- including the foreign ones this account demonstrably holds
    # (AgentCoreDynamicToolsLambdaRole and friends predate this platform). ARCC
    # cnt_AGx9pUNpmdOVZB: scope to the necessary actions and resource ARNs.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["lambda:CreateFunction", "lambda:InvokeFunction"],
            resources=[f"arn:aws:lambda:*:{stack.account}:function:AgentCore-ToolTest-*"],
        )
    )
    # Tool-test sandbox (F-11). The temporary function is created WITH owner tags
    # so teardown can tell it apart from a foreign AgentCore-* function, and
    # create_function's Tags argument is authorized as lambda:TagResource on the
    # function being created -- not as part of lambda:CreateFunction. Without this
    # statement every tool test fails at CreateFunction with AccessDenied, which
    # is exactly how it failed live before this grant existed.
    #
    # PutFunctionConcurrency caps the blast radius of code a model wrote. It is
    # best-effort in the service (a deployment that cannot cap still tests tools)
    # and mandatory only under TOOL_SANDBOX_REQUIRE_ISOLATION, but there is no
    # reason to withhold it.
    #
    # Scoped to the AgentCore-ToolTest-* prefix rather than the broader AgentCore*
    # used above (ARCC cnt_AGx9pUNpmdOVZB: scope to the necessary actions and
    # resource ARNs). tool_tester.TOOL_TEST_FN_PREFIX is the only producer of these
    # names and mints a fresh uuid4 suffix per invocation, so nothing long-lived is
    # created under it by us.
    #
    # The tag WRITE is nonetheless conditioned, and split out of the concurrency
    # statement to do it. The previous version of this comment argued that the uuid4
    # suffix meant nothing foreign could be re-tagged through this statement. That
    # describes what our code does, not what the grant permits: the live account holds
    # 57 pre-existing AgentCore-ToolTest-* log groups and a foreign AgentCoreToolTestRole
    # from months before this platform, so the prefix is demonstrably not ours alone, and
    # an unconditioned TagResource on it would let the platform stamp
    # AgentCoreGatewayTarget=allow onto a function under that prefix and thereby
    # self-grant the tag-gated lambda:AddPermission capability in step_lambdas.py. Same
    # chain as the AgentCore* grant there, one prefix narrower -- see
    # infra/tests/test_the_tool_lambda_ownership_grant.py, which asserts the property
    # across every statement rather than only the one it was written for.
    #
    # The allowed keys are exactly what tool_tester writes: create_function(Tags=...)
    # with owner_tag_list(region) and nothing else (tool_tester.py, the only producer).
    # PutFunctionConcurrency must stay UNCONDITIONED, hence the split: its request
    # carries no tags, so a StringEquals on aws:RequestTag/ManagedBy would evaluate
    # false and deny it, silently dropping the sandbox's concurrency cap.
    #
    # The AgentCoreStack VALUE is pinned too, added 2026-09-22 alongside the identical
    # fix on the gateway role's function:AgentCore* grant and the runtime grant in
    # step_lambdas.py. Pinning only ManagedBy left the owner value caller-chosen, which
    # the key allowlist does NOT cover: an arbitrary value lets this role stamp another
    # deployment's stack id onto a sandbox function, and AgentCoreStack is what teardown
    # and assert_this_deployment_may_mutate match on. tool_tester threads the SAME region
    # into _create_lambda_client and into owner_tag_list, so ${aws:RequestedRegion}
    # always equals the value's region component -- that is the property that makes this
    # pin safe rather than an outage, and it is why the variable is used instead of a
    # synth-time literal.
    #
    # NOT prevented, and deliberately not implied by any test name here: lambda has no
    # create-only condition key (aws:RequestTag/${TagKey} and aws:TagKeys are the only
    # ones, per the Service Reference feed), so this still permits stamping OUR ownership
    # onto an untagged foreign function under the AgentCore-ToolTest- prefix -- and the
    # live account holds 57 pre-existing groups under that prefix, so the prefix really
    # is not ours alone. A request tag bounds which VALUE may be written, not which
    # RESOURCE it lands on. Plain StringEquals, never IfExists: ARCC cnt_SFJJhkOueCPRkd
    # notes IfExists evaluates TRUE when the key is absent, which would authorize an
    # untagged request outright.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["lambda:TagResource"],
            resources=[f"arn:aws:lambda:*:{stack.account}:function:AgentCore-ToolTest-*"],
            conditions={
                "StringEquals": {
                    "aws:RequestTag/ManagedBy": "agentcore-flows",
                    "aws:RequestTag/AgentCoreStack": (f"{cfg.project}-{cfg.env}-${{aws:RequestedRegion}}"),
                },
                "ForAllValues:StringEquals": {"aws:TagKeys": ["ManagedBy", "AgentCoreStack"]},
            },
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["lambda:PutFunctionConcurrency"],
            resources=[f"arn:aws:lambda:*:{stack.account}:function:AgentCore-ToolTest-*"],
        )
    )
    # Claim the sandbox function's log group before Lambda creates it implicitly.
    # A group Lambda makes has no retention policy, so CloudWatch keeps it forever,
    # and the function name carries a fresh uuid4 per tool test -- measured live on
    # acfe2e-p0920 as 53 groups with retentionInDays null on all 53. See
    # tool_tester.SANDBOX_LOG_RETENTION_DAYS for why pre-creation rather than a
    # sweep: deleting a group while its function exists just makes Lambda recreate it.
    #
    # Same prefix scoping as the statement above, for the same reason. All three
    # actions support resource-level permissions on log-group (confirmed against the
    # Service Reference feed).
    #
    # logs:TagResource IS required, and this comment previously said the opposite.
    # The reasoning was that CreateLogGroup authorizes its own `tags` argument
    # through aws:RequestTag, so no separate action would be needed. A live tool
    # test on acfe2e-p0920 on 2026-09-21 says otherwise, and CloudWatch names the
    # missing permission itself:
    #
    #   AccessDeniedException ... is not authorized to perform CreateLogGroup with
    #   Tags. An additional permission "logs:TagResource" is required.
    #
    # The group was still created, with retention, because tool_tester retries the
    # create untagged -- so the feature never broke and nothing failed loudly. What
    # it cost was the ownership tags, which is what a teardown matches on, so the
    # group would have been unattributable to this deployment. Exactly the same
    # shape as the lambda:TagResource outage: tag-on-create is authorized as a
    # separate action even where the API makes it look like one call. The only
    # oracle that found it was reading the deployed group's tags after a real test.
    #
    # logs:TagResource is SPLIT OUT and conditioned, while CreateLogGroup and
    # PutRetentionPolicy stay unconditioned in the statement below. The split is forced
    # by the feed, not stylistic:
    #
    #   * logs:PutRetentionPolicy supports NO condition keys at all, so conditioning the
    #     bundled statement on aws:RequestTag would deny retention outright -- and
    #     retention is the whole point of pre-creating the group. That is the bug this
    #     split exists to avoid, and it would have been silent: the group would still be
    #     created, just kept forever.
    #   * logs:CreateLogGroup DOES support aws:RequestTag/${TagKey}, but it must stay
    #     unconditioned because _ensure_sandbox_log_group RETRIES THE CREATE UNTAGGED
    #     when the tagged form is denied (tool_tester.py). An untagged request carries no
    #     aws:RequestTag key, a plain StringEquals on it therefore evaluates FALSE, and
    #     conditioning the create would kill the very fallback that keeps a tool test
    #     working when tagging is unavailable.
    #
    # So the conditioned action is exactly the one that writes the tag. Same ownership
    # pins and same stated residual as the lambda:TagResource grant above: logs has no
    # create-only condition key either (aws:RequestTag/${TagKey} and aws:TagKeys only,
    # per the Service Reference feed for logs), so this bounds which owner VALUE may be
    # written under the prefix, not which group it lands on.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["logs:CreateLogGroup", "logs:PutRetentionPolicy"],
            resources=[
                f"arn:aws:logs:*:{stack.account}:log-group:/aws/lambda/AgentCore-ToolTest-*",
            ],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["logs:TagResource"],
            resources=[
                f"arn:aws:logs:*:{stack.account}:log-group:/aws/lambda/AgentCore-ToolTest-*",
            ],
            conditions={
                "StringEquals": {
                    "aws:RequestTag/ManagedBy": "agentcore-flows",
                    "aws:RequestTag/AgentCoreStack": (f"{cfg.project}-{cfg.env}-${{aws:RequestedRegion}}"),
                },
                "ForAllValues:StringEquals": {"aws:TagKeys": ["ManagedBy", "AgentCoreStack"]},
            },
        )
    )
    # Placing the sandbox function in the isolated VPC (F-11 phase 2). Lambda
    # validates a VpcConfig at CreateFunction time using the CALLER's credentials,
    # so without these three the create fails with an AccessDenied naming ec2 --
    # the same class of outage the missing lambda:TagResource caused.
    #
    # Resource "*" is forced: every one of these is a list/describe action that AWS
    # does not support resource-level permissions for (confirmed against the
    # Service Reference feed for ec2, not the docs). They are read-only and reveal
    # subnet and security-group metadata in this account only. The sandbox
    # function's OWN role gets AWSLambdaVPCAccessExecutionRole for the ENI
    # lifecycle -- attached by tool_tester._ensure_sandbox_role, not here, because
    # that role is created at runtime per deployment.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "ec2:DescribeSubnets",
                "ec2:DescribeSecurityGroups",
                "ec2:DescribeVpcs",
            ],
            resources=["*"],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            # The DELETE path's counterpart to the gateway step's opt-in grant
            # (step_lambdas.py, AgentCoreGatewayTarget): a manifest teardown must be
            # able to drop this gateway's AllowAgentCoreInvoke-<role> statement from a
            # customer's bring-your-own-Lambda target. Removal verbs only — and
            # deliberately no lambda:DeleteFunction: a function the platform did not
            # create is never ours to delete, however it was tagged.
            actions=["lambda:GetPolicy", "lambda:RemovePermission"],
            resources=[f"arn:aws:lambda:*:{stack.account}:function:*"],
            conditions={"StringEquals": {"aws:ResourceTag/AgentCoreGatewayTarget": "allow"}},
        )
    )
    # S3 artifacts bucket: read/write for CFN template generation
    artifacts_bucket.grant_read_write(role)
    # ... except the dependency bundles the runtimes are built from (F-21, buckets.py).
    deny_writes_to_agentcore_deps(role, artifacts_bucket)
    # Region registration validates the regional bucket, and teardown deletes from it (F-41b).
    grant_regional_artifact_buckets(role, stack, "read_write", write_tags=True, read_tags=True)
    # Phase 3 Gap 3F — webhook trigger HMAC secret. routers/triggers.py
    # mints an HMAC signing secret per webhook trigger under the
    # agentcore-trigger/ namespace; destroy_runtime (runtime_deployer)
    # cascade-deletes it per trigger row on teardown (Bug 124). Scoped to
    # the agentcore-trigger/ prefix only.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                # secretsmanager:TagResource is NOT here. It is granted below, split by
                # prefix, because this one statement spans five prefixes written by three
                # different callers that send three different tag sets -- and because the
                # condition that bounds a tag write cannot share a statement with a read.
                # aws:RequestTag is absent on GetSecretValue/DescribeSecret, and a
                # StringEquals against an absent request tag does not match, so folding the
                # ownership pin onto this statement would deny every secret read this Lambda
                # makes. Fail-closed is an outage, not a tightening.
                "secretsmanager:CreateSecret",
                "secretsmanager:GetSecretValue",
                "secretsmanager:PutSecretValue",
                "secretsmanager:DeleteSecret",
                "secretsmanager:DescribeSecret",
            ],
            resources=[
                f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:agentcore-trigger/*",
                # Phase A SaaS connectors: the direct-deploy path (services/
                # deployment.py -> deploy_gateway / cleanup_gateway_resources)
                # mints/reads/deletes connector credential secrets under the
                # agentcore-connector/ prefix — Bug 9 parity with the SFN
                # gateway step. Secrets live ONLY here — never in canvas
                # JSON, DDB, or logs.
                f"arn:aws:secretsmanager:*:{stack.account}:secret:agentcore-connector/*",
                # Workstream B: the LiteLLM registry backend's virtual key. Its own
                # prefix on purpose — agentcore-provider/ is scoped to model-provider
                # keys, and agentcore-connector/ is swept by per-deployment teardown,
                # while a registry credential outlives every deployment. This role
                # serves /api/registry, so it is the only one that needs the grant.
                f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:agentcore-registry/*",
            ],
        )
    )
    # Bug 184 — TEARDOWN parity for the harness->gateway outbound OAuth2 credential
    # provider. handle_delete_runtime (this role) calls delete_oauth2_credential_provider,
    # which cascade-deletes the provider's backing client_secret. That secret lives under
    # the bedrock-agentcore-* identity namespace (NOT agentcore-connector/ — see Bug 83),
    # so without DeleteSecret here the teardown leaks the secret with "not authorized to
    # perform: secretsmanager:DeleteSecret" and the provider delete fails.
    #
    # F-24 (signoff-g10): these two prefixes used to sit in the statement above and so
    # carried Create/Get/Put as well, account-wide, while the justification covers the
    # DELETE only. Nothing on this Lambda reads or writes a bedrock-agentcore-* or
    # AgentCore* secret: the provider's secret is written by the service during the STEP
    # role's CreateOauth2CredentialProvider, and the runtime credentials this Lambda
    # resolves are agentcore-connector/. Delete plus the existence check that precedes it.
    # ARCC cnt_SFJJhkOueCPRkd. Enforced by tests/test_f24_identity_secret_prefixes_are_delete_only.py.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["secretsmanager:DeleteSecret", "secretsmanager:DescribeSecret"],
            resources=[
                f"arn:aws:secretsmanager:*:{stack.account}:secret:bedrock-agentcore-*",
                f"arn:aws:secretsmanager:*:{stack.account}:secret:AgentCore*",
            ],
        )
    )
    # --- secretsmanager:TagResource, split by prefix (F-77) -------------------
    #
    # ``CreateSecret`` with ``Tags=`` is authorized as ``secretsmanager:TagResource`` as
    # well, so these statements gate the CREATION of the secrets above, not a later
    # re-tagging. That is why the key allowlists must admit exactly what each caller
    # sends: a key the code sends but the allowlist omits does not fail a tagging step,
    # it fails the creation of a credential secret part-way through a deploy.
    #
    # The split exists because the prefixes are not equivalent. ``agentcore-connector/``
    # names the PRODUCT, not a deployment, so every deployment in the account mints its
    # connector credentials -- raw customer API keys and OAuth2 client secrets -- under
    # one shared prefix, and teardown therefore finds them by TAG
    # (``discover_deployment_bound_secrets`` filters on the name plus a matching
    # AgentCoreStack and DeploymentId). Unconditioned, this grant let any deployment
    # stamp its own ownership onto another live deployment's connector secret, whose next
    # teardown would then delete it -- verbatim the failure ``_put_connector_secret``'s
    # own comment says the owner tag exists to prevent: "one customer teardown destroying
    # another live deployment's raw customer API keys". ARCC cnt_SaTYaDCgBBJTcv (incorrect
    # tag propagation is a security failure, not only a cost-reporting one) and
    # cnt_6gBImtb08AJqCB (tags carry ABAC decisions).
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["secretsmanager:TagResource"],
            resources=[
                f"arn:aws:secretsmanager:*:{stack.account}:secret:agentcore-connector/*",
            ],
            conditions={
                "StringEquals": {
                    "aws:RequestTag/ManagedBy": "agentcore-flows",
                    # Both halves, not just the product value: a request carrying
                    # ManagedBy=agentcore-flows plus an ARBITRARY AgentCoreStack would
                    # satisfy a ManagedBy-only pin, and AgentCoreStack is the value
                    # teardown matches on -- so that is the forgery to block.
                    #
                    # The region is a policy variable rather than ``stack.region``:
                    # ``session_for_event`` honours ``event["target_region"]`` using this
                    # same role in another region, and a literal home region would deny
                    # every such deploy at CreateSecret.
                    "aws:RequestTag/AgentCoreStack": f"{cfg.project}-{cfg.env}-${{aws:RequestedRegion}}",
                    # F-01 (b): IdentityMode is an ABAC input -- the shared runtime role reads
                    # only IdentityMode=shared -- so its VALUE is closed to the two strings the
                    # backend sends. A typo'd value would otherwise mint a secret no runtime can
                    # read and surface as a late GetSecretValue denial instead of here. Safe to
                    # require: every TagResource on this prefix is a CreateSecret with Tags= from
                    # _put_connector_secret (no secretsmanager tag_resource call exists in the
                    # backend), and that one call always sends the key.
                    "aws:RequestTag/IdentityMode": ["shared", "per_agent"],
                },
                "ForAllValues:StringLike": {
                    # What _put_connector_secret sends: governed_tag_list -> ManagedBy +
                    # AgentCoreStack (owner_tags, merged last) + Purpose + IdentityMode, then
                    # secret_binding_tags -> OwnerSubHash + DeploymentId. Plus the two
                    # admin-configurable governance namespaces.
                    "aws:TagKeys": [
                        "ManagedBy",
                        "AgentCoreStack",
                        "Purpose",
                        "IdentityMode",
                        "OwnerSubHash",
                        "DeploymentId",
                    ]
                    + governance_tag_key_globs(),
                },
            },
        )
    )
    # ``agentcore-trigger/`` gets its own statement because its writer sends a DIFFERENT
    # set: routers/triggers.py ``_store_webhook_secret`` sends ManagedBy, Purpose,
    # owner_sub and created_at -- and no AgentCoreStack, because a webhook signing key is
    # owned by a USER and outlives every deployment. Applying the statement above to this
    # prefix would deny every webhook trigger creation.
    #
    # What this does buy: ``trigger_store`` refuses to delete a trigger secret unless
    # ManagedBy, Purpose AND owner_sub all match, so those tag values are an
    # authorization input. Pinning the two constants and closing the key set means a
    # buggy or injected code path on this role cannot flip ManagedBy, repurpose the
    # secret, or invent a new key on one.
    #
    # What it deliberately does NOT buy: ``owner_sub`` is per-user and this role is
    # shared by every caller of /api/triggers, so IAM has no principal-tag binding that
    # could stop one user's request from writing another user's sub. That boundary is
    # enforced in the router, which takes owner_sub from the authenticated caller rather
    # than from the request body. Recorded so nobody reads this condition as closing it.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["secretsmanager:TagResource"],
            resources=[
                f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:agentcore-trigger/*",
            ],
            conditions={
                "StringEquals": {
                    "aws:RequestTag/ManagedBy": "agentcore-flows",
                    "aws:RequestTag/Purpose": "trigger-webhook-hmac",
                },
                "ForAllValues:StringLike": {
                    "aws:TagKeys": ["ManagedBy", "Purpose", "owner_sub", "created_at"],
                },
            },
        )
    )
    # The identity namespaces keep an UNCONDITIONED grant, and that is a refusal to risk
    # an outage rather than an oversight. This role's direct-deploy path calls
    # CreateOauth2CredentialProvider, and the backing client_secret is created under
    # ``bedrock-agentcore-*`` by the SERVICE, on its own side, via a forward access
    # session. This platform sends no tags of its own there, and whether a FAS tag write
    # satisfies an ``aws:RequestTag`` condition is unproven -- getting it wrong fails
    # closed on gateway creation. It is also not where the escalation lives: teardown's
    # tag-based discovery only scans ``agentcore-connector/``. Same call as the
    # gateway/mcp_server step roles in step_lambdas.py; the two must not disagree.
    #
    # ``agentcore-registry/*`` is absent from all three statements on purpose:
    # registry_providers/litellm.py creates its virtual-key secret with no Tags at all,
    # so it needs no TagResource. If a future change starts tagging it, that change fails
    # closed at CreateSecret with an authorization error naming this action -- which is
    # the direction an unreviewed tag write should fail in.
    #
    # ``secret:AgentCore*`` (capital A, a DIFFERENT namespace to ``agentcore-*`` because IAM
    # resource matching is case-sensitive) used to be in this statement and is now gone. It
    # was kept for two review rounds on the grounds that narrowing a grant with an unproven
    # blast radius risks an outage; three independent oracles now say it is unreachable, so
    # that reasoning no longer applies:
    #   1. An AST pass over every ``create_secret`` in backend/src/app finds six writers and
    #      not one of them names this prefix.
    #   2. Nowhere in the repo is a Secrets Manager name constructed with that prefix. The
    #      capital-A literals that do exist are a DynamoDB table, an AgentCore Memory, an
    #      IAM Sid, two Cognito user pool names and FUNCTION_NAME_PREFIX -- no secret.
    #   3. Zero secrets matching it exist in the live account, alongside 8 real
    #      ``agentcore-connector/`` and 4 ``bedrock-agentcore-identity!default/`` secrets, so
    #      the sweep that found none was demonstrably able to see secrets.
    # Only the TAG action is narrowed. The combined create/read/delete statements above keep
    # the prefix: an unconditioned authority to STAMP OWNERSHIP TAGS on a namespace any
    # principal in the account can create a secret in is a different kind of grant from an
    # inert read on a namespace nothing populates, and narrowing both at once would conflate
    # two blast radii.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["secretsmanager:TagResource"],
            resources=[
                f"arn:aws:secretsmanager:*:{stack.account}:secret:bedrock-agentcore-*",
            ],
        )
    )
    # ListSecrets does not support resource-level scoping (must be on `*`).
    # The user-delete teardown (_run_delete_cleanup) finds, by tag, a secret no
    # manifest row names: unrecorded_deployment_secret_rows. Separate minimal
    # statement so the wildcard is visible and isolated; the per-role
    # AwsSolutions-IAM5 suppression already covers it.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["secretsmanager:ListSecrets"],
            resources=["*"],
        )
    )

    fn = _lambda.Function(
        stack,
        "DeploymentLambda",
        function_name=f"{cfg.project}-{cfg.env}-deployment",
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="src/app/deployment_handler.handler",
        code=backend_code,
        memory_size=512,
        # 120s until F-11 put a VPC on the tool-test sandbox. This function
        # async-invokes itself to run a tool test (deployment_handler
        # handle_test_tool, InvocationType="Event"), so the test's whole budget is
        # this timeout -- and a VPC-attached function waits on Lambda building a
        # Hyperplane ENI, measured live at 223.3s / 223.9s / 6.1s across three
        # consecutive runs in the sandbox VPC. The test therefore could not finish
        # inside 120s on a cold ENI mapping, and failed with an unactionable
        # "Tool testing failed unexpectedly".
        #
        # This bounds only asynchronous work. Synchronous API traffic is still
        # bounded by the HTTP API's own 30s integration timeout, so nothing a
        # browser waits on got slower. See tool_tester.ACTIVE_WAIT_SECONDS_VPC
        # (300s) -- this leaves headroom for that wait plus the test cases and
        # cleanup after it.
        timeout=Duration.seconds(600),
        role=role,
        tracing=_lambda.Tracing.ACTIVE,
        environment={
            "DEPLOYMENTS_TABLE_NAME": tables.deployments.table_name,
            "DEPLOYMENT_TABLE_NAME": tables.deployments.table_name,
            "GATEWAY_NAME_CLAIMS_TABLE_NAME": tables.gateway_name_claims.table_name,
            "WORKFLOWS_TABLE_NAME": tables.workflows.table_name,
            # F-55: the flow owner check. SSM carries the same name, but an explicit value keeps
            # the gate working if the SSM read is ever narrowed.
            "DYNAMODB_FLOWS_TABLE_NAME": tables.flows.table_name,
            # Phase 1 Gap 1A — versioning tables.
            "AGENT_VERSIONS_TABLE_NAME": tables.agent_versions.table_name,
            "RUNTIME_SLOTS_TABLE_NAME": tables.runtime_slots.table_name,
            # F-82: how long a "pending" AgentVersion row may hold the friendly runtime name
            # against another tenant. Derived from the state machine's own ceiling so the two
            # cannot drift -- see DEPLOYMENT_STATE_MACHINE_TIMEOUT_MINUTES in config.py for why
            # a pending row is otherwise permanent.
            "DEPLOY_PENDING_CLAIM_TTL_SECONDS": str(
                DEPLOYMENT_STATE_MACHINE_TIMEOUT_MINUTES * 60 + PENDING_CLAIM_SLACK_SECONDS
            ),
            # Phase 2 Gap 2A — agent registry table.
            "AGENT_REGISTRY_TABLE_NAME": tables.agent_registry.table_name,
            # Phase 2 Gap 2B — usage events table (cost_tracking store).
            "USAGE_EVENTS_TABLE_NAME": tables.usage_events.table_name,
            # Phase 2 Gap 2D — HITL requests table (routers/hitl.py store).
            "HITL_REQUESTS_TABLE_NAME": tables.hitl_requests.table_name,
            # Phase 3 Gap 3F — triggers registry table (routers/triggers.py).
            "TRIGGERS_TABLE_NAME": tables.triggers.table_name,
            "TRIGGER_DISPATCH_QUEUE_ARN": trigger_dispatch_queue.queue_arn,
            "TRIGGER_DISPATCH_QUEUE_URL": trigger_dispatch_queue.queue_url,
            "TRIGGER_RULE_PREFIX": trigger_rule_prefix,
            # Phase 3 Gap 3H — prompt library table (routers/prompts.py).
            "PROMPT_LIBRARY_TABLE_NAME": tables.prompt_library.table_name,
            # Phase 2 (Loom) governance tagging table (routers/tags.py +
            # deploy-time tag resolver).
            "TAG_POLICY_TABLE_NAME": tables.tag_policy.table_name,
            # The namespaces the step roles' aws:TagKeys allowlists permit. This Lambda owns
            # the API boundary, so it is where an out-of-namespace governance key is refused
            # with an explanation instead of becoming an AccessDenied inside a deployment that
            # has already created resources. Same constant, same value, both halves.
            GOVERNANCE_TAG_KEY_PREFIXES_ENV: governance_tag_key_prefixes_env_value(),
            # Phase 4 (Loom) FinOps — cost budgets table (routers/cost.py).
            "BUDGET_TABLE_NAME": tables.budget.table_name,
            # Phase 5 (Loom) — audit trail table (middleware + admin router).
            "AUDIT_TABLE_NAME": tables.audit.table_name,
            # Loom-study 1.6 — JIT IAM permission-request workflow table.
            "PERMISSION_REQUESTS_TABLE_NAME": tables.permission_requests.table_name,
            # Loom-study 1.1 — 3rd-party IdP group-claim mapping. When OIDC
            # federation is configured, a federated user's groups arrive under
            # this claim and are mapped to internal g-*/t-* groups by
            # services/auth.extract_cognito_groups. Empty when not federated.
            "OIDC_GROUPS_CLAIM": stack.node.try_get_context("oidc_groups_claim") or "",
            "OIDC_GROUP_MAP": stack.node.try_get_context("oidc_group_map") or "",
            "ARTIFACTS_BUCKET_NAME": artifacts_bucket.bucket_name,
            "ENVIRONMENT": cfg.env,
            "PROJECT_NAME": cfg.project,
            "APP_AWS_REGION": stack.region,
            "POWERTOOLS_SERVICE_NAME": "deployment",
            "TOOL_GENERATOR_MODEL_ID": f"{'eu' if stack.region.startswith('eu-') else 'apac' if stack.region.startswith('ap-') else 'us'}.anthropic.claude-sonnet-5",
            # Tool-test sandbox network (F-11). Testing a generated tool executes
            # code a language model wrote, and without these the temporary function
            # gets default Lambda egress: the open internet. ARCC
            # cnt_MSVB0Kk8WMwmmW requires untrusted code to run from a private
            # network with no public internet access, so these are set
            # unconditionally rather than being an opt-in -- tool_tester fails
            # closed when they are empty. See tool_sandbox_net.py for what the
            # sandbox can and cannot reach, and why losing HTTP during a *test* is
            # the intended trade-off rather than a broken feature.
            "TOOL_SANDBOX_SUBNET_IDS": sandbox_network.subnet_ids_csv,
            "TOOL_SANDBOX_SECURITY_GROUP_IDS": sandbox_network.security_group_ids_csv,
            "PYTHONPATH": "/var/task/src:/var/task:/var/task/lib",
            # Needed by destroy_runtime to skip cascade-deletion of the
            # stack-managed shared runtime role (Bug 62).
            "SHARED_RUNTIME_ROLE_ARN": shared_runtime_role.role_arn,
            # The model-free MCP variant of the shared role. Same teardown-protection
            # role: destroy_runtime must recognise it as stack-owned and never delete
            # it. A standalone FastMCP runtime selects it in the IAM step.
            "SHARED_MCP_RUNTIME_ROLE_ARN": shared_mcp_runtime_role.role_arn,
            # The permissions boundary every role this Lambda creates must carry (F-06,
            # role_boundary.py). Published unconditionally so the backend can pass it as
            # PermissionsBoundary= before the iam:PermissionsBoundary conditions are switched on.
            BOUNDARY_ARN_ENV: role_boundary.managed_policy_arn,
            # The shared gateway-auth pool, for TEARDOWN PROTECTION — not for a read.
            #
            # gateway_deployer.is_platform_owned_user_pool compares a pool id against
            # this variable, and every teardown path that can delete a user pool calls
            # it first. This Lambda reaches two of them:
            # deployment_handler.py:1605 (the `cognito_user_pool` manifest branch) and
            # gateway_deployer.py:4019, via cleanup_gateway_resources at
            # deployment_handler.py:2154 (the legacy `gateway_config and not
            # manifest_used` delete path).
            #
            # The variable was set on the step Lambdas and NOT here, so in this
            # process the predicate read "" and returned False for every pool. The
            # 4019 check is an if/elif: False does not mean "skip", it falls through to
            # the `elif user_pool_id:` branch, which deletes the hosted domain and then
            # calls delete_user_pool. So an inline delete of any gateway that used the
            # shared pool would have destroyed the pool holding EVERY deployed
            # gateway's app client, plus the warm hosted domain that takes >381s to
            # reprovision — and the CDK RemovalPolicy.RETAIN cannot bring back a pool
            # deleted out-of-band by the API. Confirmed live: the deployment Lambda's
            # environment had no GATEWAY_* keys at all while step-gateway and
            # step-status-update had both.
            #
            # The predicate itself was also hardened to not depend solely on this
            # variable (see is_platform_owned_user_pool), because an env var missing
            # from one of many Lambdas is exactly the failure that happened here. Both
            # halves are needed: this one makes the check exact, that one makes its
            # absence non-catastrophic.
            "GATEWAY_SHARED_USER_POOL_ID": (gateway_auth_pool.user_pool_id if gateway_auth_pool is not None else ""),
            "GATEWAY_SHARED_USER_POOL_DOMAIN": gateway_auth_domain,
            # Scope-based RBAC (services/rbac.py) — MUST be set here and not only
            # on the workflow Lambda. This Lambda serves 51 of the 66 API
            # operations, including every scope-guarded /api/admin, /api/cost,
            # /api/registry, /api/permissions, /api/prompts, /api/tags,
            # /api/triggers, /api/connectors, /api/hitl, /api/approvals,
            # /api/evaluations, /api/versions, /api/identity, /api/models,
            # /api/mcp-servers and /api/vpc-profiles route. rbac_enforcing()
            # reads os.environ with a "" default, so an ABSENT variable is
            # indistinguishable from "false" — meaning an operator who follows
            # docs/RBAC_ROLLOUT.md step 5 (`RBAC_ENFORCE=true ./scripts/deploy.sh`)
            # would flip only the workflow Lambda's 15 operations to fail-closed
            # and silently leave the other 51 advisory, while believing the whole
            # control plane was enforcing. Measured live on stack acfe2e-p0920:
            # a caller in t-user with no g-* group (therefore zero scopes) got
            # 200 on GET /api/admin/audit and GET /api/cost/budgets. Enforcing by
            # default, like the workflow Lambda above.
            "RBAC_ENFORCE": stack.node.try_get_context("rbac_enforce") or "true",
        },
        log_group=logs.LogGroup(
            stack,
            "DeploymentLambdaLogGroup",
            log_group_name=f"/aws/lambda/{cfg.project}-{cfg.env}-deployment",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        ),
    )
    otel.apply(fn, "deployment")
    fn.add_event_source(
        lambda_event_sources.SqsEventSource(
            trigger_dispatch_queue,
            batch_size=1,
            max_concurrency=10,
            report_batch_item_failures=True,
        )
    )
    # Allow the Deployment Lambda to invoke ITSELF. The handler kicks off
    # async tool generation/test jobs by self-invoking with a job_id sentinel
    # in the event payload. Without this, every multi-turn /api/generate-tool
    # and every /api/test-tool returned plaintext "Internal Server Error" 500.
    # See tasks/lessons.md Bug 33.
    # Note: building the ARN from a literal function_name (not the Function
    # object's .function_arn attribute) — using the attribute creates a
    # circular dependency since the role policy would reference the same
    # function it's attached to. The function_name is a static string here.
    fn.role.add_to_principal_policy(
        iam.PolicyStatement(
            sid="DeploymentLambdaSelfInvoke",
            actions=["lambda:InvokeFunction"],
            resources=[f"arn:aws:lambda:{stack.region}:{stack.account}:function:{cfg.project}-{cfg.env}-deployment"],
        )
    )

    # Loom-study 0.6 — scheduled Cedar-ENFORCE promotion sweep. A Cedar
    # ENFORCE gateway attaches FAIL-CLOSED with its permit pending until the
    # gateway's authorization plane converges (20-59+ min, AWS-side). The lazy
    # promoter only fires on USER touchpoints (invoke / status GET); an idle
    # ENFORCE agent with no touchpoints would stay deny-all indefinitely
    # (observed live in P-PLAT-027). This rule self-drives the promoter every
    # 5 min by invoking the deployment Lambda with a {"policy_sweep": true}
    # sentinel (handled in deployment_handler.handler → policy_sweep_step).
    events.Rule(
        stack,
        "PolicySweepSchedule",
        rule_name=f"{cfg.project}-{cfg.env}-policy-sweep",
        description="Self-drive pending Cedar ENFORCE promotions (Loom-study 0.6)",
        schedule=events.Schedule.rate(Duration.minutes(5)),
        targets=[
            events_targets.LambdaFunction(
                fn,
                event=events.RuleTargetInput.from_object({"policy_sweep": True}),
                retry_attempts=2,
            )
        ],
    )

    # Loom-study 5.3 — scheduled FinOps cost reconciliation. Cost analytics
    # are QUERY-TIME (summarize_from_logs reads CloudWatch on demand), so a
    # budget breach only emits the BudgetBreach metric when a human opens the
    # cost panel. An idle-but-overspending agent would never trip an ops
    # alarm. This rule self-drives breach detection DAILY by invoking the
    # deployment Lambda with a {"cost_reconcile": true} sentinel (handled in
    # deployment_handler.handler → cost_reconcile_step): it walks every
    # budget, sums month-to-date actual cost from logs, and emits the metric
    # for any warn/over budget — no user touchpoint required.
    events.Rule(
        stack,
        "CostReconcileSchedule",
        rule_name=f"{cfg.project}-{cfg.env}-cost-reconcile",
        description="Self-drive budget-breach detection month-to-date (Loom-study 5.3)",
        schedule=events.Schedule.rate(Duration.hours(24)),
        targets=[
            events_targets.LambdaFunction(
                fn,
                event=events.RuleTargetInput.from_object({"cost_reconcile": True}),
                retry_attempts=2,
            )
        ],
    )
    return fn


def build_stream_lambda(
    stack: cdk.Stack,
    cfg: PlatformConfig,
    otel: OtelConfig,
    *,
    backend_code: _lambda.Code,
    deployments_table: dynamodb.Table,
    user_pool: cognito.UserPool,
    user_pool_client: cognito.UserPoolClient,
) -> tuple:
    """Create the response-streaming test Lambda + its Function URL (Bug 157).

    The API Gateway HTTP API integration has a hard 30s timeout, so
    tool-heavy agents (>30s) time out at the transport even though the
    agent finishes server-side. This Function URL exists to escape that cap:
    its function timeout is 900s and, measured live 2026-09-20, a signed
    request to it returned HTTP 200 after **240.6s** with a clean SSE body.
    The same SSE wire format the API-GW path uses is reused so the existing
    frontend SSE parser works unchanged.

    **InvokeMode is BUFFERED, not RESPONSE_STREAM** — see the add_function_url
    call below. The 30s escape is real; incremental delivery is not available
    on a managed Python runtime, and claiming RESPONSE_STREAM while returning
    an API-GW envelope produced a URL that answered HTTP 200 with zero
    parseable frames. Measured, not assumed.

    SECURITY: the URL uses **auth_type=AWS_IAM** — see the comment at the
    add_function_url call below for why (a public auth_type=NONE Function URL
    is forbidden in this org and is auto-scoped back to the account). Under
    AWS_IAM the caller must SigV4-sign, and SigV4 occupies the ``Authorization``
    header, so a Cognito bearer token cannot travel alongside it: measured live,
    a request carrying a VALID Cognito access token and no signature is rejected
    by AWS with HTTP 403 before the handler runs. The handler's own Cognito-JWT
    verify (stream_handler._verify_cognito_token) is therefore the *fallback*
    branch for a hypothetical NONE URL and for local tests — it is not a second
    gate in front of this URL. Either way this is not an unauthenticated invoke
    endpoint: the IAM gate is what stands in front of it.

    Two claims in this docstring were corrected on 2026-09-20 after measuring
    the URL for the first time: it said auth_type=NONE (contradicting the call
    twenty lines down), and it said "two gates, not one" for the Cognito verify
    that a SigV4 request can never reach. Corrected explicitly rather than
    quietly, because the wrong half of such a sentence is the half a reviewer
    acts on.
    """
    role = iam.Role(
        stack,
        "StreamLambdaRole",
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
        ],
    )
    # Read deployment records (runtime_id GSI + scan fallback) for ARN
    # resolution + tenant-isolation checks. Read-only is sufficient — the
    # stream path never mutates state.
    deployments_table.grant_read_data(role)
    # SSM read (config loader reads /agentcore-workflow/{env}/* like the
    # other Lambdas).
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"],
            resources=[f"arn:aws:ssm:{stack.region}:{stack.account}:parameter/agentcore-workflow/{cfg.env}/*"],
        )
    )
    # Same data-plane invoke perms as the deployment Lambda's test path:
    # InvokeAgentRuntime (RUNTIME mode) + InvokeHarness (Phase B HARNESS
    # mode) + Bedrock model + STS for ARN construction. AgentCore uses one
    # `bedrock-agentcore:` prefix for control + data plane (Bug 43); these
    # are the invoke-only verbs (NO create/delete here — this Lambda only
    # tests, it never provisions or tears down).
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
                "bedrock-agentcore:InvokeAgentRuntime",
                "bedrock-agentcore:InvokeHarness",
                "bedrock-agentcore:GetAgentRuntime",
                "bedrock-agentcore:GetHarness",
            ],
            resources=["*"],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["sts:GetCallerIdentity"],
            resources=["*"],
        )
    )
    # Cross-account test/invoke reconstructs the exact deployment target from
    # the persisted deployment record and assumes the target's fixed-name role.
    # Keep this name-scoped: target registration rejects arbitrary role names
    # and IAM paths so the runtime contract exactly matches this grant.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["sts:AssumeRole"],
            resources=["arn:aws:iam::*:role/AgentCoreFlowsDeploymentRole"],
        )
    )

    fn = _lambda.Function(
        stack,
        "StreamLambda",
        function_name=f"{cfg.project}-{cfg.env}-stream",
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="src/app/stream_handler.lambda_handler",
        code=backend_code,
        memory_size=512,
        # Generous timeout so tool-heavy agents can run well past the 30s
        # API Gateway cap. Function URLs support response streaming up to
        # ~15 min; the boto read timeout in the handler is bounded below it.
        timeout=Duration.minutes(15),
        role=role,
        tracing=_lambda.Tracing.ACTIVE,
        environment={
            "DEPLOYMENTS_TABLE_NAME": deployments_table.table_name,
            "DEPLOYMENT_TABLE_NAME": deployments_table.table_name,
            "ENVIRONMENT": cfg.env,
            "PROJECT_NAME": cfg.project,
            "APP_AWS_REGION": stack.region,
            "POWERTOOLS_SERVICE_NAME": "stream",
            "PYTHONPATH": "/var/task/src:/var/task:/var/task/lib",
            # In-handler Cognito JWT verification config (same pool/client as
            # the API-GW HttpJwtAuthorizer). stream_handler verifies the
            # access token's issuer + client_id + signature before invoking.
            "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
            "COGNITO_CLIENT_ID": user_pool_client.user_pool_client_id,
            "COGNITO_REGION": stack.region,
        },
        log_group=logs.LogGroup(
            stack,
            "StreamLambdaLogGroup",
            log_group_name=f"/aws/lambda/{cfg.project}-{cfg.env}-stream",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        ),
    )
    otel.apply(fn, "stream")

    # CORS mirrors the API GW config so a browser could call it directly with the
    # Authorization header once SigV4 signing exists on the client (see below).
    #
    # INVOKE MODE — BUFFERED, and this is load-bearing. Measured live 2026-09-20,
    # the first time anything drove this URL:
    #
    #   * The function is a **managed python3.12 runtime**. Lambda response
    #     streaming is a Node.js managed-runtime feature; the Python runtime never
    #     passes a writable ``response_stream``, so ``lambda_handler``'s streaming
    #     branch never executes here and every invoke falls through to its buffered
    #     ``handler()``, which returns ``{statusCode, headers, body}``. Proven with
    #     ``invoke_with_response_stream``: exactly ONE PayloadChunk, arriving at
    #     completion (4.08s), containing that envelope.
    #   * Under RESPONSE_STREAM a Function URL does NOT unwrap that envelope. It
    #     parses the leading JSON for ``headers`` — so the response carried
    #     ``Content-Type: text/event-stream`` and HTTP 200, looking perfectly
    #     healthy — and then emitted **the whole envelope JSON as the body**. An SSE
    #     parser finds no ``data:`` line: 0 token frames. Reproduced in a 20-line
    #     throwaway function to prove the fault was the invoke mode and not our code.
    #   * Under BUFFERED the same envelope is unwrapped exactly as API Gateway does:
    #     clean ``data: {...}`` bytes, ``Content-Type`` applied, and the 30s escape
    #     intact — HTTP 200 after 45.4s and again after 240.6s.
    #
    # So BUFFERED delivers the entire reason this Lambda exists (outliving API
    # Gateway's 30s integration cap) and is the only mode whose body a client can
    # parse. Incremental token-by-token delivery would need a Node.js handler or a
    # custom runtime; it was never actually happening under RESPONSE_STREAM either.
    # Pinned by backend/tests/test_stream_url_delivers_parseable_sse.py, which fails
    # if this is flipped back while the handler still returns an envelope.
    #
    # SECURITY (Palisade/Epoxy finding 19a210be + account SCP): a public
    # auth_type=NONE Function URL is a WORLD-ACCESSIBLE Lambda. Amazon's
    # Palisade detector flags it and Epoxy auto-scopes the Principal back to
    # the account — i.e. public Lambda URLs are forbidden in this org, and a
    # signed admin SigV4 call to the URL is also SCP-blocked. So the Function
    # URL uses AWS_IAM (SigV4) auth: no world access, fully compliant. Verified
    # live: an unsigned POST gets HTTP 403 ``{"Message":"Forbidden"}``, and so
    # does a POST carrying a valid Cognito access token but no signature — AWS
    # refuses both before any Python runs.
    #
    # The endpoint is provisioned but NOT yet wired to the browser — calling it
    # from the SPA needs SigV4-signed requests via a Cognito Identity Pool (the
    # app currently only has a User Pool / JWT). Tracked as future work; the >30s
    # test path falls back to the documented 30s sync limit until an Identity Pool
    # is added. That is also why the broken wire shape above went unnoticed: no
    # client has ever read this URL's body.
    fn_url = fn.add_function_url(
        auth_type=_lambda.FunctionUrlAuthType.AWS_IAM,
        invoke_mode=_lambda.InvokeMode.BUFFERED,
        cors=_lambda.FunctionUrlCorsOptions(
            allowed_origins=["*"],
            allowed_methods=[_lambda.HttpMethod.POST, _lambda.HttpMethod.GET],
            allowed_headers=["Content-Type", "Authorization"],
            max_age=Duration.minutes(5),
        ),
    )

    # Discovery: CfnOutput + SSM param so a client can find the URL the same way
    # it finds the API GW URL and Cognito IDs. NOTE (checked 2026-09-20): neither
    # has a consumer yet — ``VITE_STREAM_URL`` appears nowhere in deploy.sh or the
    # frontend, and ``TestRuntimeStreamUrl`` / ``test-runtime-stream-url`` appear
    # in no file but this one. This comment used to claim deploy.sh read it into
    # VITE_STREAM_URL, which made the URL look wired to the SPA when it is not.
    CfnOutput(
        stack,
        "TestRuntimeStreamUrl",
        value=fn_url.url,
        description="Lambda Function URL (BUFFERED, AWS_IAM) for >30s runtime tests",
    )
    ssm.StringParameter(
        stack,
        "TestRuntimeStreamUrlParam",
        parameter_name=f"/agentcore-workflow/{cfg.env}/test-runtime-stream-url",
        string_value=fn_url.url,
        description="Lambda Function URL for streaming runtime tests (Bug 157)",
    )

    return fn, fn_url
