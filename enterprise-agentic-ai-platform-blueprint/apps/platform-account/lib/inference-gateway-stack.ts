import { CfnOutput, Stack, StackProps } from 'aws-cdk-lib';
import { Construct } from 'constructs';

import {
  PlatformInferenceGatewayConstruct,
  type PlatformInferenceGatewayConstructProps,
} from '@agenticai/platform-inference-gateway';

export interface InferenceGatewayStackProps
  extends StackProps,
    PlatformInferenceGatewayConstructProps {}

/** Platform-stage stack for the central AgentCore inference Gateway. */
export class InferenceGatewayStack extends Stack {
  readonly inferenceGateway: PlatformInferenceGatewayConstruct;

  constructor(scope: Construct, id: string, props: InferenceGatewayStackProps) {
    super(scope, id, props);

    this.inferenceGateway = new PlatformInferenceGatewayConstruct(
      this,
      'InferenceGateway',
      props,
    );

    new CfnOutput(this, 'GatewayIdentifier', {
      value: this.inferenceGateway.gatewayId,
      description: 'AgentCore Gateway identifier.',
    });
    new CfnOutput(this, 'GatewayArn', {
      value: this.inferenceGateway.gatewayArn,
      description: 'AgentCore Gateway ARN.',
    });
    new CfnOutput(this, 'GatewayUrl', {
      value: this.inferenceGateway.gatewayUrl,
      description:
        'Base AgentCore Gateway URL. LiteLLMModel uses the /inference/v1 path.',
    });
    new CfnOutput(this, 'InferenceTargetId', {
      value: this.inferenceGateway.inferenceTargetId,
      description: 'Bedrock Mantle inference-target identifier.',
    });
    new CfnOutput(this, 'InferenceTargetName', {
      value: this.inferenceGateway.inferenceTargetName,
      description:
        'Gateway target-name prefix for target-qualified inference model IDs.',
    });
    new CfnOutput(this, 'RateLimitId', {
      value: this.inferenceGateway.rateLimitId,
      description: 'Native Gateway model rate-limit identifier.',
    });
    new CfnOutput(this, 'CognitoUserPoolId', {
      value: this.inferenceGateway.userPool.userPoolId,
      description: 'Cognito User Pool that issues M2M access tokens.',
    });
    new CfnOutput(this, 'CognitoClientId', {
      value: this.inferenceGateway.userPoolClient.userPoolClientId,
      description:
        'M2M client ID. The generated client secret is intentionally not output.',
    });
    new CfnOutput(this, 'OAuthScope', {
      value: this.inferenceGateway.oauthScope,
      description: 'OAuth scope required by the Gateway JWT authorizer.',
    });
    new CfnOutput(this, 'TokenEndpoint', {
      value: this.inferenceGateway.tokenEndpoint,
      description: 'Cognito OAuth 2.0 client-credentials token endpoint.',
    });
    if (this.inferenceGateway.m2mSecret) {
      new CfnOutput(this, 'M2mSecretArn', {
        value: this.inferenceGateway.m2mSecret.secretArn,
        description:
          'Cross-account M2M secret ARN. The Workstream credential provider reads it to seed CognitoOauth2.',
      });
    }
  }
}
