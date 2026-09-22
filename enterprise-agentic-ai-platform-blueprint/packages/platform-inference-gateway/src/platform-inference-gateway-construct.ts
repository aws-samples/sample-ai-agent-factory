import {
  ArnFormat,
  CfnResource,
  Duration,
  SecretValue,
  Stack,
  Tags,
} from 'aws-cdk-lib';
import { CfnGateway } from 'aws-cdk-lib/aws-bedrockagentcore';
import {
  OAuthScope,
  ResourceServerScope,
  UserPool,
  UserPoolClient,
  UserPoolDomain,
  UserPoolResourceServer,
} from 'aws-cdk-lib/aws-cognito';
import {
  AccountPrincipal,
  Effect,
  Policy,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from 'aws-cdk-lib/aws-iam';
import { Key } from 'aws-cdk-lib/aws-kms';
import { Secret } from 'aws-cdk-lib/aws-secretsmanager';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

/**
 * A positive rate allocation for one provider-qualified model ID.
 *
 * `qualifiedModelId` deliberately omits the Gateway target-name prefix. For
 * example, a target named `agenticai-inference-prod-bedrock` is invoked as
 * `agenticai-inference-prod-bedrock/openai.gpt-oss-120b`, while the matching
 * rate-limit dimension is `openai.gpt-oss-120b`.
 */
export interface InferenceModelRateLimit {
  readonly qualifiedModelId: string;
  readonly requestsPerMinute: number;
  readonly tokensPerMinute: number;
}

export interface PlatformInferenceGatewayConstructProps {
  readonly envName: string;
  readonly applicationId: string;
  readonly agentId: string;
  readonly tenantId: string;
  readonly costCentre: string;
  readonly modelRateLimits: readonly InferenceModelRateLimit[];
  readonly gatewayName?: string;
  readonly targetName?: string;
  readonly rateLimitId?: string;
  readonly mcpVersion?: string;
  readonly accessTokenValidity?: Duration;
  /**
   * Opt-in: 12-digit AWS account IDs (the Workstream accounts) allowed to read
   * the published M2M credential secret cross-account. When set, the construct
   * publishes a Secrets Manager secret holding the connection metadata plus the
   * generated client secret, with a resource policy granting exactly those
   * accounts `secretsmanager:GetSecretValue`. Omitted by default — the secret
   * is only created when a consumer account is declared.
   */
  readonly m2mSecretReaderAccountIds?: readonly string[];
}

const MAX_RATE = 10_000_000;
const DEFAULT_MCP_VERSION = '2025-11-25';

function validateName(label: string, value: string, maximum: number): void {
  if (
    value.length > maximum ||
    !/^[0-9A-Za-z](?:-?[0-9A-Za-z])*$/.test(value)
  ) {
    throw new Error(
      `PlatformInferenceGatewayConstruct: ${label} must be ${maximum} characters or fewer and contain only alphanumerics with non-consecutive hyphens; got '${value}'.`,
    );
  }
}

function validateTagValue(label: string, value: string): void {
  if (value.trim().length === 0 || value.length > 256) {
    throw new Error(
      `PlatformInferenceGatewayConstruct: ${label} must be a non-empty string of at most 256 characters.`,
    );
  }
}

function validateRate(label: string, value: number): void {
  if (!Number.isInteger(value) || value <= 0 || value > MAX_RATE) {
    throw new Error(
      `PlatformInferenceGatewayConstruct: ${label} must be an integer from 1 through ${MAX_RATE}; got ${value}.`,
    );
  }
}

function validateModelRateLimits(
  limits: readonly InferenceModelRateLimit[],
): void {
  if (limits.length === 0) {
    throw new Error(
      'PlatformInferenceGatewayConstruct: modelRateLimits must contain at least one allowed model.',
    );
  }

  const seen = new Set<string>();
  for (const limit of limits) {
    if (
      !/^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(limit.qualifiedModelId) ||
      limit.qualifiedModelId.includes('/') ||
      limit.qualifiedModelId === '*'
    ) {
      throw new Error(
        `PlatformInferenceGatewayConstruct: qualifiedModelId must be provider-qualified without a connector prefix or wildcard; got '${limit.qualifiedModelId}'.`,
      );
    }
    if (seen.has(limit.qualifiedModelId)) {
      throw new Error(
        `PlatformInferenceGatewayConstruct: duplicate qualifiedModelId '${limit.qualifiedModelId}'.`,
      );
    }
    seen.add(limit.qualifiedModelId);
    validateRate(
      `${limit.qualifiedModelId}.requestsPerMinute`,
      limit.requestsPerMinute,
    );
    validateRate(
      `${limit.qualifiedModelId}.tokensPerMinute`,
      limit.tokensPerMinute,
    );
  }
}

function buildRateLimitEntries(
  limits: readonly InferenceModelRateLimit[],
): Record<string, unknown>[] {
  const allowEntries = limits.map((limit) => ({
    Dimensions: { qualifiedModelId: limit.qualifiedModelId },
    Requests: [{ Rate: limit.requestsPerMinute, Period: 'minute' }],
    Tokens: [{ Rate: limit.tokensPerMinute, Period: 'minute' }],
  }));

  // Gateway rate limiting is fail-open traffic management, not authorization.
  // This zero-rate catch-all blocks unconfigured models during normal service
  // operation; IAM, Gateway Policy, Guardrails and SCPs remain the security
  // boundary when the managed limiter is unavailable.
  return [
    ...allowEntries,
    {
      Dimensions: { qualifiedModelId: '*' },
      Requests: [{ Rate: 0, Period: 'second' }],
    },
  ];
}

function requiredTags(
  props: PlatformInferenceGatewayConstructProps,
): Record<string, string> {
  return {
    'application-id': props.applicationId,
    'agent-id': props.agentId,
    'tenant-id': props.tenantId,
    'cost-centre': props.costCentre,
    environment: props.envName,
  };
}

/**
 * Pipeline-owned central inference path for generated agents.
 *
 * This construct uses native CloudFormation resources for AgentCore Gateway,
 * its Bedrock Mantle inference target and native rate limits. Cognito issues
 * client-credentials JWTs; generated agents point Strands `LiteLLMModel` at
 * `gatewayUrl/inference/v1` and never call Bedrock directly.
 */
export class PlatformInferenceGatewayConstruct extends Construct {
  readonly gatewayRole: Role;
  readonly userPool: UserPool;
  readonly userPoolClient: UserPoolClient;
  readonly userPoolDomain: UserPoolDomain;
  readonly gateway: CfnGateway;
  readonly inferenceTarget: CfnResource;
  readonly rateLimit: CfnResource;
  readonly gatewayId: string;
  readonly gatewayArn: string;
  readonly gatewayUrl: string;
  readonly inferenceTargetId: string;
  readonly inferenceTargetName: string;
  readonly oauthScope: string;
  readonly discoveryUrl: string;
  readonly tokenEndpoint: string;
  readonly rateLimitId: string;
  /** Present only when m2mSecretReaderAccountIds is set. */
  readonly m2mSecret?: Secret;

  constructor(
    scope: Construct,
    id: string,
    props: PlatformInferenceGatewayConstructProps,
  ) {
    super(scope, id);

    const stack = Stack.of(this);
    const gatewayName = props.gatewayName ?? `agenticai-inference-${props.envName}`;
    const targetName = props.targetName ?? `${gatewayName}-bedrock`;
    this.inferenceTargetName = targetName;
    this.rateLimitId = props.rateLimitId ?? `models-${props.envName}`;

    validateName('gatewayName', gatewayName, 48);
    validateName('targetName', targetName, 100);
    validateName('rateLimitId', this.rateLimitId, 64);
    validateModelRateLimits(props.modelRateLimits);
    for (const [key, value] of Object.entries(requiredTags(props))) {
      validateTagValue(key, value);
      Tags.of(this).add(key, value);
    }

    const sourceGatewayArn = stack.formatArn({
      service: 'bedrock-agentcore',
      resource: 'gateway',
      resourceName: `${gatewayName}-*`,
      arnFormat: ArnFormat.SLASH_RESOURCE_NAME,
    });
    this.gatewayRole = new Role(this, 'GatewayRole', {
      roleName: `AgenticAI-InferenceGateway-${props.envName}`,
      assumedBy: new ServicePrincipal('bedrock-agentcore.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': stack.account },
          ArnLike: { 'aws:SourceArn': sourceGatewayArn },
        },
      }),
      description:
        'AgentCore central inference Gateway role for the Bedrock Mantle connector.',
    });
    const mantlePolicy = new Policy(this, 'BedrockMantlePolicy', {
      statements: [
        new PolicyStatement({
          sid: 'InvokeBedrockMantle',
          effect: Effect.ALLOW,
          actions: [
            'bedrock-mantle:ListModels',
            'bedrock-mantle:CreateInference',
          ],
          // These preview actions do not expose resource-level permissions.
          resources: ['*'],
        }),
      ],
    });
    this.gatewayRole.attachInlinePolicy(mantlePolicy);
    NagSuppressions.addResourceSuppressions(
      mantlePolicy,
      [
        {
          id: 'AwsSolutions-IAM5',
          reason:
            'SEC-027: bedrock-mantle ListModels/CreateInference currently support only Resource="*"; the trust policy scopes assumption to this account and named Gateway ARN.',
        },
        {
          id: 'NIST.800.53.R5-IAMNoInlinePolicy',
          reason:
            'SEC-027: the two-action policy is lifecycle-bound to the Gateway role and cannot be shared.',
        },
      ],
      true,
    );

    this.userPool = new UserPool(this, 'UserPool', {
      userPoolName: `${gatewayName}-auth`,
      selfSignUpEnabled: false,
      deletionProtection: props.envName === 'prod',
    });
    const invokeScope = new ResourceServerScope({
      scopeName: 'invoke',
      scopeDescription: 'Invoke the central AgentCore inference Gateway',
    });
    const resourceServerIdentifier = `${gatewayName}-api`;
    const resourceServer = new UserPoolResourceServer(this, 'ResourceServer', {
      userPool: this.userPool,
      identifier: resourceServerIdentifier,
      userPoolResourceServerName: `${gatewayName} API`,
      scopes: [invokeScope],
    });
    this.oauthScope = `${resourceServerIdentifier}/${invokeScope.scopeName}`;
    this.userPoolClient = new UserPoolClient(this, 'MachineClient', {
      userPool: this.userPool,
      userPoolClientName: `${gatewayName}-m2m`,
      generateSecret: true,
      preventUserExistenceErrors: true,
      enableTokenRevocation: true,
      accessTokenValidity: props.accessTokenValidity ?? Duration.minutes(5),
      oAuth: {
        flows: { clientCredentials: true },
        scopes: [OAuthScope.resourceServer(resourceServer, invokeScope)],
      },
    });
    this.userPoolDomain = this.userPool.addDomain('Domain', {
      cognitoDomain: {
        domainPrefix: `${gatewayName}-${stack.account}-${stack.region}`.toLowerCase(),
      },
    });
    NagSuppressions.addResourceSuppressions(
      this.userPool,
      [
        {
          id: 'AwsSolutions-COG2',
          reason:
            'SEC-028: this pool has no human sign-in path; it exists only for OAuth 2.0 client-credentials grants, so user MFA is inapplicable.',
        },
        {
          id: 'AwsSolutions-COG3',
          reason:
            'SEC-028: Cognito threat-protection modes evaluate user authentication, while this pool permits only machine client-credentials grants.',
        },
      ],
      true,
    );

    this.discoveryUrl =
      `https://cognito-idp.${stack.region}.${stack.urlSuffix}/` +
      `${this.userPool.userPoolId}/.well-known/openid-configuration`;
    this.tokenEndpoint = `${this.userPoolDomain.baseUrl()}/oauth2/token`;

    this.gateway = new CfnGateway(this, 'Gateway', {
      name: gatewayName,
      roleArn: this.gatewayRole.roleArn,
      protocolType: 'MCP',
      protocolConfiguration: {
        mcp: {
          supportedVersions: [props.mcpVersion ?? DEFAULT_MCP_VERSION],
        },
      },
      authorizerType: 'CUSTOM_JWT',
      authorizerConfiguration: {
        customJwtAuthorizer: {
          discoveryUrl: this.discoveryUrl,
          allowedClients: [this.userPoolClient.userPoolClientId],
          allowedScopes: [this.oauthScope],
        },
      },
      description:
        'Central OpenAI-compatible inference Gateway for pipeline-managed agents',
      tags: requiredTags(props),
    });
    this.gateway.node.addDependency(mantlePolicy);

    // CDK 2.251.0 has the Gateway L1 but predates the August 2026 inference
    // branch on GatewayTarget and the GatewayRateLimit L1. Use their published
    // CloudFormation resource contracts directly until generated L1s catch up.
    this.inferenceTarget = new CfnResource(this, 'InferenceTarget', {
      type: 'AWS::BedrockAgentCore::GatewayTarget',
      properties: {
        GatewayIdentifier: this.gateway.ref,
        Name: targetName,
        Description: 'Bedrock Mantle inference connector',
        TargetConfiguration: {
          Inference: {
            Connector: {
              Source: { ConnectorId: 'bedrock-mantle' },
            },
          },
        },
        CredentialProviderConfigurations: [
          { CredentialProviderType: 'GATEWAY_IAM_ROLE' },
        ],
      },
    });
    this.inferenceTarget.node.addDependency(this.gateway);
    this.inferenceTarget.node.addDependency(mantlePolicy);

    this.rateLimit = new CfnResource(this, 'ModelRateLimit', {
      type: 'AWS::BedrockAgentCore::GatewayRateLimit',
      properties: {
        GatewayIdentifier: this.gateway.ref,
        RateLimitId: this.rateLimitId,
        Description:
          'Per-model RPM and TPM allocations with a zero-rate wildcard fallback',
        DimensionKeys: ['qualifiedModelId'],
        Entries: buildRateLimitEntries(props.modelRateLimits),
      },
    });
    this.rateLimit.node.addDependency(this.inferenceTarget);

    this.gatewayId = this.gateway.attrGatewayIdentifier;
    this.gatewayArn = this.gateway.attrGatewayArn;
    this.gatewayUrl = this.gateway.attrGatewayUrl;
    this.inferenceTargetId = this.inferenceTarget
      .getAtt('TargetId')
      .toString();

    const readerAccounts = props.m2mSecretReaderAccountIds ?? [];
    if (readerAccounts.length > 0) {
      for (const acct of readerAccounts) {
        if (!/^\d{12}$/.test(acct)) {
          throw new Error(
            `PlatformInferenceGatewayConstruct: m2mSecretReaderAccountIds must be 12-digit account IDs; got '${acct}'.`,
          );
        }
      }
      const uniqueReaders = [...new Set(readerAccounts)];
      // Dedicated CMK so the cross-account grant is explicit and revocable.
      const secretKey = new Key(this, 'M2mSecretKey', {
        alias: `alias/agenticai/inference-m2m-${gatewayName}`,
        description: `CMK for the cross-account inference M2M secret (${gatewayName}).`,
        enableKeyRotation: true,
      });
      for (const acct of uniqueReaders) {
        secretKey.addToResourcePolicy(
          new PolicyStatement({
            sid: `AllowDecrypt${acct}`,
            effect: Effect.ALLOW,
            principals: [new AccountPrincipal(acct)],
            actions: ['kms:Decrypt', 'kms:DescribeKey'],
            resources: ['*'],
            conditions: {
              StringEquals: {
                'kms:ViaService': `secretsmanager.${stack.region}.amazonaws.com`,
              },
            },
          }),
        );
      }
      this.m2mSecret = new Secret(this, 'M2mSecret', {
        secretName: `agenticai/inference-m2m/${gatewayName}`,
        description:
          'Cross-account M2M connection metadata + client secret for the inference Gateway. Consumed by the Workstream CognitoOauth2 credential provider.',
        encryptionKey: secretKey,
        secretObjectValue: {
          clientId: SecretValue.unsafePlainText(
            this.userPoolClient.userPoolClientId,
          ),
          clientSecret: this.userPoolClient.userPoolClientSecret,
          tokenEndpoint: SecretValue.unsafePlainText(this.tokenEndpoint),
          scope: SecretValue.unsafePlainText(this.oauthScope),
          gatewayUrl: SecretValue.unsafePlainText(this.gatewayUrl),
          inferenceTargetName: SecretValue.unsafePlainText(
            this.inferenceTargetName,
          ),
        },
      });
      this.m2mSecret.addToResourcePolicy(
        new PolicyStatement({
          sid: 'AllowWorkstreamRead',
          effect: Effect.ALLOW,
          principals: uniqueReaders.map((a) => new AccountPrincipal(a)),
          actions: ['secretsmanager:GetSecretValue', 'secretsmanager:DescribeSecret'],
          resources: ['*'],
        }),
      );
      NagSuppressions.addResourceSuppressions(
        this.m2mSecret,
        [
          {
            id: 'AwsSolutions-SMG4',
            reason:
              'SEC-030: this secret mirrors a Cognito app-client secret whose rotation is owned by Cognito; automatic Secrets Manager rotation would desynchronise the two. Rotation is handled by rotating the Cognito client secret and redeploying.',
          },
        ],
        true,
      );
    }
  }
}
