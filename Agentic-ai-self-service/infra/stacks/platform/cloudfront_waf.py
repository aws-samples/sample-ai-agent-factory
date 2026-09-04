"""S3 frontend bucket + CloudFront distribution + WAF."""

import aws_cdk as cdk
from aws_cdk import Duration
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_wafv2 as wafv2

from .config import PlatformConfig, is_home_region

# AWS only accepts scope=CLOUDFRONT WebACLs in us-east-1 (== config.HOME_REGION),
# and a CloudFront distribution can attach nothing else. So the WAF scope is a
# function of the deployment region, not a choice — see build_waf_web_acl.


def build_frontend_bucket(stack: cdk.Stack, cfg: PlatformConfig, logging_bucket: s3.Bucket) -> s3.Bucket:
    """Create S3 bucket for frontend static assets.

    Requirements: 7.1
    """
    return s3.Bucket(
        stack,
        "FrontendBucket",
        bucket_name=f"{cfg.project}-{cfg.env}-frontend-{stack.region}-{stack.account}",
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        removal_policy=cfg.removal_policy,
        auto_delete_objects=cfg.allow_destroy,
        encryption=s3.BucketEncryption.S3_MANAGED,
        server_access_logs_bucket=logging_bucket,
        server_access_logs_prefix="s3-frontend/",
        lifecycle_rules=[
            s3.LifecycleRule(
                noncurrent_version_expiration=Duration.days(30),
            ),
        ],
    )


