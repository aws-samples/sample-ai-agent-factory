"""CDK-NAG suppressions (audit issue #4) — per-construct, not stack-wide."""

import aws_cdk as cdk
import cdk_nag
import jsii
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_stepfunctions as sfn
from constructs import IConstruct

from .config import is_home_region


@jsii.implements(cdk.IAspect)
class _OverflowPolicyNagSuppressor:
    """Aspect: suppress IAM5 wildcard findings on CDK auto-generated
    ``OverflowPolicy<N>`` managed policies.

    CDK splits an over-large inline role policy into ``OverflowPolicy<N>``
    constructs during SYNTHESIS — after a stack's __init__ runs — so a
    suppression applied in __init__ can miss them (their creation races with
    the grant that tips the policy over the IAM size limit). An Aspect visits
    every node during synthesis, catching overflow policies whenever they land.
    """

    def __init__(self, reasons, *, roles):
        """*roles*: the ONLY roles whose overflow policies this aspect may suppress -- the same
        Lambda execution roles ``apply_nag_suppressions`` already suppresses with
        ``apply_to_children=True``. F-26 (signoff-g10): the first version matched ANY node whose
        id started with ``OverflowPolicy`` and swallowed every exception, so a new wildcard in a
        policy nobody had reviewed, or a suppression that silently failed to apply, both read as
        a clean nag gate. Restricting to the reviewed roles' own children makes this aspect a
        timing fix (overflow policies are created during synthesis, after the by-construct
        suppression ran) and nothing more; there is no exception path left to hide behind."""
        self._reasons = reasons
        self._role_paths = frozenset(r.node.path for r in roles if r is not None)

    def visit(self, node: IConstruct) -> None:
        if not isinstance(node, iam.ManagedPolicy) or not node.node.id.startswith("OverflowPolicy"):
            return
        parent = node.node.scope
        if parent is None or parent.node.path not in self._role_paths:
            return
        cdk_nag.NagSuppressions.add_resource_suppressions(
            node,
            [cdk_nag.NagPackSuppression(id=nid, reason=reason) for nid, reason in self._reasons],
        )


