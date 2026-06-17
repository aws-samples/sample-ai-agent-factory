# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK Stack for Module 4: AgentCore Gateway integration layer.

Provisions the governed Path B alongside Module 3's NGINX-based Path A:

- AgentCore Gateway IAM Role (assumed by bedrock-agentcore service)
- Sync Lambda (Registry API → Gateway target sync, EventBridge scheduled)
- Request Interceptor Lambda (CloudWatch audit logging)
- Response Interceptor Lambda (Bedrock Guardrails on tool outputs)
- Cognito groups (admins, developers) added to Module 3's User Pool

All identity imports come from Module 3's CFN exports (EnvironmentName=workshop).
No DynamoDB tables, no FastAPI API, no Streamlit UI — those are Module 3's domain.
"""

import os
import shutil
import subprocess
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    aws_cognito as cognito,
    aws_events as events,
    aws_events_targets as events_targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
)
from constructs import Construct

MODULE_ROOT = Path(__file__).resolve().parent.parent


class AgentCoreGatewayStack(Stack):
    """Module 4: AgentCore Gateway — governed Path B for tool access."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        env_name = "workshop"  # Must match Module 3's EnvironmentName parameter

        # ─── Import Module 3 CFN Exports ───
        cognito_pool_id = Fn.import_value(f"{env_name}-CognitoUserPoolId")
        cognito_domain = Fn.import_value(f"{env_name}-CognitoDomain")
        registry_url = Fn.import_value(f"{env_name}-RegistryUrl")
        cloudfront_url = Fn.import_value(f"{env_name}-MainCloudFrontUrl")
        m2m_client_id = Fn.import_value(f"{env_name}-CognitoM2MClientId")

        # The Sync Lambda authenticates to the Registry API using the static
        # bearer token stored in Module 3's data-stack secret (field ``api_token``).
        m2m_secret_name = f"{env_name}-registry-api-token"

        # ─── Cognito Groups (extend Module 3's pool) ───
        cognito.CfnUserPoolGroup(
            self, "AdminsGroup",
            group_name="gateway-admins",
            user_pool_id=cognito_pool_id,
            description="AgentCore Gateway administrators",
        )
        cognito.CfnUserPoolGroup(
            self, "DevelopersGroup",
            group_name="gateway-developers",
            user_pool_id=cognito_pool_id,
            description="AgentCore Gateway developers",
        )

        # ─── AgentCore Gateway IAM Role ───
        gateway_role = iam.Role(
            self, "AgentCoreGatewayRole",
            # Region-qualified: IAM role names are account-global, so a fixed
            # name would collide if the workshop is deployed in two regions of
            # the same account (matches the CFN tools-gateway stack naming).
            role_name=f"workshop-agentcore-gateway-role-{self.region}",
            assumed_by=iam.CompositePrincipal(
                iam.ServicePrincipal("bedrock.amazonaws.com"),
                iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            ),
            description="Role assumed by AgentCore Gateway for tool dispatch",
        )
        gateway_role.add_to_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[
                f"arn:aws:lambda:{self.region}:{self.account}:function:workshop-*",
                f"arn:aws:lambda:{self.region}:{self.account}:function:agentcore-*",
            ],
        ))

        # ─── Lambda Code Bundle ───
        # Handlers and services are at the module root level
        handlers_path = str(MODULE_ROOT / "handlers")
        services_path = str(MODULE_ROOT / "services")

        # Bundle handlers + services together
        lambda_code = _lambda.Code.from_asset(
            str(MODULE_ROOT),
            exclude=[
                "cdk", "cdk/*",
                "notebooks", "notebooks/*",
                "tests", "tests/*",
                ".venv", ".venv/*",
                ".claude", ".claude/*",
                ".pytest_cache", ".pytest_cache/*",
                "__pycache__",
                "*.md",
                "*.ipynb",
                "create_gateway.py",
                ".gitignore",
            ],
        )

        # ─── Sync Lambda ───
        sync_fn = _lambda.Function(
            self, "SyncLambda",
            function_name="agentcore-gateway-sync",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handlers.sync_lambda.handler",
            code=lambda_code,
            memory_size=256,
            timeout=Duration.seconds(120),
            environment={
                "REGISTRY_URL": registry_url,
                "M2M_SECRET_NAME": m2m_secret_name,
                "GATEWAY_ID": "",  # Set after create_gateway.py
                "CLOUDFRONT_URL": cloudfront_url,
                "SYNC_FILTER_TAGS": "",  # Comma-separated tags to filter; empty = sync all
                "LOG_LEVEL": "INFO",
            },
            log_group=logs.LogGroup(
                self, "SyncLogGroup",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        # Grant Secrets Manager read for M2M credentials
        # Covers both the M2M agent credentials and the static API token secret
        sync_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=[
                f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:{env_name}-*",
                f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:{env_name}/*",
            ],
        ))
        # Grant AgentCore Gateway target management
        sync_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "bedrock-agentcore:CreateGatewayTarget",
                "bedrock-agentcore:DeleteGatewayTarget",
                "bedrock-agentcore:GetGatewayTarget",
                "bedrock-agentcore:ListGatewayTargets",
                "bedrock-agentcore:SynchronizeGatewayTargets",
            ],
            resources=[f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*"],
        ))

        # ─── Demo Tool Lambda (search-knowledge-base) ───
        # Simple MCP-compatible Lambda for workshop demos.
        # Replaced by a real Bedrock KB integration in Module 5.
        demo_fn = _lambda.Function(
            self, "DemoToolLambda",
            function_name="workshop-search-knowledge-base",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handlers.demo_tool.handler",
            code=lambda_code,
            memory_size=128,
            timeout=Duration.seconds(10),
            log_group=logs.LogGroup(
                self, "DemoToolLogGroup",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        # Allow the gateway role to invoke this demo Lambda
        demo_fn.grant_invoke(gateway_role)

        # ─── Product Info Tool Lambda (workshop-product-info-tool) ───
        # MCP-compatible Lambda that returns product details by ID.
        # Referenced by Module 3 content (Register MCP Server step).
        product_info_fn = _lambda.Function(
            self, "ProductInfoToolLambda",
            function_name="workshop-product-info-tool",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handlers.product_info_tool.handler",
            code=lambda_code,
            memory_size=128,
            timeout=Duration.seconds(10),
            log_group=logs.LogGroup(
                self, "ProductInfoToolLogGroup",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        product_info_fn.grant_invoke(gateway_role)

        # ─── EventBridge Schedule (every 5 minutes) ───
        events.Rule(
            self, "SyncSchedule",
            schedule=events.Schedule.rate(Duration.minutes(5)),
            targets=[events_targets.LambdaFunction(sync_fn)],
        )

        # ─── Request Interceptor ───
        request_fn = _lambda.Function(
            self, "RequestInterceptor",
            function_name="agentcore-gateway-request-interceptor",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handlers.interceptors.request_interceptor_handler",
            code=lambda_code,
            memory_size=256,
            timeout=Duration.seconds(10),
            environment={
                "AUDIT_TABLE_NAME": "",  # Optional: set to DynamoDB table for audit
                "TOOL_ACCESS_POLICY": "",  # JSON: {"group": ["tool-pattern", ...]}
                "LOG_LEVEL": "INFO",
            },
            log_group=logs.LogGroup(
                self, "RequestInterceptorLogGroup",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        # Allow AgentCore Gateway to invoke this interceptor
        request_fn.grant_invoke(
            iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*"
                    }
                },
            )
        )

        # ─── Response Interceptor ───
        response_fn = _lambda.Function(
            self, "ResponseInterceptor",
            function_name="agentcore-gateway-response-interceptor",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handlers.interceptors.response_interceptor_handler",
            code=lambda_code,
            memory_size=256,
            timeout=Duration.seconds(10),
            environment={
                "BEDROCK_GUARDRAIL_ID": "",  # Set in notebook 6
                "BEDROCK_GUARDRAIL_VERSION": "DRAFT",
                "TOOL_ACCESS_POLICY": "",  # JSON: {"group": ["tool-pattern", ...]}
                "LOG_LEVEL": "INFO",
            },
            log_group=logs.LogGroup(
                self, "ResponseInterceptorLogGroup",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        response_fn.grant_invoke(
            iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*"
                    }
                },
            )
        )
        # Grant Bedrock Guardrails
        response_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:ApplyGuardrail"],
            resources=[f"arn:aws:bedrock:{self.region}:{self.account}:guardrail/*"],
        ))

        # ─── Outputs ───
        CfnOutput(self, "GatewayRoleArn",
                  value=gateway_role.role_arn,
                  export_name="agentcore-gateway-RoleArn",
                  description="IAM Role ARN for AgentCore Gateway")

        CfnOutput(self, "SyncLambdaArn",
                  value=sync_fn.function_arn,
                  export_name="agentcore-gateway-SyncLambdaArn",
                  description="Sync Lambda ARN")

        CfnOutput(self, "SyncLambdaName",
                  value=sync_fn.function_name,
                  export_name="agentcore-gateway-SyncLambdaName",
                  description="Sync Lambda function name")

        CfnOutput(self, "GatewayIdSsmParam",
                  value=f"/agentcore-gateway/{env_name}/gateway-id",
                  description="SSM parameter name where create_gateway.py stores the Gateway ID")

        CfnOutput(self, "RequestInterceptorArn",
                  value=request_fn.function_arn,
                  export_name="agentcore-gateway-RequestInterceptorArn",
                  description="Request Interceptor Lambda ARN")

        CfnOutput(self, "ResponseInterceptorArn",
                  value=response_fn.function_arn,
                  export_name="agentcore-gateway-ResponseInterceptorArn",
                  description="Response Interceptor Lambda ARN")

        CfnOutput(self, "DemoToolLambdaArn",
                  value=demo_fn.function_arn,
                  export_name="agentcore-gateway-DemoToolLambdaArn",
                  description="Demo search-knowledge-base Lambda ARN")

        CfnOutput(self, "ProductInfoToolLambdaArn",
                  value=product_info_fn.function_arn,
                  export_name="agentcore-gateway-ProductInfoToolArn",
                  description="Product info tool Lambda ARN (used by Module 3)")

        CfnOutput(self, "CognitoUserPoolId",
                  value=cognito_pool_id,
                  description="Cognito User Pool ID (from Module 3)")

        CfnOutput(self, "CognitoDomain",
                  value=cognito_domain,
                  description="Cognito domain (from Module 3)")

        CfnOutput(self, "RegistryUrl",
                  value=registry_url,
                  description="Registry URL (from Module 3)")

        CfnOutput(self, "CloudFrontUrl",
                  value=cloudfront_url,
                  description="CloudFront URL (from Module 3)")

        CfnOutput(self, "M2MClientId",
                  value=m2m_client_id,
                  description="Cognito M2M Client ID (from Module 3)")

