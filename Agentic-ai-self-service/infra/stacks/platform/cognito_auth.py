"""Cognito Authentication — user pool, app client, groups, provisioner, OIDC."""

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import custom_resources as cr

from .config import PlatformConfig


def build_cognito(stack: cdk.Stack, cfg: PlatformConfig) -> tuple:
    """Create Cognito User Pool, client, and pre-set users."""
    pool = cognito.UserPool(
        stack,
        "UserPool",
        user_pool_name=f"{cfg.project}-{cfg.env}-users",
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
        removal_policy=cfg.removal_policy,
    )

    client = pool.add_client(
        "FrontendClient",
        user_pool_client_name=f"{cfg.project}-{cfg.env}-frontend",
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

    # Loom-study 1.1 — OPT-IN 3rd-party OIDC IdP federation (Entra/Okta/Auth0/
    # generic OIDC). Federating INTO Cognito (vs Loom's in-app multi-issuer
    # validation) keeps the API-Gateway Cognito JWT authorizer unchanged —
    # the serverless-correct fit. Enabled only when oidc_* context is set, so
    # the default password-auth flow is undisturbed. Config:
    #   -c oidc_provider_name=Okta -c oidc_issuer=https://... \
    #   -c oidc_client_id=... -c oidc_client_secret=... \
    #   [-c oidc_groups_claim=groups] [-c oidc_hosted_domain_prefix=...]
    _oidc_name = stack.node.try_get_context("oidc_provider_name")
    _oidc_issuer = stack.node.try_get_context("oidc_issuer")
    _oidc_client_id = stack.node.try_get_context("oidc_client_id")
    _oidc_client_secret = stack.node.try_get_context("oidc_client_secret")
    if _oidc_name and _oidc_issuer and _oidc_client_id and _oidc_client_secret:
        _configure_oidc_federation(
            stack,
            cfg,
            pool,
            client,
            provider_name=str(_oidc_name),
            issuer=str(_oidc_issuer),
            client_id=str(_oidc_client_id),
            client_secret=str(_oidc_client_secret),
            groups_claim=str(stack.node.try_get_context("oidc_groups_claim") or "groups"),
            domain_prefix=str(
                stack.node.try_get_context("oidc_hosted_domain_prefix") or f"{cfg.project}-{cfg.env}-{stack.account}"
            )[:63],
        )

    # Two-persona approval workflow groups for the agent registry.
    # 'registry-admin' members can approve/reject submissions and manage any
    # entry; 'registry-developer' members publish (pending) and manage their
    # own. Persona is resolved backend-side from cognito:groups (see
    # services/auth.is_registry_admin). Higher precedence = stronger role,
    # so registry-admin (0) outranks registry-developer (10).
    cognito.CfnUserPoolGroup(
        stack,
        "RegistryAdminGroup",
        user_pool_id=pool.user_pool_id,
        group_name="registry-admin",
        description="Registry approvers",
        precedence=0,
    )
    cognito.CfnUserPoolGroup(
        stack,
        "RegistryDeveloperGroup",
        user_pool_id=pool.user_pool_id,
        group_name="registry-developer",
        description="Registry publishers",
        precedence=10,
    )

    # Scope-based RBAC groups (services/rbac.py GROUP_SCOPES). Two dimensions:
    #   * type groups (t-admin / t-user) drive which UI sections render;
    #   * resource groups (g-admins-* / g-users-*) grant capability scopes.
    # A user belongs to one type group + one or more resource groups.
    # Enforcement is advisory until RBAC_ENFORCE=true on the API Lambda.
    _rbac_groups = [
        ("TypeAdminGroup", "t-admin", "UI: all admin sections", 1),
        ("TypeUserGroup", "t-user", "UI: end-user sections only", 20),
        ("AdminSuperGroup", "g-admins-super", "All scopes", 1),
        ("AdminRegistryGroup", "g-admins-registry", "registry:read/write", 5),
        ("AdminSecurityGroup", "g-admins-security", "settings + observability", 5),
        ("AdminCostGroup", "g-admins-cost", "cost:read/write", 5),
        ("UserDefaultGroup", "g-users-default", "invoke + read-only defaults", 20),
    ]
    for _cid, _gname, _desc, _prec in _rbac_groups:
        cognito.CfnUserPoolGroup(
            stack,
            _cid,
            user_pool_id=pool.user_pool_id,
            group_name=_gname,
            description=_desc,
            precedence=_prec,
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
    cognito_users_raw = stack.node.try_get_context("cognito_users") or ""
    cognito_users = (
        [e.strip() for e in cognito_users_raw.split(",") if e.strip()]
        if isinstance(cognito_users_raw, str)
        else cognito_users_raw
    )

    if cognito_users:
        provisioner_fn = _lambda.Function(
            stack,
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
            stack,
            "CognitoUserProvisionerProvider",
            on_event_handler=provisioner_fn,
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        for email in cognito_users:
            sanitized = email.replace("@", "-at-").replace(".", "-")
            user_cr = cdk.CustomResource(
                stack,
                f"User-{sanitized}",
                service_token=provider.service_token,
                properties={
                    "UserPoolId": pool.user_pool_id,
                    "Email": email,
                },
            )
            user_cr.node.add_dependency(pool)

    return pool, client


def _configure_oidc_federation(
    stack: cdk.Stack,
    cfg: PlatformConfig,
    pool,
    client,
    *,
    provider_name: str,
    issuer: str,
    client_id: str,
    client_secret: str,
    groups_claim: str,
    domain_prefix: str,
) -> None:
    """Attach an external OIDC IdP to the Cognito pool (Loom-study 1.1).

    Adds (1) an OIDC identity provider with attribute mapping (email + the
    external group claim mapped to the Cognito ``custom:ext_groups`` attribute
    via a pre-token trigger downstream / or directly into a group claim), (2)
    a hosted-UI domain so the SPA can redirect to the IdP, and (3) supported
    identity providers + OAuth flows on the app client. Idempotent-by-name.

    Group→internal-group mapping: the external claim (e.g. Okta ``groups``) is
    surfaced so services/auth can normalize it — see docs/PERSONAS.md. Cognito
    maps OIDC claims to standard/custom attributes; the group claim is carried
    through and read by the backend group resolver.
    """
    idp = cognito.CfnUserPoolIdentityProvider(
        stack,
        "OidcIdentityProvider",
        user_pool_id=pool.user_pool_id,
        provider_name=provider_name,
        provider_type="OIDC",
        provider_details={
            "client_id": client_id,
            "client_secret": client_secret,
            "oidc_issuer": issuer,
            "authorize_scopes": "openid email profile",
            "attributes_request_method": "GET",
        },
        # Map the OIDC email claim to the Cognito email attribute so federated
        # users resolve to an email identity. The group claim is carried in the
        # token and normalized backend-side (services/auth group resolver).
        attribute_mapping={"email": "email"},
    )

    # Hosted-UI domain — required for the federated authorization-code redirect.
    cognito.CfnUserPoolDomain(
        stack,
        "CognitoHostedDomain",
        user_pool_id=pool.user_pool_id,
        domain=domain_prefix,
    )

    # Wire the app client to accept the federated IdP + code flow. The client
    # must depend on the IdP existing first (CFN ordering).
    cfn_client = client.node.default_child
    cfn_client.supported_identity_providers = ["COGNITO", provider_name]
    cfn_client.allowed_o_auth_flows = ["code"]
    cfn_client.allowed_o_auth_scopes = ["openid", "email", "profile"]
    cfn_client.allowed_o_auth_flows_user_pool_client = True
    cfn_client.add_dependency(idp)

    CfnOutput(stack, "OidcProviderName", value=provider_name)
    CfnOutput(stack, "OidcGroupsClaim", value=groups_claim)
    CfnOutput(
        stack,
        "CognitoHostedUiDomain",
        value=f"https://{domain_prefix}.auth.{stack.region}.amazoncognito.com",
    )
