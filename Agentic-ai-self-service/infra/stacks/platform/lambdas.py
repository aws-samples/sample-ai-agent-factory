"""IAM Roles + Lambda Functions (non-step Lambdas).

Audit #12: section banner — shared runtime role, workflow Lambda,
deployment Lambda and the streaming test Lambda live here; the per-step
roles + Lambdas live in step_lambdas.py.
"""

import os

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, RemovalPolicy
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm

from .config import PlatformConfig
from .otel import OtelConfig
from .tables import Tables


def get_backend_code() -> _lambda.Code:
    """Package the backend source as a Lambda code asset with bundled dependencies.

    Dependencies are pre-installed into backend/lib/ by the deploy script
    (pip install -r requirements-lambda.txt -t backend/lib/).
    The asset includes both src/ and lib/ directories.
    """
    backend_path = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))
    return _lambda.Code.from_asset(
        backend_path,
        exclude=[
            ".venv",
            "__pycache__",
            ".pytest_cache",
            ".hypothesis",
            "tests",
            ".git",
            ".env",
            "build",
            "*.pyc",
        ],
    )


def build_shared_runtime_role(
    stack: cdk.Stack,
    cfg: PlatformConfig,
    otel: OtelConfig,
    *,
    artifacts_bucket: s3.Bucket,
    hitl_requests_table: dynamodb.Table,
) -> iam.Role:
    """Create ONE stable IAM execution role shared by every AgentCore runtime.

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
    role = iam.Role(
        stack,
        "SharedRuntimeExecRole",
        role_name=f"AgentCoreRuntime-{cfg.project}-{cfg.env}-shared",
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
    # Read the agent code zip from the artifacts bucket
    artifacts_bucket.grant_read(role)
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
                # Legacy memory data-plane verbs kept for older SDK paths.
                "bedrock-agentcore:GetLastKTurns",
                "bedrock-agentcore:RetrieveMemories",
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
    # Secrets Manager for OTEL auth header storage (POST /api/observability/credentials).
    # The router always names secrets `agentcore-otel/{provider}/{uuid}`.
    # CreateSecret historically required `*` because IAM Resource matching for
    # CreateSecret pre-2023 didn't support name patterns, but modern IAM does
    # via the secret-ARN path prefix. See tasks/lessons.md Bug 40.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "secretsmanager:CreateSecret",
                "secretsmanager:TagResource",
            ],
            resources=[
                f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:agentcore-otel/*",
            ],
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
                "secretsmanager:CreateSecret",
                "secretsmanager:TagResource",
                "secretsmanager:GetSecretValue",
                "secretsmanager:PutSecretValue",
            ],
            resources=[
                f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:agentcore-git/*",
            ],
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
            "APP_AWS_REGION": stack.region,
            "POWERTOOLS_SERVICE_NAME": "workflow",
            "PYTHONPATH": "/var/task/src:/var/task:/var/task/lib",
            # Scope-based RBAC (services/rbac.py). Advisory by default: the
            # dependency logs would-be denials but allows the request. Flip
            # to "true" (redeploy) to enforce 403s once group grants are
            # validated in real traffic — mirrors the Cedar LOG_ONLY→ENFORCE
            # promotion. Overridable via `cdk deploy -c rbac_enforce=true`.
            "RBAC_ENFORCE": stack.node.try_get_context("rbac_enforce") or "false",
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
) -> _lambda.Function:
    """Create Deployment Lambda for deploy/status/test/delete operations.

    Requirements: 1.2, 6.2
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
    # Loom-study 1.6 — JIT IAM permission-request workflow. The router
    # (create/list/approve/reject) is on the deployment Lambda; on approve it
    # widens a managed role's inline policy, so grant PutRolePolicy scoped to
    # the platform's own AgentCore* managed roles (never arbitrary roles).
    tables.permission_requests.grant_read_write_data(role)
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["iam:PutRolePolicy", "iam:GetRolePolicy"],
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
            resources=[f"arn:aws:bedrock:{stack.region}:{stack.account}:knowledge-base/*"],
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
            resources=[f"arn:aws:bedrock:{stack.region}:{stack.account}:guardrail/*"],
        )
    )
    # S3 Vectors teardown (Bug 167): the KB step self-provisions a vector
    # bucket+index; the manifest delete path in THIS Lambda tears it down.
    # Scoped to vector-bucket ARNs in this account+region.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "s3vectors:GetVectorBucket",
                "s3vectors:DescribeVectorBucket",
                "s3vectors:DeleteVectorBucket",
                "s3vectors:ListIndexes",
                "s3vectors:GetIndex",
                "s3vectors:DescribeIndex",
                "s3vectors:DeleteIndex",
            ],
            resources=[f"arn:aws:s3vectors:{stack.region}:{stack.account}:bucket/*"],
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
            resources=[f"arn:aws:aoss:{stack.region}:{stack.account}:collection/*"],
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
                "bedrock-agentcore:CreateMemory",
                "bedrock-agentcore:GetMemory",
                "bedrock-agentcore:DeleteMemory",
                "bedrock-agentcore:ListMemories",
                "bedrock-agentcore:CreatePolicyEngine",
                "bedrock-agentcore:GetPolicyEngine",
                "bedrock-agentcore:DeletePolicyEngine",
                "bedrock-agentcore:ListPolicyEngines",
                # Phase 6 (Loom) — AWS Agent Registry federation (opt-in).
                # The registry router publishes/approves/searches records in
                # the org-wide AWS-native catalog. Public preview; feature is
                # off unless an admin configures a registryId.
                "bedrock-agentcore:CreateRegistry",
                "bedrock-agentcore:GetRegistry",
                "bedrock-agentcore:CreateRegistryRecord",
                "bedrock-agentcore:GetRegistryRecord",
                "bedrock-agentcore:ListRegistryRecords",
                "bedrock-agentcore:SubmitRegistryRecordForApproval",
                "bedrock-agentcore:UpdateRegistryRecordStatus",
                "bedrock-agentcore:UpdateRegistryRecord",
                "bedrock-agentcore:DeleteRegistryRecord",
                "bedrock-agentcore:SearchRegistryRecords",
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
                "bedrock-agentcore:ManageResourceScopedPolicy",
                "bedrock-agentcore:GetResourceScopedPolicy",
                "bedrock-agentcore:ListResourceScopedPolicies",
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
    # Phase 1 Gap 1C — CloudWatch Logs Insights query for evaluator scores.
    # M-2: also delete the eval-results log groups on destroy_runtime.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "logs:StartQuery",
                "logs:GetQueryResults",
                "logs:StopQuery",
                "logs:DescribeLogGroups",
                "logs:DeleteLogGroup",
            ],
            resources=["*"],
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
            ],
            resources=[f"arn:aws:cognito-idp:{stack.region}:{stack.account}:userpool/*"],
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
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "iam:CreateRole",
                "iam:GetRole",
                "iam:AttachRolePolicy",
                "iam:PassRole",
                "iam:DetachRolePolicy",
                "iam:DeleteRole",
                "iam:ListAttachedRolePolicies",
                "iam:ListRolePolicies",
                "iam:DeleteRolePolicy",
                # Phase B — the direct-deploy harness path
                # (create_harness_iam_role) builds the AgentCoreHarness-*
                # exec role with an inline policy + tag, then destroy_harness
                # tears it down. PutRolePolicy/TagRole round out the verbs the
                # existing CreateRole/Delete* already grant on AgentCore*.
                "iam:PutRolePolicy",
                "iam:TagRole",
            ],
            resources=[f"arn:aws:iam::{stack.account}:role/AgentCore*"],
        )
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
    # Stamp/repair the ManagedBy tag on runtime exec roles (needed so the
    # tag-gated cleanup grant above can match them). Scoped to *-role names.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["iam:TagRole"],
            resources=[f"arn:aws:iam::{stack.account}:role/*-role"],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "lambda:CreateFunction",
                "lambda:GetFunction",
                "lambda:InvokeFunction",
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
            ],
            resources=[
                f"arn:aws:lambda:{stack.region}:{stack.account}:function:AgentCore*",
                # Bug 175: the MCP-server path's intercept lambda is named
                # "MCPServerRuntime" (no AgentCore prefix), so deleting an
                # MCP-server flow failed lambda:DeleteFunction with AccessDenied
                # and orphaned the function. Cover the MCP lambda names too.
                f"arn:aws:lambda:{stack.region}:{stack.account}:function:MCPServer*",
            ],
        )
    )
    # S3 artifacts bucket: read/write for CFN template generation
    artifacts_bucket.grant_read_write(role)
    # Phase 3 Gap 3F — webhook trigger HMAC secret. routers/triggers.py
    # mints an HMAC signing secret per webhook trigger under the
    # agentcore-trigger/ namespace; destroy_runtime (runtime_deployer)
    # cascade-deletes it per trigger row on teardown (Bug 124). Scoped to
    # the agentcore-trigger/ prefix only.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "secretsmanager:CreateSecret",
                "secretsmanager:TagResource",
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
                f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:agentcore-connector/*",
                # Bug 184 — TEARDOWN parity for the harness->gateway outbound
                # OAuth2 credential provider. handle_delete_runtime (this
                # role) calls delete_oauth2_credential_provider, which
                # cascade-deletes the provider's backing client_secret. That
                # secret lives under the bedrock-agentcore-* identity
                # namespace (NOT agentcore-connector/ — see Bug 83), so
                # without DeleteSecret here the teardown leaks the secret with
                # "not authorized to perform: secretsmanager:DeleteSecret"
                # and the provider delete fails. The gateway/harness STEP role
                # already grants this prefix; the delete role must match.
                f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:bedrock-agentcore-*",
                f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:AgentCore*",
            ],
        )
    )
    # ListSecrets does not support resource-level scoping (must be on `*`).
    # The connector deploy/cleanup paths enumerate connector secrets by
    # prefix to reconcile orphans. Separate minimal statement so the
    # wildcard is visible and isolated; the per-role AwsSolutions-IAM5
    # suppression already covers it.
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
        timeout=Duration.seconds(120),
        role=role,
        tracing=_lambda.Tracing.ACTIVE,
        environment={
            "DEPLOYMENTS_TABLE_NAME": tables.deployments.table_name,
            "DEPLOYMENT_TABLE_NAME": tables.deployments.table_name,
            "WORKFLOWS_TABLE_NAME": tables.workflows.table_name,
            # Phase 1 Gap 1A — versioning tables.
            "AGENT_VERSIONS_TABLE_NAME": tables.agent_versions.table_name,
            "RUNTIME_SLOTS_TABLE_NAME": tables.runtime_slots.table_name,
            # Phase 2 Gap 2A — agent registry table.
            "AGENT_REGISTRY_TABLE_NAME": tables.agent_registry.table_name,
            # Phase 2 Gap 2B — usage events table (cost_tracking store).
            "USAGE_EVENTS_TABLE_NAME": tables.usage_events.table_name,
            # Phase 2 Gap 2D — HITL requests table (routers/hitl.py store).
            "HITL_REQUESTS_TABLE_NAME": tables.hitl_requests.table_name,
            # Phase 3 Gap 3F — triggers registry table (routers/triggers.py).
            "TRIGGERS_TABLE_NAME": tables.triggers.table_name,
            # Phase 3 Gap 3H — prompt library table (routers/prompts.py).
            "PROMPT_LIBRARY_TABLE_NAME": tables.prompt_library.table_name,
            # Phase 2 (Loom) governance tagging table (routers/tags.py +
            # deploy-time tag resolver).
            "TAG_POLICY_TABLE_NAME": tables.tag_policy.table_name,
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
            "APP_AWS_REGION": stack.region,
            "POWERTOOLS_SERVICE_NAME": "deployment",
            "TOOL_GENERATOR_MODEL_ID": f"{'eu' if stack.region.startswith('eu-') else 'ap' if stack.region.startswith('ap-') else 'us'}.anthropic.claude-sonnet-5",
            "PYTHONPATH": "/var/task/src:/var/task:/var/task/lib",
            # Needed by destroy_runtime to skip cascade-deletion of the
            # stack-managed shared runtime role (Bug 62).
            "SHARED_RUNTIME_ROLE_ARN": shared_runtime_role.role_arn,
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
    agent finishes server-side. A Lambda Function URL with
    InvokeMode=RESPONSE_STREAM lets ``src/app/stream_handler.lambda_handler``
    emit SSE chunks incrementally and keep the connection open well past
    30s. The same SSE wire format the API-GW path uses is reused so the
    existing frontend SSE parser works unchanged.

    SECURITY: Function URLs cannot attach a Cognito JWT authorizer the way
    the HTTP API does, so the URL uses auth_type=NONE and the handler
    verifies the Cognito *access* token itself (issuer + client_id +
    signature against the pool JWKS) before any invoke — see
    stream_handler._verify_cognito_token. This is NOT an unauthenticated
    invoke endpoint.
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

    # Function URL with RESPONSE_STREAM invoke mode. auth_type=NONE because
    # the handler authenticates the Cognito JWT itself (Function URLs can't
    # use the HTTP API's JWT authorizer). CORS mirrors the API GW config so
    # the browser can call it directly with the Authorization header.
    # SECURITY (Palisade/Epoxy finding 19a210be + account SCP): a public
    # auth_type=NONE Function URL is a WORLD-ACCESSIBLE Lambda. Amazon's
    # Palisade detector flags it and Epoxy auto-scopes the Principal back to
    # the account — i.e. public Lambda URLs are forbidden in this org, and a
    # signed admin SigV4 call to the URL is also SCP-blocked. So the Function
    # URL uses AWS_IAM (SigV4) auth: no world access, fully compliant. The
    # endpoint is provisioned but NOT yet wired to the browser — calling it
    # from the SPA needs SigV4-signed requests via a Cognito Identity Pool
    # (the app currently only has a User Pool / JWT). Tracked as future work;
    # the >30s test path falls back to the documented 30s sync limit until an
    # Identity Pool is added. The in-handler Cognito-JWT verify
    # (stream_handler._verify_cognito_token, unit-tested) remains as
    # defence-in-depth on top of the IAM gate.
    fn_url = fn.add_function_url(
        auth_type=_lambda.FunctionUrlAuthType.AWS_IAM,
        invoke_mode=_lambda.InvokeMode.RESPONSE_STREAM,
        cors=_lambda.FunctionUrlCorsOptions(
            allowed_origins=["*"],
            allowed_methods=[_lambda.HttpMethod.POST, _lambda.HttpMethod.GET],
            allowed_headers=["Content-Type", "Authorization"],
            max_age=Duration.minutes(5),
        ),
    )

    # Discovery: CfnOutput (read by deploy.sh -> VITE_STREAM_URL) + SSM param
    # so the frontend build can find the URL the same way it finds the API
    # GW URL and Cognito IDs.
    CfnOutput(
        stack,
        "TestRuntimeStreamUrl",
        value=fn_url.url,
        description="Lambda Function URL (RESPONSE_STREAM) for >30s runtime tests",
    )
    ssm.StringParameter(
        stack,
        "TestRuntimeStreamUrlParam",
        parameter_name=f"/agentcore-workflow/{cfg.env}/test-runtime-stream-url",
        string_value=fn_url.url,
        description="Lambda Function URL for streaming runtime tests (Bug 157)",
    )

    return fn, fn_url
