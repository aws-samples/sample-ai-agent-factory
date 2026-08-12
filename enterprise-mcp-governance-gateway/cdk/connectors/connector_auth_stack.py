# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Connector Authorize stack — the shared, provider-generic per-user 3LO consent SPA.

Problem it solves: MCP clients (Kiro today) don't complete AgentCore's outbound
3LO consent (the ``-32042`` URL elicitation + ``CompleteResourceTokenAuth``). This
stack hosts a tiny browser SPA that does it out-of-band, ONCE per user per provider:

    user opens  https://<cloudfront>/auth?provider=atlassian
      → Cognito Hosted UI login (the gateway's OWN user pool; federatable to Entra/Okta)
      → SPA probes a tool via the gateway /mcp (CORS-allowed) → -32042 authorization URL
      → provider consent in the browser
      → /callback: Cognito Identity Pool → temp AWS creds → CompleteResourceTokenAuth
      → token vaulted for that user; Kiro (same user) now just works.

Design choices (see the connectors README):
  * Reuses the GATEWAY's Cognito user pool (imported by id from SSM) so the vaulted
    token binds to the SAME user identity Kiro authenticates as — NOT a new pool.
  * The gateway's workload identity is SERVICE-LINKED, so the browser cannot
    self-drive GetResourceOauth2Token ("cannot retrieve an access token by the
    caller"). Instead the SPA probes the gateway /mcp (CORS is open: allow-origin
    ``*``, allow-headers authorization/content-type/mcp-protocol-version) to get the
    ``-32042`` URL, and completes with CompleteResourceTokenAuth from the browser —
    the proven path (authorize.py + the enterprise-mcp-demo).
  * Provider-generic: one SPA + Identity Pool + role serve every connector. A new
    connector (GitHub, Slack, …) is just another entry in the SPA ``config.json``
    providers map + its own credential provider + Cedar policy. Fits the
    ``mcp-connector-*`` convention already used by the gateway role grant.

The JWT never touches any server: the browser holds it and exchanges it via the
Identity Pool for scoped temp creds. This stack stores NO secrets.
"""
from pathlib import Path

from constructs import Construct

from aws_cdk import (
    CfnOutput,
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_s3 as s3,
    aws_ssm as ssm,
    custom_resources as cr,
)

from connectors._bundling import bundled_asset

_HERE = Path(__file__).resolve().parent


class ConnectorAuthStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── Import the gateway's identity + workload from SSM ────────────────────
        # Published by the core gateway stack under /enterprise-mcp-gateway/*.
        pool_id = ssm.StringParameter.value_for_string_parameter(
            self, "/enterprise-mcp-gateway/cognito/pool-id")
        gateway_id = ssm.StringParameter.value_for_string_parameter(
            self, "/enterprise-mcp-gateway/gateway/id")
        # The gateway's auto-created workload identity name == the gateway id; the
        # SPA passes this to GetWorkloadAccessTokenForUserId / GetResourceOauth2Token.
        workload_name = gateway_id

        user_pool = cognito.UserPool.from_user_pool_id(self, "GatewayUserPool", pool_id)

        # ── Static SPA bucket + CloudFront (OAC, TLS only) ───────────────────────
        site_bucket = s3.Bucket(
            self,
            "AuthSiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        distribution = cloudfront.Distribution(
            self,
            "AuthSiteDistribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            comment="Connector Authorize SPA (per-user 3LO consent)",
        )
        site_url = f"https://{distribution.distribution_domain_name}"
        # Two DISTINCT redirect targets:
        #  - site_url         : where Cognito Hosted UI returns after LOGIN (SPA root).
        #  - return_url       : where AgentCore returns after PROVIDER CONSENT
        #                       (callback.html); must be allow-listed on the gateway
        #                       workload identity (custom resource below).
        return_url = f"{site_url}/callback.html"

        # ── Hosted UI on the gateway pool: domain + public PKCE client ───────────
        # A user pool has ONE cognito prefix domain; this assumes the gateway pool
        # has none yet (the gateway uses ADMIN_USER_PASSWORD_AUTH, no Hosted UI).
        hosted_ui = user_pool.add_domain(
            "ConnectorAuthHostedUi",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"mcp-connector-auth-{self.account[-6:]}",
            ),
        )
        web_client = user_pool.add_client(
            "ConnectorAuthWebClient",
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL,
                        cognito.OAuthScope.PROFILE,
                        # The gateway requires an MCP-authorizing scope on the access
                        # token. The admin-flow token Kiro uses carries
                        # aws.cognito.signin.user.admin; a Hosted-UI token has none of
                        # that, so tools/call → -32002 insufficient_scope. Request the
                        # gateway pool's resource-server scope instead ("mcp-gateway"
                        # resource server, "read" scope) — matches the enterprise-mcp-demo.
                        cognito.OAuthScope.custom("mcp-gateway/read")],
                # Cognito Hosted UI login returns to the SPA root; localhost for dev.
                callback_urls=[site_url, "http://localhost:5173"],
                logout_urls=[site_url, "http://localhost:5173"],
            ),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
            generate_secret=False,  # public SPA client (PKCE), no secret in the browser
        )

        # ── Cognito Identity Pool → temp AWS creds for the browser ───────────────
        identity_pool = cognito.CfnIdentityPool(
            self,
            "ConnectorAuthIdentityPool",
            identity_pool_name="mcp-connector-auth-identity-pool",
            allow_unauthenticated_identities=False,
            cognito_identity_providers=[
                cognito.CfnIdentityPool.CognitoIdentityProviderProperty(
                    client_id=web_client.user_pool_client_id,
                    provider_name=f"cognito-idp.{self.region}.amazonaws.com/{pool_id}",
                )
            ],
        )

        # ── AuthOnboardingRole — assumed by authenticated Identity Pool users ────
        # The browser (as the signed-in user) assumes this via AssumeRoleWithWebIdentity
        # and calls the AgentCore Identity APIs to drive + complete the 3LO consent.
        auth_role = iam.Role(
            self,
            "AuthOnboardingRole",
            assumed_by=iam.FederatedPrincipal(
                "cognito-identity.amazonaws.com",
                conditions={
                    "StringEquals": {"cognito-identity.amazonaws.com:aud": identity_pool.ref},
                    "ForAnyValue:StringLike": {"cognito-identity.amazonaws.com:amr": "authenticated"},
                },
                assume_role_action="sts:AssumeRoleWithWebIdentity",
            ),
            description="Authenticated Cognito Identity Pool users: drive + complete "
                        "AgentCore 3LO consent for mcp-connector-* providers",
        )
        # Complete the 3LO session binding from the browser. NOTE: the SPA does NOT
        # self-drive GetResourceOauth2Token — the gateway's workload identity is
        # service-linked ("cannot retrieve an access token by the caller"), so the
        # SPA instead PROBES a tool through the gateway (/mcp tools/call, CORS-allowed)
        # to obtain the -32042 authorization URL, then completes here. So the only
        # AgentCore action the browser needs is CompleteResourceTokenAuth.
        auth_role.add_to_policy(
            iam.PolicyStatement(
                sid="CompleteResourceTokenAuth",
                effect=iam.Effect.ALLOW,
                actions=["bedrock-agentcore:CompleteResourceTokenAuth"],
                # SCOPED to the four resource types this action requires, per the IAM
                # service authorization reference:
                # https://docs.aws.amazon.com/service-authorization/latest/reference/list_bedrock-agentcore.html
                # (CompleteResourceTokenAuth, access level Read, requires
                # oauth2credentialprovider + token-vault + workload-identity +
                # workload-identity-directory).
                #
                # The credential-provider half uses the `mcp-connector-*` naming convention
                # (see PROVIDER_NAME in connectors/atlassian_stack.py) so new connectors are
                # drop-in, while still preventing completion against any UNRELATED provider
                # in the account. The workload half is pinned to THIS gateway's identity.
                resources=[
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:token-vault/default",
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}"
                    f":token-vault/default/oauth2credentialprovider/mcp-connector-*",
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}"
                    f":workload-identity-directory/default",
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}"
                    f":workload-identity-directory/default/workload-identity/{workload_name}",
                ],
            )
        )
        # Browser-side CompleteResourceTokenAuth exchanges the auth code using the
        # provider's client secret. It reads it either from AgentCore's MANAGED vault
        # (bedrock-agentcore-identity*) or, for EXTERNAL providers, from the connector's
        # own secret — so grant both. Scoped to the enterprise-mcp-connector/* naming
        # convention to stay generic across connectors (not tied to one provider).
        # NOTE (hardening): this is inherent to browser-side completion — the federated
        # SPA role can read connector client secrets. A server-side completion Lambda
        # would remove that exposure (tracked as a future option).
        auth_role.add_to_policy(
            iam.PolicyStatement(
                sid="ConnectorProviderSecretRead",
                effect=iam.Effect.ALLOW,
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:bedrock-agentcore-identity*",
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:enterprise-mcp-connector/*",
                ],
            )
        )
        cognito.CfnIdentityPoolRoleAttachment(
            self,
            "ConnectorAuthIdentityPoolRoles",
            identity_pool_id=identity_pool.ref,
            roles={"authenticated": auth_role.role_arn},
        )

        # ── Register the SPA client in the gateway's allowedClients (Option A) ───
        # The gateway must trust the Hosted-UI client's tokens (its authorizer's
        # allowedClients). That list lives on the gateway (another stack) and this
        # client is created here — a cross-stack cycle — so a config-preserving
        # custom resource registers it: it does a full get_gateway → merge the
        # client into allowedClients → update_gateway carrying every other field
        # forward. (NEVER a partial update_gateway — that would drop
        # policyEngineConfiguration and silently disable Cedar enforcement.)
        gateway_arn = f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/{gateway_id}"
        gw_role_arn = ssm.StringParameter.value_for_string_parameter(
            self, "/enterprise-mcp-gateway/roles/gateway-exec-arn")
        register_fn = lambda_.Function(
            self,
            "RegisterGatewayClientFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.on_event",
            code=bundled_asset(_HERE / "register_gateway_client"),
            timeout=Duration.minutes(2),
            memory_size=256,
            description="Custom resource: add the connector-auth SPA client to the "
                        "gateway allowedClients, preserving all other config (CDK)",
        )
        register_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="ManageGatewayAllowedClients",
                effect=iam.Effect.ALLOW,
                actions=["bedrock-agentcore:GetGateway", "bedrock-agentcore:UpdateGateway"],
                resources=[gateway_arn],
            )
        )
        # update_gateway re-passes the gateway's execution role, which requires PassRole.
        register_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="PassGatewayExecRole",
                effect=iam.Effect.ALLOW,
                actions=["iam:PassRole"],
                resources=[gw_role_arn],
            )
        )
        register_provider = cr.Provider(
            self, "RegisterGatewayClientProvider", on_event_handler=register_fn)
        CustomResource(
            self,
            "RegisterGatewayClient",
            service_token=register_provider.service_token,
            properties={"GatewayId": gateway_id, "ClientId": web_client.user_pool_client_id},
        )

        # ── Register the CloudFront /callback as an allowed 3LO return URL ───────
        # on the gateway's workload identity (required — the return URL must be
        # allow-listed there, per the OAuth session-binding docs). Keep localhost
        # too so the dev-shortcut connectors/atlassian/scripts/authorize.py still works.
        cr.AwsCustomResource(
            self,
            "AllowlistReturnUrl",
            on_create=cr.AwsSdkCall(
                service="bedrock-agentcore-control",
                action="updateWorkloadIdentity",
                parameters={
                    "name": workload_name,
                    "allowedResourceOauth2ReturnUrls": [return_url, "http://localhost:8080/callback"],
                },
                physical_resource_id=cr.PhysicalResourceId.of(f"allowlist-{workload_name}"),
            ),
            on_update=cr.AwsSdkCall(
                service="bedrock-agentcore-control",
                action="updateWorkloadIdentity",
                parameters={
                    "name": workload_name,
                    "allowedResourceOauth2ReturnUrls": [return_url, "http://localhost:8080/callback"],
                },
                physical_resource_id=cr.PhysicalResourceId.of(f"allowlist-{workload_name}"),
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements([
                iam.PolicyStatement(
                    # SCOPED to THIS gateway's workload identity. Workload identities are
                    # addressed by name in the API calls above, but they DO have ARNs in the
                    # account's agent identity directory —
                    # `workload-identity-directory/default` with child
                    # `workload-identity/<name>` entries — so IAM can bind to them. See
                    # https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/understanding-agent-identities.html
                    # The directory ARN is included because the update call resolves the
                    # child through its parent directory (same pattern as the gateway exec
                    # role's token-vault grant in enterprise_gateway/gateway_stack.py).
                    actions=["bedrock-agentcore:UpdateWorkloadIdentity",
                             "bedrock-agentcore:GetWorkloadIdentity"],
                    resources=[
                        f"arn:aws:bedrock-agentcore:{self.region}:{self.account}"
                        f":workload-identity-directory/default",
                        f"arn:aws:bedrock-agentcore:{self.region}:{self.account}"
                        f":workload-identity-directory/default/workload-identity/{workload_name}",
                    ],
                )
            ]),
            install_latest_aws_sdk=False,
        )

        # ── Outputs (consumed by deploy-connector-auth.sh → config.json) ─────────
        cognito_login = f"https://{hosted_ui.domain_name}.auth.{self.region}.amazoncognito.com"
        CfnOutput(self, "SiteUrl", value=site_url,
                  description="Connector Authorize SPA URL (visit /auth?provider=<name>)")
        CfnOutput(self, "SiteBucketName", value=site_bucket.bucket_name)
        CfnOutput(self, "IdentityPoolId", value=identity_pool.ref)
        CfnOutput(self, "UserPoolClientId", value=web_client.user_pool_client_id)
        CfnOutput(self, "HostedUiDomain", value=hosted_ui.domain_name)
        CfnOutput(self, "CognitoLoginDomain", value=cognito_login)
        CfnOutput(self, "WorkloadName", value=workload_name)
        CfnOutput(self, "CallbackUrl", value=return_url)
