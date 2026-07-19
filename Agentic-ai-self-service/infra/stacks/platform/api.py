"""API Gateway HTTP API — routes, JWT authorizer, throttling, CORS."""

import aws_cdk as cdk
from aws_cdk import Duration
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as apigw_authorizers
from aws_cdk import aws_apigatewayv2_integrations as apigw_integrations
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_stepfunctions as sfn

from .config import PlatformConfig


def build_api_gateway(
    stack: cdk.Stack,
    cfg: PlatformConfig,
    *,
    workflow_lambda: _lambda.Function,
    deployment_lambda: _lambda.Function,
    user_pool: cognito.UserPool,
    user_pool_client: cognito.UserPoolClient,
    state_machine: sfn.StateMachine,
) -> apigwv2.HttpApi:
    """Create API Gateway HTTP API with route mappings and CORS.

    Routes:
    - /api/workflows/* → Workflow Lambda
    - /api/deploy, /api/test-runtime, /api/runtime/* → Deployment Lambda
    - /health → Workflow Lambda

    Requirements: 1.1, 1.2, 1.3, 1.4, 1.5
    """
    # CORS origins: localhost for local development. CloudFront distribution
    # URL is added post-construction by add_cloudfront_cors_origin() since
    # the distribution is created after the API. Browsers send CORS preflight
    # for requests with Authorization headers even on same-origin via CloudFront.
    api = apigwv2.HttpApi(
        stack,
        "HttpApi",
        api_name=f"{cfg.project}-{cfg.env}-api",
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
    # scope_permission_to_route=False grants ONE broad lambda:InvokeFunction
    # permission per integration instead of one AWS::Lambda::Permission per
    # route. The deployment Lambda backs ~29 routes; per-route permissions
    # pushed its resource-based policy past the 20,480-byte hard limit
    # (deploy failed with "final policy size ... bigger than the limit").
    # A single wildcard-source-ARN permission is functionally equivalent
    # (API Gateway is still the only invoker) and keeps the policy tiny.
    workflow_integration = apigw_integrations.HttpLambdaIntegration(
        "WorkflowIntegration", workflow_lambda, scope_permission_to_route=False
    )

    # Deployment Lambda integration
    deployment_integration = apigw_integrations.HttpLambdaIntegration(
        "DeploymentIntegration", deployment_lambda, scope_permission_to_route=False
    )

    # JWT Authorizer (Cognito)
    jwt_authorizer = apigw_authorizers.HttpJwtAuthorizer(
        "CognitoAuthorizer",
        jwt_issuer=f"https://cognito-idp.{stack.region}.amazonaws.com/{user_pool.user_pool_id}",
        jwt_audience=[user_pool_client.user_pool_client_id],
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
        # POST added for /api/runtime/import (Loom-study 1.5 — adopt an
        # externally-built runtime by ARN).
        methods=[apigwv2.HttpMethod.DELETE, apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
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
    # Verified external MCP-server catalog (read-only). GET list + GET /{id}
    # detail on the deployment Lambda (routers/mcp_servers.py). Browsable in
    # the Registry UI alongside published agent blueprints.
    api.add_routes(
        path="/api/mcp-servers",
        methods=[apigwv2.HttpMethod.GET],
        integration=deployment_integration,
        authorizer=jwt_authorizer,
    )
    api.add_routes(
        path="/api/mcp-servers/{proxy+}",
        methods=[apigwv2.HttpMethod.GET],
        integration=deployment_integration,
        authorizer=jwt_authorizer,
    )
    # Phase 1 (Loom-study 1.2/1.3) — identity inspection + OBO dry-run.
    # GET /token-info + POST /test-obo on the deployment Lambda (routers/identity).
    api.add_routes(
        path="/api/identity/{proxy+}",
        methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
        integration=deployment_integration,
        authorizer=jwt_authorizer,
    )
    # Loom-study 1.6 — JIT IAM permission requests (create/list/approve/reject).
    api.add_routes(
        path="/api/permissions/{proxy+}",
        methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
        integration=deployment_integration,
        authorizer=jwt_authorizer,
    )
    # Loom-study 5.1 — live model catalog.
    api.add_routes(
        path="/api/models",
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
    # Phase 2 (Loom) governance tagging — routers/tags.py mounts
    # /api/settings/tags + /api/settings/tag-profiles on the deployment
    # Lambda. Bug 21 enumeration: HTTP API needs explicit routes per path.
    api.add_routes(
        path="/api/settings/{proxy+}",
        methods=[
            apigwv2.HttpMethod.GET,
            apigwv2.HttpMethod.POST,
            apigwv2.HttpMethod.DELETE,
        ],
        integration=deployment_integration,
        authorizer=jwt_authorizer,
    )
    # Phase 4 (Loom) FinOps — routers/cost.py budgets_router mounts
    # /api/cost/budgets on the deployment Lambda.
    api.add_routes(
        path="/api/cost/{proxy+}",
        methods=[
            apigwv2.HttpMethod.GET,
            apigwv2.HttpMethod.POST,
            apigwv2.HttpMethod.DELETE,
        ],
        integration=deployment_integration,
        authorizer=jwt_authorizer,
    )
    # Phase 5 (Loom) — admin audit dashboard (/api/admin/audit) + Phase 7
    # deployment-targets management (/api/admin/deploy-targets*, POST).
    api.add_routes(
        path="/api/admin/{proxy+}",
        methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
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
    deployment_lambda.add_environment("STATE_MACHINE_ARN", state_machine.state_machine_arn)

    return api


def add_cloudfront_cors_origin(api: apigwv2.HttpApi) -> None:
    """Widen API Gateway CORS to allow CloudFront origin.

    Cannot reference distribution.domain_name here — CloudFront depends on
    the API URL, so a back-reference creates a circular dependency.
    Token-based auth (Cognito JWT) means allow_origins=["*"] is safe:
    no ambient credentials (cookies) are sent cross-origin.
    """
    cfn_api = api.node.default_child
    if cfn_api:
        cfn_api.add_property_override(
            "CorsConfiguration.AllowOrigins",
            ["*"],
        )
