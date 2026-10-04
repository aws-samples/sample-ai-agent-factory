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

from tests.iam_attachment import statements_by_role


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


def _statements_for_role_prefix(template_json: dict, prefix: str) -> list[dict]:
    """Collect inline and overflow-policy statements attached to one role."""
    resources = template_json["Resources"]
    role_ids = [
        logical_id
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::IAM::Role" and logical_id.startswith(prefix)
    ]
    assert len(role_ids) == 1, f"expected one {prefix} role, found {role_ids}"
    role_id = role_ids[0]

    statements: list[dict] = []
    for policy in resources[role_id]["Properties"].get("Policies", []) or []:
        statements.extend(policy.get("PolicyDocument", {}).get("Statement", []) or [])
    for resource in resources.values():
        if resource["Type"] not in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            continue
        roles = resource["Properties"].get("Roles", []) or []
        if any(isinstance(role, dict) and role.get("Ref") == role_id for role in roles):
            statements.extend(resource["Properties"].get("PolicyDocument", {}).get("Statement", []) or [])
    return statements


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

    def test_has_dynamodb_tables(self, template):
        # Core tables (workflows, deployments) plus governance/feature tables
        # (approvals, cost, evaluations, registry, tags, triggers, ...). Exact
        # count so an accidental table addition/removal is caught in review.
        # 15th: gateway-name-claims (F-64), the atomic claim on a gateway name.
        template.resource_count_is("AWS::DynamoDB::Table", 15)

    def test_has_s3_buckets(self, template):
        template.resource_count_is("AWS::S3::Bucket", 3)

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

    def test_the_only_vpc_is_the_tool_sandbox(self, template_json):
        """Was ``resource_count_is("AWS::EC2::VPC", 0)``, and that became wrong.

        The intent of this class is "the old ECS/ALB/VPC application architecture is
        gone", and it still holds -- there is no cluster, no service, no load
        balancer, no NAT gateway. What changed is that the tool-test sandbox
        deliberately introduced one isolated VPC to run model-written code in.

        Rewritten rather than deleted or relaxed to ``>= 0``, because the original
        assertion was doing real work: it is what would notice the app architecture
        creeping back. Naming the one permitted VPC keeps that, and makes a second
        VPC fail here instead of passing silently.
        """
        vpcs = {lid for lid, r in template_json["Resources"].items() if r["Type"] == "AWS::EC2::VPC"}
        assert len(vpcs) == 1, f"expected exactly one VPC (the tool sandbox), found {sorted(vpcs)}"
        assert next(iter(vpcs)).startswith("ToolSandboxVpc"), f"the one VPC is not the tool sandbox: {sorted(vpcs)}"

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

    def test_every_subnet_belongs_to_the_tool_sandbox_and_is_isolated(self, template_json):
        """The old ALB needed public subnets; the sandbox must have none.

        This is the assertion that actually matters after the rewrite above. A
        ``PUBLIC`` subnet in the sandbox VPC would create an internet gateway and give
        model-written code a route off the VPC, and the blanket count-of-zero this
        replaces could not distinguish that from the isolated pair we do want.
        """
        subnets = {
            lid: r["Properties"] for lid, r in template_json["Resources"].items() if r["Type"] == "AWS::EC2::Subnet"
        }
        assert len(subnets) == 2, f"expected the sandbox's two isolated subnets, found {sorted(subnets)}"
        for lid, props in subnets.items():
            assert lid.startswith("ToolSandboxVpc"), f"{lid} is not part of the tool sandbox VPC"
            assert not props.get("MapPublicIpOnLaunch"), f"{lid} auto-assigns a public IP"

    def test_every_security_group_belongs_to_the_tool_sandbox(self, template_json):
        """Two groups: the sandbox itself, and the interface endpoint it may reach.

        ``tests/test_tool_sandbox_network.py`` asserts what the rules are. This only
        asserts that no *other* component has quietly acquired a security group,
        which is what the count-of-zero used to cover.
        """
        groups = {lid for lid, r in template_json["Resources"].items() if r["Type"] == "AWS::EC2::SecurityGroup"}
        assert len(groups) == 2, f"expected exactly the two tool-sandbox security groups, found {sorted(groups)}"
        assert all(lid.startswith("ToolSandbox") for lid in groups), (
            f"a non-sandbox security group exists: {sorted(groups)}"
        )


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

    @pytest.mark.parametrize(
        "role_prefix",
        ["DeploymentLambdaRole", "StreamLambdaRole"],
    )
    def test_cross_account_invoke_roles_assume_only_the_fixed_target_role(
        self,
        template_json,
        role_prefix,
    ):
        statements = _statements_for_role_prefix(template_json, role_prefix)
        assume_role = []
        for statement in statements:
            actions = statement.get("Action", [])
            actions = [actions] if isinstance(actions, str) else actions
            if "sts:AssumeRole" in actions:
                assume_role.append(statement)

        assert assume_role == [
            {
                "Action": "sts:AssumeRole",
                "Effect": "Allow",
                "Resource": ("arn:aws:iam::*:role/AgentCoreFlowsDeploymentRole"),
            }
        ]


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
        for resource in resources.values():
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
        for resource in resources.values():
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
        for resource in resources.values():
            if resource.get("Type") != "AWS::StepFunctions::StateMachine":
                continue
            props = resource.get("Properties", {})
            definition_str = props.get("DefinitionString", "")
            raw = json.dumps(definition_str) if isinstance(definition_str, dict) else definition_str
            assert "BackoffRate" in raw, "Retry configuration should include BackoffRate for exponential backoff"

    def test_state_machine_retry_max_attempts(self, template_json):
        """Retry only transient infra errors — NEVER the States.TaskFailed wildcard.

        Bug 134: retrying States.TaskFailed masked deterministic handler errors
        (a Cedar-validation failure could "succeed" on a lucky retry). The stack
        now retries only Lambda service/throttle/timeout errors.
        """
        resources = template_json.get("Resources", {})
        for resource in resources.values():
            if resource.get("Type") != "AWS::StepFunctions::StateMachine":
                continue
            props = resource.get("Properties", {})
            definition_str = props.get("DefinitionString", "")
            raw = json.dumps(definition_str) if isinstance(definition_str, dict) else definition_str
            # The definition is escaped JSON inside Fn::Join — MaxAttempts appears as \\"MaxAttempts\\":3
            assert "MaxAttempts" in raw, "Retry configuration should have MaxAttempts"
            assert "Lambda.TooManyRequestsException" in raw, "Retry should cover transient Lambda throttling"
            assert "States.TaskFailed" not in raw, (
                "States.TaskFailed must NOT be retried — the wildcard masks deterministic handler errors (Bug 134)"
            )


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

    def test_api_gateway_exposes_the_deployer_target_catalog(self, template_json):
        route_keys = {
            route.get("Properties", {}).get("RouteKey")
            for route in template_json["Resources"].values()
            if route.get("Type") == "AWS::ApiGatewayV2::Route"
        }
        assert "GET /api/deploy-targets" in route_keys

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

    def test_all_lambdas_use_python_312(self, template, template_json):
        """All application Lambda functions should use Python 3.12 runtime."""
        functions = template.find_resources("AWS::Lambda::Function")
        custom_resource_providers = set()
        for resource in template_json["Resources"].values():
            resource_type = resource["Type"]
            if not (resource_type.startswith("Custom::") or resource_type == "AWS::CloudFormation::CustomResource"):
                continue
            service_token = resource.get("Properties", {}).get("ServiceToken")
            if isinstance(service_token, dict) and "Fn::GetAtt" in service_token:
                custom_resource_providers.add(service_token["Fn::GetAtt"][0])

        for logical_id, fn in functions.items():
            runtime = fn.get("Properties", {}).get("Runtime")
            # CDK-managed providers choose their own runtime. Derive them from the
            # custom resources' ServiceToken references rather than relying on
            # generated logical-id spelling.
            if logical_id in custom_resource_providers:
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

    def test_spa_routing_without_error_response_masking(self, template, template_json):
        """SPA deep links are handled by a CloudFront Function, NOT error responses.

        Bug 138: distribution-wide CustomErrorResponses (404→/index.html) also
        rewrote /api/* 4xx into 200 text/html pages, breaking the frontend's
        404→empty-state logic. The stack must use a viewer-request CloudFront
        Function on the default (S3) behavior instead.
        """
        template.resource_count_is("AWS::CloudFront::Function", 1)
        for resource in template_json.get("Resources", {}).values():
            if resource.get("Type") != "AWS::CloudFront::Distribution":
                continue
            config = resource.get("Properties", {}).get("DistributionConfig", {})
            assert "CustomErrorResponses" not in config, (
                "CustomErrorResponses must NOT be set — they re-mask /api/* 4xx (Bug 138)"
            )
            associations = config.get("DefaultCacheBehavior", {}).get("FunctionAssociations", [])
            assert any(a.get("EventType") == "viewer-request" for a in associations), (
                "Default behavior should attach the SPA router function on viewer-request"
            )

    def test_has_api_origin_behavior(self, template_json):
        """CloudFront should have an additional cache behavior for /api/*."""
        resources = template_json.get("Resources", {})
        for resource in resources.values():
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


