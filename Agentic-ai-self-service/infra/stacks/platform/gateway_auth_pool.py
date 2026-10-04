"""A platform-owned Cognito pool + hosted domain for gateway client-credentials.

WHY THIS EXISTS — a measured, deterministic deploy failure.

``gateway_deployer._create_cognito_oauth`` used to create a brand-new Cognito user
pool AND a brand-new hosted domain on every gateway deploy, then immediately build
the token endpoint ``https://<domain>.auth.<region>.amazoncognito.com/oauth2/token``
and hand it to the deploy-time MCP ``tools/list`` probe.

A Cognito hosted domain provisions a CloudFront distribution. Measured in us-east-1
against a throwaway pool: ``create_user_pool_domain`` returns immediately,
``describe_user_pool_domain`` reports ``Status=ACTIVE`` within **4 seconds**, and the
DNS name still did not resolve at **t+381s**. ACTIVE is not a readiness oracle for
the endpoint. The probe window is 90 seconds and starts ~20s after the domain is
created, so on a fresh gateway the token endpoint was unreachable for the entire
window, every poll raised, and the deploy concluded the gateway served 0 tools.

Worse, the cure made it permanent: the "empty tool plane" retry deletes the gateway
and recreates it, which creates yet another pool and another cold domain. Live
(deploy ``3ef480e2``, run ``df698a37``): three attempts, three pools
(``v8OiJanup`` -> ``QU487tO1L`` -> ``yvymML5Pm``), each retry restarting the
provisioning clock, 368s of billed Lambda, and a healthy gateway destroyed twice.

The domain cannot be waited out inside the step: the gateway Lambda and its Step
Functions task are capped at 300s, and the measurement says the domain needs longer
than that on its own. So the domain has to already exist. This construct creates it
once, at PLATFORM deploy time, so it is warm long before any agent is deployed and
the tool plane becomes verifiable at deploy time for the first time.

SECURITY — what is shared and what is not. ARCC ``cnt_PQjUx2msVXY1wU`` requires a
separate set of credentials per entity ("If credentials are shared, please create
unique credentials per entity"). Only the pool and its hosted domain are shared, and
neither is a credential: the domain is a public, unauthenticated endpoint. Every
gateway still gets its OWN app client (its own ``client_id``/``client_secret``) and
its OWN resource server, so a gateway's client is issued only its own
``agentcore-<gateway>/invoke`` scope and cannot obtain another gateway's scope --
Cognito rejects a ``client_credentials`` request for a scope the client is not
configured for. Per-gateway isolation is therefore unchanged; only the cold-start
CloudFront provisioning is amortized.

Also note ``cnt_vtSS0S3iwKjSuk``: the per-gateway client secret is still minted at
deploy time and must not be moved onto a diagnostic path. This construct creates no
app client and holds no secret.
"""

import hashlib

import aws_cdk as cdk
from aws_cdk import aws_cognito as cognito

from .config import PlatformConfig

#: Cognito rejects a domain prefix longer than this.
MAX_PREFIX_LEN = 63
#: ``${AWS::AccountId}`` is 12 digits once CloudFormation resolves it.
_ACCOUNT_ID_LEN = 12
_DIGEST_LEN = 6


