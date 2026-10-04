"""Serverless CDK stack for the AgentCore Visual Workflow Platform.

Replaces the ECS Fargate + ALB architecture with:
- API Gateway HTTP API
- Lambda functions (workflow, deployment, step handlers)
- Step Functions state machine for deployment orchestration
- DynamoDB tables (workflows + deployments)
- S3 + CloudFront for frontend
- Least-privilege IAM roles

The resource definitions live in ``stacks/platform/`` builder modules; this
class orchestrates them in dependency order. All builders create constructs
with ``scope=stack`` and the original construct ids, so every CloudFormation
logical ID is byte-identical to the pre-refactor monolith (the stack is
deployed live — changed logical IDs would replace resources).

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 6.1, 6.2, 6.3, 7.1, 7.2, 7.3, 7.4, 7.5
"""

import os

import aws_cdk as cdk
from aws_cdk import RemovalPolicy
from constructs import Construct

from .platform.api import add_cloudfront_cors_origin, build_api_gateway
from .platform.buckets import build_artifacts_bucket, build_logging_bucket, upload_agentcore_deps
from .platform.cloudfront_waf import build_cloudfront_distribution, build_frontend_bucket, build_waf_web_acl
from .platform.cognito_auth import build_cognito
from .platform.config import PlatformConfig
from .platform.gateway_auth_pool import build_gateway_auth_pool
from .platform.lambdas import (
    build_deployment_lambda,
    build_shared_mcp_runtime_role,
    build_shared_runtime_role,
    build_stream_lambda,
    build_trigger_dispatch_queues,
    build_workflow_lambda,
    get_backend_code,
)
from .platform.nag_suppressions import apply_nag_suppressions
from .platform.observability import build_lambda_alarms
from .platform.otel import OtelConfig
from .platform.outputs import build_stack_outputs
from .platform.role_boundary import build_role_permissions_boundary
from .platform.ssm_params import build_runtime_ssm_parameters, build_ssm_parameters
from .platform.step_functions import build_state_machine
from .platform.step_lambdas import build_step_lambdas
from .platform.tables import build_tables
from .platform.tool_sandbox_net import build_tool_sandbox_network


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

        # Audit issue #9: gate RemovalPolicy.DESTROY on environment so prod-like
        # envs don't lose data on teardown. dev/test/sandbox/preview environments
        # use DESTROY; everything else uses RETAIN_ON_UPDATE_OR_DELETE, which
        # synthesizes DeletionPolicy: RetainExceptOnCreate + UpdateReplacePolicy:
        # Retain. Plain RETAIN also retains on the rollback of a FAILED FIRST
        # CREATE: on 2026-09-25 a fresh stack died at its VPC (quota 5/5);
        # CloudFormation emitted 20 DELETE_SKIPPED events, and the three resources
        # that had already been physically created (two Cognito pools, the DLQ)
        # persisted through rollback AND stack delete, blocking a same-name retry
        # until removed by hand. RetainExceptOnCreate deletes those on the initial
        # rollback and keeps full protection on later updates and deletes.
        # Override via env var AGENTCORE_ALLOW_DESTROY=true.
        _destroy_envs = {"dev", "test", "sandbox", "preview", "ephemeral"}
        _allow_destroy = (environment_name or "").lower() in _destroy_envs or os.environ.get(
            "AGENTCORE_ALLOW_DESTROY", "false"
        ).lower() == "true"
        cfg = PlatformConfig(
            env=environment_name,
            project=project_name,
            removal_policy=RemovalPolicy.DESTROY if _allow_destroy else RemovalPolicy.RETAIN_ON_UPDATE_OR_DELETE,
            allow_destroy=_allow_destroy,
        )
        # Platform OTEL defaults — feature is enabled iff both endpoint and
        # secret ARN are provided. Lambdas + agents inherit these.
        otel = OtelConfig(
            endpoint=otel_endpoint,
            auth_secret_arn=otel_auth_secret_arn,
            sample_rate=otel_sample_rate or "1.0",
            service_name_prefix=otel_service_name_prefix or project_name,
            env=environment_name,
        )

        # --- Storage ---
        tables = build_tables(self, cfg)
        self.tables = tables
        self.workflows_table = tables.workflows
        self.deployments_table = tables.deployments
        self.flows_table = tables.flows
        self.agent_versions_table = tables.agent_versions
        self.runtime_slots_table = tables.runtime_slots
        self.agent_registry_table = tables.agent_registry
        self.usage_events_table = tables.usage_events
        self.hitl_requests_table = tables.hitl_requests
        self.triggers_table = tables.triggers
        self.prompt_library_table = tables.prompt_library
        self.tag_policy_table = tables.tag_policy
        self.budget_table = tables.budget
        self.audit_table = tables.audit
        self.permission_requests_table = tables.permission_requests
        self.logging_bucket = build_logging_bucket(self, cfg)
        self.artifacts_bucket = build_artifacts_bucket(self, cfg, self.logging_bucket)
        self.agentcore_deps_deployment = upload_agentcore_deps(self, self.artifacts_bucket)
        (
            self.trigger_dispatch_queue,
            self.trigger_dispatch_dead_letter_queue,
            self.trigger_rule_prefix,
        ) = build_trigger_dispatch_queues(self, cfg)

        # --- SSM Parameters ---
        build_ssm_parameters(
            self,
            cfg,
            otel,
            workflows_table=tables.workflows,
            deployments_table=tables.deployments,
            flows_table=tables.flows,
        )

        # Shared gateway-auth Cognito pool + hosted domain. MUST be built before the
        # deployment Lambda and the step Lambdas, both of which need its id: the
        # gateway/harness STEP roles (control plane — they create the app client, so
        # they must read its secret once) get DescribeUserPoolClient on its exact ARN,
        # and every Lambda receives the id as an environment variable so each teardown
        # path can recognize the pool and refuse to delete it.
        #
        # The shared AgentCore RUNTIME role deliberately gets no such grant: it is
        # tenant-facing and shared, and Cognito IAM cannot scope below the pool, so the
        # grant would let any agent read every other agent's gateway client secret.
        # See the grant site in lambdas.build_shared_runtime_role.
        #
        # A hosted domain takes minutes to become reachable (measured >381s), which is
        # longer than the gateway step's entire 300s budget — so it is created once
        # here, at platform deploy time, and is warm before any agent deploy needs it.
        # See gateway_auth_pool.py for the measurement and the failure it fixes.
        self.gateway_auth_pool, self.gateway_auth_domain = build_gateway_auth_pool(self, cfg)

        # --- Shared AgentCore runtime execution role ---
        # Created once at stack-deploy time so AgentCore's IAM cache has
        # fully propagated by the time any user-deploy runs. Eliminates the
        # 17-20 min IAM-propagation race that per-deploy roles hit.
        # See tasks/lessons.md Bug 60.
        self.shared_runtime_role = build_shared_runtime_role(
            self,
            cfg,
            otel,
            artifacts_bucket=self.artifacts_bucket,
            hitl_requests_table=tables.hitl_requests,
        )
        # Model-free sibling of the shared runtime role, pre-warmed for the same
        # Bug-60 IAM-propagation reason. A standalone FastMCP runtime selects this
        # one so a protocol-only container can never invoke a model.
        self.shared_mcp_runtime_role = build_shared_mcp_runtime_role(
            self,
            cfg,
            artifacts_bucket=self.artifacts_bucket,
        )

        # --- Permissions boundary for every role the Lambdas mint (F-06) ---
        # Built before the Lambdas so its ARN can reach them as an environment variable and
        # its conditions can be written onto their CreateRole/PutRolePolicy grants. See
        # platform/role_boundary.py for the cap and for why enforcement is a separate switch.
        self.role_boundary = build_role_permissions_boundary(self, cfg)

        # --- Lambda Functions ---
        self.backend_code = get_backend_code()
        self.workflow_lambda = build_workflow_lambda(
            self,
            cfg,
            otel,
            backend_code=self.backend_code,
            workflows_table=tables.workflows,
            flows_table=tables.flows,
        )
        # Where a generated tool gets executed. Its own isolated VPC: no internet
        # gateway, no NAT gateway, and outbound only to a CloudWatch Logs interface
        # endpoint. ARCC cnt_MSVB0Kk8WMwmmW requires untrusted customer-provided
        # code to run from a private network with no public internet access; before
        # this existed the tool-test sandbox ran with default Lambda egress, which is
        # full outbound internet. See platform/tool_sandbox_net.py for why the VPC is
        # dedicated rather than shared, and for the HTTP trade-off it accepts.
        self.tool_sandbox_network = build_tool_sandbox_network(
            self,
            resource_prefix=f"{cfg.project}-{cfg.env}",
            removal_policy=cfg.removal_policy,
        )
        self.deployment_lambda = build_deployment_lambda(
            self,
            cfg,
            otel,
            backend_code=self.backend_code,
            tables=tables,
            artifacts_bucket=self.artifacts_bucket,
            shared_runtime_role=self.shared_runtime_role,
            shared_mcp_runtime_role=self.shared_mcp_runtime_role,
            role_boundary=self.role_boundary,
            sandbox_network=self.tool_sandbox_network,
            trigger_dispatch_queue=self.trigger_dispatch_queue,
            trigger_rule_prefix=self.trigger_rule_prefix,
            gateway_auth_pool=self.gateway_auth_pool,
            gateway_auth_domain=self.gateway_auth_domain,
        )
        self.step_lambdas = build_step_lambdas(
            self,
            cfg,
            otel,
            backend_code=self.backend_code,
            tables=tables,
            artifacts_bucket=self.artifacts_bucket,
            shared_runtime_role=self.shared_runtime_role,
            shared_mcp_runtime_role=self.shared_mcp_runtime_role,
            role_boundary=self.role_boundary,
            gateway_auth_pool=self.gateway_auth_pool,
            gateway_auth_domain=self.gateway_auth_domain,
        )

        # --- Step Functions ---
        self.state_machine = build_state_machine(self, cfg, step_lambdas=self.step_lambdas, tables=tables)

        # --- Grant deployment Lambda permission to start executions ---
        self.state_machine.grant_start_execution(self.deployment_lambda)

        # --- API Gateway ---
        self.user_pool, self.user_pool_client = build_cognito(self, cfg)
        self.api = build_api_gateway(
            self,
            cfg,
            workflow_lambda=self.workflow_lambda,
            deployment_lambda=self.deployment_lambda,
            user_pool=self.user_pool,
            user_pool_client=self.user_pool_client,
            state_machine=self.state_machine,
        )

        # --- Streaming test Lambda + Function URL (Bug 157) ---
        # API Gateway HTTP API has a hard 30s integration cap; tool-heavy agents
        # (>30s) time out at the transport. A Lambda Function URL with
        # InvokeMode=RESPONSE_STREAM lets long invokes stream past 30s. Created
        # after Cognito so it can verify the same pool/client JWT in-handler.
        self.stream_lambda, self.stream_function_url = build_stream_lambda(
            self,
            cfg,
            otel,
            backend_code=self.backend_code,
            deployments_table=tables.deployments,
            user_pool=self.user_pool,
            user_pool_client=self.user_pool_client,
        )

        # --- S3 + CloudFront + WAF ---
        # Scope depends on the region: CLOUDFRONT (attached to the distribution)
        # in us-east-1, REGIONAL (attached to the Cognito user pool) everywhere
        # else, because AWS only offers CLOUDFRONT-scoped WebACLs in us-east-1
        # and a distribution will not accept any other scope.
        self.web_acl = build_waf_web_acl(self, cfg, user_pool=self.user_pool)
        # NOTE: We previously attempted a regional WAF on the API Gateway stage
        # to prevent direct execute-api.amazonaws.com bypass of the CloudFront
        # WAF, but WAFv2 only supports REST API Gateway (v1), NOT HTTP API
        # Gateway (v2) which this stack uses. Tracked as a known gap; mitigated
        # by API Gateway throttling already configured on the default stage.
        # See tasks/lessons.md Bug 41 (revised).
        self.bucket = build_frontend_bucket(self, cfg, self.logging_bucket)
        self.distribution = build_cloudfront_distribution(
            self,
            cfg,
            bucket=self.bucket,
            api=self.api,
            web_acl=self.web_acl,
            logging_bucket=self.logging_bucket,
        )

        # --- Post-creation: add CloudFront URL to API Gateway CORS ---
        add_cloudfront_cors_origin(self.api)

        # --- CloudWatch Alarms ---
        self.alarm_topic = build_lambda_alarms(
            self,
            cfg,
            workflow_lambda=self.workflow_lambda,
            deployment_lambda=self.deployment_lambda,
            step_lambdas=self.step_lambdas,
            tables=tables,
            state_machine=self.state_machine,
            trigger_dead_letter_queue=self.trigger_dispatch_dead_letter_queue,
        )

        # --- Update SSM with runtime URLs ---
        build_runtime_ssm_parameters(self, cfg, self.api)

        # --- Stack Outputs ---
        build_stack_outputs(
            self,
            api=self.api,
            distribution=self.distribution,
            bucket=self.bucket,
            user_pool=self.user_pool,
            user_pool_client=self.user_pool_client,
        )

        # --- CDK-NAG suppressions (per-construct, audit issue #4) ---
        apply_nag_suppressions(
            self,
            workflow_lambda=self.workflow_lambda,
            deployment_lambda=self.deployment_lambda,
            stream_lambda=self.stream_lambda,
            step_lambdas=self.step_lambdas,
            shared_runtime_role=self.shared_runtime_role,
            shared_mcp_runtime_role=self.shared_mcp_runtime_role,
            role_boundary=self.role_boundary,
            state_machine=self.state_machine,
            logging_bucket=self.logging_bucket,
            distribution=self.distribution,
            api=self.api,
            user_pool=self.user_pool,
            gateway_auth_pool=self.gateway_auth_pool,
            trigger_dead_letter_queue=self.trigger_dispatch_dead_letter_queue,
        )
