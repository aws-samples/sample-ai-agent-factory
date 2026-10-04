"""Cognito Authentication — user pool, app client, groups, provisioner, OIDC."""

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import custom_resources as cr

from .config import PlatformConfig

# The groups every provisioned user joins: a standard user's scopes (g-users-default,
# services/rbac.py GROUP_SCOPES) and the end-user UI (t-user).
PROVISIONED_USER_GROUPS = ("g-users-default", "t-user")


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
    #   -c oidc_client_id=... -c oidc_client_secret_arn=arn:aws:secretsmanager:...:secret:... \
    #   [-c oidc_client_secret_json_key=client_secret] \
    #   [-c oidc_groups_claim=groups] [-c oidc_hosted_domain_prefix=...]
    #
    # F-07 (signoff-g10): the client secret used to arrive as `-c oidc_client_secret=<value>`
    # and was written verbatim into ProviderDetails, i.e. into cdk.out/*.template.json,
    # GetTemplate, `cdk diff` output and every stack event. ARCC cnt_9OT33u5q3kyAPq: a
    # sensitive value in a CloudFormation template must be a Secrets Manager dynamic
    # reference, which CloudFormation resolves at deploy time and never retains, logs or
    # passes on. The context value is now the SECRET's ARN (or name); the operator stores the
    # client secret in Secrets Manager once. The legacy key is refused, not ignored: an
    # operator still passing it would otherwise believe federation was configured while the
    # `if` below silently skipped it.
    _oidc_name = stack.node.try_get_context("oidc_provider_name")
    _oidc_issuer = stack.node.try_get_context("oidc_issuer")
    _oidc_client_id = stack.node.try_get_context("oidc_client_id")
    _oidc_client_secret_ref = stack.node.try_get_context("oidc_client_secret_arn")
    if stack.node.try_get_context("oidc_client_secret") is not None:
        raise ValueError(
            "The CDK context key `oidc_client_secret` (a plaintext client secret) is no longer accepted: it "
            "would be written into the CloudFormation template. Store the client secret in AWS Secrets Manager "
            "and pass its ARN or name as `-c oidc_client_secret_arn=...` (optionally "
            "`-c oidc_client_secret_json_key=<field>` when the secret is a JSON object)."
        )
    if _oidc_name and _oidc_issuer and _oidc_client_id and _oidc_client_secret_ref:
        _configure_oidc_federation(
            stack,
            cfg,
            pool,
            client,
            provider_name=str(_oidc_name),
            issuer=str(_oidc_issuer),
            client_id=str(_oidc_client_id),
            client_secret_ref=str(_oidc_client_secret_ref),
            client_secret_json_key=stack.node.try_get_context("oidc_client_secret_json_key"),
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
    # The API Lambdas enforce by default (RBAC_ENFORCE=true), so a user in no
    # g-* group holds no scopes; the provisioner below grants the defaults.
    _rbac_groups = [
        ("TypeAdminGroup", "t-admin", "UI: all admin sections", 1),
        ("TypeUserGroup", "t-user", "UI: end-user sections only", 20),
        ("AdminSuperGroup", "g-admins-super", "All scopes", 1),
        ("AdminRegistryGroup", "g-admins-registry", "registry:read/write", 5),
        ("AdminSecurityGroup", "g-admins-security", "settings + observability", 5),
        ("AdminCostGroup", "g-admins-cost", "cost:read/write", 5),
        ("UserDefaultGroup", "g-users-default", "Build, deploy and invoke own agents", 20),
    ]
    rbac_groups = {}
    for _cid, _gname, _desc, _prec in _rbac_groups:
        rbac_groups[_gname] = cognito.CfnUserPoolGroup(
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
                    "cognito-idp:AdminAddUserToGroup",
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
                    # A standard user (services/rbac.py): without a g-* group the
                    # enforcing API grants no scopes. Admin groups stay a manual grant.
                    "Groups": list(PROVISIONED_USER_GROUPS),
                },
            )
            user_cr.node.add_dependency(pool)
            for group in PROVISIONED_USER_GROUPS:
                user_cr.node.add_dependency(rbac_groups[group])

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
    client_secret_ref: str,
    client_secret_json_key: str | None,
    groups_claim: str,
    domain_prefix: str,
) -> None:
    """Attach an external OIDC IdP to the Cognito pool (Loom-study 1.1).

    ``client_secret_ref`` is a Secrets Manager secret ARN or name, never the secret: it is
    rendered as a ``{{resolve:secretsmanager:...}}`` dynamic reference (F-07, ARCC
    cnt_9OT33u5q3kyAPq). ``client_secret_json_key`` selects one field when the secret is a
    JSON object; ``None`` means the whole SecretString is the client secret.

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
    # `unsafe_unwrap` is the CDK escape hatch for placing a SecretValue in a plain string
    # property. It is safe HERE because the value being unwrapped is the dynamic-reference
    # token itself ("{{resolve:secretsmanager:<ref>[:SecretString:<key>]}}"), not a secret:
    # CloudFormation substitutes the real value at deploy time and it never enters the
    # template, the cloud assembly or a stack event. The check is
    # infra/tests/test_f07_oidc_client_secret_is_a_dynamic_reference.py.
    client_secret_reference = cdk.SecretValue.secrets_manager(
        client_secret_ref,
        json_field=client_secret_json_key or None,
    ).unsafe_unwrap()
    idp = cognito.CfnUserPoolIdentityProvider(
        stack,
        "OidcIdentityProvider",
        user_pool_id=pool.user_pool_id,
        provider_name=provider_name,
        provider_type="OIDC",
        provider_details={
            "client_id": client_id,
            "client_secret": client_secret_reference,
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