def gateway_domain_prefix_pattern(region: str, cfg: PlatformConfig) -> str:
    """A deterministic ``Fn::Sub`` pattern for this deployment's Cognito domain prefix.

    Returns something like ``acfe2e-p0920-gw-3f2a1b-${AWS::AccountId}``. The caller
    wraps it in ``cdk.Fn.sub`` so CloudFormation substitutes the real account id at
    DEPLOY time.

    WHY THE ACCOUNT IS A ``${AWS::AccountId}`` PLACEHOLDER AND NOT A DIGEST INPUT.

    This function used to take ``account`` and hash ``account/region/project/env``
    into the prefix. The call site passed ``stack.account`` -- and ``app.py``
    synthesizes with ``cdk.Environment(region=...)`` and NO account, so the stack is
    account-agnostic and ``stack.account`` is not an account id at all: it is the
    unresolved token string ``${Token[AWS.AccountId.7]}``. The digest was therefore
    computed over a token INDEX, which is an artifact of how many CDK tokens happened
    to be allocated earlier in that synth. Measured: four synths of identical code
    produced four different prefixes (``ba16e004ad``, ``c2a441523f``, ``7eb66a6477``,
    ``60a65f780f``), and the deployed stack holds the first while a fresh synth
    produces another.

    THE PREFIX IS EFFECTIVELY IMMUTABLE, which is measured, not assumed. A Cognito
    user pool accepts exactly ONE hosted domain, and CloudFormation replaces a domain
    by creating the new one before removing the old, so the create is rejected against
    a pool that still has one. Live against ``acfe2e-p0920``::

        UPDATE_FAILED  AWS::Cognito::UserPoolDomain  GatewayAuthDomain
        Resource handler returned message: "Invalid request provided:
        AWS::Cognito::UserPoolDomain" (HandlerErrorCode: InvalidRequest)

    and the whole stack update rolled back, taking all 15 step Lambdas with it
    ("Resource update cancelled"). The error names neither the cause nor the
    conflicting resource. Landing a prefix change therefore requires an operator to
    ``delete-user-pool-domain`` by hand first.

    So the nondeterminism was not a latent risk. Because the prefix changed on every
    synth, EVERY ``cdk deploy`` after the first would have attempted this replacement
    and failed: the platform stack became permanently un-updatable the moment it was
    first deployed, with an opaque error and a full rollback. Three further
    consequences, had a replacement ever succeeded:

    * **Availability.** A new hosted domain is unreachable for minutes (see the module
      docstring: 727s measured), so every deployed agent's token endpoint would go
      down -- the exact failure this whole construct exists to prevent.
    * **Security.** ARCC ``cnt_ua0cTwldOsODs8``: Cognito hosted domains live in a
      global namespace, and a released name "can be re-used by other AWS accounts,
      and therefore cannot be trusted after their deletion" -- the dangling-resource
      pattern. Because the domain is ``RETAIN``, a replaced one is abandoned rather
      than deleted, while already-deployed agents keep the OLD
      ``https://<prefix>.auth.<region>.amazoncognito.com/oauth2/token`` URL in their
      runtime env. Those agents send ``client_credentials`` -- their client id and
      secret -- to that host. Whoever claims the abandoned prefix receives them.
    * **Cross-account collision.** The digest was supposed to make the name unique per
      account, but since it hashed a token index rather than an account, two different
      accounts deploying the same project/env could compute the SAME prefix, and the
      second one's create would fail on a global-namespace conflict it does not own.

    Making the account a CloudFormation placeholder fixes both: the prefix is now a
    pure function of synth-time-known values (region, project, env) plus the account
    CloudFormation fills in, so it is byte-identical across every synth of the same
    deployment and genuinely unique per account+region+project+env.

    Prefix rules enforced here: lowercase alphanumerics and hyphens only, no
    leading/trailing hyphen, <=63 chars, and Cognito additionally forbids a prefix
    beginning with ``aws``.
    """
    # Only synth-time-known values enter the digest. Never a token.
    digest = hashlib.sha256(f"{region}/{cfg.project}/{cfg.env}".encode()).hexdigest()[:_DIGEST_LEN]
    base = f"{cfg.project}-{cfg.env}".lower()
    base = "".join(c if (c.isalnum() or c == "-") else "-" for c in base)
    # Cognito rejects a prefix starting with "aws", so a project literally named
    # "aws-*" would fail the deploy with a message that names no cause.
    if base.startswith("aws"):
        base = f"x{base}"
    # Reserve room for "-gw-", the digest, the "-" before the account, and the 12
    # digits CloudFormation substitutes for ${AWS::AccountId}.
    reserved = len("-gw-") + len(digest) + 1 + _ACCOUNT_ID_LEN
    base = base[: MAX_PREFIX_LEN - reserved].strip("-")
    return f"{base}-gw-{digest}-${{AWS::AccountId}}"