# ---------------------------------------------------------------
# Step-lambda IAM grants
# ---------------------------------------------------------------


class TestStepLambdaCognitoGrants:
    """Guard the CreateUserPool/TagResource pairing in the gateway step role."""

    def test_create_user_pool_is_paired_with_tag_resource(self, template_json):
        """Any role allowed to CreateUserPool must also be allowed TagResource.

        create_gateway_cognito_auth passes UserPoolTags, and Cognito authorizes
        that as a separate cognito-idp:TagResource check against the not-yet-
        created pool. Without the pairing the whole CreateUserPool call fails
        with AccessDeniedException — the tags are not silently dropped — so the
        gateway deploy dies at the first step.
        """
        # Paired by ROLE, not by policy document: CDK spills statements past the inline
        # size limit into <Role>OverflowPolicy<N>, so a role can hold the two halves in
        # different documents (tests/iam_attachment.py).
        offenders = []
        holders = []
        for role_id, statements in statements_by_role(template_json).items():
            granted = set()
            for _source, statement in statements:
                if statement.get("Effect") != "Allow":
                    continue
                action = statement.get("Action", [])
                granted.update([action] if isinstance(action, str) else action)
            if "cognito-idp:CreateUserPool" in granted:
                holders.append(role_id)
                if "cognito-idp:TagResource" not in granted:
                    offenders.append(role_id)

        assert holders, "no role grants cognito-idp:CreateUserPool; the pairing check would pass vacuously"
        assert not offenders, (
            f"these roles grant cognito-idp:CreateUserPool without cognito-idp:TagResource: {offenders}"
        )


