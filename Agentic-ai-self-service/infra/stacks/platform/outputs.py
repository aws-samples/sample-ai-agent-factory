"""CloudFormation Stack Outputs."""

import aws_cdk as cdk
from aws_cdk import CfnOutput
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_s3 as s3


def build_stack_outputs(
    stack: cdk.Stack,
    *,
    api: apigwv2.HttpApi,
    distribution: cloudfront.Distribution,
    bucket: s3.Bucket,
    user_pool: cognito.UserPool,
    user_pool_client: cognito.UserPoolClient,
) -> None:
    """Create CloudFormation stack outputs.

    Requirements: 7.3
    """
    CfnOutput(
        stack,
        "ApiGatewayUrl",
        value=api.url or "",
        description="API Gateway HTTP API URL",
    )

    CfnOutput(
        stack,
        "CloudFrontUrl",
        value=f"https://{distribution.distribution_domain_name}",
        description="CloudFront distribution URL",
    )

    CfnOutput(
        stack,
        "S3BucketName",
        value=bucket.bucket_name,
        description="Frontend S3 bucket name",
    )

    CfnOutput(
        stack,
        "UserPoolId",
        value=user_pool.user_pool_id,
        description="Cognito User Pool ID",
    )

    CfnOutput(
        stack,
        "UserPoolClientId",
        value=user_pool_client.user_pool_client_id,
        description="Cognito User Pool Client ID",
    )