def apply_nag_suppressions(
    stack: cdk.Stack,
    *,
    workflow_lambda: _lambda.Function,
    deployment_lambda: _lambda.Function,
    stream_lambda: _lambda.Function | None,
    step_lambdas: dict[str, _lambda.Function],
    shared_runtime_role: iam.Role,
    shared_mcp_runtime_role: iam.Role,
    role_boundary: iam.ManagedPolicy,
    state_machine: sfn.StateMachine,
    logging_bucket: s3.Bucket,
    distribution: cloudfront.Distribution,
    api: apigwv2.HttpApi,
    user_pool: cognito.UserPool,
    trigger_dead_letter_queue: sqs.IQueue,
    gateway_auth_pool: cognito.UserPool | None = None,
) -> None:
    """Apply CDK-NAG suppressions to specific constructs.

    Per-construct (not stack-wide) so any new wildcard added in an
    unrelated construct will surface as a fresh nag finding instead of
    being silently absorbed. Each rule is scoped to the resource that
    legitimately needs it; reasons match the originals from app.py.
    """

    def _suppress(node, ids_with_reasons: list[tuple[str, str]]) -> None:
        cdk_nag.NagSuppressions.add_resource_suppressions(
            node,
            [cdk_nag.NagPackSuppression(id=nid, reason=reason) for nid, reason in ids_with_reasons],
            apply_to_children=True,
        )

    # ---- IAM4 + IAM5: every Lambda role uses AWSLambdaBasicExecutionRole
    # and almost all of them have at least one wildcard resource for
    # dynamically-created Cognito pools, AgentCore runtimes, or Bedrock
    # model invocations. Suppress per Lambda execution role rather than
    # stack-wide. apply_to_children=True covers DefaultPolicy attached
    # by L2 grant_* helpers.
    iam_reasons = [
        (
            "AwsSolutions-IAM4",
            "AWSLambdaBasicExecutionRole is AWS-recommended for Lambda CloudWatch logging",
        ),
        (
            "AwsSolutions-IAM5",
            "Wildcard resources required for dynamically-created Cognito "
            "pools, AgentCore runtimes, and Bedrock model invocations",
        ),
    ]
    _suppress(workflow_lambda.role, iam_reasons)
    _suppress(deployment_lambda.role, iam_reasons)
    # When a role's inline policy exceeds the IAM size limit, CDK splits the
    # excess into auto-generated "OverflowPolicy<N>" managed policies DURING
    # SYNTHESIS — after this __init__-time method runs — so a fixed by-path
    # suppression can miss them once a grant tips the policy over the limit
    # (hit when the Phase 2 tag-policy grant grew the deployment role). An
    # Aspect visits nodes during synth and suppresses the same IAM5 wildcard
    # findings on any OverflowPolicy, whenever/wherever CDK creates it.
    cdk.Aspects.of(stack).add(
        _OverflowPolicyNagSuppressor(
            iam_reasons,
            roles=[workflow_lambda.role, deployment_lambda.role, stream_lambda.role if stream_lambda else None]
            + [fn.role for fn in step_lambdas.values()],
        )
    )
    # Bug 157 — streaming test Lambda role: same invoke-on-* wildcards as the
    # deployment Lambda's test path (InvokeAgentRuntime/InvokeHarness).
    if stream_lambda is not None:
        _suppress(stream_lambda.role, iam_reasons)
    for fn in step_lambdas.values():
        _suppress(fn.role, iam_reasons)
    # The permissions boundary for roles the backend mints (F-06, role_boundary.py). It is a
    # CAP, not a grant: nothing is attached to it and no principal is widened by it, so its
    # namespace wildcards (bedrock-agentcore:*, s3:*, ...) on Resource '*' describe the
    # ceiling a created role can never exceed rather than authority anyone holds. Suppressed
    # on this one construct only; an IAM5 anywhere else still fails the nag gate.
    _suppress(
        role_boundary,
        [
            (
                "AwsSolutions-IAM5",
                "Permissions boundary: an intersection cap on roles the platform creates, attached to no "
                "principal. Its wildcards bound what a created role MAY be granted; the created roles' own "
                "policies (written by the backend with exact ARNs) are what grants anything",
            ),
        ],
    )
    # Shared AgentCore runtime exec role: every action is an exact list (the
    # artifact-bucket read uses explicit s3 verbs, not grant_read's wildcards).
    # Only RESOURCES carry wildcards, of two distinct kinds spelled out in the
    # reason below -- genuine Resource='*' vs scoped ARNs with a wildcard
    # SEGMENT. See build_shared_runtime_role.
    _suppress(
        shared_runtime_role,
        [
            (
                "AwsSolutions-IAM5",
                "Every action is an exact list; the wildcards are all in "
                "RESOURCES, of two kinds. (a) Genuine Resource='*': Bedrock "
                "model invocation, the AgentCore tool-plane integrations "
                "(browser/code-interpreter/memory/gateway/guardrails/evaluation/"
                "policy sessions), and the CloudWatch log group AgentCore "
                "Runtime auto-creates -- all named dynamically at runtime, so "
                "ARNs are unknowable at synth time. (b) Scoped ARNs with a "
                "wildcard SEGMENT (not '*'): the artifacts-bucket read (the "
                "bucket ARN plus <bucket>/* for objects) and the account-scoped "
                "regional artifact buckets (arn:aws:s3:::agentcore-flows-"
                "artifacts-<account>-* and that + /*), whose per-region names "
                "are unknowable at synth time",
            ),
        ],
    )
    # Shared standalone-FastMCP runtime exec role: a DELIBERATELY model-free
    # role whose only wildcards are (1) the artifacts-bucket read that AgentCore
    # uses to fetch the staged runtime ZIP as this role -- current bucket plus the
    # regional buckets a non-home deploy stages into, whose account-scoped names
    # are unknowable at synth time -- and (2) the CloudWatch log group AgentCore
    # Runtime auto-creates for it at a runtime-determined name. It carries NO
    # bedrock model or AgentCore tool-plane actions; that invariant is enforced by
    # test_mcp_shared_runtime_role.py, not by this suppression, so a truthful
    # per-construct reason here cannot mask a future model grant. See
    # build_shared_mcp_runtime_role.
    _suppress(
        shared_mcp_runtime_role,
        [
            (
                "AwsSolutions-IAM5",
                "Wildcard RESOURCES only -- every action is exact (s3:ListBucket, "
                "s3:GetBucketLocation, s3:GetObject, s3:GetObjectVersion, and the "
                "three logs actions): this model-free role reads the staged runtime "
                "ZIP from the artifacts bucket (current bucket objects plus the "
                "regional buckets a non-home deploy stages into, whose account-scoped "
                "names are unknowable at synth time) and writes to the CloudWatch log "
                "group AgentCore Runtime auto-creates at a runtime-determined name; "
                "it carries no model or tool-plane actions",
            ),
        ],
    )
    # Step Functions role wraps grant_invoke on every step Lambda; CDK's
    # grant_* helpers attach a DefaultPolicy whose statements use the
    # function ARN with a wildcard suffix for versions/aliases.
    if state_machine is not None and state_machine.role is not None:
        _suppress(state_machine.role, iam_reasons)

    # ---- L1: All Lambdas use Python 3.12 deliberately.
    l1_reasons = [
        (
            "AwsSolutions-L1",
            "Using Python 3.12 for CDK Lambda construct stability",
        ),
    ]
    _suppress(workflow_lambda, l1_reasons)
    _suppress(deployment_lambda, l1_reasons)
    if stream_lambda is not None:
        _suppress(stream_lambda, l1_reasons)
    for fn in step_lambdas.values():
        _suppress(fn, l1_reasons)

    # ---- S1: only the access-log bucket itself is exempt — everything
    # else writes its access logs INTO this bucket.
    _suppress(
        logging_bucket,
        [
            (
                "AwsSolutions-S1",
                "S3 access logging is hosted by this bucket itself; "
                "logging the log bucket would be a circular dependency",
            ),
        ],
    )

    # ---- CloudFront: distribution-only.
    cloudfront_reasons = [
        (
            "AwsSolutions-CFR1",
            "CloudFront geo restrictions not required — internal development tool",
        ),
        (
            "AwsSolutions-CFR4",
            "Using CloudFront default certificate — custom domain with ACM planned for production",
        ),
    ]
    if not is_home_region(stack):
        # A distribution accepts ONLY a scope=CLOUDFRONT WebACL, and AWS creates
        # those exclusively in us-east-1 — a single-region stack outside
        # us-east-1 has none to attach. The equivalent protection is applied to
        # the Cognito user pool via a REGIONAL WebACL with the same rule set
        # (build_waf_web_acl). Operators who want edge filtering as well can
        # create a CLOUDFRONT ACL in us-east-1 and pass its ARN via the
        # `cloudfront_web_acl_arn` context key.
        cloudfront_reasons.append(
            (
                "AwsSolutions-CFR2",
                "CLOUDFRONT-scoped WAF WebACLs only exist in us-east-1 and a "
                "distribution accepts no other scope; this stack deploys to "
                f"{stack.region}, where the same WAF rule set is applied "
                "REGIONALly to the Cognito user pool instead. Supply "
                "`-c cloudfront_web_acl_arn=...` to also attach an edge ACL.",
            )
        )
    _suppress(distribution, cloudfront_reasons)

    # A dead-letter queue is intentionally terminal. Giving it another DLQ
    # would create an unbounded chain rather than an operator-visible failure
    # boundary; the CloudWatch alarm is the recovery path.
    _suppress(
        trigger_dead_letter_queue,
        [
            (
                "AwsSolutions-SQS3",
                "This is the terminal trigger dead-letter queue; a CloudWatch alarm pages on its first visible message",
            ),
        ],
    )

    # ---- API Gateway: APIG1 (access logging) and APIG4 (route auth)
    # apply only to the HTTP API. /health is public health metadata; /hooks/*
    # uses a per-trigger body HMAC because external senders have no Cognito JWT.
    _suppress(
        api,
        [
            (
                "AwsSolutions-APIG1",
                "API Gateway access logging planned for production — using Lambda CloudWatch logs",
            ),
            (
                "AwsSolutions-APIG4",
                "JWT authorizer on all /api/* routes; /health is intentionally "
                "public and /hooks/* authenticates timestamp, delivery id, and "
                "the exact request body with a per-trigger HMAC",
            ),
        ],
    )

    # ---- Cognito: COG2/COG4/COG8 only apply to the user pool / clients.
    _suppress(
        user_pool,
        [
            (
                "AwsSolutions-COG2",
                "MFA enforced at the IdP for FederateOIDC SSO logins; "
                "Cognito-native MFA would be redundant for this internal "
                "development tool",
            ),
            (
                "AwsSolutions-COG4",
                "Cognito JWT authorizer on all /api/* routes; /health is "
                "intentionally public and /hooks/* uses a per-trigger body HMAC",
            ),
            (
                "AwsSolutions-COG8",
                "Cognito Plus tier (advanced security) not required for "
                "this internal development tool; upstream IdP provides "
                "threat protection",
            ),
        ],
    )

    # ---- The shared gateway-auth pool: COG2 only.
    #
    # Scoped separately from the app pool rather than folded into it, because the
    # reason is different and the app pool's reason would be false here. This pool
    # has no human users and no interactive sign-in: sign-up is disabled, no user is
    # ever created in it, and its only clients use the client_credentials grant,
    # which by definition has no user and therefore no second factor to present.
    # MFA is not weakened here — it is not an applicable control. The credential this
    # pool actually issues is a per-gateway client secret, protected by ARCC
    # cnt_qljjTWYkQl2eci's 32-character machine-account password policy (set on the
    # pool) and by never being returned in a step result (cnt_vtSS0S3iwKjSuk).
    #
    # COG1/COG3 are deliberately NOT suppressed: they are satisfied for real.
    if gateway_auth_pool is not None:
        _suppress(
            gateway_auth_pool,
            [
                (
                    "AwsSolutions-COG2",
                    "Machine-to-machine pool: client_credentials only, no human users and no "
                    "interactive sign-in flow, so there is no authentication event for MFA to "
                    "apply to. Sign-up is disabled and no user is ever created in this pool",
                ),
            ],
        )

    # ---- Step Functions: SF1 (ALL-level logging) on the state machine.
    _suppress(
        state_machine,
        [
            (
                "AwsSolutions-SF1",
                "Step Functions logs ERROR-level events; ALL-level logging planned for production",
            ),
        ],
    )

    # ---- BucketDeployment singleton custom resource: CDK creates a
    # shared Lambda at the stack root (Custom::CDKBucketDeployment*) when
    # any BucketDeployment is used. Path-scoped suppressions because the
    # construct lives outside our owned constructs — owned by
    # aws-cdk-lib's BucketDeployment L2; we cannot tighten its IAM,
    # runtime version, or managed policy without forking the L2.
    for child in stack.node.find_all():
        try:
            node_path = child.node.path  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover
            continue
        if "Custom::CDKBucketDeployment" in node_path:
            cdk_nag.NagSuppressions.add_resource_suppressions_by_path(
                stack,
                node_path,
                [
                    cdk_nag.NagPackSuppression(
                        id="AwsSolutions-L1",
                        reason="BucketDeployment is a CDK-managed L2; runtime version is owned by aws-cdk-lib.",
                    ),
                    cdk_nag.NagPackSuppression(
                        id="AwsSolutions-IAM4",
                        reason="BucketDeployment uses AWSLambdaBasicExecutionRole — owned by the CDK L2 construct.",
                        applies_to=[
                            "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                        ],
                    ),
                    cdk_nag.NagPackSuppression(
                        id="AwsSolutions-IAM5",
                        reason="BucketDeployment requires s3:Get*/List*/Abort*/DeleteObject* wildcards on the source CDK assets bucket and the destination artifacts bucket — owned by the CDK L2.",
                        applies_to=[
                            "Action::s3:GetBucket*",
                            "Action::s3:GetObject*",
                            "Action::s3:List*",
                            "Action::s3:Abort*",
                            "Action::s3:DeleteObject*",
                            # Region-templated rather than hardcoded so the
                            # suppression holds across all deployment regions.
                            # Without this every non-us-east-1 deploy fails
                            # CDK-NAG with an unmatched IAM5 wildcard.
                            f"Resource::arn:<AWS::Partition>:s3:::cdk-hnb659fds-assets-<AWS::AccountId>-{stack.region}/*",
                            "Resource::<ArtifactsBucket2AAC5544.Arn>/*",
                        ],
                    ),
                ],
            )

    # ---- Cognito user provisioner (only created when COGNITO_USERS env
    # var is non-empty). Three sub-constructs need scoped suppressions:
    # (1) our own provisioner Lambda + role (we OWN the code; managed
    # policy is CDK auto-attach for any lambda_.Function),
    # (2) CDK's Provider framework which spawns its own framework-onEvent
    # Lambda we don't control,
    # (3) CDK's LogRetention helper Lambda used by `log_retention=`.
    cognito_provisioner_paths = [
        f"{stack.stack_name}/CognitoUserProvisionerFn",
    ]
    for p in cognito_provisioner_paths:
        try:
            cdk_nag.NagSuppressions.add_resource_suppressions_by_path(
                stack,
                p,
                [
                    cdk_nag.NagPackSuppression(
                        id="AwsSolutions-L1",
                        reason="Using Python 3.12 deliberately for CDK Lambda construct stability (matches all other platform Lambdas).",
                    ),
                    cdk_nag.NagPackSuppression(
                        id="AwsSolutions-IAM4",
                        reason="Lambda execution role auto-attaches AWSLambdaBasicExecutionRole; required for CloudWatch logging.",
                        applies_to=[
                            "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                        ],
                    ),
                ],
                apply_to_children=True,
            )
        except Exception:  # pragma: no cover
            pass

    # CDK Provider framework + LogRetention helper — both CDK-managed L2s
    # we do not own. Path-scoped because the constructs may not exist
    # (only created when COGNITO_USERS is set).
    for child in stack.node.find_all():
        try:
            node_path = child.node.path  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover
            continue
        if "CognitoUserProvisionerProvider" in node_path or "LogRetention" in node_path:
            try:
                cdk_nag.NagSuppressions.add_resource_suppressions_by_path(
                    stack,
                    node_path,
                    [
                        cdk_nag.NagPackSuppression(
                            id="AwsSolutions-L1",
                            reason="Provider framework / LogRetention helper Lambda is a CDK-managed L2; runtime version is owned by aws-cdk-lib.",
                        ),
                        cdk_nag.NagPackSuppression(
                            id="AwsSolutions-IAM4",
                            reason="CDK-managed L2 Lambda uses AWSLambdaBasicExecutionRole — owned by aws-cdk-lib.",
                            applies_to=[
                                "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                            ],
                        ),
                        cdk_nag.NagPackSuppression(
                            id="AwsSolutions-IAM5",
                            reason="CDK Provider framework grants `lambda:InvokeFunction` on the user provisioner Lambda's ARN with version-suffix wildcard, and LogRetention helper requires `logs:PutRetentionPolicy` / `logs:DeleteRetentionPolicy` on `*` to set retention on dynamically-named log groups — both owned by aws-cdk-lib.",
                            applies_to=[
                                "Resource::*",
                                "Resource::<CognitoUserProvisionerFn43674288.Arn>:*",
                            ],
                        ),
                    ],
                )
            except Exception:  # pragma: no cover
                pass
