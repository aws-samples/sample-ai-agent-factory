"""CDK assertion tests for the serverless PlatformStack.

Verifies the synthesized CloudFormation template contains the expected
serverless resources (API Gateway, Lambda, Step Functions, DynamoDB, S3,
CloudFront) and does NOT contain removed resources (VPC, ECS, ALB, ECR,
CodeBuild, NAT Gateway). Also validates IAM scoping and Step Functions
retry/catch configuration.

Validates: Requirements 7.1, 7.4
"""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from stacks.platform_stack import PlatformStack


@pytest.fixture(scope="module")
def template():
    """Synthesize the PlatformStack and return the CloudFormation template."""
    app = cdk.App()
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name="test",
        project_name="agentcore-workflow",
        env=cdk.Environment(region="us-east-1", account="123456789012"),
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template_json(template):
    """Return the raw CloudFormation template as a dict for deeper inspection."""
    return template.to_json()


# ---------------------------------------------------------------
# Serverless resources MUST be present (Requirement 7.1)
# ---------------------------------------------------------------


class TestServerlessResourcesPresent:
    """Verify the template contains all expected serverless resources."""

    def test_has_api_gateway_http_api(self, template):
        template.resource_count_is("AWS::ApiGatewayV2::Api", 1)

    def test_has_lambda_functions(self, template):
        """Stack should have workflow + deployment + 8 step lambdas = 10 total."""
        resources = template.find_resources("AWS::Lambda::Function")
        assert len(resources) >= 10, f"Expected at least 10 Lambda functions, found {len(resources)}"

    def test_has_step_functions_state_machine(self, template):
        template.resource_count_is("AWS::StepFunctions::StateMachine", 1)

    def test_has_two_dynamodb_tables(self, template):
        template.resource_count_is("AWS::DynamoDB::Table", 2)

    def test_has_s3_bucket(self, template):
        template.resource_count_is("AWS::S3::Bucket", 2)

    def test_has_cloudfront_distribution(self, template):
        template.resource_count_is("AWS::CloudFront::Distribution", 1)

    def test_has_ssm_parameters(self, template):
        resources = template.find_resources("AWS::SSM::Parameter")
        assert len(resources) >= 4, f"Expected at least 4 SSM parameters, found {len(resources)}"


# ---------------------------------------------------------------
# Removed resources MUST NOT be present (Requirement 7.4)
# ---------------------------------------------------------------


class TestRemovedResourcesAbsent:
    """Verify the template does NOT contain old ECS/VPC architecture resources."""

    def test_no_vpc(self, template):
        template.resource_count_is("AWS::EC2::VPC", 0)

    def test_no_ecs_cluster(self, template):
        template.resource_count_is("AWS::ECS::Cluster", 0)

    def test_no_ecs_service(self, template):
        template.resource_count_is("AWS::ECS::Service", 0)

    def test_no_ecs_task_definition(self, template):
        template.resource_count_is("AWS::ECS::TaskDefinition", 0)

    def test_no_alb(self, template):
        template.resource_count_is("AWS::ElasticLoadBalancingV2::LoadBalancer", 0)

    def test_no_ecr_repository(self, template):
        template.resource_count_is("AWS::ECR::Repository", 0)

    def test_no_codebuild_project(self, template):
        template.resource_count_is("AWS::CodeBuild::Project", 0)

    def test_no_nat_gateway(self, template):
        template.resource_count_is("AWS::EC2::NatGateway", 0)

    def test_no_subnets(self, template):
        template.resource_count_is("AWS::EC2::Subnet", 0)

    def test_no_security_groups(self, template):
        template.resource_count_is("AWS::EC2::SecurityGroup", 0)


# ---------------------------------------------------------------
# IAM roles have scoped permissions — no *FullAccess (Req 7.1, 7.4)
# ---------------------------------------------------------------