class TestPolicyPathCanCallTheGateway:
    """Creating a gateway-scoped Cedar policy requires calling the gateway.

    AgentCore resolves the gateway named in a Cedar statement AS THE CALLER, so a
    principal that creates such a policy needs bedrock-agentcore:InvokeGateway on
    the gateway ARN. Without it create_policy ends CREATE_FAILED "Insufficient
    permissions to call gateway with ID <id>" in BOTH validation modes — proven
    live on the customer-export path, which had the identical gap.

    Worse here than there: policy_step classifies that message as transient, so a
    missing grant is retried six times and then attached in ENFORCE fail-closed,
    i.e. every tool denied, and handed to a promoter running as a principal that
    also lacked the action. A permanent deny-all logged as a race.

    Asserted on the SYNTHESIZED template, so it holds for what CloudFormation
    actually receives rather than for how the CDK source happens to be written.

    Grouped BY ROLE across both AWS::IAM::Policy and AWS::IAM::ManagedPolicy,
    which is not incidental: the deployment Lambda role has so many grants that
    the CDK spills them out of its inline document into a generated
    `...OverflowPolicy...` MANAGED policy. A check that walks AWS::IAM::Policy
    only reports that role as clean no matter what it holds — the first version of
    this test did exactly that and passed while the grant was deliberately broken.
    """

    @staticmethod
    def _roles_to_statements(template_json):
        """Map role logical id -> every Allow statement attached to it.

        Both document types, unioned per role, because an action and the action it
        must be paired with can land in different documents for the same role.
        """
        by_role = {}
        for resource in template_json.get("Resources", {}).values():
            if resource.get("Type") not in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
                continue
            props = resource["Properties"]
            for role in props.get("Roles", []) or []:
                logical_id = role.get("Ref") if isinstance(role, dict) else role
                for statement in props["PolicyDocument"]["Statement"]:
                    if statement.get("Effect") != "Allow":
                        continue
                    action = statement.get("Action", [])
                    actions = set([action] if isinstance(action, str) else action)
                    by_role.setdefault(logical_id, []).append((statement, actions))
        return by_role

    def test_every_policy_creating_role_can_invoke_the_gateway(self, template_json):
        offenders = []
        for role, statements in self._roles_to_statements(template_json).items():
            granted = set().union(*(actions for _s, actions in statements)) if statements else set()
            if "bedrock-agentcore:CreatePolicy" in granted and "bedrock-agentcore:InvokeGateway" not in granted:
                offenders.append(role)

        assert not offenders, (
            "these roles are granted bedrock-agentcore:CreatePolicy without "
            f"bedrock-agentcore:InvokeGateway: {offenders} — every gateway-scoped "
            "Cedar policy they try to create will end CREATE_FAILED, and the "
            "fail-closed engine will deny every tool until someone reads the IAM"
        )

    def test_that_grant_is_scoped_to_gateway_arns(self, template_json):
        """It is a DATA-plane verb: `*` would let a deploy-time Lambda call the
        tools of every gateway in the account.

        Only checked on roles that create policies. The shared agent runtime role
        also holds InvokeGateway on `*` — that is the agent calling its own
        gateway at request time, a different principal with a different
        justification, and it creates no policies.
        """
        unscoped = []
        for role, statements in self._roles_to_statements(template_json).items():
            granted = set().union(*(actions for _s, actions in statements)) if statements else set()
            if "bedrock-agentcore:CreatePolicy" not in granted:
                continue
            for statement, actions in statements:
                if "bedrock-agentcore:InvokeGateway" not in actions:
                    continue
                resources = statement.get("Resource", [])
                resources = [resources] if isinstance(resources, str) else resources
                if not all(isinstance(r, str) and ":gateway/" in r for r in resources):
                    unscoped.append((role, resources))

        assert not unscoped, (
            f"InvokeGateway is granted on non-gateway resources in {unscoped} — "
            "the ARN prefix is knowable at synth time, so a data-plane invoke verb "
            'must not ride along on the control-plane resources=["*"] statement'
        )