def _build_waf_rules(name_prefix: str) -> list:
    """Common WAF rule set, scope-agnostic — used by the CLOUDFRONT-scoped ACL
    in us-east-1 and by the REGIONAL ACL everywhere else. Includes Common +
    KnownBadInputs managed rule sets plus an IP-based rate limit.
    See tasks/lessons.md Bug 41.
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


def build_waf_web_acl(
    stack: cdk.Stack,
    cfg: PlatformConfig,
    *,
    user_pool: cognito.UserPool | None = None,
) -> wafv2.CfnWebACL:
    """Create the WAFv2 WebACL, with a scope determined by the deploy region.

    Two AWS constraints, neither of them ours, decide the shape here:

    1. A CloudFront distribution accepts ONLY a ``scope=CLOUDFRONT`` WebACL,
       and those can only be created in us-east-1.
    2. WAFv2 does not support API Gateway **HTTP** APIs (v2) — only REST (v1).
       We tried and reverted the regional API-stage ACL; see the note below
       and tasks/lessons.md Bug 41 (revised).

    So:

    * **us-east-1** — unchanged from the original behaviour. A CLOUDFRONT ACL,
      attached to the distribution in ``build_cloudfront_distribution``.
    * **any other region** (e.g. eu-central-1) — a REGIONAL ACL created *in the
      deployment region*, associated with the Cognito user pool, which is the
      only WAF-attachable resource in this architecture. The distribution gets
      no WebACL unless ``cloudfront_web_acl_arn`` context supplies a
      pre-existing us-east-1 one.
    """
    if is_home_region(stack):
        # Construct id and name are load-bearing: the stack is deployed live in
        # us-east-1 and a changed logical ID would replace the WebACL.
        return wafv2.CfnWebACL(
            stack,
            "CloudFrontWebACL",
            name=f"{cfg.project}-{cfg.env}-cloudfront-waf",
            scope="CLOUDFRONT",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=f"{cfg.project}-{cfg.env}-waf",
                sampled_requests_enabled=True,
            ),
            rules=_build_waf_rules(f"{cfg.project}-{cfg.env}"),
        )

    web_acl = wafv2.CfnWebACL(
        stack,
        "RegionalWebACL",
        name=f"{cfg.project}-{cfg.env}-regional-waf",
        scope="REGIONAL",
        default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
        visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
            cloud_watch_metrics_enabled=True,
            metric_name=f"{cfg.project}-{cfg.env}-regional-waf",
            sampled_requests_enabled=True,
        ),
        rules=_build_waf_rules(f"{cfg.project}-{cfg.env}"),
    )

    # Cognito user pools accept REGIONAL WebACLs. Associating here protects the
    # sign-in surface (credential stuffing, known-bad inputs, rate limiting) in
    # the deployment region, which is the closest available equivalent to the
    # CloudFront ACL that us-east-1 gets.
    if user_pool is not None:
        wafv2.CfnWebACLAssociation(
            stack,
            "UserPoolWebACLAssociation",
            resource_arn=user_pool.user_pool_arn,
            web_acl_arn=web_acl.attr_arn,
        )

    return web_acl


def resolve_cloudfront_web_acl_arn(stack: cdk.Stack, web_acl: wafv2.CfnWebACL) -> str | None:
    """The ARN to attach to the distribution, or None if there is none to attach.

    In us-east-1 this is the stack's own CLOUDFRONT ACL. Elsewhere the stack's
    ACL is REGIONAL and CloudFront would reject it, so we fall back to an
    operator-supplied ARN from the ``cloudfront_web_acl_arn`` context key —
    letting a customer who wants CloudFront-edge filtering create that ACL in
    us-east-1 themselves and point this stack at it.
    """
    if is_home_region(stack):
        return web_acl.attr_arn
    supplied = stack.node.try_get_context("cloudfront_web_acl_arn")
    return str(supplied) if supplied else None


# Removed: _create_api_waf_and_attach. WAFv2 does not support API Gateway
# HTTP APIs (only REST APIs); the resource type RESOURCE_ARN was rejected.
# See tasks/lessons.md Bug 41 (revised).


def build_cloudfront_distribution(
    stack: cdk.Stack,
    cfg: PlatformConfig,
    *,
    bucket: s3.Bucket,
    api: apigwv2.HttpApi,
    web_acl: wafv2.CfnWebACL,
    logging_bucket: s3.Bucket,
) -> cloudfront.Distribution:
    """Create CloudFront distribution with S3 + API Gateway origins.

    - /* → S3 (frontend)
    - /api/* → API Gateway
    - /health → API Gateway

    Requirements: 7.2, 7.3
    """
    # S3 origin for frontend (OAC — recommended over legacy OAI).
    #
    # CDK's auto-generated OAC name is derived from the stack name + construct
    # path only — it carries NO region — so two regional deployments in one
    # account would request the identical account-global OAC name. us-east-1
    # keeps the generated name (pinning one there would replace the live OAC);
    # every other region supplies an explicit region-qualified name.
    if is_home_region(stack):
        s3_origin = origins.S3BucketOrigin.with_origin_access_control(bucket)
    else:
        s3_origin = origins.S3BucketOrigin.with_origin_access_control(
            bucket,
            origin_access_control=cloudfront.S3OriginAccessControl(
                stack,
                "FrontendOriginAccessControl",
                origin_access_control_name=cfg.global_resource_name(stack, "frontend-oac"),
            ),
        )

    # API Gateway origin — extract domain from the API URL
    # API URL format: https://{api-id}.execute-api.{region}.amazonaws.com/
    api_domain = cdk.Fn.select(2, cdk.Fn.split("/", api.url or ""))
    api_origin = origins.HttpOrigin(
        domain_name=api_domain,
        protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
    )

    # Security response headers (HSTS, X-Frame-Options, X-Content-Type-Options, etc.)
    # CSP added 2026-05-16 — see tasks/lessons.md Bug 39 (security audit).
    security_headers = cloudfront.ResponseHeadersPolicy(
        stack,
        "SecurityHeadersPolicy",
        response_headers_policy_name=cfg.global_resource_name(stack, "security-headers"),
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
                    f"connect-src 'self' https://*.amazoncognito.com https://cognito-idp.{stack.region}.amazonaws.com; "
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
    # NOTE: CloudFront Function names are account-global, but CDK's
    # auto-generated name already embeds the region (e.g.
    # "us-east-1agentcore-workflpaRouterFunctionD46F5396"), so two regional
    # deployments do not collide here and no explicit name is needed.
    spa_router_fn = cloudfront.Function(
        stack,
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
        stack,
        "FrontendDistribution",
        comment=f"{cfg.project}-{cfg.env} distribution",
        default_root_object="index.html",
        minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
        # None outside us-east-1 unless an operator supplied a CLOUDFRONT-scoped
        # ACL ARN — CloudFront rejects the REGIONAL ACL this stack creates there.
        web_acl_id=resolve_cloudfront_web_acl_arn(stack, web_acl),
        log_bucket=logging_bucket,
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