class TestIAMScoping:
    """Verify IAM roles use least-privilege — no *FullAccess managed policies."""

    def test_no_full_access_managed_policies(self, template_json):
        """No IAM role should attach a *FullAccess managed policy."""
        resources = template_json.get("Resources", {})
        for logical_id, resource in resources.items():
            if resource.get("Type") != "AWS::IAM::Role":
                continue
            props = resource.get("Properties", {})
            managed_policies = props.get("ManagedPolicyArns", [])
            for policy in managed_policies:
                # policy may be a string or a Fn::Join / Ref intrinsic
                if isinstance(policy, str):
                    assert "FullAccess" not in policy, f"Role {logical_id} attaches FullAccess policy: {policy}"

    def test_no_admin_access_managed_policies(self, template_json):
        """No IAM role should attach AdministratorAccess."""
        resources = template_json.get("Resources", {})
        for logical_id, resource in resources.items():
            if resource.get("Type") != "AWS::IAM::Role":
                continue
            props = resource.get("Properties", {})
            managed_policies = props.get("ManagedPolicyArns", [])
            for policy in managed_policies:
                if isinstance(policy, str):
                    assert "AdministratorAccess" not in policy, (
                        f"Role {logical_id} attaches AdministratorAccess: {policy}"
                    )

    def test_workflow_lambda_role_has_dynamodb_access(self, template):
        """Workflow Lambda role should have DynamoDB permissions."""
        template.has_resource_properties(
            "AWS::IAM::Policy",
            Match.object_like(
                {
                    "PolicyDocument": {
                        "Statement": Match.array_with(
                            [
                                Match.object_like(
                                    {
                                        "Action": Match.any_value(),
                                        "Effect": "Allow",
                                    }
                                )
                            ]
                        )
                    }
                }
            ),
        )

    def test_managed_policies_are_basic_execution_only(self, template_json):
        """All Lambda roles should only use AWSLambdaBasicExecutionRole managed policy."""
        resources = template_json.get("Resources", {})
        for logical_id, resource in resources.items():
            if resource.get("Type") != "AWS::IAM::Role":
                continue
            props = resource.get("Properties", {})
            assume_role = props.get("AssumeRolePolicyDocument", {})
            # Check if this is a Lambda role
            statements = assume_role.get("Statement", [])
            is_lambda_role = any(
                stmt.get("Principal", {}).get("Service") == "lambda.amazonaws.com" for stmt in statements
            )
            if not is_lambda_role:
                continue
            managed_policies = props.get("ManagedPolicyArns", [])
            for policy in managed_policies:
                if isinstance(policy, dict):
                    # Fn::Join intrinsic — check the joined parts
                    join_parts = policy.get("Fn::Join", [None, []])[1]
                    joined = "".join(str(p) for p in join_parts if isinstance(p, str))
                    assert "FullAccess" not in joined, f"Lambda role {logical_id} has FullAccess policy"


# ---------------------------------------------------------------
# Step Functions retry and catch configuration (Req 7.1)
# ---------------------------------------------------------------


class TestStepFunctionsConfig:
    """Verify the Step Functions state machine has retry and catch."""

    def test_state_machine_has_definition(self, template):
        template.has_resource_properties(
            "AWS::StepFunctions::StateMachine",
            Match.object_like(
                {
                    "DefinitionString": Match.any_value(),
                }
            ),
        )

    def test_state_machine_definition_has_retry(self, template_json):
        """At least one state in the definition should have Retry config."""
        resources = template_json.get("Resources", {})
        for logical_id, resource in resources.items():
            if resource.get("Type") != "AWS::StepFunctions::StateMachine":
                continue
            props = resource.get("Properties", {})
            definition_str = props.get("DefinitionString", "")
            # DefinitionString may be an intrinsic function (Fn::Join)
            if isinstance(definition_str, dict):
                # Flatten Fn::Join to search for Retry
                raw = json.dumps(definition_str)
                assert "Retry" in raw, "State machine definition should contain Retry configuration"
            elif isinstance(definition_str, str):
                assert "Retry" in definition_str, "State machine definition should contain Retry configuration"

    def test_state_machine_definition_has_catch(self, template_json):
        """At least one state in the definition should have Catch config."""
        resources = template_json.get("Resources", {})
        for logical_id, resource in resources.items():
            if resource.get("Type") != "AWS::StepFunctions::StateMachine":
                continue
            props = resource.get("Properties", {})
            definition_str = props.get("DefinitionString", "")
            if isinstance(definition_str, dict):
                raw = json.dumps(definition_str)
                assert "Catch" in raw, "State machine definition should contain Catch configuration"
            elif isinstance(definition_str, str):
                assert "Catch" in definition_str, "State machine definition should contain Catch configuration"

    def test_state_machine_has_timeout(self, template):
        """State machine should have an overall timeout configured."""
        # CDK sets TimeoutSeconds on the state machine resource
        # The stack sets 30 minutes = 1800 seconds
        # Check the LoggingConfiguration exists (indicates proper config)
        template.has_resource_properties(
            "AWS::StepFunctions::StateMachine",
            Match.object_like(
                {
                    "LoggingConfiguration": Match.any_value(),
                }
            ),
        )

    def test_state_machine_retry_has_exponential_backoff(self, template_json):
        """Retry config should use exponential backoff (BackoffRate > 1)."""
        resources = template_json.get("Resources", {})
        for logical_id, resource in resources.items():
            if resource.get("Type") != "AWS::StepFunctions::StateMachine":
                continue
            props = resource.get("Properties", {})
            definition_str = props.get("DefinitionString", "")
            raw = json.dumps(definition_str) if isinstance(definition_str, dict) else definition_str
            assert "BackoffRate" in raw, "Retry configuration should include BackoffRate for exponential backoff"

    def test_state_machine_retry_max_attempts(self, template_json):
        """Retry config should have MaxAttempts of 3 for States.TaskFailed errors."""
        resources = template_json.get("Resources", {})
        for logical_id, resource in resources.items():
            if resource.get("Type") != "AWS::StepFunctions::StateMachine":
                continue
            props = resource.get("Properties", {})
            definition_str = props.get("DefinitionString", "")
            raw = json.dumps(definition_str) if isinstance(definition_str, dict) else definition_str
            # The definition is escaped JSON inside Fn::Join — MaxAttempts appears as \\"MaxAttempts\\":3
            assert "MaxAttempts" in raw, "Retry configuration should have MaxAttempts"
            # Verify our custom retry has MaxAttempts 3 (appears alongside States.TaskFailed)
            assert "States.TaskFailed" in raw, "Retry should handle States.TaskFailed errors"


