"""S3 frontend bucket + CloudFront distribution + WAF."""

import aws_cdk as cdk
from aws_cdk import Duration
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_wafv2 as wafv2

from .config import PlatformConfig


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


def build_waf_web_acl(stack: cdk.Stack, cfg: PlatformConfig) -> wafv2.CfnWebACL:
    """Create WAFv2 WebACL for CloudFront."""
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
    # S3 origin for frontend (OAC — recommended over legacy OAI)
    s3_origin = origins.S3BucketOrigin.with_origin_access_control(
        bucket,
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
        response_headers_policy_name=f"{cfg.project}-{cfg.env}-security-headers",
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
        web_acl_id=web_acl.attr_arn,
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
