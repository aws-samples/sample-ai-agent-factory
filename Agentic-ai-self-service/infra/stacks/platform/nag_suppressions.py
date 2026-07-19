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
from aws_cdk import aws_stepfunctions as sfn
from constructs import IConstruct


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

    def __init__(self, reasons):
        self._reasons = reasons

    def visit(self, node: IConstruct) -> None:
        if node.node.id.startswith("OverflowPolicy"):
            try:
                cdk_nag.NagSuppressions.add_resource_suppressions(
                    node,
                    [cdk_nag.NagPackSuppression(id=nid, reason=reason) for nid, reason in self._reasons],
                )
            except Exception:  # noqa: BLE001
                pass


def apply_nag_suppressions(
    stack: cdk.Stack,
    *,
    workflow_lambda: _lambda.Function,
    deployment_lambda: _lambda.Function,
    stream_lambda: _lambda.Function | None,
    step_lambdas: dict[str, _lambda.Function],
    shared_runtime_role: iam.Role,
    state_machine: sfn.StateMachine,
    logging_bucket: s3.Bucket,
    distribution: cloudfront.Distribution,
    api: apigwv2.HttpApi,
    user_pool: cognito.UserPool,
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
    cdk.Aspects.of(stack).add(_OverflowPolicyNagSuppressor(iam_reasons))
    # Bug 157 — streaming test Lambda role: same invoke-on-* wildcards as the
    # deployment Lambda's test path (InvokeAgentRuntime/InvokeHarness).
    if stream_lambda is not None:
        _suppress(stream_lambda.role, iam_reasons)
    for fn in step_lambdas.values():
        _suppress(fn.role, iam_reasons)
    # Shared AgentCore runtime exec role: exact bedrock-agentcore action
    # lists (browser/code-interpreter/memory sessions) on Resource "*" —
    # those resources are created dynamically per-deploy so their ARNs are
    # unknowable at synth time. See build_shared_runtime_role docstring.
    _suppress(
        shared_runtime_role,
        [
            (
                "AwsSolutions-IAM5",
                "Wildcard RESOURCES only (actions are exact lists): browser/"
                "code-interpreter/memory sessions, gateways and Bedrock "
                "models are created or selected dynamically per deploy, so "
                "ARNs are unknowable at synth time",
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
    _suppress(
        distribution,
        [
            (
                "AwsSolutions-CFR1",
                "CloudFront geo restrictions not required — internal development tool",
            ),
            (
                "AwsSolutions-CFR4",
                "Using CloudFront default certificate — custom domain with ACM planned for production",
            ),
        ],
    )

    # ---- API Gateway: APIG1 (access logging) and APIG4 (route auth)
    # apply only to the HTTP API. /health is intentionally unauthenticated.
    _suppress(
        api,
        [
            (
                "AwsSolutions-APIG1",
                "API Gateway access logging planned for production — using Lambda CloudWatch logs",
            ),
            (
                "AwsSolutions-APIG4",
                "JWT authorizer on all /api/* routes; /health is intentionally unauthenticated",
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
                "Cognito JWT authorizer on all /api/* routes; /health is intentionally unauthenticated",
            ),
            (
                "AwsSolutions-COG8",
                "Cognito Plus tier (advanced security) not required for "
                "this internal development tool; upstream IdP provides "
                "threat protection",
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