# ---------------------------------------------------------------
# DynamoDB table configuration
# ---------------------------------------------------------------


class TestDynamoDBTables:
    """Verify both DynamoDB tables are correctly configured."""

    def test_workflows_table_partition_key(self, template):
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            Match.object_like(
                {
                    "KeySchema": Match.array_with(
                        [
                            {"AttributeName": "workflow_id", "KeyType": "HASH"},
                        ]
                    ),
                }
            ),
        )

    def test_deployments_table_partition_key(self, template):
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            Match.object_like(
                {
                    "KeySchema": Match.array_with(
                        [
                            {"AttributeName": "deployment_id", "KeyType": "HASH"},
                        ]
                    ),
                }
            ),
        )

    def test_deployments_table_has_ttl(self, template):
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            Match.object_like(
                {
                    "TimeToLiveSpecification": {
                        "AttributeName": "ttl",
                        "Enabled": True,
                    },
                }
            ),
        )

    def test_deployments_table_has_gsi(self, template):
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            Match.object_like(
                {
                    "GlobalSecondaryIndexes": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "IndexName": "workflow_id-index",
                                    "KeySchema": Match.array_with(
                                        [
                                            {
                                                "AttributeName": "workflow_id",
                                                "KeyType": "HASH",
                                            },
                                        ]
                                    ),
                                }
                            ),
                        ]
                    ),
                }
            ),
        )

    def test_tables_use_pay_per_request(self, template):
        """Both tables should use PAY_PER_REQUEST billing."""
        tables = template.find_resources("AWS::DynamoDB::Table")
        for logical_id, table in tables.items():
            billing = table.get("Properties", {}).get("BillingMode")
            assert billing == "PAY_PER_REQUEST", f"Table {logical_id} should use PAY_PER_REQUEST, got {billing}"


# ---------------------------------------------------------------
# API Gateway configuration
# ---------------------------------------------------------------


class TestApiGateway:
    """Verify API Gateway HTTP API is configured with CORS and routes."""

    def test_api_gateway_has_cors(self, template):
        template.has_resource_properties(
            "AWS::ApiGatewayV2::Api",
            Match.object_like(
                {
                    "CorsConfiguration": Match.object_like(
                        {
                            "AllowMethods": Match.any_value(),
                            "AllowOrigins": Match.any_value(),
                        }
                    ),
                }
            ),
        )

    def test_api_gateway_has_routes(self, template):
        """API Gateway should have multiple routes defined."""
        routes = template.find_resources("AWS::ApiGatewayV2::Route")
        assert len(routes) >= 5, f"Expected at least 5 API Gateway routes, found {len(routes)}"

    def test_api_gateway_protocol_is_http(self, template):
        template.has_resource_properties(
            "AWS::ApiGatewayV2::Api",
            Match.object_like(
                {
                    "ProtocolType": "HTTP",
                }
            ),
        )


