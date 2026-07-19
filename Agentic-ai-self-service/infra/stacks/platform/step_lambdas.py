"""Per-step IAM roles (1:1) + step Lambda functions for the deploy pipeline."""

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3

from .config import PlatformConfig
from .otel import OtelConfig
from .tables import Tables


def _create_step_role(
    stack: cdk.Stack,
    cfg: PlatformConfig,
    step_name: str,
    *,
    tables: Tables,
    artifacts_bucket: s3.Bucket,
) -> iam.Role:
    """Create a dedicated IAM role for a step Lambda (1:1 relationship).

    Per-step least-privilege. Previously every step Lambda shared an
    identical kitchen-sink policy with iam:CreateRole + lambda:CreateFunction
    + secretsmanager:* on `*` — meaning RCE in any step Lambda became full
    account compromise. Verified live 2026-05-16; tasks/lessons.md Bug 36.
    """
    role = iam.Role(
        stack,
        f"Step{step_name.title().replace('_', '')}Role",
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
        ],
    )
    # ── Common: every step needs to update its DDB row + read SSM config ─
    tables.deployments.grant_read_write_data(role)
    tables.workflows.grant_read_data(role)
    # Phase 1 Gap 1A — every step's versioning hooks read the AgentVersions
    # table and status_update writes to both tables. Granting read to all
    # steps is acceptable: the data is owner-scoped via owner_sub anyway,
    # and these tables hold no secrets.
    tables.agent_versions.grant_read_write_data(role)
    tables.runtime_slots.grant_read_write_data(role)
    # Loom-study 2.2 — runtime_configure/harness steps READ approval policies
    # (tag-policy table) to inject LOOM_APPROVAL_POLICIES into the runtime.
    if step_name in {"runtime_configure", "harness"}:
        tables.tag_policy.grant_read_data(role)
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"],
            resources=[f"arn:aws:ssm:{stack.region}:{stack.account}:parameter/agentcore-workflow/{cfg.env}/*"],
        )
    )
    role.add_to_policy(iam.PolicyStatement(actions=["sts:GetCallerIdentity"], resources=["*"]))
    role.add_to_policy(iam.PolicyStatement(actions=["cloudwatch:PutMetricData"], resources=["*"]))
    # Phase 7 (opt-in) cross-account deploy: each step Lambda assumes the
    # target account's deployment role (services/step_clients). NAME-SCOPED
    # to the agreed role name — NOT a blanket AssumeRole. Feature is off by
    # default (no target → no assume-role call is ever made).
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["sts:AssumeRole"],
            resources=["arn:aws:iam::*:role/AgentCoreFlowsDeploymentRole"],
        )
    )

    # ── Per-step grants ──────────────────────────────────────────────────
    # Every step that writes to S3 (codegen, gateway, knowledge_base,
    # mcp_server) gets bucket access. runtime_configure / runtime_launch
    # only need read because AgentCore's CreateAgentRuntime does a
    # pre-flight S3 check on the CALLING principal's identity — not just
    # the passed roleArn. Without s3:GetObject, the call fails with
    # `ValidationException: Access denied when trying to retrieve zip
    # file from S3` even though the shared runtime exec role can read it.
    # Verified live 2026-05-18: same role+bucket+key, direct boto3 from
    # an S3-permitted user succeeds; from the step Lambda fails. See
    # tasks/lessons.md Bug 66.
    s3_writers = {"codegen", "gateway", "knowledge_base", "mcp_server"}
    s3_readers = {"runtime_configure", "runtime_launch"}
    if step_name in s3_writers:
        artifacts_bucket.grant_read_write(role)
    elif step_name in s3_readers:
        artifacts_bucket.grant_read(role)

    # iam_step: creates and tags the runtime's execution role.
    # mcp_server / gateway / knowledge_base / memory also create paired
    # IAM roles for their own dynamically-created Lambdas / AgentCore
    # resources. (memory creates AgentCoreMemory-* role for the memory
    # resource — see tasks/lessons.md Bug 45.)
    # evaluation creates AgentCoreEval-* role for the AgentCore evaluation
    # engine — same drift-across-paths shape as Bug 45/71/77; see
    # tasks/lessons.md Bug 118 (Phase 1 Gap 1C).
    if step_name in {"iam", "mcp_server", "gateway", "knowledge_base", "memory", "evaluation", "harness"}:
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "iam:CreateRole",
                    "iam:AttachRolePolicy",
                    "iam:PutRolePolicy",
                    "iam:GetRole",
                    "iam:PassRole",
                    "iam:DeleteRole",
                    "iam:DetachRolePolicy",
                    "iam:DeleteRolePolicy",
                    "iam:ListAttachedRolePolicies",
                    "iam:ListRolePolicies",
                    "iam:TagRole",
                ],
                resources=[f"arn:aws:iam::{stack.account}:role/AgentCore*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:CreateServiceLinkedRole"],
                resources=[f"arn:aws:iam::{stack.account}:role/aws-service-role/*"],
            )
        )

    # runtime_configure / runtime_launch / mcp_server PASS the runtime's
    # IAM role to AgentCore via CreateAgentRuntime / CreateAgentRuntimeEndpoint.
    # iam:PassRole is required at the calling principal — see tasks/lessons.md
    # Bug 49. Resource includes the shared runtime role (Bug 60) and the
    # legacy per-deploy AgentCoreRuntime-* / AgentCoreMemory-* patterns
    # so cleanup of older deployments still works.
    if step_name in {"runtime_configure", "runtime_launch", "mcp_server", "evaluation"}:
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[
                    f"arn:aws:iam::{stack.account}:role/AgentCoreRuntime-{cfg.project}-{cfg.env}-shared",
                    f"arn:aws:iam::{stack.account}:role/AgentCoreRuntime-*",
                    f"arn:aws:iam::{stack.account}:role/AgentCoreEval-*",
                    f"arn:aws:iam::{stack.account}:role/AgentCoreMemory-*",
                    f"arn:aws:iam::{stack.account}:role/AgentCoreMCP-*",
                ],
                # Defence-in-depth: these roles may only be passed to AgentCore,
                # never to another service (matches the policy-step grant below).
                conditions={"StringEquals": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}},
            )
        )

    # Non-Bedrock model providers: runtime_configure / harness resolve the
    # agent's provider_api_key_ref secret at deploy time and inject it as the
    # PROVIDER_API_KEY env var. Scope GetSecretValue to the agentcore-provider/
    # namespace (same namespace-lock discipline as the OTEL secret) so a
    # tenant cannot point provider_api_key_ref at an arbitrary foreign secret.
    if step_name in {"runtime_configure", "harness"}:
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:agentcore-provider/*",
                ],
            )
        )
        # VPC egress (Loom-study 0.1): a VPC-mode runtime makes AWS lazily
        # create the AWSServiceRoleForBedrockAgentCoreNetwork service-linked
        # role on first use. Without CreateServiceLinkedRole (scoped to that
        # SLR) the FIRST VPC-mode deploy in an account fails. Scoped by the
        # iam:AWSServiceName condition so it can only create THIS SLR.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:CreateServiceLinkedRole"],
                resources=[
                    f"arn:aws:iam::{stack.account}:role/aws-service-role/"
                    "network.bedrock-agentcore.amazonaws.com/AWSServiceRoleForBedrockAgentCoreNetwork*"
                ],
                conditions={"StringEquals": {"iam:AWSServiceName": "network.bedrock-agentcore.amazonaws.com"}},
            )
        )

    # policy step calls update_gateway(roleArn=...) when binding the
    # PolicyEngine — re-passing the gateway's existing role triggers
    # iam:PassRole on the calling principal. See tasks/lessons.md Bug 76.
    if step_name == "policy":
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[f"arn:aws:iam::{stack.account}:role/AgentCoreGateway-*"],
                conditions={"StringEquals": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}},
            )
        )

    # Phase B — the harness step PASSES the harness exec role to AgentCore
    # via CreateHarness (mirrors runtime_configure's CreateAgentRuntime
    # PassRole, Bug 49). Per-harness roles follow the AgentCoreHarness-*
    # convention (get_shared_or_new_harness_role); the optional shared
    # harness role, if added, also matches this prefix. Defence-in-depth:
    # the role may only ever be passed to AgentCore.
    if step_name == "harness":
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[f"arn:aws:iam::{stack.account}:role/AgentCoreHarness-*"],
                conditions={"StringEquals": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}},
            )
        )

    # gateway / mcp_server / codegen / knowledge_base create user Lambdas
    # (custom tools, MCP servers, KB transformer Lambdas).
    if step_name in {"gateway", "mcp_server", "codegen", "knowledge_base"}:
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "lambda:CreateFunction",
                    "lambda:UpdateFunctionCode",
                    "lambda:UpdateFunctionConfiguration",
                    "lambda:DeleteFunction",
                    "lambda:GetFunction",
                    "lambda:InvokeFunction",
                    "lambda:AddPermission",
                    "lambda:RemovePermission",
                    # GetPolicy is REQUIRED by _prune_orphaned_lambda_permissions:
                    # without it the prune's GetPolicy call is implicitly denied,
                    # the bare except swallows it, and no dangling gateway-role
                    # principal is ever removed — so a shared tool Lambda reused
                    # by a new gateway fails AddPermission with "invalid principal"
                    # forever (matrix-run Defect A; the root cause of the
                    # multi-gateway / multi-target deploy failures).
                    "lambda:GetPolicy",
                ],
                resources=[f"arn:aws:lambda:{stack.region}:{stack.account}:function:AgentCore*"],
            )
        )

    # gateway AND mcp_server both create a Cognito user pool — gateway
    # for OAuth2 client_credentials between caller and gateway,
    # mcp_server for the gateway-to-MCP-server-runtime auth bridge.
    # See tasks/lessons.md Bug 77.
    if step_name in {"gateway", "mcp_server"}:
        # CreateUserPool does not support resource-level permissions
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["cognito-idp:CreateUserPool"],
                resources=["*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:DeleteUserPool",
                    "cognito-idp:CreateUserPoolClient",
                    "cognito-idp:DescribeUserPool",
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminSetUserPassword",
                    "cognito-idp:AdminInitiateAuth",
                    "cognito-idp:CreateResourceServer",
                    "cognito-idp:CreateUserPoolDomain",
                    "cognito-idp:DeleteUserPoolClient",
                    "cognito-idp:DeleteUserPoolDomain",
                ],
                resources=[f"arn:aws:cognito-idp:{stack.region}:{stack.account}:userpool/*"],
            )
        )
        # gateway also stores the OAuth2 client_secret in Secrets Manager.
        # Phase A SaaS connectors: the gateway step also mints/reads/deletes
        # connector credential secrets under the agentcore-connector/ prefix
        # (raw API keys + OAuth2 client_secrets that back the credential
        # providers). DescribeSecret is used by the cleanup path to confirm
        # existence before delete. Secrets live ONLY here — never in canvas
        # JSON, DDB, or logs.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:CreateSecret",
                    "secretsmanager:DeleteSecret",
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:PutSecretValue",
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:TagResource",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:AgentCore*",
                    f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:agentcore-*",
                    # Phase A SaaS connector credential secrets.
                    f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:agentcore-connector/*",
                    # CreateOauth2CredentialProvider writes its client_secret
                    # under the bedrock-agentcore-identity!default/oauth2/<n>
                    # Secrets Manager namespace, not the platform's prefix.
                    # See tasks/lessons.md Bug 83.
                    f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:bedrock-agentcore-*",
                ],
            )
        )
        # ListSecrets does not support resource-level scoping (must be on
        # `*`). The connector deploy/cleanup paths enumerate connector
        # secrets by prefix to reconcile orphans. Kept as a separate minimal
        # statement so the wildcard is visible and isolated. The existing
        # per-role AwsSolutions-IAM5 suppression covers this wildcard.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:ListSecrets"],
                resources=["*"],
            )
        )

    # Bug 150 — the harness step registers an OAuth2 credential provider for a
    # connected gateway; CreateOauth2CredentialProvider writes its client_secret
    # under the bedrock-agentcore-identity! Secrets Manager namespace, so the
    # harness step role needs to write/read/delete there (mirrors the gateway
    # step's secret perms, minus the Cognito + connector-secret scope it doesn't use).
    if step_name == "harness":
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:CreateSecret",
                    "secretsmanager:DeleteSecret",
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:PutSecretValue",
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:TagResource",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:bedrock-agentcore-*",
                ],
            )
        )

    # codegen reads bedrock:Converse to render system prompts that
    # describe a tool's purpose (used by the customer-support template).
    if step_name == "codegen":
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                # Resource "*" required: cross-region inference profiles route
                # to foundation-model ARNs in OTHER regions at runtime, and the
                # model is user-selectable per flow.
                resources=["*"],
            )
        )

    # knowledge_base creates KBs / data sources / ingestion jobs.
    if step_name == "knowledge_base":
        # KB / data-source lifecycle verbs support resource-level scoping on
        # the knowledge-base ARN. KB ids are SERVICE-GENERATED (the ARN carries
        # the id, not the user-supplied name), so the tightest pre-creation
        # pattern is knowledge-base/* in this account+region.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:CreateKnowledgeBase",
                    "bedrock:GetKnowledgeBase",
                    "bedrock:DeleteKnowledgeBase",
                    "bedrock:CreateDataSource",
                    "bedrock:GetDataSource",
                    # ListDataSources powers the idempotent create_data_source
                    # conflict recovery (matrix-run finding, P-KB-008).
                    "bedrock:ListDataSources",
                    "bedrock:DeleteDataSource",
                    "bedrock:StartIngestionJob",
                    "bedrock:GetIngestionJob",
                    "bedrock:ListIngestionJobs",
                    "bedrock:Retrieve",
                    "bedrock:RetrieveAndGenerate",
                ],
                resources=[f"arn:aws:bedrock:{stack.region}:{stack.account}:knowledge-base/*"],
            )
        )
        # Account-level list verbs — no resource-level scoping supported.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:ListKnowledgeBases", "bedrock:ListFoundationModels"],
                resources=["*"],
            )
        )
        # KB step auto-creates the S3 Vectors index on user-supplied
        # buckets when missing (Bug 88). Needs list/create permissions
        # on the s3vectors service. Scoped to vector-bucket ARNs (and their
        # index/* sub-resources) in this account+region; bucket names are
        # user-suppliable so a name-prefix pattern would break real flows —
        # the auto-provisioned buckets use the agentcore-kbvec- prefix.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "s3vectors:ListIndexes",
                    "s3vectors:CreateIndex",
                    "s3vectors:GetIndex",
                    "s3vectors:DescribeIndex",
                    "s3vectors:CreateVectorBucket",
                    "s3vectors:DescribeVectorBucket",
                    "s3vectors:GetVectorBucket",
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
        # OpenSearch Serverless: KB step auto-provisions a collection +
        # security/access policies + vector index when the caller supplies no
        # opensearchCollectionArn (Bedrock requires a pre-existing collection).
        # Collection verbs scope to collection ARNs (ids are service-generated,
        # so collection/* is the tightest pre-creation pattern).
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "aoss:CreateCollection",
                    "aoss:DeleteCollection",
                    "aoss:CreateIndex",
                    "aoss:DescribeIndex",
                    "aoss:DeleteIndex",
                    "aoss:APIAccessAll",
                ],
                resources=[f"arn:aws:aoss:{stack.region}:{stack.account}:collection/*"],
            )
        )
        # aoss security/data-access policies AND the Batch/List read APIs do
        # NOT support resource-level permissions (account-level APIs) —
        # Resource must stay "*"; least privilege is enforced by the exact
        # action list. (BatchGetCollection on collection/* fails AccessDenied
        # — live-verified by the matrix run.)
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "aoss:BatchGetCollection",
                    "aoss:ListCollections",
                    "aoss:CreateSecurityPolicy",
                    "aoss:GetSecurityPolicy",
                    "aoss:DeleteSecurityPolicy",
                    "aoss:CreateAccessPolicy",
                    "aoss:GetAccessPolicy",
                    "aoss:DeleteAccessPolicy",
                ],
                resources=["*"],
            )
        )

    # guardrails creates Bedrock Guardrails. Guardrail ids are service-
    # generated (ARN carries the id, not the name), so guardrail/* in this
    # account+region is the tightest pre-creation pattern.
    if step_name == "guardrails":
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:CreateGuardrail",
                    "bedrock:GetGuardrail",
                    "bedrock:UpdateGuardrail",
                    "bedrock:DeleteGuardrail",
                    "bedrock:CreateGuardrailVersion",
                ],
                resources=[f"arn:aws:bedrock:{stack.region}:{stack.account}:guardrail/*"],
            )
        )
        # ListGuardrails (account-level listing) — no resource-level scoping.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:ListGuardrails"],
                resources=["*"],
            )
        )

    # AgentCore control-plane verbs are split across many steps.
    agentcore_steps = {
        "mcp_server": [
            "bedrock-agentcore:CreateAgentRuntime",
            "bedrock-agentcore:GetAgentRuntime",
            "bedrock-agentcore:UpdateAgentRuntime",
            "bedrock-agentcore:ListAgentRuntimes",
            "bedrock-agentcore:CreateAgentRuntimeEndpoint",
            "bedrock-agentcore:CreateWorkloadIdentity",
            "bedrock-agentcore:DeleteWorkloadIdentity",
            # Bug 171: the step pre-warms the MCP runtime (sends an MCP
            # initialize) so the Gateway's 30s tool-discovery probe hits a
            # warm container instead of timing out on cold start. Needs the
            # data-plane invoke verb on its own runtime.
            "bedrock-agentcore:InvokeAgentRuntime",
            "bedrock-agentcore:GetAgentRuntimeEndpoint",
            "bedrock-agentcore:ListAgentRuntimeEndpoints",
        ],
        "gateway": [
            "bedrock-agentcore:CreateGateway",
            "bedrock-agentcore:GetGateway",
            "bedrock-agentcore:UpdateGateway",
            "bedrock-agentcore:ListGateways",
            "bedrock-agentcore:CreateGatewayTarget",
            "bedrock-agentcore:DeleteGatewayTarget",
            "bedrock-agentcore:ListGatewayTargets",
            # Bug 134: the gateway step now reads each target's MCP tool
            # manifest (GetGatewayTarget) and triggers a target sync
            # (SynchronizeGatewayTargets) so the policy step gets the real,
            # synced tool action names for schema-valid Cedar.
            "bedrock-agentcore:GetGatewayTarget",
            "bedrock-agentcore:SynchronizeGatewayTargets",
            # Bug 171: when an MCP target lands FAILED (cold-start probe), the
            # gateway step RETRIES it via UpdateGatewayTarget. Without this the
            # retry path itself 403s and the target can never recover.
            "bedrock-agentcore:UpdateGatewayTarget",
            "bedrock-agentcore:CreateOauth2CredentialProvider",
            "bedrock-agentcore:GetOauth2CredentialProvider",
            "bedrock-agentcore:DeleteOauth2CredentialProvider",
            "bedrock-agentcore:ListOauth2CredentialProviders",
            # Phase A SaaS connectors: the gateway step now mints API-key
            # credential providers (Jira/Asana/GitHub/Slack/Salesforce/
            # generic OpenAPI) in addition to OAuth2 ones, and the cleanup
            # path deletes them. Same provisioning shape as the OAuth2 verbs
            # above (token-vault + workload-identity creation still apply).
            "bedrock-agentcore:CreateApiKeyCredentialProvider",
            "bedrock-agentcore:GetApiKeyCredentialProvider",
            "bedrock-agentcore:DeleteApiKeyCredentialProvider",
            "bedrock-agentcore:ListApiKeyCredentialProviders",
            # CreateOauth2CredentialProvider transparently provisions a
            # token vault under the account's identity directory if one
            # doesn't exist. Without these, mcp-server-gateway-target
            # deploys fail with "not authorized to perform:
            # bedrock-agentcore:CreateTokenVault on resource:
            # token-vault/default". See tasks/lessons.md Bug 79.
            "bedrock-agentcore:CreateTokenVault",
            "bedrock-agentcore:GetTokenVault",
            "bedrock-agentcore:ListTokenVaults",
            # CreateGateway transparently creates a workload-identity
            # record under the gateway's identity directory. Without this
            # action, the gateway lands in FAILED status with
            # "Failed to create gateway dependencies: ... not authorized
            # to perform: bedrock-agentcore:CreateWorkloadIdentity ..."
            # Verified live 2026-05-18 — see tasks/lessons.md Bug 65.
            "bedrock-agentcore:CreateWorkloadIdentity",
            "bedrock-agentcore:GetWorkloadIdentity",
            "bedrock-agentcore:DeleteWorkloadIdentity",
            "bedrock-agentcore:ListWorkloadIdentities",
            # Bug 169 (caught live 2026-06-25): an MCP-server-as-gateway-target
            # with an OAUTH credential provider makes the gateway service mint
            # a workload access token to call the MCP runtime. Setting up that
            # target validates the caller can GetWorkloadAccessToken — without
            # it the TARGET lands in FAILED ("not authorized to perform
            # bedrock-agentcore:GetWorkloadAccessToken on ... workload-identity/
            # <gw>"), the gateway serves 0 tools, and the agent 500s at init.
            # (Mis-attributed to a wiring/endpoint bug; it is purely IAM.)
            "bedrock-agentcore:GetWorkloadAccessToken",
            "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
            "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
            # Bug 169 (cont.): wiring the MCP target's OAUTH credential
            # provider also reads the oauth2 token (and, for api-key targets,
            # the api key) from the token vault during setup/validation. Both
            # surface as the same "OAuth setup ... not authorized to perform
            # bedrock-agentcore:GetResourceOauth2Token on .../token-vault/
            # default/oauth2credentialprovider/<name>" 403 → target FAILED.
            "bedrock-agentcore:GetResourceOauth2Token",
            "bedrock-agentcore:GetResourceApiKey",
        ],
        "memory": [
            "bedrock-agentcore:CreateMemory",
            "bedrock-agentcore:GetMemory",
            "bedrock-agentcore:DeleteMemory",
            "bedrock-agentcore:ListMemories",
        ],
        "policy": [
            "bedrock-agentcore:CreatePolicyEngine",
            "bedrock-agentcore:GetPolicyEngine",
            "bedrock-agentcore:DeletePolicyEngine",
            "bedrock-agentcore:ListPolicyEngines",
            "bedrock-agentcore:CreatePolicy",
            "bedrock-agentcore:DeletePolicy",
            "bedrock-agentcore:ListPolicies",
            "bedrock-agentcore:UpdatePolicy",
            "bedrock-agentcore:GetPolicy",
            # CreatePolicy implicitly requires ManageAdminPolicy
            # (undocumented). See tasks/lessons.md Bug 93.
            "bedrock-agentcore:ManageAdminPolicy",
            # Bug 134 (real root cause): creating a policy SCOPED TO A GATEWAY
            # is authorized as bedrock-agentcore:ManageResourceScopedPolicy on
            # the *gateway* ARN — NOT CreatePolicy. Without it, create_policy
            # silently AccessDenied'd, the engine attached with ZERO policies,
            # and ENFORCE default-deny returned 0 tools (looked like a Cedar
            # bug; it was a missing IAM grant). Grant the manage + read verbs.
            "bedrock-agentcore:ManageResourceScopedPolicy",
            "bedrock-agentcore:GetResourceScopedPolicy",
            "bedrock-agentcore:ListResourceScopedPolicies",
            # The policy step reads the gateway it's about to attach the
            # engine to (and updates it). Without GetGateway, the bind
            # call fails with AccessDenied. See tasks/lessons.md Bug 70.
            "bedrock-agentcore:GetGateway",
            "bedrock-agentcore:UpdateGateway",
            # Bug 134: the policy step reads each target's MCP tool manifest
            # (list + get gateway targets) to generate Cedar that references
            # only REAL tool actions — referencing a non-existent tool fails
            # schema validation (CREATE_FAILED).
            "bedrock-agentcore:ListGatewayTargets",
            "bedrock-agentcore:GetGatewayTarget",
        ],
        "evaluation": [
            "bedrock-agentcore:Evaluate",
            "bedrock-agentcore:CreateOnlineEvaluationConfig",
            "bedrock-agentcore:GetOnlineEvaluationConfig",
            "bedrock-agentcore:ListOnlineEvaluationConfigs",
            "bedrock-agentcore:UpdateOnlineEvaluationConfig",
            "bedrock-agentcore:DeleteOnlineEvaluationConfig",
            "logs:StartQuery",
            "logs:GetQueryResults",
            # AgentCore eval reads the aws/spans CloudWatch Logs index
            # policy (X-Ray-backed traces). The error message
            # "Access denied when accessing index policy for aws/spans"
            # really means logs:DescribeIndexPolicies / PutIndexPolicy
            # on the aws/spans log group. See lessons.md Bug 119.
            "logs:DescribeIndexPolicies",
            "logs:DescribeFieldIndexes",
            "logs:DescribeLogGroups",
            "logs:PutIndexPolicy",
            # AgentCore's CreateOnlineEvaluationConfig validates that the
            # calling principal can read X-Ray's per-account index policy
            # (where AgentCore stores the runtime's span index). Without
            # these the API returns AccessDeniedException with the
            # confusing message "Access denied when accessing index
            # policy for aws/spans" — it's the caller, not the eval
            # execution role, that needs the grant. See lessons.md Bug 119.
            "xray:GetIndexingRules",
            "xray:UpdateIndexingRule",
            "xray:GetGroup",
            "xray:GetGroups",
            "xray:CreateGroup",
            "xray:UpdateGroup",
            "xray:GetTraceSummaries",
            "xray:BatchGetTraces",
            "application-signals:Get*",
            "application-signals:List*",
            "application-signals:BatchGet*",
        ],
        "runtime_configure": [
            # CreateAgentRuntime auto-creates a default endpoint AND a
            # workload-identity record; the caller IAM principal must
            # hold all three action sets — see tasks/lessons.md Bug 46/53.
            "bedrock-agentcore:CreateAgentRuntime",
            "bedrock-agentcore:GetAgentRuntime",
            "bedrock-agentcore:UpdateAgentRuntime",
            "bedrock-agentcore:ListAgentRuntimes",
            "bedrock-agentcore:DeleteAgentRuntime",
            "bedrock-agentcore:CreateAgentRuntimeEndpoint",
            "bedrock-agentcore:GetAgentRuntimeEndpoint",
            "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
            "bedrock-agentcore:ListAgentRuntimeEndpoints",
            "bedrock-agentcore:UpdateAgentRuntimeEndpoint",
            "bedrock-agentcore:CreateWorkloadIdentity",
            "bedrock-agentcore:GetWorkloadIdentity",
            "bedrock-agentcore:DeleteWorkloadIdentity",
            "bedrock-agentcore:ListWorkloadIdentities",
        ],
        "runtime_launch": [
            "bedrock-agentcore:GetAgentRuntime",
            "bedrock-agentcore:CreateAgentRuntimeEndpoint",
            "bedrock-agentcore:GetAgentRuntimeEndpoint",
            "bedrock-agentcore:ListAgentRuntimeEndpoints",
            "bedrock-agentcore:UpdateAgentRuntimeEndpoint",
            "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
        ],
        # Phase B — AgentCore Harness lifecycle. The harness step creates
        # the harness and polls it to READY; InvokeHarness is included so
        # Bug-9 parity holds if the step ever smoke-tests. NO harness-
        # endpoint verbs exist. InvokeHarness is
        # served by the SAME bedrock-agentcore: action prefix even though it
        # is on the DATA plane (mirrors InvokeAgentRuntime, Bug 43).
        "harness": [
            "bedrock-agentcore:CreateHarness",
            "bedrock-agentcore:GetHarness",
            "bedrock-agentcore:ListHarnesses",
            "bedrock-agentcore:UpdateHarness",
            "bedrock-agentcore:DeleteHarness",
            "bedrock-agentcore:InvokeHarness",
            # Bug 151 (caught live): a Harness is implemented ON TOP OF an
            # AgentCore Runtime — CreateHarness internally calls
            # CreateAgentRuntime (and get/update/delete mirror it), so the
            # harness step role MUST also hold the AgentRuntime lifecycle
            # verbs or CreateHarness fails with AccessDenied on
            # bedrock-agentcore:CreateAgentRuntime (resource runtime/*).
            "bedrock-agentcore:CreateAgentRuntime",
            "bedrock-agentcore:GetAgentRuntime",
            "bedrock-agentcore:UpdateAgentRuntime",
            "bedrock-agentcore:DeleteAgentRuntime",
            "bedrock-agentcore:ListAgentRuntimes",
            "bedrock-agentcore:CreateAgentRuntimeEndpoint",
            "bedrock-agentcore:GetAgentRuntimeEndpoint",
            "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
            # CreateHarness transparently provisions a workload-identity
            # record under the harness's identity directory (same shape as
            # CreateAgentRuntime / CreateGateway, Bug 53/65).
            "bedrock-agentcore:CreateWorkloadIdentity",
            "bedrock-agentcore:GetWorkloadIdentity",
            "bedrock-agentcore:DeleteWorkloadIdentity",
            "bedrock-agentcore:ListWorkloadIdentities",
            # Bug 150 — when a gateway is connected, the harness step registers
            # an OAuth2 credential provider (ensure_gateway_outbound_provider)
            # so the harness can authenticate outbound to the CUSTOM_JWT
            # gateway. Without these the harness step gets AccessDenied
            # creating that provider on the SFN path.
            "bedrock-agentcore:CreateOauth2CredentialProvider",
            "bedrock-agentcore:GetOauth2CredentialProvider",
            "bedrock-agentcore:DeleteOauth2CredentialProvider",
            "bedrock-agentcore:ListOauth2CredentialProviders",
            # Bug 153 (caught live): the FIRST CreateOauth2CredentialProvider in
            # an account/region implicitly provisions the default token-vault,
            # so the caller needs CreateTokenVault/GetTokenVault or it fails with
            # AccessDenied on bedrock-agentcore:CreateTokenVault
            # (token-vault/default). Idempotent once the vault exists.
            "bedrock-agentcore:CreateTokenVault",
            "bedrock-agentcore:GetTokenVault",
            # Bug 152 (caught live): CreateHarness ALWAYS auto-provisions a
            # default AgentCore Memory for the harness session (even a "bare"
            # harness with no memory configured). The CALLER (this step role)
            # must hold the Memory lifecycle verbs or the harness lands in
            # CREATE_FAILED with "Memory operation failed: not authorized to
            # perform: bedrock-agentcore:CreateMemory". Delete cascades on
            # DeleteHarness, but grant Delete/Get too for completeness.
            "bedrock-agentcore:CreateMemory",
            "bedrock-agentcore:GetMemory",
            "bedrock-agentcore:ListMemories",
            "bedrock-agentcore:UpdateMemory",
            "bedrock-agentcore:DeleteMemory",
        ],
    }
    if step_name in agentcore_steps:
        role.add_to_policy(
            iam.PolicyStatement(
                actions=agentcore_steps[step_name],
                # Least privilege: AgentCore control-plane resources (runtimes,
                # gateways, memories, harnesses, token vaults, workload
                # identities, ...) are created dynamically per user deploy —
                # their ARNs are unknowable when this stack is synthesized, and
                # several verbs (Create*, List*) have no resource-ARN form.
                # Scoped by the exact per-step action lists above instead of a
                # service wildcard (AgentCore also ignores `bedrock-agentcore:*`
                # wildcards at authorization time — Bug 47).
                resources=["*"],
            )
        )

    # Phase 1 Gap 1D — runtime_launch creates a CloudWatch dashboard
    # for the deployed runtime. cloudwatch:PutDashboard / GetDashboard
    # are global (no resource ARN format for dashboards in IAM).
    if step_name == "runtime_launch":
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudwatch:PutDashboard",
                    "cloudwatch:GetDashboard",
                    "cloudwatch:DeleteDashboards",
                ],
                resources=["*"],
            )
        )

    # Bug 196 — auto-cleanup on failure. When a deployment fails, the
    # status_update step iterates created_resources and deletes them to
    # prevent orphans (KB, Cognito pools, gateways, IAM roles, Lambdas,
    # vector buckets). Needs DELETE verbs across every resource type.
    if step_name == "status_update":
        # KB ids are service-generated — knowledge-base/* in this
        # account+region is the tightest pattern for cleanup-by-manifest.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:DeleteKnowledgeBase",
                    "bedrock:ListDataSources",
                    "bedrock:DeleteDataSource",
                    "bedrock:GetKnowledgeBase",
                ],
                resources=[f"arn:aws:bedrock:{stack.region}:{stack.account}:knowledge-base/*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "s3vectors:DeleteVectorBucket",
                    "s3vectors:GetVectorBucket",
                    "s3vectors:ListIndexes",
                    "s3vectors:DeleteIndex",
                ],
                resources=[f"arn:aws:s3vectors:{stack.region}:{stack.account}:bucket/*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "iam:DeleteRole",
                    "iam:GetRole",
                    "iam:ListAttachedRolePolicies",
                    "iam:DetachRolePolicy",
                    "iam:ListRolePolicies",
                    "iam:DeleteRolePolicy",
                ],
                resources=[f"arn:aws:iam::{stack.account}:role/AgentCore*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:DeleteUserPool",
                    "cognito-idp:DescribeUserPool",
                    "cognito-idp:DeleteUserPoolDomain",
                ],
                resources=[f"arn:aws:cognito-idp:{stack.region}:{stack.account}:userpool/*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:DeleteGateway",
                    "bedrock-agentcore:GetGateway",
                    "bedrock-agentcore:DeleteAgentRuntime",
                    "bedrock-agentcore:GetAgentRuntime",
                ],
                # AgentCore gateway/runtime ids are minted per-deploy — ARNs
                # unknowable at synth time; scoped by exact delete/get verbs.
                resources=["*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "lambda:DeleteFunction",
                    "lambda:GetFunction",
                    # Required by _release_shared_tool_lambda (Defect C): the
                    # failure-path auto-cleanup ref-counts SHARED tool Lambdas —
                    # it reads the resource policy and drops this gateway's
                    # invoke grant. Without these it fail-safes to "kept" and
                    # the shared Lambda leaks with dangling grants.
                    "lambda:GetPolicy",
                    "lambda:RemovePermission",
                ],
                resources=[f"arn:aws:lambda:{stack.region}:{stack.account}:function:AgentCore*"],
            )
        )

    return role


def build_step_lambdas(
    stack: cdk.Stack,
    cfg: PlatformConfig,
    otel: OtelConfig,
    *,
    backend_code: _lambda.Code,
    tables: Tables,
    artifacts_bucket: s3.Bucket,
    shared_runtime_role: iam.Role,
) -> dict[str, _lambda.Function]:
    """Create individual Lambda functions for each Step Functions step.

    Requirements: 1.3, 6.3
    """
    step_configs = {
        "validate": {
            "handler": "src/app/step_handlers/validate_step.handler",
            "memory": 256,
            "timeout": 30,
        },
        "codegen": {
            "handler": "src/app/step_handlers/codegen_step.handler",
            "memory": 1024,
            "timeout": 90,
        },
        "iam": {
            "handler": "src/app/step_handlers/iam_step.handler",
            "memory": 256,
            "timeout": 60,
        },
        "mcp_server": {
            "handler": "src/app/step_handlers/mcp_server_step.handler",
            "memory": 1024,
            "timeout": 600,
        },
        "gateway": {
            "handler": "src/app/step_handlers/gateway_step.handler",
            "memory": 512,
            # Bug 134: lockstep with the DeployGateway SFN task timeout (720s).
            # The step probes the gateway MCP tool plane and may recreate the
            # gateway up to 3x to beat the empty-tool-plane provisioning flake.
            "timeout": 720,
        },
        "runtime_configure": {
            "handler": "src/app/step_handlers/runtime_configure_step.handler",
            "memory": 512,
            # 240s budget: Bug 52's IAM-race retry can spend up to 75s
            # waiting for AgentCore's IAM cache to populate after
            # put_role_policy. Plus the create call itself can be slow.
            # 60s caused 100% deploy regression. See lessons Bug 54.
            "timeout": 240,
        },
        "runtime_launch": {
            "handler": "src/app/step_handlers/runtime_launch_step.handler",
            "memory": 512,
            "timeout": 600,
        },
        "auth": {
            "handler": "src/app/step_handlers/auth_step.handler",
            "memory": 256,
            "timeout": 60,
        },
        "status_update": {
            "handler": "src/app/step_handlers/status_update_step.handler",
            "memory": 256,
            "timeout": 15,
        },
        "memory": {
            "handler": "src/app/step_handlers/memory_step.handler",
            "memory": 512,
            "timeout": 120,
        },
        "evaluation": {
            "handler": "src/app/step_handlers/evaluation_step.handler",
            "memory": 512,
            "timeout": 120,
        },
        "policy": {
            "handler": "src/app/step_handlers/policy_step.handler",
            "memory": 512,
            # Bug 177: a freshly-created policy engine takes minutes to become
            # truly ACTIVE for policy creation (create_policy 409s "engine is
            # CREATING" / validates "Insufficient permissions to call gateway"
            # until it converges). The step waits for the engine + retries the
            # policy create across that window, so it needs a generous budget.
            "timeout": 600,
        },
        "knowledge_base": {
            "handler": "src/app/step_handlers/knowledge_base_step.handler",
            "memory": 1024,
            "timeout": 600,
        },
        "guardrails": {
            "handler": "src/app/step_handlers/guardrails_step.handler",
            "memory": 512,
            "timeout": 120,
        },
        # Phase B — AgentCore Harness (parallel authoring/deploy path).
        # Builds the harness exec role, calls CreateHarness, then polls
        # wait_for_harness_ready (up to 600s service-side); the 300s Lambda
        # budget covers role creation + create + a partial ready poll, with
        # the SFN task timeout matched below.
        "harness": {
            "handler": "src/app/step_handlers/harness_step.handler",
            "memory": 512,
            "timeout": 300,
        },
    }

    lambdas: dict[str, _lambda.Function] = {}
    for step_name, config in step_configs.items():
        step_role = _create_step_role(stack, cfg, step_name, tables=tables, artifacts_bucket=artifacts_bucket)
        fn = _lambda.Function(
            stack,
            f"Step{step_name.title().replace('_', '')}Lambda",
            function_name=f"{cfg.project}-{cfg.env}-step-{step_name.replace('_', '-')}",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler=config["handler"],
            code=backend_code,
            memory_size=config["memory"],
            timeout=Duration.seconds(config["timeout"]),
            role=step_role,
            tracing=_lambda.Tracing.ACTIVE,
            environment={
                "DEPLOYMENTS_TABLE_NAME": tables.deployments.table_name,
                "DEPLOYMENT_TABLE_NAME": tables.deployments.table_name,
                "WORKFLOWS_TABLE_NAME": tables.workflows.table_name,
                # Phase 1 Gap 1A — versioning tables; status_update_step
                # writes the AgentVersion + RuntimeSlots rows on success.
                "AGENT_VERSIONS_TABLE_NAME": tables.agent_versions.table_name,
                "RUNTIME_SLOTS_TABLE_NAME": tables.runtime_slots.table_name,
                # Phase 2 Gap 2D — HITL table name so runtime_configure_step
                # injects it into the runtime's environmentVariables.
                "HITL_REQUESTS_TABLE_NAME": tables.hitl_requests.table_name,
                # Loom-study 2.2 — approval policies live in the tag-policy
                # table; runtime_configure_step reads them to inject
                # LOOM_APPROVAL_POLICIES into the runtime (guaranteed HITL hook).
                "TAG_POLICY_TABLE_NAME": tables.tag_policy.table_name,
                "ARTIFACTS_BUCKET_NAME": artifacts_bucket.bucket_name,
                "ENVIRONMENT": cfg.env,
                "APP_AWS_REGION": stack.region,
                "PYTHONPATH": "/var/task/src:/var/task:/var/task/lib",
                # Shared runtime execution role — pre-created at stack
                # init to avoid the per-deploy IAM-propagation race.
                "SHARED_RUNTIME_ROLE_ARN": shared_runtime_role.role_arn,
            },
            log_group=logs.LogGroup(
                stack,
                f"Step{step_name.title().replace('_', '')}LogGroup",
                log_group_name=f"/aws/lambda/{cfg.project}-{cfg.env}-step-{step_name.replace('_', '-')}",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        otel.apply(fn, f"step-{step_name.replace('_', '-')}")
        lambdas[step_name] = fn

    return lambdas
