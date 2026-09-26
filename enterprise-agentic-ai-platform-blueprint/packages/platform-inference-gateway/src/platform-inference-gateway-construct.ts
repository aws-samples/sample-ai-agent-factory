import * as path from 'node:path';

import {
  ArnFormat,
  CfnResource,
  CustomResource,
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
  ManagedPolicy,
  Policy,
  PolicyDocument,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from 'aws-cdk-lib/aws-iam';
import { Key } from 'aws-cdk-lib/aws-kms';
import { Code, Function as LambdaFunction, Runtime } from 'aws-cdk-lib/aws-lambda';
import { Secret } from 'aws-cdk-lib/aws-secretsmanager';
import { Provider } from 'aws-cdk-lib/custom-resources';
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

/**
 * The Bedrock Guardrail that the Gateway REQUEST interceptor applies to every
 * inference request before the model is called. All three values normally
 * come from the same-stage `GuardrailStack` (`attrGuardrailId`,
 * `attrVersion`, `attrGuardrailArn`); the ARN scopes the interceptor role's
 * `bedrock:ApplyGuardrail` grant to exactly this guardrail.
 */
export interface InferenceInputGuardrail {
  readonly guardrailIdentifier: string;
  readonly guardrailVersion: string;
  readonly guardrailArn: string;
}

export interface PlatformInferenceGatewayConstructProps {
  readonly envName: string;
  readonly applicationId: string;
  readonly agentId: string;
  readonly tenantId: string;
  readonly costCentre: string;
  readonly modelRateLimits: readonly InferenceModelRateLimit[];
  /**
   * Mandatory server-side guardrail. The Gateway's Mantle connector ignores
   * any client-supplied `guardrail_identifier`, `bedrock-mantle` exposes no
   * guardrail IAM condition key, and AgentCore Policy guardrail providers
   * cannot read the OpenAI `messages` set (live 2026-09-24), so the only
   * enforcement point is a REQUEST interceptor calling `ApplyGuardrail`.
   * Making the prop required keeps the inference path guardrail-free by
   * construction impossible.
   */
  readonly inputGuardrail: InferenceInputGuardrail;
  readonly gatewayName?: string;
  readonly targetName?: string;
  readonly rateLimitId?: string;
  readonly mcpVersion?: string;
  readonly accessTokenValidity?: Duration;
  /**
   * Upper bound on the request text (characters) the interceptor evaluates;
   * larger requests are refused with HTTP 413 rather than passed unguarded.
   * Defaults to 200 000.
   */
  readonly maxGuardedCharacters?: number;
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

/**
 * Inline handler that reads the Cognito app-client secret (same-account) and
 * merges it into the metadata M2M secret via PutSecretValue. Runs on
 * Create/Update; Delete is a no-op (the Secret is deleted by CloudFormation).
 * The client secret is read in-process only and never logged.
 */
const M2M_SECRET_POPULATOR_HANDLER = `
import json
import boto3


def on_event(event, context):
    if event["RequestType"] == "Delete":
        return {"PhysicalResourceId": event["ResourceProperties"]["SecretId"]}
    props = event["ResourceProperties"]
    region = props["Region"]
    cognito = boto3.client("cognito-idp", region_name=region)
    secrets = boto3.client("secretsmanager", region_name=region)
    client = cognito.describe_user_pool_client(
        UserPoolId=props["UserPoolId"], ClientId=props["ClientId"]
    )["UserPoolClient"]
    client_secret = client["ClientSecret"]
    current = secrets.get_secret_value(SecretId=props["SecretId"])["SecretString"]
    data = json.loads(current)
    data["clientSecret"] = client_secret
    secrets.put_secret_value(
        SecretId=props["SecretId"], SecretString=json.dumps(data)
    )
    del client_secret, data
    return {"PhysicalResourceId": props["SecretId"]}
`;

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

function validateInputGuardrail(guardrail: InferenceInputGuardrail | undefined): void {
  if (!guardrail) {
    throw new Error(
      'PlatformInferenceGatewayConstruct: inputGuardrail is required; the inference path must never deploy guardrail-free.',
    );
  }
  for (const [key, value] of Object.entries(guardrail)) {
    if (typeof value !== 'string' || value.trim().length === 0) {
      throw new Error(
        `PlatformInferenceGatewayConstruct: inputGuardrail.${key} must be a non-empty string.`,
      );
    }
  }
}

/**
 * IAM `bedrock-mantle:Model` values for the allow-listed models. The
 * connector accepts both the provider-qualified id (`openai.gpt-oss-120b`)
 * and the provider-stripped alias (`gpt-oss-120b`), so both spellings of each
 * allocated model are permitted and everything else is denied by IAM even
 * when the fail-open rate limiter admits it.
 */
export function allowedMantleModelIds(
  limits: readonly InferenceModelRateLimit[],
): string[] {
  const ids = new Set<string>();
  for (const limit of limits) {
    ids.add(limit.qualifiedModelId);
    const dot = limit.qualifiedModelId.indexOf('.');
    if (dot > 0 && dot < limit.qualifiedModelId.length - 1) {
      ids.add(limit.qualifiedModelId.slice(dot + 1));
    }
  }
  return [...ids].sort();
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
  /** Dedicated M2M client published cross-account for generated agents. */
  readonly workstreamUserPoolClient?: UserPoolClient;
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
  /** REQUEST interceptor applying the platform guardrail to every request. */
  readonly guardrailInterceptor: LambdaFunction;
  readonly guardrailInterceptorRole: Role;

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
    validateInputGuardrail(props.inputGuardrail);
    const maxGuardedCharacters = props.maxGuardedCharacters ?? 200_000;
    if (
      !Number.isInteger(maxGuardedCharacters) ||
      maxGuardedCharacters < 1_000 ||
      maxGuardedCharacters > 5_000_000
    ) {
      throw new Error(
        `PlatformInferenceGatewayConstruct: maxGuardedCharacters must be an integer from 1000 through 5000000; got ${maxGuardedCharacters}.`,
      );
    }
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
          sid: 'ListBedrockMantleModels',
          effect: Effect.ALLOW,
          actions: ['bedrock-mantle:ListModels'],
          // These preview actions do not expose resource-level permissions.
          resources: ['*'],
        }),
        new PolicyStatement({
          sid: 'InvokeAllocatedBedrockMantleModels',
          effect: Effect.ALLOW,
          actions: ['bedrock-mantle:CreateInference'],
          resources: ['*'],
          // Fail-closed model allow-list: the native rate limiter is fail-open
          // traffic shaping, so IAM denies every model outside the allocation.
          conditions: {
            StringEquals: {
              'bedrock-mantle:Model': allowedMantleModelIds(props.modelRateLimits),
            },
          },
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
            'SEC-027: bedrock-mantle ListModels/CreateInference currently support only Resource="*"; CreateInference is pinned to the allocated models with bedrock-mantle:Model and the trust policy scopes assumption to this account and named Gateway ARN.',
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
    if ((props.m2mSecretReaderAccountIds?.length ?? 0) > 0) {
      // Do not reuse the long-lived Platform operator client: clients that
      // entered Cognito's multi-secret lifecycle no longer expose
      // ClientSecret through DescribeUserPoolClient. A new dedicated client
      // isolates generated-agent rotation/recovery and leaves existing callers
      // untouched.
      this.workstreamUserPoolClient = new UserPoolClient(
        this,
        'GeneratedAgentMachineClient',
        {
          userPool: this.userPool,
          userPoolClientName: `${gatewayName}-generated-agent-m2m`,
          generateSecret: true,
          preventUserExistenceErrors: true,
          enableTokenRevocation: true,
          accessTokenValidity:
            props.accessTokenValidity ?? Duration.minutes(5),
          oAuth: {
            flows: { clientCredentials: true },
            scopes: [OAuthScope.resourceServer(resourceServer, invokeScope)],
          },
        },
      );
    }
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

    // Server-side guardrail enforcement. Explicitly-named role and function so
    // both stay inside the AgenticAI* CFN-exec boundary and the Gateway role's
    // invoke grant names one exact function ARN.
    const interceptorName = `agenticai-inference-guardrail-${props.envName}`.slice(0, 64);
    this.guardrailInterceptorRole = new Role(this, 'GuardrailInterceptorRole', {
      roleName: `AgenticAI-InferenceGuardrail-${props.envName}`.slice(0, 64),
      assumedBy: new ServicePrincipal('lambda.amazonaws.com'),
      description:
        'Applies the platform baseline Bedrock Guardrail to every inference Gateway request (REQUEST interceptor).',
      inlinePolicies: {
        ApplyGuardrail: new PolicyDocument({
          statements: [
            new PolicyStatement({
              sid: 'ApplyPlatformGuardrail',
              effect: Effect.ALLOW,
              actions: ['bedrock:ApplyGuardrail'],
              resources: [props.inputGuardrail.guardrailArn],
            }),
          ],
        }),
      },
      managedPolicies: [
        ManagedPolicy.fromAwsManagedPolicyName(
          'service-role/AWSLambdaBasicExecutionRole',
        ),
      ],
    });
    this.guardrailInterceptor = new LambdaFunction(this, 'GuardrailInterceptor', {
      functionName: interceptorName,
      description:
        'AgentCore inference Gateway REQUEST interceptor: bedrock:ApplyGuardrail on every request body; fails closed.',
      runtime: Runtime.PYTHON_3_13,
      handler: 'index.handler',
      timeout: Duration.seconds(25),
      memorySize: 256,
      role: this.guardrailInterceptorRole,
      code: Code.fromAsset(
        path.join(__dirname, '..', 'lambda', 'guardrail-interceptor'),
        { exclude: ['test_*.py', '__pycache__', '.pytest_cache'] },
      ),
      environment: {
        GUARDRAIL_IDENTIFIER: props.inputGuardrail.guardrailIdentifier,
        GUARDRAIL_VERSION: props.inputGuardrail.guardrailVersion,
        MAX_GUARDED_CHARACTERS: String(maxGuardedCharacters),
        ENV_NAME: props.envName,
      },
    });
    const interceptorInvokePolicy = new Policy(this, 'GuardrailInterceptorInvoke', {
      statements: [
        new PolicyStatement({
          sid: 'InvokeGuardrailInterceptor',
          effect: Effect.ALLOW,
          actions: ['lambda:InvokeFunction'],
          resources: [this.guardrailInterceptor.functionArn],
        }),
      ],
    });
    this.gatewayRole.attachInlinePolicy(interceptorInvokePolicy);
    NagSuppressions.addResourceSuppressions(
      this.guardrailInterceptorRole,
      [
        {
          id: 'AwsSolutions-IAM4',
          reason:
            'SEC-005: AWSLambdaBasicExecutionRole is the standard log-write policy for the interceptor Lambda; its only other grant is bedrock:ApplyGuardrail on the exact platform guardrail ARN.',
        },
      ],
      true,
    );
    NagSuppressions.addResourceSuppressions(
      this.guardrailInterceptor,
      [
        {
          id: 'AwsSolutions-L1',
          reason:
            'SEC-031: pinned to the Python 3.13 runtime shipped with this release; bumped deliberately with the offline handler tests.',
        },
      ],
      true,
    );

    this.gateway = new CfnGateway(this, 'Gateway', {
      name: gatewayName,
      roleArn: this.gatewayRole.roleArn,
      protocolType: 'MCP',
      protocolConfiguration: {
        mcp: {
          supportedVersions: [props.mcpVersion ?? DEFAULT_MCP_VERSION],
        },
      },
      // The interceptor evaluates the request body before the target is called;
      // headers (bearer tokens) are deliberately never passed to it.
      interceptorConfigurations: [
        {
          interceptor: { lambda: { arn: this.guardrailInterceptor.functionArn } },
          interceptionPoints: ['REQUEST'],
          inputConfiguration: { passRequestHeaders: false },
        },
      ],
      authorizerType: 'CUSTOM_JWT',
      authorizerConfiguration: {
        customJwtAuthorizer: {
          discoveryUrl: this.discoveryUrl,
          allowedClients: [
            this.userPoolClient.userPoolClientId,
            ...(this.workstreamUserPoolClient
              ? [this.workstreamUserPoolClient.userPoolClientId]
              : []),
          ],
          allowedScopes: [this.oauthScope],
        },
      },
      description:
        'Central OpenAI-compatible inference Gateway for pipeline-managed agents',
      tags: requiredTags(props),
    });
    this.gateway.node.addDependency(mantlePolicy);
    this.gateway.node.addDependency(interceptorInvokePolicy);
    this.gateway.node.addDependency(this.guardrailInterceptor);

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
        // Metadata only at synth. The client secret is merged in at deploy time
        // by an explicitly-named custom resource (below) reading it same-account
        // via DescribeUserPoolClient. Reading userPoolClient.userPoolClientSecret
        // here would emit a CDK-generated AwsCustomResource whose role name is
        // outside the scoped AgenticAI* CFN-exec boundary (live defect: the
        // exec role is denied iam:CreateRole for the generated name).
        secretObjectValue: {
          clientId: SecretValue.unsafePlainText(
            this.workstreamUserPoolClient!.userPoolClientId,
          ),
          issuer: SecretValue.unsafePlainText(
            `https://cognito-idp.${stack.region}.${stack.urlSuffix}/${this.userPool.userPoolId}`,
          ),
          authorizationEndpoint: SecretValue.unsafePlainText(
            `${this.userPoolDomain.baseUrl()}/oauth2/authorize`,
          ),
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
      // Deploy-time populator: an explicitly-named role (inside the AgenticAI*
      // boundary) reads the Cognito client secret same-account and merges it
      // into the metadata secret. Avoids the CDK-generated custom-resource role.
      const populatorRoleName = `AgenticAI-InferenceM2mSecret-${props.envName}`.slice(0, 64);
      const populatorRole = new Role(this, 'M2mSecretPopulatorRole', {
        roleName: populatorRoleName,
        assumedBy: new ServicePrincipal('lambda.amazonaws.com'),
        description:
          'Populates the inference M2M secret with the Cognito client secret at deploy time (same-account).',
        inlinePolicies: {
          Populate: new PolicyDocument({
            statements: [
              new PolicyStatement({
                sid: 'ReadCognitoClientSecret',
                effect: Effect.ALLOW,
                actions: ['cognito-idp:DescribeUserPoolClient'],
                resources: [this.userPool.userPoolArn],
              }),
              new PolicyStatement({
                sid: 'WriteM2mSecret',
                effect: Effect.ALLOW,
                actions: [
                  'secretsmanager:GetSecretValue',
                  'secretsmanager:PutSecretValue',
                ],
                resources: [this.m2mSecret.secretArn],
              }),
              new PolicyStatement({
                sid: 'EncryptM2mSecret',
                effect: Effect.ALLOW,
                actions: ['kms:GenerateDataKey', 'kms:Decrypt'],
                resources: [secretKey.keyArn],
              }),
            ],
          }),
        },
        managedPolicies: [
          ManagedPolicy.fromAwsManagedPolicyName(
            'service-role/AWSLambdaBasicExecutionRole',
          ),
        ],
      });
      const populatorFn = new LambdaFunction(this, 'M2mSecretPopulatorFn', {
        runtime: Runtime.PYTHON_3_13,
        handler: 'index.on_event',
        timeout: Duration.minutes(2),
        role: populatorRole,
        code: Code.fromInline(M2M_SECRET_POPULATOR_HANDLER),
      });
      const frameworkRole = new Role(this, 'M2mSecretPopulatorFwRole', {
        roleName: `AgenticAI-InferenceM2mSecretFw-${props.envName}`.slice(0, 64),
        assumedBy: new ServicePrincipal('lambda.amazonaws.com'),
        description:
          'Provider framework role for the inference M2M secret populator.',
        managedPolicies: [
          ManagedPolicy.fromAwsManagedPolicyName(
            'service-role/AWSLambdaBasicExecutionRole',
          ),
        ],
      });
      const populatorProvider = new Provider(this, 'M2mSecretPopulatorProvider', {
        onEventHandler: populatorFn,
        // Explicit framework role (inside the AgenticAI* boundary); a generated
        // role name would be denied iam:CreateRole by the scoped CFN exec role.
        frameworkOnEventRole: frameworkRole,
      });
      const populator = new CustomResource(this, 'M2mSecretPopulator', {
        serviceToken: populatorProvider.serviceToken,
        properties: {
          Region: stack.region,
          UserPoolId: this.userPool.userPoolId,
          ClientId: this.workstreamUserPoolClient!.userPoolClientId,
          SecretId: this.m2mSecret.secretArn,
          MetadataVersion: '3',
        },
      });
      populator.node.addDependency(this.m2mSecret);
      populator.node.addDependency(this.workstreamUserPoolClient!);
      populatorFn.grantInvoke(frameworkRole);
      NagSuppressions.addResourceSuppressions(
        frameworkRole,
        [
          {
            id: 'AwsSolutions-IAM4',
            reason:
              'SEC-005: AWSLambdaBasicExecutionRole is the standard log-write policy for the provider framework Lambda.',
          },
          {
            id: 'AwsSolutions-IAM5',
            reason:
              'SEC-005: the provider framework role invokes exactly its onEvent function; CDK renders the grant as a function ARN which cdk-nag flags generically.',
          },
        ],
        true,
      );
      NagSuppressions.addResourceSuppressions(
        populatorRole,
        [
          {
            id: 'AwsSolutions-IAM4',
            reason:
              'SEC-005: AWSLambdaBasicExecutionRole is the standard log-write policy for a custom-resource Lambda.',
          },
        ],
        true,
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