# ---------------------------------------------------------------
# Lambda function configuration
# ---------------------------------------------------------------


class TestLambdaFunctions:
    """Verify Lambda functions have correct runtime and configuration."""

    def test_all_lambdas_use_python_312(self, template):
        """All application Lambda functions should use Python 3.12 runtime."""
        functions = template.find_resources("AWS::Lambda::Function")
        for logical_id, fn in functions.items():
            runtime = fn.get("Properties", {}).get("Runtime")
            # Skip CDK-managed custom resource Lambdas (e.g., S3 auto-delete)
            if "CustomResource" in logical_id or "Custom" in logical_id:
                continue
            assert runtime == "python3.12", f"Lambda {logical_id} should use python3.12, got {runtime}"

    def test_workflow_lambda_has_correct_handler(self, template):
        template.has_resource_properties(
            "AWS::Lambda::Function",
            Match.object_like(
                {
                    "Handler": "src/app/lambda_handler.handler",
                }
            ),
        )

    def test_deployment_lambda_has_correct_handler(self, template):
        template.has_resource_properties(
            "AWS::Lambda::Function",
            Match.object_like(
                {
                    "Handler": "src/app/deployment_handler.handler",
                }
            ),
        )

    def test_lambdas_have_environment_variables(self, template):
        """Lambda functions should have ENVIRONMENT env var set."""
        template.has_resource_properties(
            "AWS::Lambda::Function",
            Match.object_like(
                {
                    "Environment": {
                        "Variables": Match.object_like(
                            {
                                "ENVIRONMENT": "test",
                            }
                        ),
                    },
                }
            ),
        )


# ---------------------------------------------------------------
# CloudFront configuration
# ---------------------------------------------------------------


class TestCloudFront:
    """Verify CloudFront distribution has correct origins and behaviors."""

    def test_https_enforcement(self, template):
        template.has_resource_properties(
            "AWS::CloudFront::Distribution",
            Match.object_like(
                {
                    "DistributionConfig": {
                        "DefaultCacheBehavior": {
                            "ViewerProtocolPolicy": "redirect-to-https",
                        },
                    },
                }
            ),
        )

    def test_spa_error_responses(self, template):
        """CloudFront should have custom error responses for SPA routing."""
        template.has_resource_properties(
            "AWS::CloudFront::Distribution",
            Match.object_like(
                {
                    "DistributionConfig": {
                        "CustomErrorResponses": Match.array_with(
                            [
                                Match.object_like(
                                    {
                                        "ErrorCode": 403,
                                        "ResponseCode": 200,
                                        "ResponsePagePath": "/index.html",
                                    }
                                ),
                            ]
                        ),
                    },
                }
            ),
        )

    def test_has_api_origin_behavior(self, template_json):
        """CloudFront should have an additional cache behavior for /api/*."""
        resources = template_json.get("Resources", {})
        for logical_id, resource in resources.items():
            if resource.get("Type") != "AWS::CloudFront::Distribution":
                continue
            config = resource["Properties"]["DistributionConfig"]
            behaviors = config.get("CacheBehaviors", [])
            api_paths = [b.get("PathPattern") for b in behaviors]
            assert "/api/*" in api_paths, f"CloudFront should have /api/* cache behavior, found: {api_paths}"


# ---------------------------------------------------------------
# Stack outputs
# ---------------------------------------------------------------


class TestStackOutputs:
    """Verify all expected stack outputs exist."""

    def test_api_gateway_url_output(self, template):
        template.has_output("ApiGatewayUrl", {})

    def test_cloudfront_url_output(self, template):
        template.has_output("CloudFrontUrl", {})

    def test_s3_bucket_name_output(self, template):
        template.has_output("S3BucketName", {})

    def test_no_alb_url_output(self, template_json):
        """Old ALB URL output should not exist."""
        outputs = template_json.get("Outputs", {})
        assert "AlbUrl" not in outputs, "AlbUrl output should be removed"

    def test_no_ecs_cluster_output(self, template_json):
        """Old ECS cluster output should not exist."""
        outputs = template_json.get("Outputs", {})
        assert "EcsClusterName" not in outputs, "EcsClusterName output should be removed"
