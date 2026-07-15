"""Serverless CDK stack for the AgentCore Visual Workflow Platform.

Replaces the ECS Fargate + ALB architecture with:
- API Gateway HTTP API
- Lambda functions (workflow, deployment, step handlers)
- Step Functions state machine for deployment orchestration
- DynamoDB tables (workflows + deployments)
- S3 + CloudFront for frontend
- Least-privilege IAM roles

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 6.1, 6.2, 6.3, 7.1, 7.2, 7.3, 7.4, 7.5
"""

import os

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, RemovalPolicy, Size
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as apigw_authorizers
from aws_cdk import aws_apigatewayv2_integrations as apigw_integrations
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3_deployment
from aws_cdk import aws_ssm as ssm
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as sfn_tasks
from aws_cdk import aws_wafv2 as wafv2
from aws_cdk import custom_resources as cr
import cdk_nag
from constructs import Construct


class PlatformStack(cdk.Stack):
    """CDK stack defining all serverless resources for the platform."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment_name: str,
        project_name: str,
        otel_endpoint: str = "",
        otel_auth_secret_arn: str = "",
        otel_sample_rate: str = "1.0",
        otel_service_name_prefix: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        cdk.Tags.of(self).add("Environment", environment_name)
        cdk.Tags.of(self).add("Project", project_name)

        self._env = environment_name
        self._project = project_name
        # Audit issue #9: gate RemovalPolicy.DESTROY on environment so prod-like
        # envs don't lose data on teardown. dev/test/sandbox/preview environments
        # use DESTROY; everything else uses RETAIN. Override via env var
        # AGENTCORE_ALLOW_DESTROY=true.
        _destroy_envs = {"dev", "test", "sandbox", "preview", "ephemeral"}
        _allow_destroy = (environment_name or "").lower() in _destroy_envs or os.environ.get(
            "AGENTCORE_ALLOW_DESTROY", "false"
        ).lower() == "true"
        self._removal_policy = RemovalPolicy.DESTROY if _allow_destroy else RemovalPolicy.RETAIN
        self._allow_destroy = _allow_destroy
        # Platform OTEL defaults — feature is enabled iff both endpoint and
        # secret ARN are provided. Lambdas + agents inherit these.
        self._otel_endpoint = otel_endpoint
        self._otel_auth_secret_arn = otel_auth_secret_arn
        self._otel_sample_rate = otel_sample_rate or "1.0"
        self._otel_service_name_prefix = otel_service_name_prefix or project_name
        self._otel_enabled = bool(otel_endpoint and otel_auth_secret_arn)

        # --- Storage ---
        self.workflows_table = self._create_workflows_table()
        self.deployments_table = self._create_deployments_table()
        self.flows_table = self._create_flows_table()
        # Phase 1 Gap 1A — versioning + slot tables.
        self.agent_versions_table = self._create_agent_versions_table()
        self.runtime_slots_table = self._create_runtime_slots_table()
        # Phase 2 Gap 2A — agent registry / catalog table.
        self.agent_registry_table = self._create_agent_registry_table()
        # Phase 2 Gap 2B — usage events table (optional/dormant write path for
        # explicit per-invocation usage events; primary cost path is query-time).
        self.usage_events_table = self._create_usage_events_table()
        # Phase 2 Gap 2D — human-in-the-loop approval requests table.
        self.hitl_requests_table = self._create_hitl_requests_table()
        # Phase 3 Gap 3F — scheduled / event triggers registry table.
        self.triggers_table = self._create_triggers_table()
        # Phase 3 Gap 3H — prompt library / catalog table.
        self.prompt_library_table = self._create_prompt_library_table()
        self.logging_bucket = self._create_logging_bucket()
        self.artifacts_bucket = self._create_artifacts_bucket()
        self._upload_agentcore_deps()

        # --- SSM Parameters ---
        self._create_ssm_parameters()

        # --- Shared AgentCore runtime execution role ---
        # Created once at stack-deploy time so AgentCore's IAM cache has
        # fully propagated by the time any user-deploy runs. Eliminates the
        # 17-20 min IAM-propagation race that per-deploy roles hit.
        # See tasks/lessons.md Bug 60.
        self.shared_runtime_role = self._create_shared_runtime_role()

        # --- Lambda Functions ---
        self.backend_code = self._get_backend_code()
        self.workflow_lambda = self._create_workflow_lambda()
        self.deployment_lambda = self._create_deployment_lambda()
        self.step_lambdas = self._create_step_lambdas()

        # --- Step Functions ---
        self.state_machine = self._create_state_machine()

        # --- Grant deployment Lambda permission to start executions ---
        self.state_machine.grant_start_execution(self.deployment_lambda)

        # --- API Gateway ---
        self.user_pool, self.user_pool_client = self._create_cognito()
        self.api = self._create_api_gateway()

        # --- Streaming test Lambda + Function URL (Bug 157) ---
        # API Gateway HTTP API has a hard 30s integration cap; tool-heavy agents
        # (>30s) time out at the transport. A Lambda Function URL with
        # InvokeMode=RESPONSE_STREAM lets long invokes stream past 30s. Created
        # after Cognito so it can verify the same pool/client JWT in-handler.
        self.stream_lambda, self.stream_function_url = self._create_stream_lambda()

        # --- S3 + CloudFront + WAF ---
        self.web_acl = self._create_waf_web_acl()
        # NOTE: We previously attempted a regional WAF on the API Gateway stage
        # to prevent direct execute-api.amazonaws.com bypass of the CloudFront
        # WAF, but WAFv2 only supports REST API Gateway (v1), NOT HTTP API
        # Gateway (v2) which this stack uses. Tracked as a known gap; mitigated
        # by API Gateway throttling already configured on the default stage.
        # See tasks/lessons.md Bug 41 (revised).
        self.bucket = self._create_s3_bucket()
        self.distribution = self._create_cloudfront_distribution()

        # --- Post-creation: add CloudFront URL to API Gateway CORS ---
        self._add_cloudfront_cors_origin()

        # --- CloudWatch Alarms ---
        self._create_lambda_alarms()

        # --- Update SSM with runtime URLs ---
        self._create_runtime_ssm_parameters()

        # --- Stack Outputs ---
        self._create_stack_outputs()

        # --- CDK-NAG suppressions (per-construct, audit issue #4) ---
        self._apply_nag_suppressions()

    # ------------------------------------------------------------------
    # DynamoDB Tables
    # ------------------------------------------------------------------

    def _create_workflows_table(self) -> dynamodb.Table:
        """Create DynamoDB table for workflow storage (kept from previous arch).

        Requirements: 7.1
        """
        return dynamodb.Table(
            self,
            "WorkflowsTable",
            table_name=f"{self._project}-{self._env}-workflows",
            partition_key=dynamodb.Attribute(
                name="workflow_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            # Audit #9: gated on env so prod doesn't lose data on teardown.
            removal_policy=self._removal_policy,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
        )

    def _create_deployments_table(self) -> dynamodb.Table:
        """Create DynamoDB table for deployment state with TTL and GSI.

        Requirements: 4.1, 4.2, 4.3, 7.1
        """
        table = dynamodb.Table(
            self,
            "DeploymentsTable",
            table_name=f"{self._project}-{self._env}-deployments",
            partition_key=dynamodb.Attribute(
                name="deployment_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=self._removal_policy,
            time_to_live_attribute="ttl",
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
        )
        table.add_global_secondary_index(
            index_name="workflow_id-index",
            partition_key=dynamodb.Attribute(
                name="workflow_id",
                type=dynamodb.AttributeType.STRING,
            ),
        )
        table.add_global_secondary_index(
            index_name="user_id-index",
            partition_key=dynamodb.Attribute(
                name="user_id",
                type=dynamodb.AttributeType.STRING,
            ),
        )
        # Audit issue #7: deployment_handler._scan_for_runtime previously did
        # a full O(N) Scan on every test/delete. Adding a runtime_id GSI lets
        # the handler use Query instead — O(1) on the GSI partition key.
        table.add_global_secondary_index(
            index_name="runtime_id-index",
            partition_key=dynamodb.Attribute(
                name="runtime_id",
                type=dynamodb.AttributeType.STRING,
            ),
        )
        return table

    def _create_flows_table(self) -> dynamodb.Table:
        """Create DynamoDB table for named, saveable flow persistence.

        Requirements: 7.1
        """
        return dynamodb.Table(
            self,
            "FlowsTable",
            table_name=f"{self._project}-{self._env}-flows",
            partition_key=dynamodb.Attribute(
                name="flow_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=self._removal_policy,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
        )

    def _create_agent_versions_table(self) -> dynamodb.Table:
        """Phase 1 Gap 1A — DynamoDB table for AgentVersions.

        PK: ``runtime_name`` (the friendly name the user typed)
        SK: ``version_id`` (sortable id; lex order = chronological)
        GSI: ``owner_sub-version_id-index`` for list-by-user queries.

        Composite key supports list-versions-of-a-runtime via Query (newest
        first via ScanIndexForward=False). The owner_sub GSI supports the
        cross-runtime "all my versions" view for a future registry tab.
        """
        table = dynamodb.Table(
            self,
            "AgentVersionsTable",
            table_name=f"{self._project}-{self._env}-agent-versions",
            partition_key=dynamodb.Attribute(
                name="runtime_name",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="version_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=self._removal_policy,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
        )
        table.add_global_secondary_index(
            index_name="owner_sub-version_id-index",
            partition_key=dynamodb.Attribute(
                name="owner_sub",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="version_id",
                type=dynamodb.AttributeType.STRING,
            ),
        )
        return table

    def _create_runtime_slots_table(self) -> dynamodb.Table:
        """Phase 1 Gap 1A — DynamoDB table for runtime production/staging slots.

        PK: ``runtime_name``. One row per friendly name. Stores which version
        is currently in production vs. staging, plus the previous-production
        pointer used by /rollback. Owner_sub is on the row itself (not a GSI)
        because reads are always keyed on runtime_name.
        """
        return dynamodb.Table(
            self,
            "RuntimeSlotsTable",
            table_name=f"{self._project}-{self._env}-runtime-slots",
            partition_key=dynamodb.Attribute(
                name="runtime_name",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=self._removal_policy,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
        )

    def _create_agent_registry_table(self) -> dynamodb.Table:
        """Phase 2 Gap 2A — DynamoDB table for the agent registry / catalog.

        PK: ``org_id``, SK: ``agent_slug``. One row per published agent.
        GSI ``owner_sub-agent_slug-index`` for list-by-publisher.
        GSI ``visibility-agent_slug-index`` for list-public discovery.

        Visibility model (private/org/public) is enforced in routers/registry.py;
        the table stores the raw entries and the router filters on read.
        """
        table = dynamodb.Table(
            self,
            "AgentRegistryTable",
            table_name=f"{self._project}-{self._env}-agent-registry",
            partition_key=dynamodb.Attribute(
                name="org_id",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="agent_slug",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=self._removal_policy,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
        )
        table.add_global_secondary_index(
            index_name="owner_sub-agent_slug-index",
            partition_key=dynamodb.Attribute(
                name="owner_sub",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="agent_slug",
                type=dynamodb.AttributeType.STRING,
            ),
        )
        table.add_global_secondary_index(
            index_name="visibility-agent_slug-index",
            partition_key=dynamodb.Attribute(
                name="visibility",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="agent_slug",
                type=dynamodb.AttributeType.STRING,
            ),
        )
        return table

    def _create_hitl_requests_table(self) -> dynamodb.Table:
        """Phase 2 Gap 2D — DynamoDB table for human-in-the-loop approvals.

        PK ``runtime_id`` (the agent-stamped AgentCore runtime NAME), SK
        ``request_id`` (sortable). GSI ``owner_sub-request_id-index`` powers the
        tenant-scoped pending queue. Rows carry a ``ttl`` (24h) so DynamoDB
        auto-expires decided/abandoned requests — no destroy_runtime cascade.
        """
        table = dynamodb.Table(
            self,
            "HitlRequestsTable",
            table_name=f"{self._project}-{self._env}-hitl-requests",
            partition_key=dynamodb.Attribute(
                name="runtime_id",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="request_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=self._removal_policy,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            time_to_live_attribute="ttl",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
        )
        table.add_global_secondary_index(
            index_name="owner_sub-request_id-index",
            partition_key=dynamodb.Attribute(
                name="owner_sub",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="request_id",
                type=dynamodb.AttributeType.STRING,
            ),
        )
        return table

    def _create_triggers_table(self) -> dynamodb.Table:
        """Phase 3 Gap 3F — DynamoDB table for scheduled / event triggers.

        PK ``runtime_name`` (tenant-supplied friendly name; the router gates
        every write/list/delete through the production-slot owner, so the Bug
        122 PK-collision class is closed by ownership resolution). SK
        ``trigger_id`` (sortable hex). GSI ``owner_sub-trigger_id-index`` powers
        the owner-scoped list-across-runtimes query. No TTL — rows live until the
        trigger is deleted or destroy_runtime cleans them up (Bug 124).
        """
        table = dynamodb.Table(
            self,
            "TriggersTable",
            table_name=f"{self._project}-{self._env}-triggers",
            partition_key=dynamodb.Attribute(
                name="runtime_name",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="trigger_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=self._removal_policy,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
        )
        table.add_global_secondary_index(
            index_name="owner_sub-trigger_id-index",
            partition_key=dynamodb.Attribute(
                name="owner_sub",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="trigger_id",
                type=dynamodb.AttributeType.STRING,
            ),
        )
        return table

    def _create_prompt_library_table(self) -> dynamodb.Table:
        """Phase 3 Gap 3H — DynamoDB table for the prompt library / catalog.

        PK ``org_id``, SK ``prompt_name``. One row per saved prompt. GSI
        ``owner_sub-prompt_name-index`` for the list-by-author view. Mirrors
        _create_agent_registry_table; routers/prompts.py (mounted on the
        deployment Lambda) reads/writes this table.
        """
        table = dynamodb.Table(
            self,
            "PromptLibraryTable",
            table_name=f"{self._project}-{self._env}-prompt-library",
            partition_key=dynamodb.Attribute(
                name="org_id",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="prompt_name",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=self._removal_policy,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
        )
        table.add_global_secondary_index(
            index_name="owner_sub-prompt_name-index",
            partition_key=dynamodb.Attribute(
                name="owner_sub",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="prompt_name",
                type=dynamodb.AttributeType.STRING,
            ),
        )
        return table

    def _create_usage_events_table(self) -> dynamodb.Table:
        """Phase 2 Gap 2B — DynamoDB table for explicit per-invocation usage
        events (cost analytics + FinOps).

        PK ``runtime_id`` (AWS-assigned, never tenant-supplied), SK
        ``event_id`` (sortable). GSI ``owner_sub-event_id-index`` for the
        list-by-owner cross-runtime view. TTL on ``ttl`` (90-day) bounds growth.

        OPTIONAL / DORMANT in the primary flow: the cost endpoint derives
        cost at query-time from CloudWatch Logs gen_ai.usage attrs, so no
        rows are written until a future codegen span-processor hook lands.
        """
        table = dynamodb.Table(
            self,
            "UsageEventsTable",
            table_name=f"{self._project}-{self._env}-usage-events",
            partition_key=dynamodb.Attribute(
                name="runtime_id",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="event_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=self._removal_policy,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            time_to_live_attribute="ttl",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
        )
        table.add_global_secondary_index(
            index_name="owner_sub-event_id-index",
            partition_key=dynamodb.Attribute(
                name="owner_sub",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="event_id",
                type=dynamodb.AttributeType.STRING,
            ),
        )
        return table

    # ------------------------------------------------------------------
    # SSM Parameters
    # ------------------------------------------------------------------

    def _create_ssm_parameters(self) -> None:
        """Create SSM parameters under /agentcore-workflow/{env}/ path.

        Requirements: 7.5
        """
        prefix = f"/agentcore-workflow/{self._env}"

        ssm.StringParameter(
            self,
            "CorsOriginsParam",
            parameter_name=f"{prefix}/cors-origins",
            string_value="http://localhost:5173",
            description="Allowed CORS origins for the backend API",
        )

        ssm.StringParameter(
            self,
            "AwsRegionParam",
            parameter_name=f"{prefix}/aws-region",
            string_value=self.region,
            description="AWS region for the platform",
        )

        ssm.StringParameter(
            self,
            "WorkflowsTableNameParam",
            parameter_name=f"{prefix}/dynamodb-table-name",
            string_value=self.workflows_table.table_name,
            description="DynamoDB table name for workflow storage",
        )

        ssm.StringParameter(
            self,
            "DeploymentsTableNameParam",
            parameter_name=f"{prefix}/deployments-table-name",
            string_value=self.deployments_table.table_name,
            description="DynamoDB table name for deployment state",
        )

        ssm.StringParameter(
            self,
            "FlowsTableNameParam",
            parameter_name=f"{prefix}/dynamodb-flows-table-name",
            string_value=self.flows_table.table_name,
            description="DynamoDB table name for flow persistence",
        )

        # Platform OTEL defaults — only written when the feature is enabled.
        # Backend reads these via services.observability.get_platform_observability_defaults().
        if self._otel_enabled:
            ssm.StringParameter(
                self,
                "OtelEndpointParam",
                parameter_name=f"{prefix}/otel/endpoint",
                string_value=self._otel_endpoint,
                description="Platform-default OTLP endpoint for all deployed agents",
            )
            ssm.StringParameter(
                self,
                "OtelAuthSecretArnParam",
                parameter_name=f"{prefix}/otel/auth-secret-arn",
                string_value=self._otel_auth_secret_arn,
                description="Platform-default OTLP auth header Secrets Manager ARN",
            )
            ssm.StringParameter(
                self,
                "OtelSampleRateParam",
                parameter_name=f"{prefix}/otel/sample-rate",
                string_value=self._otel_sample_rate,
                description="Platform-default OTLP trace sample rate (0.0-1.0)",
            )
            ssm.StringParameter(
                self,
                "OtelServiceNamePrefixParam",
                parameter_name=f"{prefix}/otel/service-name-prefix",
                string_value=self._otel_service_name_prefix,
                description="Platform-default OTEL service.name prefix",
            )

    def _create_runtime_ssm_parameters(self) -> None:
        """Create SSM parameters that depend on runtime resources (API GW URL)."""
        prefix = f"/agentcore-workflow/{self._env}"

        ssm.StringParameter(
            self,
            "ApiGatewayUrlParam",
            parameter_name=f"{prefix}/api-gateway-url",
            string_value=self.api.url or "",
            description="API Gateway HTTP API URL",
        )

    # ------------------------------------------------------------------
    # Lambda Code Asset
    # ------------------------------------------------------------------

    def _get_backend_code(self) -> _lambda.Code:
        """Package the backend source as a Lambda code asset with bundled dependencies.

        Dependencies are pre-installed into backend/lib/ by the deploy script
        (pip install -r requirements-lambda.txt -t backend/lib/).
        The asset includes both src/ and lib/ directories.
        """
        backend_path = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
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

    # ------------------------------------------------------------------
    # S3 (Artifacts Bucket + AgentCore Deps Upload)
    # Audit #12: section banner — _create_artifacts_bucket and
    # _upload_agentcore_deps create S3 resources, distinct from the
    # "Lambda Functions" group below at _create_shared_runtime_role.
    # ------------------------------------------------------------------

    def _create_artifacts_bucket(self) -> s3.Bucket:
        """Create S3 bucket for deployment code artifacts."""
        return s3.Bucket(
            self,
            "ArtifactsBucket",
            bucket_name=f"{self._project}-{self._env}-artifacts-{self.region}-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=self._removal_policy,
            auto_delete_objects=self._allow_destroy,
            encryption=s3.BucketEncryption.S3_MANAGED,
            server_access_logs_bucket=self.logging_bucket,
            server_access_logs_prefix="s3-artifacts/",
            lifecycle_rules=[
                s3.LifecycleRule(expiration=Duration.days(90), prefix="deployments/"),
            ],
        )

    def _upload_agentcore_deps(self) -> None:
        """Upload pre-built aarch64 dependency bundles to S3 artifacts bucket.

        Uses s3_deployment.BucketDeployment to sync backend/agentcore-deps/*.zip
        to s3://{artifacts_bucket}/agentcore-deps/

        Gracefully skips if the bundle directory does not exist (e.g. local dev).

        Requirements: 2.1, 2.2, 2.3
        """
        deps_path = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "backend", "agentcore-deps"))
        if not os.path.isdir(deps_path):
            return

        self.agentcore_deps_deployment = s3_deployment.BucketDeployment(
            self,
            "AgentCoreDepsDeployment",
            sources=[s3_deployment.Source.asset(deps_path)],
            destination_bucket=self.artifacts_bucket,
            destination_key_prefix="agentcore-deps",
            memory_limit=512,
            ephemeral_storage_size=Size.mebibytes(1024),
        )

    # ------------------------------------------------------------------
    # Platform OTEL — env vars + IAM for every platform Lambda.
    # ------------------------------------------------------------------
    # We deliberately do NOT use the AWS-managed ADOT Python Lambda layer.
    # Verified live on 2026-05-15 (tasks/lessons.md Bug 22): the ADOT
    # exec-wrapper at /opt/otel-instrument calls Python __import__() on the
    # handler string, which fails for slash-form handlers like
    # `src/app/lambda_handler.handler` used here. The ADOT layer also bundles
    # an older `typing_extensions` that shadows /var/task/lib/typing_extensions/
    # and breaks pydantic_core import.
    #
    # Instead, services/_otel_platform.py manually builds an OTLP exporter
    # at module import time using the SDK packages bundled by
    # requirements-lambda.txt. Each handler module imports it FIRST.

    def _platform_otel_env(self) -> dict[str, str]:
        """Standard OTEL env vars for platform Lambdas.

        Empty dict when platform OTEL is not configured — caller can blindly
        merge into the Lambda's environment without conditionals.
        """
        if not self._otel_enabled:
            return {}
        return {
            "OTEL_EXPORTER_OTLP_ENDPOINT": self._otel_endpoint,
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
            "OTEL_TRACES_SAMPLER_ARG": self._otel_sample_rate,
            "OTEL_RESOURCE_ATTRIBUTES": (
                f"service.namespace=agentcore-platform,"
                f"deployment.environment={self._env}"
            ),
            "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
            # Tighten span export so a slow Langfuse endpoint doesn't burn 10s
            # of Lambda CPU per failed export. Live test 2026-05-16 saw repeated
            # 10s read timeouts; tasks/lessons.md Bug 30.
            "OTEL_EXPORTER_OTLP_TIMEOUT": "2000",
            "OTEL_BSP_SCHEDULE_DELAY": "1000",
            "OTEL_BSP_EXPORT_TIMEOUT": "5000",
            # Resolved to OTEL_EXPORTER_OTLP_HEADERS at module import by
            # services/_otel_platform.py.
            "OTEL_AUTH_SECRET_ARN": self._otel_auth_secret_arn,
        }

    def _apply_platform_otel(
        self, fn: _lambda.Function, fn_purpose: str
    ) -> None:
        """Add OTEL env vars + scoped Secrets Manager perms to a Lambda.

        Idempotent and no-op when platform OTEL is not configured (so callers
        don't have to gate on self._otel_enabled). When configured, the Lambda
        will:
          - Receive the OTLP endpoint + auth-secret ARN as env vars
          - Resolve the auth-header secret at module import (via _otel_platform.py)
          - Emit spans tagged service.name={fn_purpose}, service.namespace=agentcore-platform
        """
        if not self._otel_enabled:
            return

        for k, v in self._platform_otel_env().items():
            fn.add_environment(k, v)
        # Per-Lambda service.name so each shows up distinctly in Langfuse.
        fn.add_environment("OTEL_SERVICE_NAME", f"{self._otel_service_name_prefix}-{fn_purpose}")

        # Scoped GetSecretValue on the platform OTEL auth secret only.
        fn.role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="PlatformOtelAuthSecretRead",
                actions=["secretsmanager:GetSecretValue"],
                resources=[self._otel_auth_secret_arn],
            )
        )

    # ------------------------------------------------------------------
    # IAM Roles + Lambda Functions
    # Audit #12: section banner — shared runtime role, workflow Lambda,
    # deployment Lambda, per-step IAM roles (_create_step_role), and per-step
    # Lambdas (_create_step_lambdas) all live below this banner.
    # ------------------------------------------------------------------

    def _create_shared_runtime_role(self) -> iam.Role:
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
            self,
            "SharedRuntimeExecRole",
            role_name=f"AgentCoreRuntime-{self._project}-{self._env}-shared",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description=(
                "Shared execution role used by every AgentCore runtime "
                "deployed by this stack. Pre-created so AgentCore's IAM "
                "cache has propagated by user-deploy time."
            ),
        )
        # Bedrock model access (Strands needs both InvokeModel + Stream)
        role.add_to_policy(iam.PolicyStatement(
            actions=[
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
                "bedrock:Converse",
                "bedrock:ConverseStream",
            ],
            resources=["*"],
        ))
        # Read the agent code zip from the artifacts bucket
        self.artifacts_bucket.grant_read(role)
        # CloudWatch Logs (auto-instrumented by AgentCore Runtime)
        role.add_to_policy(iam.PolicyStatement(
            actions=[
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
            ],
            resources=["*"],
        ))
        # All AgentCore runtime tool integrations (browser, code interpreter,
        # gateway, memory, guardrails, evaluation, policy). Wildcards because
        # tools are dynamically connected per-runtime; restricting per-tool
        # would require separate roles, which defeats the IAM-race fix.
        role.add_to_policy(iam.PolicyStatement(
            actions=[
                "bedrock-agentcore:*Browser*",
                "bedrock-agentcore:*CodeInterpreter*",
                "bedrock-agentcore:InvokeGateway",
                "bedrock-agentcore:ListGateways",
                "bedrock-agentcore:GetGateway",
                "bedrock-agentcore:*Memory*",
                "bedrock-agentcore:CreateEvent",
                "bedrock-agentcore:GetLastKTurns",
                "bedrock-agentcore:RetrieveMemories",
                "bedrock-agentcore:ListSessions",
                "bedrock-agentcore:ListActors",
                "bedrock-agentcore:ListEvents",
                "bedrock:ApplyGuardrail",
                "bedrock:GetGuardrail",
                # Knowledge Base retrieve (called by retrieve_from_kb tool
                # in agents that have a KB connected). See lessons Bug 87.
                "bedrock:Retrieve",
                "bedrock:RetrieveAndGenerate",
            ],
            resources=["*"],
        ))
        # Optional OTEL auth-header secret (when platform OTEL is configured).
        if self._otel_enabled:
            role.add_to_policy(iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[self._otel_auth_secret_arn],
            ))
        # Phase 2 Gap 2D — the injected human_approval @tool writes PENDING
        # approval rows. Scoped to the single HITL table; PutItem only.
        role.add_to_policy(iam.PolicyStatement(
            actions=["dynamodb:PutItem"],
            resources=[self.hitl_requests_table.table_arn],
        ))
        return role

    def _create_workflow_lambda(self) -> _lambda.Function:
        """Create Workflow Lambda (FastAPI + Mangum) for CRUD operations.

        Requirements: 1.1, 1.5, 6.1
        """
        role = iam.Role(
            self,
            "WorkflowLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
            ],
        )
        # DynamoDB workflows table: read/write
        self.workflows_table.grant_read_write_data(role)
        # DynamoDB flows table: read/write
        self.flows_table.grant_read_write_data(role)
        # SSM read for app config
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ssm:GetParameter",
                    "ssm:GetParameters",
                    "ssm:GetParametersByPath",
                ],
                resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/agentcore-workflow/{self._env}/*"],
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
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:agentcore-otel/*",
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
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:agentcore-git/*",
                ],
            )
        )

        fn = _lambda.Function(
            self,
            "WorkflowLambda",
            function_name=f"{self._project}-{self._env}-workflow",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="src/app/lambda_handler.handler",
            code=self.backend_code,
            memory_size=512,
            timeout=Duration.seconds(30),
            role=role,
            tracing=_lambda.Tracing.ACTIVE,
            environment={
                "DYNAMODB_TABLE_NAME": self.workflows_table.table_name,
                "DYNAMODB_FLOWS_TABLE_NAME": self.flows_table.table_name,
                "ENVIRONMENT": self._env,
                "APP_AWS_REGION": self.region,
                "POWERTOOLS_SERVICE_NAME": "workflow",
                "PYTHONPATH": "/var/task/src:/var/task:/var/task/lib",
            },
            log_group=logs.LogGroup(
                self,
                "WorkflowLambdaLogGroup",
                log_group_name=f"/aws/lambda/{self._project}-{self._env}-workflow",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        self._apply_platform_otel(fn, "workflow")
        return fn

    def _create_deployment_lambda(self) -> _lambda.Function:
        """Create Deployment Lambda for deploy/status/test/delete operations.

        Requirements: 1.2, 6.2
        """
        role = iam.Role(
            self,
            "DeploymentLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
            ],
        )
        # DynamoDB deployments table: read/write
        self.deployments_table.grant_read_write_data(role)
        # Phase 1 Gap 1A — versions + slots tables. The deployment Lambda is
        # the read-write owner: handle_deploy() seeds the AgentVersion row,
        # and the versions router promotes/rolls back slots.
        self.agent_versions_table.grant_read_write_data(role)
        self.runtime_slots_table.grant_read_write_data(role)
        # Phase 2 Gap 2A — agent registry. The registry router (publish/
        # search/clone/update/delete) is mounted on the deployment Lambda.
        self.agent_registry_table.grant_read_write_data(role)
        # Phase 2 Gap 2B — usage events table (optional write path). The
        # query-time cost path uses logs:StartQuery already granted below.
        self.usage_events_table.grant_read_write_data(role)
        # Phase 2 Gap 2D — HITL approval queue. routers/hitl.py (mounted on the
        # deployment Lambda) reads the owner_sub GSI and decides requests.
        self.hitl_requests_table.grant_read_write_data(role)
        # Phase 3 Gap 3F — triggers registry. routers/triggers.py (mounted on
        # the deployment Lambda) reads/writes this table + the owner_sub GSI.
        self.triggers_table.grant_read_write_data(role)
        # Phase 3 Gap 3H — prompt library. routers/prompts.py (mounted on the
        # deployment Lambda) reads/writes this table + the owner_sub GSI.
        self.prompt_library_table.grant_read_write_data(role)
        # states:StartExecution on the state machine (granted after SM creation)
        # SSM read
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ssm:GetParameter",
                    "ssm:GetParameters",
                    "ssm:GetParametersByPath",
                ],
                resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/agentcore-workflow/{self._env}/*"],
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
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    # KB cleanup on runtime delete (Bug 90).
                    "bedrock:GetKnowledgeBase",
                    "bedrock:ListKnowledgeBases",
                    "bedrock:DeleteKnowledgeBase",
                    "bedrock:DeleteDataSource",
                    "bedrock:GetDataSource",
                    "bedrock:ListDataSources",
                    # Guardrail cleanup on runtime delete. The manifest delete path
                    # (and the legacy guardrails_result fallback) run in THIS
                    # Lambda and call DeleteGuardrail — without it the guardrail
                    # orphans with AccessDenied (Bug 165, caught live: the guardrail
                    # step role had Delete but the deployment/delete role did not).
                    "bedrock:GetGuardrail",
                    "bedrock:DeleteGuardrail",
                    # Explicit AgentCore actions used by the deployment Lambda:
                    # /api/test-runtime invokes; /api/runtime/{id} DELETE
                    # cascades through Get/Delete on runtime + endpoint +
                    # gateway + memory + policy resources.
                    "bedrock-agentcore:InvokeAgentRuntime",
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
                    # Bug 167 (caught live 2026-06-25): the KB step now
                    # self-provisions an S3 Vectors bucket+index (Bug 145), so
                    # the manifest delete path in THIS Lambda must be able to
                    # tear it down. Without these the s3_vectors_bucket cleanup
                    # fails AccessDenied -> orphaned vector bucket + delete
                    # returns success=False. Mirrors the KB step role's create
                    # verbs with the matching delete/list verbs.
                    "s3vectors:ListVectorBuckets",
                    "s3vectors:GetVectorBucket",
                    "s3vectors:DescribeVectorBucket",
                    "s3vectors:DeleteVectorBucket",
                    "s3vectors:ListIndexes",
                    "s3vectors:GetIndex",
                    "s3vectors:DescribeIndex",
                    "s3vectors:DeleteIndex",
                    # OpenSearch Serverless: the KB step auto-provisions an OSS
                    # collection; this (deployment/teardown) role only needs the
                    # DELETE verbs to reclaim it via the manifest (standing billable
                    # resource — orphan = ~$350/mo). Create verbs live on the KB step
                    # role, keeping this policy small enough to avoid an overflow policy.
                    "aoss:BatchGetCollection",
                    "aoss:DeleteCollection",
                    "aoss:DeleteSecurityPolicy",
                    "aoss:DeleteAccessPolicy",
                ],
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
                resources=[f"arn:aws:cognito-idp:{self.region}:{self.account}:userpool/*"],
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
                resources=[f"arn:aws:iam::{self.account}:role/AgentCore*"],
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
                resources=[f"arn:aws:iam::{self.account}:role/*-role"],
                conditions={
                    "StringEquals": {"aws:ResourceTag/ManagedBy": "agentcore-flows"}
                },
            )
        )
        # Stamp/repair the ManagedBy tag on runtime exec roles (needed so the
        # tag-gated cleanup grant above can match them). Scoped to *-role names.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:TagRole"],
                resources=[f"arn:aws:iam::{self.account}:role/*-role"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "lambda:CreateFunction",
                    "lambda:GetFunction",
                    "lambda:InvokeFunction",
                    "lambda:DeleteFunction",
                ],
                resources=[
                    f"arn:aws:lambda:{self.region}:{self.account}:function:AgentCore*",
                    # Bug 175: the MCP-server path's intercept lambda is named
                    # "MCPServerRuntime" (no AgentCore prefix), so deleting an
                    # MCP-server flow failed lambda:DeleteFunction with AccessDenied
                    # and orphaned the function. Cover the MCP lambda names too.
                    f"arn:aws:lambda:{self.region}:{self.account}:function:MCPServer*",
                ],
            )
        )
        # S3 artifacts bucket: read/write for CFN template generation
        self.artifacts_bucket.grant_read_write(role)
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
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:agentcore-trigger/*",
                    # Phase A SaaS connectors: the direct-deploy path (services/
                    # deployment.py -> deploy_gateway / cleanup_gateway_resources)
                    # mints/reads/deletes connector credential secrets under the
                    # agentcore-connector/ prefix — Bug 9 parity with the SFN
                    # gateway step. Secrets live ONLY here — never in canvas
                    # JSON, DDB, or logs.
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:agentcore-connector/*",
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
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:bedrock-agentcore-*",
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:AgentCore*",
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
            self,
            "DeploymentLambda",
            function_name=f"{self._project}-{self._env}-deployment",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="src/app/deployment_handler.handler",
            code=self.backend_code,
            memory_size=512,
            timeout=Duration.seconds(120),
            role=role,
            tracing=_lambda.Tracing.ACTIVE,
            environment={
                "DEPLOYMENTS_TABLE_NAME": self.deployments_table.table_name,
                "DEPLOYMENT_TABLE_NAME": self.deployments_table.table_name,
                "WORKFLOWS_TABLE_NAME": self.workflows_table.table_name,
                # Phase 1 Gap 1A — versioning tables.
                "AGENT_VERSIONS_TABLE_NAME": self.agent_versions_table.table_name,
                "RUNTIME_SLOTS_TABLE_NAME": self.runtime_slots_table.table_name,
                # Phase 2 Gap 2A — agent registry table.
                "AGENT_REGISTRY_TABLE_NAME": self.agent_registry_table.table_name,
                # Phase 2 Gap 2B — usage events table (cost_tracking store).
                "USAGE_EVENTS_TABLE_NAME": self.usage_events_table.table_name,
                # Phase 2 Gap 2D — HITL requests table (routers/hitl.py store).
                "HITL_REQUESTS_TABLE_NAME": self.hitl_requests_table.table_name,
                # Phase 3 Gap 3F — triggers registry table (routers/triggers.py).
                "TRIGGERS_TABLE_NAME": self.triggers_table.table_name,
                # Phase 3 Gap 3H — prompt library table (routers/prompts.py).
                "PROMPT_LIBRARY_TABLE_NAME": self.prompt_library_table.table_name,
                "ARTIFACTS_BUCKET_NAME": self.artifacts_bucket.bucket_name,
                "ENVIRONMENT": self._env,
                "APP_AWS_REGION": self.region,
                "POWERTOOLS_SERVICE_NAME": "deployment",
                "TOOL_GENERATOR_MODEL_ID": f"{'eu' if self.region.startswith('eu-') else 'ap' if self.region.startswith('ap-') else 'us'}.anthropic.claude-sonnet-5",
                "PYTHONPATH": "/var/task/src:/var/task:/var/task/lib",
                # Needed by destroy_runtime to skip cascade-deletion of the
                # stack-managed shared runtime role (Bug 62).
                "SHARED_RUNTIME_ROLE_ARN": self.shared_runtime_role.role_arn,
            },
            log_group=logs.LogGroup(
                self,
                "DeploymentLambdaLogGroup",
                log_group_name=f"/aws/lambda/{self._project}-{self._env}-deployment",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        self._apply_platform_otel(fn, "deployment")
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
                resources=[
                    f"arn:aws:lambda:{self.region}:{self.account}:function:"
                    f"{self._project}-{self._env}-deployment"
                ],
            )
        )
        return fn

    def _create_stream_lambda(self) -> tuple:
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
            self,
            "StreamLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
            ],
        )
        # Read deployment records (runtime_id GSI + scan fallback) for ARN
        # resolution + tenant-isolation checks. Read-only is sufficient — the
        # stream path never mutates state.
        self.deployments_table.grant_read_data(role)
        # SSM read (config loader reads /agentcore-workflow/{env}/* like the
        # other Lambdas).
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"],
                resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/agentcore-workflow/{self._env}/*"],
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
            self,
            "StreamLambda",
            function_name=f"{self._project}-{self._env}-stream",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="src/app/stream_handler.lambda_handler",
            code=self.backend_code,
            memory_size=512,
            # Generous timeout so tool-heavy agents can run well past the 30s
            # API Gateway cap. Function URLs support response streaming up to
            # ~15 min; the boto read timeout in the handler is bounded below it.
            timeout=Duration.minutes(15),
            role=role,
            tracing=_lambda.Tracing.ACTIVE,
            environment={
                "DEPLOYMENTS_TABLE_NAME": self.deployments_table.table_name,
                "DEPLOYMENT_TABLE_NAME": self.deployments_table.table_name,
                "ENVIRONMENT": self._env,
                "APP_AWS_REGION": self.region,
                "POWERTOOLS_SERVICE_NAME": "stream",
                "PYTHONPATH": "/var/task/src:/var/task:/var/task/lib",
                # In-handler Cognito JWT verification config (same pool/client as
                # the API-GW HttpJwtAuthorizer). stream_handler verifies the
                # access token's issuer + client_id + signature before invoking.
                "COGNITO_USER_POOL_ID": self.user_pool.user_pool_id,
                "COGNITO_CLIENT_ID": self.user_pool_client.user_pool_client_id,
                "COGNITO_REGION": self.region,
            },
            log_group=logs.LogGroup(
                self,
                "StreamLambdaLogGroup",
                log_group_name=f"/aws/lambda/{self._project}-{self._env}-stream",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        self._apply_platform_otel(fn, "stream")

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
            self,
            "TestRuntimeStreamUrl",
            value=fn_url.url,
            description="Lambda Function URL (RESPONSE_STREAM) for >30s runtime tests",
        )
        ssm.StringParameter(
            self,
            "TestRuntimeStreamUrlParam",
            parameter_name=f"/agentcore-workflow/{self._env}/test-runtime-stream-url",
            string_value=fn_url.url,
            description="Lambda Function URL for streaming runtime tests (Bug 157)",
        )

        return fn, fn_url

    def _create_step_role(self, step_name: str) -> iam.Role:
        """Create a dedicated IAM role for a step Lambda (1:1 relationship).

        Per-step least-privilege. Previously every step Lambda shared an
        identical kitchen-sink policy with iam:CreateRole + lambda:CreateFunction
        + secretsmanager:* on `*` — meaning RCE in any step Lambda became full
        account compromise. Verified live 2026-05-16; tasks/lessons.md Bug 36.
        """
        role = iam.Role(
            self,
            f"Step{step_name.title().replace('_', '')}Role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
            ],
        )
        # ── Common: every step needs to update its DDB row + read SSM config ─
        self.deployments_table.grant_read_write_data(role)
        self.workflows_table.grant_read_data(role)
        # Phase 1 Gap 1A — every step's versioning hooks read the AgentVersions
        # table and status_update writes to both tables. Granting read to all
        # steps is acceptable: the data is owner-scoped via owner_sub anyway,
        # and these tables hold no secrets.
        self.agent_versions_table.grant_read_write_data(role)
        self.runtime_slots_table.grant_read_write_data(role)
        role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"],
            resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/agentcore-workflow/{self._env}/*"],
        ))
        role.add_to_policy(iam.PolicyStatement(actions=["sts:GetCallerIdentity"], resources=["*"]))
        role.add_to_policy(iam.PolicyStatement(actions=["cloudwatch:PutMetricData"], resources=["*"]))

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
            self.artifacts_bucket.grant_read_write(role)
        elif step_name in s3_readers:
            self.artifacts_bucket.grant_read(role)

        # iam_step: creates and tags the runtime's execution role.
        # mcp_server / gateway / knowledge_base / memory also create paired
        # IAM roles for their own dynamically-created Lambdas / AgentCore
        # resources. (memory creates AgentCoreMemory-* role for the memory
        # resource — see tasks/lessons.md Bug 45.)
        # evaluation creates AgentCoreEval-* role for the AgentCore evaluation
        # engine — same drift-across-paths shape as Bug 45/71/77; see
        # tasks/lessons.md Bug 118 (Phase 1 Gap 1C).
        if step_name in {"iam", "mcp_server", "gateway", "knowledge_base", "memory", "evaluation", "harness"}:
            role.add_to_policy(iam.PolicyStatement(
                actions=[
                    "iam:CreateRole", "iam:AttachRolePolicy", "iam:PutRolePolicy", "iam:GetRole",
                    "iam:PassRole", "iam:DeleteRole", "iam:DetachRolePolicy", "iam:DeleteRolePolicy",
                    "iam:ListAttachedRolePolicies", "iam:ListRolePolicies",
                    "iam:TagRole",
                ],
                resources=[f"arn:aws:iam::{self.account}:role/AgentCore*"],
            ))
            role.add_to_policy(iam.PolicyStatement(
                actions=["iam:CreateServiceLinkedRole"],
                resources=[f"arn:aws:iam::{self.account}:role/aws-service-role/*"],
            ))

        # runtime_configure / runtime_launch / mcp_server PASS the runtime's
        # IAM role to AgentCore via CreateAgentRuntime / CreateAgentRuntimeEndpoint.
        # iam:PassRole is required at the calling principal — see tasks/lessons.md
        # Bug 49. Resource includes the shared runtime role (Bug 60) and the
        # legacy per-deploy AgentCoreRuntime-* / AgentCoreMemory-* patterns
        # so cleanup of older deployments still works.
        if step_name in {"runtime_configure", "runtime_launch", "mcp_server", "evaluation"}:
            role.add_to_policy(iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[
                    f"arn:aws:iam::{self.account}:role/AgentCoreRuntime-{self._project}-{self._env}-shared",
                    f"arn:aws:iam::{self.account}:role/AgentCoreRuntime-*",
                    f"arn:aws:iam::{self.account}:role/AgentCoreEval-*",
                    f"arn:aws:iam::{self.account}:role/AgentCoreMemory-*",
                    f"arn:aws:iam::{self.account}:role/AgentCoreMCP-*",
                ],
                # Defence-in-depth: these roles may only be passed to AgentCore,
                # never to another service (matches the policy-step grant below).
                conditions={"StringEquals": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}},
            ))

        # policy step calls update_gateway(roleArn=...) when binding the
        # PolicyEngine — re-passing the gateway's existing role triggers
        # iam:PassRole on the calling principal. See tasks/lessons.md Bug 76.
        if step_name == "policy":
            role.add_to_policy(iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[f"arn:aws:iam::{self.account}:role/AgentCoreGateway-*"],
                conditions={"StringEquals": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}},
            ))

        # Phase B — the harness step PASSES the harness exec role to AgentCore
        # via CreateHarness (mirrors runtime_configure's CreateAgentRuntime
        # PassRole, Bug 49). Per-harness roles follow the AgentCoreHarness-*
        # convention (get_shared_or_new_harness_role); the optional shared
        # harness role, if added, also matches this prefix. Defence-in-depth:
        # the role may only ever be passed to AgentCore.
        if step_name == "harness":
            role.add_to_policy(iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[f"arn:aws:iam::{self.account}:role/AgentCoreHarness-*"],
                conditions={"StringEquals": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}},
            ))

        # gateway / mcp_server / codegen / knowledge_base create user Lambdas
        # (custom tools, MCP servers, KB transformer Lambdas).
        if step_name in {"gateway", "mcp_server", "codegen", "knowledge_base"}:
            role.add_to_policy(iam.PolicyStatement(
                actions=[
                    "lambda:CreateFunction", "lambda:UpdateFunctionCode",
                    "lambda:UpdateFunctionConfiguration",
                    "lambda:DeleteFunction", "lambda:GetFunction", "lambda:InvokeFunction",
                    "lambda:AddPermission", "lambda:RemovePermission",
                ],
                resources=[f"arn:aws:lambda:{self.region}:{self.account}:function:AgentCore*"],
            ))

        # gateway AND mcp_server both create a Cognito user pool — gateway
        # for OAuth2 client_credentials between caller and gateway,
        # mcp_server for the gateway-to-MCP-server-runtime auth bridge.
        # See tasks/lessons.md Bug 77.
        if step_name in {"gateway", "mcp_server"}:
            # CreateUserPool does not support resource-level permissions
            role.add_to_policy(iam.PolicyStatement(
                actions=["cognito-idp:CreateUserPool"],
                resources=["*"],
            ))
            role.add_to_policy(iam.PolicyStatement(
                actions=[
                    "cognito-idp:DeleteUserPool", "cognito-idp:CreateUserPoolClient",
                    "cognito-idp:DescribeUserPool", "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminSetUserPassword", "cognito-idp:AdminInitiateAuth",
                    "cognito-idp:CreateResourceServer", "cognito-idp:CreateUserPoolDomain",
                    "cognito-idp:DeleteUserPoolClient", "cognito-idp:DeleteUserPoolDomain",
                ],
                resources=[f"arn:aws:cognito-idp:{self.region}:{self.account}:userpool/*"],
            ))
            # gateway also stores the OAuth2 client_secret in Secrets Manager.
            # Phase A SaaS connectors: the gateway step also mints/reads/deletes
            # connector credential secrets under the agentcore-connector/ prefix
            # (raw API keys + OAuth2 client_secrets that back the credential
            # providers). DescribeSecret is used by the cleanup path to confirm
            # existence before delete. Secrets live ONLY here — never in canvas
            # JSON, DDB, or logs.
            role.add_to_policy(iam.PolicyStatement(
                actions=[
                    "secretsmanager:CreateSecret", "secretsmanager:DeleteSecret",
                    "secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue",
                    "secretsmanager:DescribeSecret", "secretsmanager:TagResource",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:AgentCore*",
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:agentcore-*",
                    # Phase A SaaS connector credential secrets.
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:agentcore-connector/*",
                    # CreateOauth2CredentialProvider writes its client_secret
                    # under the bedrock-agentcore-identity!default/oauth2/<n>
                    # Secrets Manager namespace, not the platform's prefix.
                    # See tasks/lessons.md Bug 83.
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:bedrock-agentcore-*",
                ],
            ))
            # ListSecrets does not support resource-level scoping (must be on
            # `*`). The connector deploy/cleanup paths enumerate connector
            # secrets by prefix to reconcile orphans. Kept as a separate minimal
            # statement so the wildcard is visible and isolated. The existing
            # per-role AwsSolutions-IAM5 suppression covers this wildcard.
            role.add_to_policy(iam.PolicyStatement(
                actions=["secretsmanager:ListSecrets"],
                resources=["*"],
            ))

        # Bug 150 — the harness step registers an OAuth2 credential provider for a
        # connected gateway; CreateOauth2CredentialProvider writes its client_secret
        # under the bedrock-agentcore-identity! Secrets Manager namespace, so the
        # harness step role needs to write/read/delete there (mirrors the gateway
        # step's secret perms, minus the Cognito + connector-secret scope it doesn't use).
        if step_name == "harness":
            role.add_to_policy(iam.PolicyStatement(
                actions=[
                    "secretsmanager:CreateSecret", "secretsmanager:DeleteSecret",
                    "secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue",
                    "secretsmanager:DescribeSecret", "secretsmanager:TagResource",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:bedrock-agentcore-*",
                ],
            ))

        # codegen reads bedrock:Converse to render system prompts that
        # describe a tool's purpose (used by the customer-support template).
        if step_name == "codegen":
            role.add_to_policy(iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=["*"],
            ))

        # knowledge_base creates KBs / data sources / ingestion jobs.
        if step_name == "knowledge_base":
            role.add_to_policy(iam.PolicyStatement(
                actions=[
                    "bedrock:CreateKnowledgeBase", "bedrock:GetKnowledgeBase",
                    "bedrock:ListKnowledgeBases", "bedrock:DeleteKnowledgeBase",
                    "bedrock:CreateDataSource", "bedrock:DeleteDataSource",
                    "bedrock:StartIngestionJob", "bedrock:GetIngestionJob",
                    "bedrock:ListFoundationModels",
                    "bedrock:Retrieve", "bedrock:RetrieveAndGenerate",
                ],
                resources=["*"],
            ))
            # KB step auto-creates the S3 Vectors index on user-supplied
            # buckets when missing (Bug 88). Needs list/create permissions
            # on the s3vectors service.
            role.add_to_policy(iam.PolicyStatement(
                actions=[
                    "s3vectors:ListIndexes",
                    "s3vectors:CreateIndex",
                    "s3vectors:GetIndex",
                    "s3vectors:DescribeIndex",
                    "s3vectors:CreateVectorBucket",
                    "s3vectors:DescribeVectorBucket",
                    "s3vectors:GetVectorBucket",
                    "s3vectors:ListVectorBuckets",
                ],
                resources=["*"],
            ))
            # OpenSearch Serverless: KB step auto-provisions a collection +
            # security/access policies + vector index when the caller supplies no
            # opensearchCollectionArn (Bedrock requires a pre-existing collection).
            role.add_to_policy(iam.PolicyStatement(
                actions=[
                    "aoss:CreateCollection", "aoss:BatchGetCollection", "aoss:DeleteCollection",
                    "aoss:CreateSecurityPolicy", "aoss:GetSecurityPolicy", "aoss:DeleteSecurityPolicy",
                    "aoss:CreateAccessPolicy", "aoss:GetAccessPolicy", "aoss:DeleteAccessPolicy",
                    "aoss:CreateIndex", "aoss:DescribeIndex", "aoss:DeleteIndex",
                    "aoss:APIAccessAll",
                ],
                resources=["*"],
            ))

        # guardrails creates Bedrock Guardrails.
        if step_name == "guardrails":
            role.add_to_policy(iam.PolicyStatement(
                actions=[
                    "bedrock:CreateGuardrail", "bedrock:GetGuardrail",
                    "bedrock:ListGuardrails", "bedrock:UpdateGuardrail",
                    "bedrock:DeleteGuardrail", "bedrock:CreateGuardrailVersion",
                ],
                resources=["*"],
            ))

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
                "logs:StartQuery", "logs:GetQueryResults",
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
            role.add_to_policy(iam.PolicyStatement(
                actions=agentcore_steps[step_name],
                resources=["*"],
            ))

        # Phase 1 Gap 1D — runtime_launch creates a CloudWatch dashboard
        # for the deployed runtime. cloudwatch:PutDashboard / GetDashboard
        # are global (no resource ARN format for dashboards in IAM).
        if step_name == "runtime_launch":
            role.add_to_policy(iam.PolicyStatement(
                actions=[
                    "cloudwatch:PutDashboard",
                    "cloudwatch:GetDashboard",
                    "cloudwatch:DeleteDashboards",
                ],
                resources=["*"],
            ))

        # Bug 196 — auto-cleanup on failure. When a deployment fails, the
        # status_update step iterates created_resources and deletes them to
        # prevent orphans (KB, Cognito pools, gateways, IAM roles, Lambdas,
        # vector buckets). Needs DELETE verbs across every resource type.
        if step_name == "status_update":
            role.add_to_policy(iam.PolicyStatement(
                actions=[
                    "bedrock:DeleteKnowledgeBase",
                    "bedrock:ListDataSources",
                    "bedrock:DeleteDataSource",
                    "bedrock:GetKnowledgeBase",
                ],
                resources=["*"],
            ))
            role.add_to_policy(iam.PolicyStatement(
                actions=[
                    "s3vectors:DeleteVectorBucket",
                    "s3vectors:GetVectorBucket",
                    "s3vectors:ListIndexes",
                    "s3vectors:DeleteIndex",
                ],
                resources=["*"],
            ))
            role.add_to_policy(iam.PolicyStatement(
                actions=[
                    "iam:DeleteRole",
                    "iam:GetRole",
                    "iam:ListAttachedRolePolicies",
                    "iam:DetachRolePolicy",
                    "iam:ListRolePolicies",
                    "iam:DeleteRolePolicy",
                ],
                resources=[f"arn:aws:iam::{self.account}:role/AgentCore*"],
            ))
            role.add_to_policy(iam.PolicyStatement(
                actions=[
                    "cognito-idp:DeleteUserPool",
                    "cognito-idp:DescribeUserPool",
                    "cognito-idp:DeleteUserPoolDomain",
                ],
                resources=[f"arn:aws:cognito-idp:{self.region}:{self.account}:userpool/*"],
            ))
            role.add_to_policy(iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:DeleteGateway",
                    "bedrock-agentcore:GetGateway",
                    "bedrock-agentcore:DeleteAgentRuntime",
                    "bedrock-agentcore:GetAgentRuntime",
                ],
                resources=["*"],
            ))
            role.add_to_policy(iam.PolicyStatement(
                actions=[
                    "lambda:DeleteFunction",
                    "lambda:GetFunction",
                ],
                resources=[f"arn:aws:lambda:{self.region}:{self.account}:function:AgentCore*"],
            ))

        return role

    def _create_step_lambdas(self) -> dict[str, _lambda.Function]:
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
            step_role = self._create_step_role(step_name)
            fn = _lambda.Function(
                self,
                f"Step{step_name.title().replace('_', '')}Lambda",
                function_name=f"{self._project}-{self._env}-step-{step_name.replace('_', '-')}",
                runtime=_lambda.Runtime.PYTHON_3_12,
                handler=config["handler"],
                code=self.backend_code,
                memory_size=config["memory"],
                timeout=Duration.seconds(config["timeout"]),
                role=step_role,
                tracing=_lambda.Tracing.ACTIVE,
                environment={
                    "DEPLOYMENTS_TABLE_NAME": self.deployments_table.table_name,
                    "DEPLOYMENT_TABLE_NAME": self.deployments_table.table_name,
                    "WORKFLOWS_TABLE_NAME": self.workflows_table.table_name,
                    # Phase 1 Gap 1A — versioning tables; status_update_step
                    # writes the AgentVersion + RuntimeSlots rows on success.
                    "AGENT_VERSIONS_TABLE_NAME": self.agent_versions_table.table_name,
                    "RUNTIME_SLOTS_TABLE_NAME": self.runtime_slots_table.table_name,
                    # Phase 2 Gap 2D — HITL table name so runtime_configure_step
                    # injects it into the runtime's environmentVariables.
                    "HITL_REQUESTS_TABLE_NAME": self.hitl_requests_table.table_name,
                    "ARTIFACTS_BUCKET_NAME": self.artifacts_bucket.bucket_name,
                    "ENVIRONMENT": self._env,
                    "APP_AWS_REGION": self.region,
                    "PYTHONPATH": "/var/task/src:/var/task:/var/task/lib",
                    # Shared runtime execution role — pre-created at stack
                    # init to avoid the per-deploy IAM-propagation race.
                    "SHARED_RUNTIME_ROLE_ARN": self.shared_runtime_role.role_arn,
                },
                log_group=logs.LogGroup(
                    self,
                    f"Step{step_name.title().replace('_', '')}LogGroup",
                    log_group_name=f"/aws/lambda/{self._project}-{self._env}-step-{step_name.replace('_', '-')}",
                    retention=logs.RetentionDays.ONE_MONTH,
                    removal_policy=RemovalPolicy.DESTROY,
                ),
            )
            self._apply_platform_otel(fn, f"step-{step_name.replace('_', '-')}")
            lambdas[step_name] = fn

        return lambdas

    # ------------------------------------------------------------------
    # Step Functions State Machine
    # ------------------------------------------------------------------

    def _create_state_machine(self) -> sfn.StateMachine:
        """Create Step Functions state machine for deployment orchestration.

        Retry: 3 attempts with exponential backoff (2s, 4s, 8s)
        Catch: fallback to failure handler writing error to DynamoDB
        Per-step timeouts per design table
        Overall timeout: 30 minutes

        Requirements: 1.3, 1.4, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 7.1
        """
        # Failure handler — writes error to DynamoDB
        failure_handler = self._create_step_task(
            "StatusUpdateFailure",
            self.step_lambdas["status_update"],
            timeout_seconds=15,
            result_path="$.failure_result",
        )
        failure_handler.add_retry(**self._retry_kwargs())
        fail_state = sfn.Fail(self, "DeploymentFailed", cause="Deployment failed", error="DeploymentError")
        failure_handler.next(fail_state)

        # --- Define steps ---
        # Each step handler returns {**event, ...new_fields} so we use result_path="$"
        # to replace the entire state, allowing fields to accumulate across steps.
        validate = self._create_step_task(
            "ValidateWorkflow",
            self.step_lambdas["validate"],
            timeout_seconds=30,
            result_path="$",
        )
        validate.add_retry(**self._retry_kwargs())
        validate.add_catch(**self._catch_kwargs(failure_handler))

        guardrails = self._create_step_task(
            "CreateGuardrails",
            self.step_lambdas["guardrails"],
            timeout_seconds=120,
            result_path="$",
        )
        guardrails.add_retry(**self._retry_kwargs())
        guardrails.add_catch(**self._catch_kwargs(failure_handler))

        mcp_server = self._create_step_task(
            "DeployMCPServer",
            self.step_lambdas["mcp_server"],
            timeout_seconds=600,
            result_path="$",
        )
        mcp_server.add_retry(**self._retry_kwargs())
        mcp_server.add_catch(**self._catch_kwargs(failure_handler))

        codegen = self._create_step_task(
            "GenerateCode",
            self.step_lambdas["codegen"],
            timeout_seconds=90,
            result_path="$",
        )
        codegen.add_retry(**self._retry_kwargs())
        codegen.add_catch(**self._catch_kwargs(failure_handler))

        iam_step = self._create_step_task(
            "CreateIAMRole",
            self.step_lambdas["iam"],
            # 90s budget: create_runtime_iam_role does put_role_policy + 15s
            # IAM-propagation sleep + per-tool inline policy attachments. 60s
            # was tight on cold starts.
            timeout_seconds=90,
            result_path="$",
        )
        iam_step.add_retry(**self._retry_kwargs())
        iam_step.add_catch(**self._catch_kwargs(failure_handler))

        gateway = self._create_step_task(
            "DeployGateway",
            self.step_lambdas["gateway"],
            # Bug 134: the gateway step now also resolves + waits for the target
            # MCP tool manifest (up to ~90s) so the policy step gets authoritative
            # tool action names. Raise the cap in lockstep with the Lambda timeout
            # (Bug 56) so a slow manifest sync surfaces as a real failure, not a
            # retryable States.Timeout that could mask a broken policy.
            timeout_seconds=720,
            result_path="$",
        )
        gateway.add_retry(**self._retry_kwargs())
        gateway.add_catch(**self._catch_kwargs(failure_handler))

        knowledge_base = self._create_step_task(
            "CreateKnowledgeBase",
            self.step_lambdas["knowledge_base"],
            timeout_seconds=600,
            result_path="$",
        )
        knowledge_base.add_retry(**self._retry_kwargs())
        knowledge_base.add_catch(**self._catch_kwargs(failure_handler))

        memory_step = self._create_step_task(
            "CreateMemory",
            self.step_lambdas["memory"],
            timeout_seconds=120,
            result_path="$",
        )
        memory_step.add_retry(**self._retry_kwargs())
        memory_step.add_catch(**self._catch_kwargs(failure_handler))

        policy_step = self._create_step_task(
            "CreatePolicy",
            self.step_lambdas["policy"],
            # Bug 177 + Cedar IGNORE_ALL_FINDINGS convergence: matched to the 600s
            # Lambda budget — the engine CREATING->ACTIVE + up to 12 policy-create
            # retries (as the engine<->gateway authorization converges) can take
            # several minutes on a freshly-created gateway.
            timeout_seconds=600,
            result_path="$",
        )
        policy_step.add_retry(**self._retry_kwargs())
        policy_step.add_catch(**self._catch_kwargs(failure_handler))

        runtime_configure = self._create_step_task(
            "ConfigureRuntime",
            self.step_lambdas["runtime_configure"],
            # Match the underlying Lambda timeout (240s — bumped for Bug 54).
            # The IAM-propagation retry loop inside `create_agent_runtime` can
            # legitimately spend up to 75s waiting for AgentCore's IAM cache.
            # See tasks/lessons.md Bug 56 — SFN task TimeoutSeconds is the
            # outer cap and must match the Lambda's.
            timeout_seconds=240,
            result_path="$",
        )
        runtime_configure.add_retry(**self._retry_kwargs())
        runtime_configure.add_catch(**self._catch_kwargs(failure_handler))

        runtime_launch = self._create_step_task(
            "LaunchRuntime",
            self.step_lambdas["runtime_launch"],
            timeout_seconds=600,
            result_path="$",
        )
        runtime_launch.add_retry(**self._retry_kwargs())
        runtime_launch.add_catch(**self._catch_kwargs(failure_handler))

        # Phase B — AgentCore Harness deploy task (parallel to the codegen →
        # iam → configure → launch Runtime path). Matches the SFN task timeout
        # to the Lambda budget (300s). Shares the same retry/catch wrappers.
        harness_step = self._create_step_task(
            "DeployHarness",
            self.step_lambdas["harness"],
            timeout_seconds=300,
            result_path="$",
        )
        harness_step.add_retry(**self._retry_kwargs())
        harness_step.add_catch(**self._catch_kwargs(failure_handler))

        evaluation_step = self._create_step_task(
            "CreateEvaluation",
            self.step_lambdas["evaluation"],
            timeout_seconds=120,
            result_path="$",
        )
        evaluation_step.add_retry(**self._retry_kwargs())
        evaluation_step.add_catch(**self._catch_kwargs(failure_handler))

        auth = self._create_step_task(
            "ConfigureJWTAuth",
            self.step_lambdas["auth"],
            timeout_seconds=60,
            result_path="$",
        )
        auth.add_retry(**self._retry_kwargs())
        auth.add_catch(**self._catch_kwargs(failure_handler))

        status_update = self._create_step_task(
            "UpdateStatusSuccess",
            self.step_lambdas["status_update"],
            timeout_seconds=15,
            result_path="$",
        )
        status_update.add_retry(**self._retry_kwargs())
        status_update.add_catch(**self._catch_kwargs(failure_handler))

        succeed = sfn.Succeed(self, "DeploymentSucceeded")

        # --- Build chain with conditionals ---
        # Flow: validate → [mcp_server?] → [knowledge_base?] → [gateway?] → [memory?] → [policy?]
        #       → codegen → iam → configure → launch → [evaluation?] → [auth?] → status
        #
        # KB runs BEFORE gateway because deploy_gateway() reads knowledge_base_result
        # from the event to create the KB Lambda target.
        #
        # Each optional step uses a Pass state as a skip target so that
        # each Lambda task's .next() is called exactly once (CDK requirement).
        has_guardrails = sfn.Condition.is_present("$.guardrails_config")
        has_mcp_server = sfn.Condition.is_present("$.mcp_server_config")
        has_gateway = sfn.Condition.is_present("$.gateway_config")
        has_knowledge_base = sfn.Condition.is_present("$.knowledge_base_config")
        has_memory = sfn.Condition.is_present("$.memory_config")
        has_policy = sfn.Condition.is_present("$.policy_config")
        has_evaluation = sfn.Condition.is_present("$.evaluation_config")

        skip_guardrails = sfn.Pass(self, "SkipGuardrails")
        skip_mcp_server = sfn.Pass(self, "SkipMCPServer")
        skip_knowledge_base = sfn.Pass(self, "SkipKnowledgeBase")
        skip_gateway = sfn.Pass(self, "SkipGateway")
        skip_memory = sfn.Pass(self, "SkipMemory")
        skip_policy = sfn.Pass(self, "SkipPolicy")
        skip_evaluation = sfn.Pass(self, "SkipEvaluation")
        skip_auth = sfn.Pass(self, "SkipAuth")

        # validate → guardrails choice
        validate.next(sfn.Choice(self, "HasGuardrails?").when(has_guardrails, guardrails).otherwise(skip_guardrails))
        guardrails.next(skip_guardrails)

        # → mcp_server choice
        skip_guardrails.next(sfn.Choice(self, "HasMCPServer?").when(has_mcp_server, mcp_server).otherwise(skip_mcp_server))
        mcp_server.next(skip_mcp_server)  # converge after mcp_server

        # → knowledge base choice (runs before gateway so result is available)
        skip_mcp_server.next(sfn.Choice(self, "HasKnowledgeBase?").when(has_knowledge_base, knowledge_base).otherwise(skip_knowledge_base))
        knowledge_base.next(skip_knowledge_base)

        # → gateway choice (reads knowledge_base_result to create KB Lambda target)
        skip_knowledge_base.next(sfn.Choice(self, "HasGateway?").when(has_gateway, gateway).otherwise(skip_gateway))
        gateway.next(skip_gateway)  # converge after gateway

        # → memory choice
        skip_gateway.next(sfn.Choice(self, "HasMemory?").when(has_memory, memory_step).otherwise(skip_memory))
        memory_step.next(skip_memory)

        # → policy choice (only meaningful when gateway exists, but handler handles gracefully)
        skip_memory.next(sfn.Choice(self, "HasPolicy?").when(has_policy, policy_step).otherwise(skip_policy))
        policy_step.next(skip_policy)

        # → harness vs. runtime deploy-mode choice.
        # Phase B: deployment_mode=="harness" diverts to the AgentCore Harness
        # task (no codegen / no per-runtime IAM / no runtime configure+launch),
        # then rejoins the shared tail at the evaluation choice — so the SAME
        # status_update (and optional auth) steps still run, keeping connectors+
        # memory parity in BOTH modes. The default (Visual Canvas) Runtime path
        # is unchanged: absent/any-other deployment_mode falls through to codegen.
        # Both branches converge on `post_deploy_choice` so each task's .next()
        # is wired exactly once (CDK requirement).
        post_deploy_choice = sfn.Choice(self, "HasEvaluation?").when(
            has_evaluation, evaluation_step
        ).otherwise(skip_evaluation)

        is_harness_mode = sfn.Condition.string_equals("$.deployment_mode", "harness")
        skip_policy.next(
            sfn.Choice(self, "IsHarnessMode?")
            .when(is_harness_mode, harness_step)
            .otherwise(codegen)
        )

        # Default Runtime path (UNCHANGED): codegen → iam → configure → launch
        codegen.next(iam_step)
        iam_step.next(runtime_configure)
        runtime_configure.next(runtime_launch)
        runtime_launch.next(post_deploy_choice)

        # Harness path rejoins the shared tail at the evaluation choice, exactly
        # where runtime_launch would continue — so status_update still runs.
        harness_step.next(post_deploy_choice)

        # → evaluation choice (shared tail)
        evaluation_step.next(skip_evaluation)

        # → auth choice (only when gateway was deployed)
        skip_evaluation.next(sfn.Choice(self, "HasGatewayForAuth?").when(has_gateway, auth).otherwise(skip_auth))
        auth.next(skip_auth)

        # → status update → succeed
        skip_auth.next(status_update)
        status_update.next(succeed)

        # State machine role
        sm_role = iam.Role(
            self,
            "StateMachineRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
        )
        # Grant invoke on all step lambdas
        for fn in self.step_lambdas.values():
            fn.grant_invoke(sm_role)
        # DynamoDB access for deployment state
        self.deployments_table.grant_read_write_data(sm_role)
        # Phase 1 Gap 1A — state machine writes versions/slots via status_update.
        self.agent_versions_table.grant_read_write_data(sm_role)
        self.runtime_slots_table.grant_read_write_data(sm_role)

        return sfn.StateMachine(
            self,
            "DeploymentStateMachine",
            state_machine_name=f"{self._project}-{self._env}-deployment",
            definition_body=sfn.DefinitionBody.from_chainable(validate),
            role=sm_role,
            timeout=Duration.minutes(30),
            tracing_enabled=True,
            logs=sfn.LogOptions(
                destination=logs.LogGroup(
                    self,
                    "StateMachineLogGroup",
                    log_group_name=f"/stepfunctions/{self._project}-{self._env}/deployment",
                    retention=logs.RetentionDays.ONE_MONTH,
                    removal_policy=RemovalPolicy.DESTROY,
                ),
                level=sfn.LogLevel.ERROR,
            ),
        )

    def _create_step_task(
        self,
        id: str,
        fn: _lambda.Function,
        *,
        timeout_seconds: int,
        result_path: str,
    ) -> sfn_tasks.LambdaInvoke:
        """Create a Step Functions LambdaInvoke task with payload passthrough."""
        return sfn_tasks.LambdaInvoke(
            self,
            id,
            lambda_function=fn,
            payload_response_only=True,
            result_path=result_path,
            task_timeout=sfn.Timeout.duration(Duration.seconds(timeout_seconds)),
        )

    @staticmethod
    def _retry_kwargs() -> dict:
        """Return retry configuration kwargs for add_retry().

        Bug 134 (root cause): previously this retried ``States.TaskFailed`` —
        a WILDCARD that matches ANY application error (incl. a deterministic
        Cedar-validation RuntimeError from the policy step). When a step raised
        on attempt 1 but a later attempt happened to succeed (e.g. the gateway
        tool manifest finished syncing between attempts), Step Functions took the
        SUCCESS path and the Catch (which only fires after retries are exhausted)
        never ran — so a broken Cedar policy shipped as "succeeded". We now retry
        ONLY genuinely-transient infra errors (Lambda service/throttle/timeout),
        NOT the catch-all TaskFailed. A deterministic handler error now goes
        straight to Catch(States.ALL) -> StatusUpdateFailure -> DeploymentFailed.
        """
        return {
            "errors": [
                "States.Timeout",
                "Lambda.ServiceException",
                "Lambda.AWSLambdaException",
                "Lambda.SdkClientException",
                "Lambda.ClientExecutionTimeoutException",
                "Lambda.TooManyRequestsException",
            ],
            "interval": Duration.seconds(2),
            "max_attempts": 3,
            "backoff_rate": 2.0,
        }

    @staticmethod
    def _catch_kwargs(handler: sfn_tasks.LambdaInvoke) -> dict:
        """Return catch configuration kwargs for add_catch()."""
        return {
            "handler": handler,
            "result_path": "$.error_info",
        }

    # ------------------------------------------------------------------
    # Cognito Authentication
    # ------------------------------------------------------------------

    def _create_cognito(self) -> tuple:
        """Create Cognito User Pool, client, and pre-set users."""
        pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{self._project}-{self._env}-users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            standard_threat_protection_mode=cognito.StandardThreatProtectionMode.FULL_FUNCTION,
            user_invitation=cognito.UserInvitationConfig(
                email_subject="Your AgentCore Workflow credentials",
                email_body=(
                    "<p>Your AgentCore Workflow account is ready.</p>"
                    "<p>Username:<br><code>{username}</code></p>"
                    "<p>Temporary password (copy exactly, no surrounding whitespace):<br>"
                    "<code>{####}</code></p>"
                    "<p>You will be prompted to set a new password on first sign-in.</p>"
                ),
            ),
            removal_policy=self._removal_policy,
        )

        client = pool.add_client(
            "FrontendClient",
            user_pool_client_name=f"{self._project}-{self._env}-frontend",
            generate_secret=False,
            # Drop USER_PASSWORD_AUTH (sends plaintext password) — keep SRP only.
            # See tasks/lessons.md Bug 38 (Cognito hardening from security audit).
            auth_flows=cognito.AuthFlow(
                user_password=False,
                user_srp=True,
            ),
            # Suppress username enumeration (response is the same regardless of
            # whether the user exists or the password is wrong).
            prevent_user_existence_errors=True,
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(7),
        )

        # Two-persona approval workflow groups for the agent registry.
        # 'registry-admin' members can approve/reject submissions and manage any
        # entry; 'registry-developer' members publish (pending) and manage their
        # own. Persona is resolved backend-side from cognito:groups (see
        # services/auth.is_registry_admin). Higher precedence = stronger role,
        # so registry-admin (0) outranks registry-developer (10).
        cognito.CfnUserPoolGroup(
            self,
            "RegistryAdminGroup",
            user_pool_id=pool.user_pool_id,
            group_name="registry-admin",
            description="Registry approvers",
            precedence=0,
        )
        cognito.CfnUserPoolGroup(
            self,
            "RegistryDeveloperGroup",
            user_pool_id=pool.user_pool_id,
            group_name="registry-developer",
            description="Registry publishers",
            precedence=10,
        )

        # Pre-create users from context (comma-separated string via env var).
        #
        # A Lambda-backed Custom Resource generates a temporary password from
        # an HTML-safe charset (no <, >, &, ', ", ., ,) and passes it to
        # AdminCreateUser. Cognito emails the invitation containing that
        # exact password, so what the user sees in their inbox matches what
        # Cognito stored. The user still lands in FORCE_CHANGE_PASSWORD and
        # sets a real password on first sign-in.
        #
        # This replaces AWS::Cognito::UserPoolUser, which does not expose
        # TemporaryPassword and so leaves Cognito to auto-generate one —
        # those generated passwords can contain HTML-special chars that get
        # silently stripped by email renderers, producing a displayed
        # password that does not match the stored verifier.
        cognito_users_raw = self.node.try_get_context("cognito_users") or ""
        cognito_users = [e.strip() for e in cognito_users_raw.split(",") if e.strip()] if isinstance(cognito_users_raw, str) else cognito_users_raw

        if cognito_users:
            provisioner_fn = _lambda.Function(
                self,
                "CognitoUserProvisionerFn",
                runtime=_lambda.Runtime.PYTHON_3_12,
                handler="handler.handler",
                code=_lambda.Code.from_asset("stacks/cognito_user_provisioner"),
                timeout=Duration.seconds(60),
                memory_size=256,
                log_retention=logs.RetentionDays.ONE_MONTH,
                description="Provisions Cognito users with an HTML-safe generated temporary password",
            )
            provisioner_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=[
                        "cognito-idp:AdminCreateUser",
                        "cognito-idp:AdminSetUserPassword",
                        "cognito-idp:AdminDeleteUser",
                    ],
                    resources=[pool.user_pool_arn],
                )
            )

            provider = cr.Provider(
                self,
                "CognitoUserProvisionerProvider",
                on_event_handler=provisioner_fn,
                log_retention=logs.RetentionDays.ONE_MONTH,
            )

            for email in cognito_users:
                sanitized = email.replace("@", "-at-").replace(".", "-")
                user_cr = cdk.CustomResource(
                    self,
                    f"User-{sanitized}",
                    service_token=provider.service_token,
                    properties={
                        "UserPoolId": pool.user_pool_id,
                        "Email": email,
                    },
                )
                user_cr.node.add_dependency(pool)

        return pool, client

    # ------------------------------------------------------------------
    # API Gateway HTTP API
    # ------------------------------------------------------------------

    def _create_api_gateway(self) -> apigwv2.HttpApi:
        """Create API Gateway HTTP API with route mappings and CORS.

        Routes:
        - /api/workflows/* → Workflow Lambda
        - /api/deploy, /api/test-runtime, /api/runtime/* → Deployment Lambda
        - /health → Workflow Lambda

        Requirements: 1.1, 1.2, 1.3, 1.4, 1.5
        """
        # CORS origins: localhost for local development. CloudFront distribution
        # URL is added post-construction by _add_cloudfront_cors_origin() since
        # the distribution is created after the API. Browsers send CORS preflight
        # for requests with Authorization headers even on same-origin via CloudFront.
        api = apigwv2.HttpApi(
            self,
            "HttpApi",
            api_name=f"{self._project}-{self._env}-api",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["http://localhost:5173"],
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.PUT,
                    apigwv2.CorsHttpMethod.DELETE,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=[
                    "Content-Type",
                    "Authorization",
                    "X-Amz-Date",
                    "X-Api-Key",
                ],
                max_age=Duration.minutes(5),
            ),
        )

        # Workflow Lambda integration
        workflow_integration = apigw_integrations.HttpLambdaIntegration("WorkflowIntegration", self.workflow_lambda)

        # Deployment Lambda integration
        deployment_integration = apigw_integrations.HttpLambdaIntegration(
            "DeploymentIntegration", self.deployment_lambda
        )

        # JWT Authorizer (Cognito)
        jwt_authorizer = apigw_authorizers.HttpJwtAuthorizer(
            "CognitoAuthorizer",
            jwt_issuer=f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool.user_pool_id}",
            jwt_audience=[self.user_pool_client.user_pool_client_id],
        )

        # --- Workflow routes ---
        api.add_routes(
            path="/api/workflows",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=workflow_integration,
            authorizer=jwt_authorizer,
        )
        api.add_routes(
            path="/api/workflows/{proxy+}",
            methods=[
                apigwv2.HttpMethod.GET,
                apigwv2.HttpMethod.PUT,
                apigwv2.HttpMethod.DELETE,
                apigwv2.HttpMethod.POST,
            ],
            integration=workflow_integration,
            authorizer=jwt_authorizer,
        )

        # --- Flow routes ---
        api.add_routes(
            path="/api/flows",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=workflow_integration,
            authorizer=jwt_authorizer,
        )
        api.add_routes(
            path="/api/flows/{proxy+}",
            methods=[
                apigwv2.HttpMethod.GET,
                apigwv2.HttpMethod.PUT,
                apigwv2.HttpMethod.DELETE,
            ],
            integration=workflow_integration,
            authorizer=jwt_authorizer,
        )

        # --- Deployment routes ---
        api.add_routes(
            path="/api/deploy",
            methods=[apigwv2.HttpMethod.POST],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        api.add_routes(
            path="/api/deploy/{proxy+}",
            methods=[apigwv2.HttpMethod.GET],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        api.add_routes(
            path="/api/deployments",
            methods=[apigwv2.HttpMethod.GET],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        api.add_routes(
            path="/api/test-runtime",
            methods=[apigwv2.HttpMethod.POST],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        api.add_routes(
            path="/api/test-runtime-stream",
            methods=[apigwv2.HttpMethod.POST],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        api.add_routes(
            path="/api/runtime/{proxy+}",
            methods=[apigwv2.HttpMethod.DELETE, apigwv2.HttpMethod.GET],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        # Phase 1 Gap 1A — versions + slot management endpoints. Routed to
        # the deployment Lambda which mounts routers/versions.py. The proxy
        # path covers GET /versions, GET /slots, POST /versions/.../promote,
        # POST /rollback. Per Bug 21, every new router needs an explicit API
        # GW route enumeration here; the IAM grants for the new tables are
        # added on the deployment Lambda role + state machine role above.
        api.add_routes(
            path="/api/runtimes/{proxy+}",
            # Phase 3 Gap 3F adds DELETE for DELETE /api/runtimes/{name}/triggers/{id}.
            methods=[
                apigwv2.HttpMethod.GET,
                apigwv2.HttpMethod.POST,
                apigwv2.HttpMethod.DELETE,
            ],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        api.add_routes(
            path="/api/generate-tool",
            methods=[apigwv2.HttpMethod.POST],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        api.add_routes(
            path="/api/generate-tool/{jobId}",
            methods=[apigwv2.HttpMethod.GET],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        api.add_routes(
            path="/api/test-tool",
            methods=[apigwv2.HttpMethod.POST],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        api.add_routes(
            path="/api/test-tool/{testId}",
            methods=[apigwv2.HttpMethod.GET],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        api.add_routes(
            path="/api/generate-cfn-template",
            methods=[apigwv2.HttpMethod.POST],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        # Phase 3 Gap 3G — eject standalone Python project. Same deployment
        # Lambda + artifacts bucket grant as the CFN export; only a new route.
        api.add_routes(
            path="/api/export-python",
            methods=[apigwv2.HttpMethod.POST],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        # Phase 1 Gap 1E — NL agent (canvas) generator. Same Bedrock
        # InvokeModel grant as the existing tool generator (already on
        # the deployment Lambda role); only a new API GW route is needed.
        api.add_routes(
            path="/api/generate-canvas",
            methods=[apigwv2.HttpMethod.POST],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        # Phase 2 Gap 2A — agent registry. POST publish + GET search on the
        # collection, plus GET/PUT/DELETE/clone on /{slug} via proxy.
        api.add_routes(
            path="/api/registry",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        api.add_routes(
            path="/api/registry/{proxy+}",
            methods=[
                apigwv2.HttpMethod.GET,
                apigwv2.HttpMethod.POST,
                apigwv2.HttpMethod.PUT,
                apigwv2.HttpMethod.DELETE,
            ],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        # Phase 2 Gap 2D — HITL approval queue. GET /api/hitl/pending +
        # POST /api/hitl/{request_id}/decision via the proxy.
        api.add_routes(
            path="/api/hitl",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        api.add_routes(
            path="/api/hitl/{proxy+}",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        # Phase 3 Gap 3E — pre-built connector catalog (read-only). GET list +
        # GET /{id} detail on the deployment Lambda (mounts routers/connectors).
        api.add_routes(
            path="/api/connectors",
            methods=[apigwv2.HttpMethod.GET],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        api.add_routes(
            path="/api/connectors/{proxy+}",
            methods=[apigwv2.HttpMethod.GET],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        # Phase 3 Gap 3H — prompt management library. POST save + GET list on
        # the collection, plus GET/PUT/DELETE on /{prompt_name} via proxy.
        # Mounted on the deployment Lambda (routers/prompts.py).
        api.add_routes(
            path="/api/prompts",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        api.add_routes(
            path="/api/prompts/{proxy+}",
            methods=[
                apigwv2.HttpMethod.GET,
                apigwv2.HttpMethod.POST,
                apigwv2.HttpMethod.PUT,
                apigwv2.HttpMethod.DELETE,
            ],
            integration=deployment_integration,
            authorizer=jwt_authorizer,
        )
        # Phase 2 Gap 2E — workspace sharing. GET /api/workspaces routes to the
        # WORKFLOW Lambda (main.py mounts workspaces_router; it reads workflow
        # storage). The share endpoints /api/workflows/{id}/share already match
        # the existing /api/workflows/{proxy+} route → workflow_integration.
        api.add_routes(
            path="/api/workspaces",
            # Bug 139: workspaces_router only declares GET /workspaces — POST was
            # dead API surface. Match the route to the router (Bug 21 enumeration).
            methods=[apigwv2.HttpMethod.GET],
            integration=workflow_integration,
            authorizer=jwt_authorizer,
        )

        # --- Observability credential storage route ---
        # Routes to workflow_lambda because main.py (workflow Lambda) is the only
        # FastAPI app that mounts the observability_router (deployment_handler is
        # a separate FastAPI app without it).
        api.add_routes(
            path="/api/observability/credentials",
            methods=[apigwv2.HttpMethod.POST],
            integration=workflow_integration,
            authorizer=jwt_authorizer,
        )
        # --- Platform-defaults read route (UI uses this to show platform-managed
        # OTEL settings as read-only).
        api.add_routes(
            path="/api/observability/platform-defaults",
            methods=[apigwv2.HttpMethod.GET],
            integration=workflow_integration,
            authorizer=jwt_authorizer,
        )

        # --- Health check route ---
        api.add_routes(
            path="/health",
            methods=[apigwv2.HttpMethod.GET],
            integration=workflow_integration,
        )

        # Add throttling to the default stage to prevent abuse
        default_stage = api.default_stage
        if default_stage:
            cfn_stage = default_stage.node.default_child
            if cfn_stage:
                cfn_stage.add_property_override("DefaultRouteSettings.ThrottlingBurstLimit", 50)
                cfn_stage.add_property_override("DefaultRouteSettings.ThrottlingRateLimit", 100)

        # Store state machine ARN in deployment lambda env
        self.deployment_lambda.add_environment("STATE_MACHINE_ARN", self.state_machine.state_machine_arn)

        return api

    def _add_cloudfront_cors_origin(self) -> None:
        """Widen API Gateway CORS to allow CloudFront origin.

        Cannot reference distribution.domain_name here — CloudFront depends on
        the API URL, so a back-reference creates a circular dependency.
        Token-based auth (Cognito JWT) means allow_origins=["*"] is safe:
        no ambient credentials (cookies) are sent cross-origin.
        """
        cfn_api = self.api.node.default_child
        if cfn_api:
            cfn_api.add_property_override(
                "CorsConfiguration.AllowOrigins",
                ["*"],
            )

    # ------------------------------------------------------------------
    # S3 + CloudFront
    # ------------------------------------------------------------------

    def _create_logging_bucket(self) -> s3.Bucket:
        """Create S3 bucket for access logs (S3 + CloudFront)."""
        return s3.Bucket(
            self,
            "LoggingBucket",
            bucket_name=f"{self._project}-{self._env}-logs-{self.region}-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=self._removal_policy,
            auto_delete_objects=self._allow_destroy,
            encryption=s3.BucketEncryption.S3_MANAGED,
            object_ownership=s3.ObjectOwnership.OBJECT_WRITER,
            lifecycle_rules=[
                s3.LifecycleRule(expiration=Duration.days(90)),
            ],
        )

    def _create_s3_bucket(self) -> s3.Bucket:
        """Create S3 bucket for frontend static assets.

        Requirements: 7.1
        """
        return s3.Bucket(
            self,
            "FrontendBucket",
            bucket_name=f"{self._project}-{self._env}-frontend-{self.region}-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=self._removal_policy,
            auto_delete_objects=self._allow_destroy,
            encryption=s3.BucketEncryption.S3_MANAGED,
            server_access_logs_bucket=self.logging_bucket,
            server_access_logs_prefix="s3-frontend/",
            lifecycle_rules=[
                s3.LifecycleRule(
                    noncurrent_version_expiration=Duration.days(30),
                ),
            ],
        )

    def _build_waf_rules(self, name_prefix: str) -> list:
        """Common WAF rule set used by both the CloudFront ACL and the
        regional API Gateway ACL. Includes Common + KnownBadInputs managed
        rule sets plus an IP-based rate limit. See tasks/lessons.md Bug 41.
        """
        return [
            wafv2.CfnWebACL.RuleProperty(
                name="AWSManagedRulesCommonRuleSet",
                priority=1,
                override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                statement=wafv2.CfnWebACL.StatementProperty(
                    managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                        vendor_name="AWS",
                        name="AWSManagedRulesCommonRuleSet",
                    ),
                ),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    cloud_watch_metrics_enabled=True,
                    metric_name=f"{name_prefix}-common-rules",
                    sampled_requests_enabled=True,
                ),
            ),
            wafv2.CfnWebACL.RuleProperty(
                name="AWSManagedRulesKnownBadInputsRuleSet",
                priority=2,
                override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                statement=wafv2.CfnWebACL.StatementProperty(
                    managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                        vendor_name="AWS",
                        name="AWSManagedRulesKnownBadInputsRuleSet",
                    ),
                ),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    cloud_watch_metrics_enabled=True,
                    metric_name=f"{name_prefix}-known-bad-inputs",
                    sampled_requests_enabled=True,
                ),
            ),
            wafv2.CfnWebACL.RuleProperty(
                name="RateLimitRule",
                priority=3,
                action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                statement=wafv2.CfnWebACL.StatementProperty(
                    rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                        limit=2000,
                        aggregate_key_type="IP",
                    ),
                ),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    cloud_watch_metrics_enabled=True,
                    metric_name=f"{name_prefix}-rate-limit",
                    sampled_requests_enabled=True,
                ),
            ),
        ]

    def _create_waf_web_acl(self) -> wafv2.CfnWebACL:
        """Create WAFv2 WebACL for CloudFront."""
        return wafv2.CfnWebACL(
            self,
            "CloudFrontWebACL",
            name=f"{self._project}-{self._env}-cloudfront-waf",
            scope="CLOUDFRONT",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=f"{self._project}-{self._env}-waf",
                sampled_requests_enabled=True,
            ),
            rules=self._build_waf_rules(f"{self._project}-{self._env}"),
        )

    # Removed: _create_api_waf_and_attach. WAFv2 does not support API Gateway
    # HTTP APIs (only REST APIs); the resource type RESOURCE_ARN was rejected.
    # See tasks/lessons.md Bug 41 (revised).

    def _create_cloudfront_distribution(self) -> cloudfront.Distribution:
        """Create CloudFront distribution with S3 + API Gateway origins.

        - /* → S3 (frontend)
        - /api/* → API Gateway
        - /health → API Gateway

        Requirements: 7.2, 7.3
        """
        # S3 origin for frontend (OAC — recommended over legacy OAI)
        s3_origin = origins.S3BucketOrigin.with_origin_access_control(
            self.bucket,
        )

        # API Gateway origin — extract domain from the API URL
        # API URL format: https://{api-id}.execute-api.{region}.amazonaws.com/
        api_domain = cdk.Fn.select(2, cdk.Fn.split("/", self.api.url or ""))
        api_origin = origins.HttpOrigin(
            domain_name=api_domain,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
        )

        # Security response headers (HSTS, X-Frame-Options, X-Content-Type-Options, etc.)
        # CSP added 2026-05-16 — see tasks/lessons.md Bug 39 (security audit).
        security_headers = cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeadersPolicy",
            response_headers_policy_name=f"{self._project}-{self._env}-security-headers",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True,
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.seconds(63072000),
                    include_subdomains=True,
                    preload=True,
                    override=True,
                ),
                xss_protection=cloudfront.ResponseHeadersXSSProtection(
                    protection=True,
                    mode_block=True,
                    override=True,
                ),
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    # Baseline SPA-friendly CSP. The frontend bundle is served
                    # from this same CloudFront origin, so 'self' is sufficient
                    # for scripts and styles. We allow 'unsafe-inline' for styles
                    # because Tailwind/runtime-injected CSS uses inline rules;
                    # scripts are NOT inline-allowed. connect-src includes
                    # CloudFront (same-origin via /api/*) and Cognito for auth.
                    #
                    # CSP Level 3 host-source grammar only allows `*` at the
                    # *start* of the host (e.g. `*.example.com`). A middle
                    # wildcard like `cognito-idp.*.amazonaws.com` is invalid
                    # and silently matches nothing in most browsers — Amplify's
                    # SRP fetch to `cognito-idp.{region}.amazonaws.com` would
                    # be blocked, surfacing as "A network error has occurred."
                    # We bake the deploy region into the CSP at synth time.
                    content_security_policy=(
                        "default-src 'self'; "
                        "script-src 'self'; "
                        # fonts.googleapis.com serves the Barlow/Instrument Serif
                        # @font-face stylesheet (MotionSites reskin); the actual
                        # woff2 files come from fonts.gstatic.com (font-src below).
                        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                        "img-src 'self' data: https:; "
                        "font-src 'self' data: https://fonts.gstatic.com; "
                        f"connect-src 'self' https://*.amazoncognito.com https://cognito-idp.{self.region}.amazonaws.com; "
                        "frame-ancestors 'none'; "
                        "object-src 'none'; "
                        "base-uri 'self'; "
                        "form-action 'self'"
                    ),
                    override=True,
                ),
            ),
        )

        # SPA client-side routing WITHOUT masking API errors (Bug 138).
        # CloudFront custom error_responses are DISTRIBUTION-WIDE — a 404→index.html
        # rule also rewrites every /api/* 404 into a 200 text/html page, which the
        # frontend then reports as "Unexpected response from server" and which makes
        # the panels' 404→empty-state logic unreachable. Instead, handle SPA deep
        # links with a CloudFront Function on the DEFAULT behavior only (the S3
        # origin). It rewrites extensionless navigation paths to /index.html so the
        # SPA loads, while /api/* (a separate behavior the function is NOT attached
        # to) passes origin status codes through untouched as real JSON.
        spa_router_fn = cloudfront.Function(
            self,
            "SpaRouterFunction",
            comment="Rewrite extensionless SPA routes to /index.html (default behavior only)",
            runtime=cloudfront.FunctionRuntime.JS_2_0,
            code=cloudfront.FunctionCode.from_inline(
                "function handler(event) {\n"
                "  var request = event.request;\n"
                "  var uri = request.uri;\n"
                "  if (uri === '/') { request.uri = '/index.html'; return request; }\n"
                "  // A path whose last segment has no '.' is a client-side route\n"
                "  // (e.g. /canvas/123) -> serve the SPA shell. Real assets\n"
                "  // (/assets/app.js, /vite.svg) keep their URI and 404 honestly.\n"
                "  var lastSlash = uri.lastIndexOf('/');\n"
                "  var lastSegment = uri.substring(lastSlash + 1);\n"
                "  if (lastSegment.indexOf('.') === -1) { request.uri = '/index.html'; }\n"
                "  return request;\n"
                "}\n"
            ),
        )

        distribution = cloudfront.Distribution(
            self,
            "FrontendDistribution",
            comment=f"{self._project}-{self._env} distribution",
            default_root_object="index.html",
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            web_acl_id=self.web_acl.attr_arn,
            log_bucket=self.logging_bucket,
            log_file_prefix="cloudfront/",
            default_behavior=cloudfront.BehaviorOptions(
                origin=s3_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                response_headers_policy=security_headers,
                function_associations=[
                    cloudfront.FunctionAssociation(
                        function=spa_router_fn,
                        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                    )
                ],
            ),
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(
                    origin=api_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    response_headers_policy=security_headers,
                ),
                "/health": cloudfront.BehaviorOptions(
                    origin=api_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    response_headers_policy=security_headers,
                ),
            },
            # NOTE: no distribution-wide error_responses — they would re-mask /api/*
            # 4xx. SPA routing is handled by spa_router_fn on the default behavior.
        )

        return distribution

    # ------------------------------------------------------------------
    # Stack Outputs
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # CloudWatch Alarms (LAMBDA-011)
    # ------------------------------------------------------------------

    def _create_lambda_alarms(self) -> None:
        """Create CloudWatch alarms for all Lambda functions."""
        all_fns: dict[str, _lambda.Function] = {
            "workflow": self.workflow_lambda,
            "deployment": self.deployment_lambda,
            **{f"step-{k}": v for k, v in self.step_lambdas.items()},
        }
        for name, fn in all_fns.items():
            slug = name.replace("_", "-")
            fn.metric_errors(period=Duration.minutes(5)).create_alarm(
                self,
                f"Alarm-{slug}-errors",
                alarm_name=f"{self._project}-{self._env}-{slug}-errors",
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            fn.metric_throttles(period=Duration.minutes(5)).create_alarm(
                self,
                f"Alarm-{slug}-throttles",
                alarm_name=f"{self._project}-{self._env}-{slug}-throttles",
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )

    # ------------------------------------------------------------------
    # CDK-NAG suppressions (audit issue #4)
    # ------------------------------------------------------------------

    def _apply_nag_suppressions(self) -> None:
        """Apply CDK-NAG suppressions to specific constructs.

        Per-construct (not stack-wide) so any new wildcard added in an
        unrelated construct will surface as a fresh nag finding instead of
        being silently absorbed. Each rule is scoped to the resource that
        legitimately needs it; reasons match the originals from app.py.
        """

        def _suppress(node, ids_with_reasons: list[tuple[str, str]]) -> None:
            cdk_nag.NagSuppressions.add_resource_suppressions(
                node,
                [
                    cdk_nag.NagPackSuppression(id=nid, reason=reason)
                    for nid, reason in ids_with_reasons
                ],
                apply_to_children=True,
            )

        # ---- IAM4 + IAM5: every Lambda role uses AWSLambdaBasicExecutionRole
        # and almost all of them have at least one wildcard resource for
        # dynamically-created Cognito pools, AgentCore runtimes, or Bedrock
        # model invocations. Suppress per Lambda execution role rather than
        # stack-wide. apply_to_children=True covers DefaultPolicy attached
        # by L2 grant_* helpers.
        iam_reasons = [
            (
                "AwsSolutions-IAM4",
                "AWSLambdaBasicExecutionRole is AWS-recommended for Lambda "
                "CloudWatch logging",
            ),
            (
                "AwsSolutions-IAM5",
                "Wildcard resources required for dynamically-created Cognito "
                "pools, AgentCore runtimes, and Bedrock model invocations",
            ),
        ]
        _suppress(self.workflow_lambda.role, iam_reasons)
        _suppress(self.deployment_lambda.role, iam_reasons)
        # When the deployment role's inline policy exceeds the IAM size limit, CDK
        # splits the excess into a separate "OverflowPolicy<N>" managed-policy
        # resource that apply_to_children on the role does NOT reach. Suppress the
        # same IAM5 wildcard findings on it by path (best-effort; ignored if absent).
        for _n in range(1, 4):
            try:
                cdk_nag.NagSuppressions.add_resource_suppressions_by_path(
                    self,
                    f"/{self.stack_name}/DeploymentLambdaRole/OverflowPolicy{_n}/Resource",
                    [cdk_nag.NagPackSuppression(id=nid, reason=reason) for nid, reason in iam_reasons],
                )
            except Exception:  # noqa: BLE001
                pass
        # Bug 157 — streaming test Lambda role: same invoke-on-* wildcards as the
        # deployment Lambda's test path (InvokeAgentRuntime/InvokeHarness).
        if hasattr(self, "stream_lambda"):
            _suppress(self.stream_lambda.role, iam_reasons)
        for fn in self.step_lambdas.values():
            _suppress(fn.role, iam_reasons)
        # Shared AgentCore runtime exec role: needs bedrock:* and
        # bedrock-agentcore:*Browser*/Memory etc on Resource: "*" — see
        # _create_shared_runtime_role docstring.
        _suppress(
            self.shared_runtime_role,
            [
                (
                    "AwsSolutions-IAM5",
                    "Wildcard resources required for dynamically-created "
                    "Cognito pools, AgentCore runtimes, and Bedrock model "
                    "invocations",
                ),
            ],
        )
        # Step Functions role wraps grant_invoke on every step Lambda; CDK's
        # grant_* helpers attach a DefaultPolicy whose statements use the
        # function ARN with a wildcard suffix for versions/aliases.
        if hasattr(self, "state_machine") and self.state_machine.role is not None:
            _suppress(self.state_machine.role, iam_reasons)

        # ---- L1: All Lambdas use Python 3.12 deliberately.
        l1_reasons = [
            (
                "AwsSolutions-L1",
                "Using Python 3.12 for CDK Lambda construct stability",
            ),
        ]
        _suppress(self.workflow_lambda, l1_reasons)
        _suppress(self.deployment_lambda, l1_reasons)
        if hasattr(self, "stream_lambda"):
            _suppress(self.stream_lambda, l1_reasons)
        for fn in self.step_lambdas.values():
            _suppress(fn, l1_reasons)

        # ---- S1: only the access-log bucket itself is exempt — everything
        # else writes its access logs INTO this bucket.
        _suppress(
            self.logging_bucket,
            [
                (
                    "AwsSolutions-S1",
                    "S3 access logging is hosted by this bucket itself; "
                    "logging the log bucket would be a circular dependency",
                ),
            ],
        )

        # ---- CloudFront: distribution-only.
        _suppress(
            self.distribution,
            [
                (
                    "AwsSolutions-CFR1",
                    "CloudFront geo restrictions not required — internal "
                    "development tool",
                ),
                (
                    "AwsSolutions-CFR4",
                    "Using CloudFront default certificate — custom domain "
                    "with ACM planned for production",
                ),
            ],
        )

        # ---- API Gateway: APIG1 (access logging) and APIG4 (route auth)
        # apply only to the HTTP API. /health is intentionally unauthenticated.
        _suppress(
            self.api,
            [
                (
                    "AwsSolutions-APIG1",
                    "API Gateway access logging planned for production — "
                    "using Lambda CloudWatch logs",
                ),
                (
                    "AwsSolutions-APIG4",
                    "JWT authorizer on all /api/* routes; /health is "
                    "intentionally unauthenticated",
                ),
            ],
        )

        # ---- Cognito: COG2/COG4/COG8 only apply to the user pool / clients.
        _suppress(
            self.user_pool,
            [
                (
                    "AwsSolutions-COG2",
                    "MFA enforced at the IdP for FederateOIDC SSO logins; "
                    "Cognito-native MFA would be redundant for this internal "
                    "development tool",
                ),
                (
                    "AwsSolutions-COG4",
                    "Cognito JWT authorizer on all /api/* routes; /health is "
                    "intentionally unauthenticated",
                ),
                (
                    "AwsSolutions-COG8",
                    "Cognito Plus tier (advanced security) not required for "
                    "this internal development tool; upstream IdP provides "
                    "threat protection",
                ),
            ],
        )

        # ---- Step Functions: SF1 (ALL-level logging) on the state machine.
        _suppress(
            self.state_machine,
            [
                (
                    "AwsSolutions-SF1",
                    "Step Functions logs ERROR-level events; ALL-level "
                    "logging planned for production",
                ),
            ],
        )

        # ---- BucketDeployment singleton custom resource: CDK creates a
        # shared Lambda at the stack root (Custom::CDKBucketDeployment*) when
        # any BucketDeployment is used. Path-scoped suppressions because the
        # construct lives outside our owned constructs — owned by
        # aws-cdk-lib's BucketDeployment L2; we cannot tighten its IAM,
        # runtime version, or managed policy without forking the L2.
        for child in self.node.find_all():
            try:
                node_path = child.node.path  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover
                continue
            if "Custom::CDKBucketDeployment" in node_path:
                cdk_nag.NagSuppressions.add_resource_suppressions_by_path(
                    self,
                    node_path,
                    [
                        cdk_nag.NagPackSuppression(
                            id="AwsSolutions-L1",
                            reason="BucketDeployment is a CDK-managed L2; runtime version is owned by aws-cdk-lib.",
                        ),
                        cdk_nag.NagPackSuppression(
                            id="AwsSolutions-IAM4",
                            reason="BucketDeployment uses AWSLambdaBasicExecutionRole — owned by the CDK L2 construct.",
                            applies_to=[
                                "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                            ],
                        ),
                        cdk_nag.NagPackSuppression(
                            id="AwsSolutions-IAM5",
                            reason="BucketDeployment requires s3:Get*/List*/Abort*/DeleteObject* wildcards on the source CDK assets bucket and the destination artifacts bucket — owned by the CDK L2.",
                            applies_to=[
                                "Action::s3:GetBucket*",
                                "Action::s3:GetObject*",
                                "Action::s3:List*",
                                "Action::s3:Abort*",
                                "Action::s3:DeleteObject*",
                                # Region-templated rather than hardcoded so the
                                # suppression holds across all deployment regions.
                                # Without this every non-us-east-1 deploy fails
                                # CDK-NAG with an unmatched IAM5 wildcard.
                                f"Resource::arn:<AWS::Partition>:s3:::cdk-hnb659fds-assets-<AWS::AccountId>-{self.region}/*",
                                "Resource::<ArtifactsBucket2AAC5544.Arn>/*",
                            ],
                        ),
                    ],
                )

        # ---- Cognito user provisioner (only created when COGNITO_USERS env
        # var is non-empty). Three sub-constructs need scoped suppressions:
        # (1) our own provisioner Lambda + role (we OWN the code; managed
        # policy is CDK auto-attach for any lambda_.Function),
        # (2) CDK's Provider framework which spawns its own framework-onEvent
        # Lambda we don't control,
        # (3) CDK's LogRetention helper Lambda used by `log_retention=`.
        cognito_provisioner_paths = [
            f"{self.stack_name}/CognitoUserProvisionerFn",
        ]
        for p in cognito_provisioner_paths:
            try:
                cdk_nag.NagSuppressions.add_resource_suppressions_by_path(
                    self,
                    p,
                    [
                        cdk_nag.NagPackSuppression(
                            id="AwsSolutions-L1",
                            reason="Using Python 3.12 deliberately for CDK Lambda construct stability (matches all other platform Lambdas).",
                        ),
                        cdk_nag.NagPackSuppression(
                            id="AwsSolutions-IAM4",
                            reason="Lambda execution role auto-attaches AWSLambdaBasicExecutionRole; required for CloudWatch logging.",
                            applies_to=[
                                "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                            ],
                        ),
                    ],
                    apply_to_children=True,
                )
            except Exception:  # pragma: no cover
                pass

        # CDK Provider framework + LogRetention helper — both CDK-managed L2s
        # we do not own. Path-scoped because the constructs may not exist
        # (only created when COGNITO_USERS is set).
        for child in self.node.find_all():
            try:
                node_path = child.node.path  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover
                continue
            if "CognitoUserProvisionerProvider" in node_path or "LogRetention" in node_path:
                try:
                    cdk_nag.NagSuppressions.add_resource_suppressions_by_path(
                        self,
                        node_path,
                        [
                            cdk_nag.NagPackSuppression(
                                id="AwsSolutions-L1",
                                reason="Provider framework / LogRetention helper Lambda is a CDK-managed L2; runtime version is owned by aws-cdk-lib.",
                            ),
                            cdk_nag.NagPackSuppression(
                                id="AwsSolutions-IAM4",
                                reason="CDK-managed L2 Lambda uses AWSLambdaBasicExecutionRole — owned by aws-cdk-lib.",
                                applies_to=[
                                    "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                                ],
                            ),
                            cdk_nag.NagPackSuppression(
                                id="AwsSolutions-IAM5",
                                reason="CDK Provider framework grants `lambda:InvokeFunction` on the user provisioner Lambda's ARN with version-suffix wildcard, and LogRetention helper requires `logs:PutRetentionPolicy` / `logs:DeleteRetentionPolicy` on `*` to set retention on dynamically-named log groups — both owned by aws-cdk-lib.",
                                applies_to=[
                                    "Resource::*",
                                    "Resource::<CognitoUserProvisionerFn43674288.Arn>:*",
                                ],
                            ),
                        ],
                    )
                except Exception:  # pragma: no cover
                    pass

    def _create_stack_outputs(self) -> None:
        """Create CloudFormation stack outputs.

        Requirements: 7.3
        """
        CfnOutput(
            self,
            "ApiGatewayUrl",
            value=self.api.url or "",
            description="API Gateway HTTP API URL",
        )

        CfnOutput(
            self,
            "CloudFrontUrl",
            value=f"https://{self.distribution.distribution_domain_name}",
            description="CloudFront distribution URL",
        )

        CfnOutput(
            self,
            "S3BucketName",
            value=self.bucket.bucket_name,
            description="Frontend S3 bucket name",
        )

        CfnOutput(
            self,
            "UserPoolId",
            value=self.user_pool.user_pool_id,
            description="Cognito User Pool ID",
        )

        CfnOutput(
            self,
            "UserPoolClientId",
            value=self.user_pool_client.user_pool_client_id,
            description="Cognito User Pool Client ID",
        )