def build_gateway_auth_pool(stack: cdk.Stack, cfg: PlatformConfig) -> tuple[cognito.UserPool, str]:
    """Create the shared gateway-auth user pool and its hosted domain.

    Returns ``(pool, domain_prefix)``. The gateway step Lambda receives both via
    environment variables; ``gateway_deployer`` creates only a per-gateway resource
    server + app client inside this pool when they are set, and falls back to
    creating its own pool when they are not (so an older platform stack keeps
    working unchanged).

    This pool holds NO human users. It exists solely to issue machine-to-machine
    ``client_credentials`` tokens for gateway invocation, so sign-up is closed and
    there is no hosted-UI login flow to protect.
    """
    pool = cognito.UserPool(
        stack,
        "GatewayAuthUserPool",
        user_pool_name=f"{cfg.project}-{cfg.env}-gateway-auth",
        self_sign_up_enabled=False,
        # No human ever signs in here, but the pool still has a password policy and
        # cdk-nag AwsSolutions-COG1 fails the synth without one — so it must be set,
        # and the value should be the one that fits what this pool is for.
        #
        # ARCC cnt_qljjTWYkQl2eci separates the two cases: 8 (recommended 16) for
        # non-privileged HUMAN users, but "when using passwords to authenticate
        # machines or for privileged access ... the minimum length for system account
        # passwords must be 32 bytes. This is 32 UTF-8 characters." This pool is
        # machine-to-machine only, so 32 is the applicable floor, not the app pool's 8.
        password_policy=cognito.PasswordPolicy(
            min_length=32,
            require_lowercase=True,
            require_uppercase=True,
            require_digits=True,
            require_symbols=True,
        ),
        # Matches the app pool. Satisfies AwsSolutions-COG3; with no interactive
        # sign-in there are no sign-in attempts to score, so this is posture rather
        # than active defence — and it costs nothing at zero monthly active users.
        standard_threat_protection_mode=cognito.StandardThreatProtectionMode.FULL_FUNCTION,
        # Follows the environment's removal policy (platform_stack.py): prod-like
        # envs get RETAIN_ON_UPDATE_OR_DELETE, i.e. DeletionPolicy RetainExceptOnCreate
        # + UpdateReplacePolicy Retain. Every gateway deployed against this platform
        # has its app client and resource server inside this pool, and those clients
        # are the credentials live agents authenticate with, so after a SUCCESSFUL
        # create the pool survives a stack delete, a replacing update, and any later
        # rollback -- destroying it would revoke every deployed agent's gateway access
        # at once, and the domain is expensive to recreate (module docstring: >381s to
        # become reachable). It does NOT survive the rollback of the operation that
        # first creates it: on 2026-09-25 plain RETAIN persisted this very pool
        # (us-east-1_wPnNWjU4U) through a failed first create and blocked the retry.
        # dev/test/sandbox/preview/ephemeral use DESTROY like every other resource,
        # so a throwaway environment is really throwaway; cleanup.sh keeps its
        # retained-pool path for stacks created before this change.
        removal_policy=cfg.removal_policy,
    )

    # The hosted domain. This is the only thing that takes minutes, and creating it
    # here means it is warm before the first agent deploy rather than during it.
    #
    # `prefix` is a CloudFormation token (Fn::Sub), NOT a Python string: the account
    # id is substituted at deploy time so the value cannot drift between synths. See
    # gateway_domain_prefix_pattern for the measurement that forced this. CDK's
    # domain-prefix validation is guarded by Token.isUnresolved, so an Fn::Sub is
    # accepted here; everything downstream (the CfnOutput and the step Lambdas'
    # GATEWAY_SHARED_USER_POOL_DOMAIN env var) is resolved by CloudFormation too.
    prefix = cdk.Fn.sub(gateway_domain_prefix_pattern(stack.region, cfg))
    domain = pool.add_domain(
        "GatewayAuthDomain",
        cognito_domain=cognito.CognitoDomainOptions(domain_prefix=prefix),
    )
    # Same reasoning and the same knob as the pool: a replaced domain is a
    # multi-minute outage for every deployed agent's token mint, not a fast rebuild.
    domain.apply_removal_policy(cfg.removal_policy)

    cdk.CfnOutput(
        stack,
        "GatewayAuthUserPoolId",
        value=pool.user_pool_id,
        description="Shared Cognito pool that holds each gateway's own app client and resource server",
    )
    cdk.CfnOutput(
        stack,
        "GatewayAuthDomainPrefix",
        value=prefix,
        description="Cognito hosted-domain prefix backing every gateway's OAuth2 token endpoint",
    )
    return pool, prefix
