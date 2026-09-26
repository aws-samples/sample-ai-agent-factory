import { App, Stack } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { InferenceGatewayStack } from '../../apps/platform-account/lib/inference-gateway-stack';

import {
  PlatformInferenceGatewayConstruct,
  type InferenceModelRateLimit,
} from '@agenticai/platform-inference-gateway';

const GUARDRAIL = {
  guardrailIdentifier: 'abcdef123456',
  guardrailVersion: 'DRAFT',
  guardrailArn:
    'arn:aws:bedrock:us-west-2:123456789012:guardrail/abcdef123456',
} as const;

const MODEL_LIMITS: readonly InferenceModelRateLimit[] = [
  {
    qualifiedModelId: 'openai.gpt-oss-120b',
    requestsPerMinute: 10,
    tokensPerMinute: 10_000,
  },
  {
    qualifiedModelId: 'anthropic.claude-sonnet-4-5-20250929-v1:0',
    requestsPerMinute: 20,
    tokensPerMinute: 40_000,
  },
];

function synth(
  modelRateLimits: readonly InferenceModelRateLimit[] = MODEL_LIMITS,
): Template {
  const app = new App();
  const stack = new Stack(app, 'TestPlatformInferenceGateway', {
    env: { account: '123456789012', region: 'us-west-2' },
  });
  new PlatformInferenceGatewayConstruct(stack, 'InferenceGateway', {
    envName: 'nonprod',
    applicationId: 'platform-inference',
    agentId: 'shared',
    tenantId: 'shared',
    costCentre: 'platform',
    modelRateLimits,
    inputGuardrail: GUARDRAIL,
  });
  return Template.fromStack(stack);
}

function synthWithReaders(
  readerAccountIds: readonly string[] | undefined,
): Template {
  const app = new App();
  const stack = new Stack(app, 'TestPlatformInferenceGatewayReaders', {
    env: { account: '123456789012', region: 'us-west-2' },
  });
  new PlatformInferenceGatewayConstruct(stack, 'InferenceGateway', {
    envName: 'nonprod',
    applicationId: 'platform-inference',
    agentId: 'shared',
    tenantId: 'shared',
    costCentre: 'platform',
    modelRateLimits: MODEL_LIMITS,
    inputGuardrail: GUARDRAIL,
    m2mSecretReaderAccountIds: readerAccountIds,
  });
  return Template.fromStack(stack);
}

function onlyResource(
  template: Template,
  type: string,
): Record<string, unknown> {
  const resources = template.findResources(type);
  expect(Object.keys(resources)).toHaveLength(1);
  return Object.values(resources)[0] as Record<string, unknown>;
}

function properties(resource: Record<string, unknown>): Record<string, unknown> {
  return resource.Properties as Record<string, unknown>;
}

function policiesBySid(template: Template): Record<string, Record<string, unknown>> {
  const bySid: Record<string, Record<string, unknown>> = {};
  for (const resource of Object.values(
    template.findResources('AWS::IAM::Policy'),
  )) {
    const props = properties(resource as Record<string, unknown>);
    for (const statement of (props.PolicyDocument as { Statement: any[] })
      .Statement) {
      bySid[statement.Sid] = props;
    }
  }
  return bySid;
}

function mantlePolicy(template: Template): Record<string, unknown> {
  const policy = policiesBySid(template).InvokeAllocatedBedrockMantleModels;
  expect(policy).toBeDefined();
  return policy;
}

describe('Phase 9 — native Platform inference Gateway', () => {
  it('emits Gateway, inference target and native rate limit without the old load balancers', () => {
    const template = synth();
    template.resourceCountIs('AWS::BedrockAgentCore::Gateway', 1);
    template.resourceCountIs('AWS::BedrockAgentCore::GatewayTarget', 1);
    template.resourceCountIs('AWS::BedrockAgentCore::GatewayRateLimit', 1);
    template.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 0);
    template.resourceCountIs('AWS::EC2::VPCEndpointService', 0);
    // The only Lambda on the default path is the guardrail REQUEST interceptor.
    template.resourceCountIs('AWS::Lambda::Function', 1);
  });

  it('uses MCP and the Cognito custom-JWT authorizer', () => {
    const template = synth();
    const gateway = properties(
      onlyResource(template, 'AWS::BedrockAgentCore::Gateway'),
    );
    expect(gateway.ProtocolType).toBe('MCP');
    expect(gateway.ProtocolConfiguration).toEqual({
      Mcp: { SupportedVersions: ['2025-11-25'] },
    });
    expect(gateway.AuthorizerType).toBe('CUSTOM_JWT');
    const authorizer = gateway.AuthorizerConfiguration as {
      CustomJWTAuthorizer: {
        DiscoveryUrl: unknown;
        AllowedClients: unknown[];
        AllowedScopes: string[];
      };
    };
    expect(authorizer.CustomJWTAuthorizer.AllowedClients).toEqual([
      expect.objectContaining({ Ref: expect.any(String) }),
    ]);
    expect(authorizer.CustomJWTAuthorizer.AllowedScopes).toEqual([
      'agenticai-inference-nonprod-api/invoke',
    ]);
    expect(
      JSON.stringify(authorizer.CustomJWTAuthorizer.DiscoveryUrl),
    ).toContain('/.well-known/openid-configuration');
  });

  it('applies all five required tags to the taggable Gateway', () => {
    const gateway = properties(
      onlyResource(synth(), 'AWS::BedrockAgentCore::Gateway'),
    );
    expect(gateway.Tags).toEqual({
      'application-id': 'platform-inference',
      'agent-id': 'shared',
      'tenant-id': 'shared',
      'cost-centre': 'platform',
      environment: 'nonprod',
    });
  });

  it('derives the OAuth endpoint from the Cognito hosted domain', () => {
    const app = new App();
    const stack = new Stack(app, 'TokenEndpointStack', {
      env: { account: '123456789012', region: 'us-west-2' },
    });
    const gateway = new PlatformInferenceGatewayConstruct(
      stack,
      'InferenceGateway',
      {
        envName: 'nonprod',
        applicationId: 'platform-inference',
        agentId: 'shared',
        tenantId: 'shared',
        costCentre: 'platform',
        modelRateLimits: MODEL_LIMITS,
        inputGuardrail: GUARDRAIL,
      },
    );

    expect(gateway.tokenEndpoint).toContain(
      '.auth.us-west-2.amazoncognito.com/oauth2/token',
    );
    expect(gateway.tokenEndpoint).not.toContain(
      '.auth.us-west-2.amazonaws.com/oauth2/token',
    );
  });

  it('provisions a confidential client-credentials Cognito client', () => {
    const template = synth();
    template.resourceCountIs('AWS::Cognito::UserPool', 1);
    template.resourceCountIs('AWS::Cognito::UserPoolResourceServer', 1);
    template.resourceCountIs('AWS::Cognito::UserPoolDomain', 1);
    template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
      GenerateSecret: true,
      AllowedOAuthFlows: ['client_credentials'],
      AllowedOAuthFlowsUserPoolClient: true,
      AccessTokenValidity: 5,
      TokenValidityUnits: { AccessToken: 'minutes' },
    });
    template.hasResourceProperties('AWS::Cognito::UserPoolResourceServer', {
      Identifier: 'agenticai-inference-nonprod-api',
      Scopes: [
        {
          ScopeName: 'invoke',
          ScopeDescription: 'Invoke the central AgentCore inference Gateway',
        },
      ],
    });
    const resourceServers = template.findResources(
      'AWS::Cognito::UserPoolResourceServer',
    );
    const resourceServerLogicalId = Object.keys(resourceServers)[0];
    const client = properties(
      onlyResource(template, 'AWS::Cognito::UserPoolClient'),
    );
    const scopes = JSON.stringify(client.AllowedOAuthScopes);
    expect(scopes).toContain(resourceServerLogicalId);
    expect(scopes).toContain('/invoke');
  });

  it('creates the live-proven Bedrock Mantle inference connector target', () => {
    const template = synth();
    const gatewayResources = template.findResources(
      'AWS::BedrockAgentCore::Gateway',
    );
    const gatewayLogicalId = Object.keys(gatewayResources)[0];
    template.hasResourceProperties('AWS::BedrockAgentCore::GatewayTarget', {
      GatewayIdentifier: { Ref: gatewayLogicalId },
      TargetConfiguration: {
        Inference: {
          Connector: { Source: { ConnectorId: 'bedrock-mantle' } },
        },
      },
      CredentialProviderConfigurations: [
        { CredentialProviderType: 'GATEWAY_IAM_ROLE' },
      ],
    });
  });

  it('outputs the target name used to qualify discovered model routes', () => {
    const stack = new InferenceGatewayStack(new App(), 'InferenceGatewayStack', {
      env: { account: '123456789012', region: 'us-west-2' },
      envName: 'nonprod',
      applicationId: 'platform-inference',
      agentId: 'shared',
      tenantId: 'shared',
      costCentre: 'platform',
      modelRateLimits: MODEL_LIMITS,
      inputGuardrail: GUARDRAIL,
    });
    const template = Template.fromStack(stack);

    expect(stack.inferenceGateway.inferenceTargetName).toBe(
      'agenticai-inference-nonprod-bedrock',
    );
    template.hasResourceProperties('AWS::BedrockAgentCore::GatewayTarget', {
      Name: 'agenticai-inference-nonprod-bedrock',
    });
    template.hasOutput('InferenceTargetName', {
      Value: 'agenticai-inference-nonprod-bedrock',
    });
  });

  it('allows configured model RPM/TPM and zero-rates the wildcard fallback', () => {
    const rateLimit = properties(
      onlyResource(synth(), 'AWS::BedrockAgentCore::GatewayRateLimit'),
    );
    expect(rateLimit.DimensionKeys).toEqual(['qualifiedModelId']);
    expect(rateLimit.Entries).toEqual([
      {
        Dimensions: { qualifiedModelId: 'openai.gpt-oss-120b' },
        Requests: [{ Rate: 10, Period: 'minute' }],
        Tokens: [{ Rate: 10_000, Period: 'minute' }],
      },
      {
        Dimensions: {
          qualifiedModelId: 'anthropic.claude-sonnet-4-5-20250929-v1:0',
        },
        Requests: [{ Rate: 20, Period: 'minute' }],
        Tokens: [{ Rate: 40_000, Period: 'minute' }],
      },
      {
        Dimensions: { qualifiedModelId: '*' },
        Requests: [{ Rate: 0, Period: 'second' }],
      },
    ]);
  });
});

describe('Phase 9 — Gateway IAM boundary and lifecycle ordering', () => {
  it('trusts AgentCore only from this account and named Gateway ARN', () => {
    const roles = Object.values(synth().findResources('AWS::IAM::Role')).map(
      (resource) => properties(resource as Record<string, unknown>),
    );
    const role = roles.find(
      (candidate) => candidate.RoleName === 'AgenticAI-InferenceGateway-nonprod',
    ) as Record<string, unknown>;
    expect(role).toBeDefined();
    const trust = role.AssumeRolePolicyDocument as {
      Statement: Array<{
        Action: string;
        Effect: string;
        Principal: Record<string, string>;
        Condition: {
          StringEquals: Record<string, string>;
          ArnLike: Record<string, unknown>;
        };
      }>;
      Version: string;
    };
    expect(trust.Version).toBe('2012-10-17');
    expect(trust.Statement).toHaveLength(1);
    expect(trust.Statement[0]).toMatchObject({
      Action: 'sts:AssumeRole',
      Effect: 'Allow',
      Principal: { Service: 'bedrock-agentcore.amazonaws.com' },
      Condition: {
        StringEquals: { 'aws:SourceAccount': '123456789012' },
      },
    });
    const sourceArn = JSON.stringify(
      trust.Statement[0].Condition.ArnLike['aws:SourceArn'],
    );
    expect(sourceArn).toContain('AWS::Partition');
    expect(sourceArn).toContain(
      ':bedrock-agentcore:us-west-2:123456789012:gateway/agenticai-inference-nonprod-*',
    );
  });

  it('grants exactly the two live-proven Bedrock Mantle actions', () => {
    const policy = JSON.stringify(mantlePolicy(synth()));
    expect(policy).toContain('bedrock-mantle:ListModels');
    expect(policy).toContain('bedrock-mantle:CreateInference');
    expect(policy).not.toContain('bedrock:InvokeModel');
  });

  it('pins CreateInference to the allocated models with bedrock-mantle:Model (fail closed)', () => {
    const statements = (
      mantlePolicy(synth()).PolicyDocument as { Statement: any[] }
    ).Statement;
    const invoke = statements.find(
      (statement) => statement.Sid === 'InvokeAllocatedBedrockMantleModels',
    );
    expect(invoke.Action).toEqual('bedrock-mantle:CreateInference');
    expect(invoke.Condition).toEqual({
      StringEquals: {
        'bedrock-mantle:Model': [
          'anthropic.claude-sonnet-4-5-20250929-v1:0',
          'claude-sonnet-4-5-20250929-v1:0',
          'gpt-oss-120b',
          'openai.gpt-oss-120b',
        ],
      },
    });
    const list = statements.find(
      (statement) => statement.Sid === 'ListBedrockMantleModels',
    );
    expect(list.Action).toEqual('bedrock-mantle:ListModels');
    expect(list.Condition).toBeUndefined();
  });

  it('orders target after Gateway and rate limit after target', () => {
    const template = synth();
    const targetResources = template.findResources(
      'AWS::BedrockAgentCore::GatewayTarget',
    );
    const targetLogicalId = Object.keys(targetResources)[0];
    const rateLimit = onlyResource(
      template,
      'AWS::BedrockAgentCore::GatewayRateLimit',
    );
    const dependencies = rateLimit.DependsOn as string[];
    expect(dependencies).toContain(targetLogicalId);
  });
});

describe('Phase 9 — fail-closed configuration validation', () => {
  function constructWith(
    modelRateLimits: readonly InferenceModelRateLimit[],
  ): () => PlatformInferenceGatewayConstruct {
    return () => {
      const stack = new Stack(new App(), 'Reject', {
        env: { account: '123456789012', region: 'us-west-2' },
      });
      return new PlatformInferenceGatewayConstruct(stack, 'Gateway', {
        envName: 'nonprod',
        applicationId: 'platform-inference',
        agentId: 'shared',
        tenantId: 'shared',
        costCentre: 'platform',
        modelRateLimits,
        inputGuardrail: GUARDRAIL,
      });
    };
  }

  it('rejects an empty model allocation', () => {
    expect(constructWith([])).toThrow(/at least one allowed model/i);
  });

  it('rejects connector-prefixed model IDs', () => {
    expect(
      constructWith([
        {
          qualifiedModelId: 'bedrock-mantle/openai.gpt-oss-120b',
          requestsPerMinute: 1,
          tokensPerMinute: 1,
        },
      ]),
    ).toThrow(/without a connector prefix/i);
  });

  it('rejects duplicate models', () => {
    expect(constructWith([MODEL_LIMITS[0], MODEL_LIMITS[0]])).toThrow(
      /duplicate qualifiedModelId/i,
    );
  });

  it.each([0, -1, 1.5, Number.MAX_SAFE_INTEGER])(
    'rejects an invalid positive rate %s',
    (requestsPerMinute) => {
      expect(
        constructWith([
          {
            qualifiedModelId: 'openai.gpt-oss-120b',
            requestsPerMinute,
            tokensPerMinute: 10_000,
          },
        ]),
      ).toThrow(/requestsPerMinute must be an integer/i);
    },
  );
});

describe('Phase 9 — opt-in cross-account M2M secret', () => {
  it('creates no secret by default', () => {
    const template = synthWithReaders(undefined);
    expect(
      Object.keys(template.findResources('AWS::SecretsManager::Secret')),
    ).toHaveLength(0);
  });

  it('publishes a CMK-encrypted secret readable by the declared account', () => {
    const template = synthWithReaders(['444444444444']);
    template.resourceCountIs('AWS::Cognito::UserPoolClient', 2);
    const gateway = onlyResource(template, 'AWS::BedrockAgentCore::Gateway');
    const allowedClients = (
      (gateway.Properties as any).AuthorizerConfiguration.CustomJWTAuthorizer
        .AllowedClients as unknown[]
    );
    expect(allowedClients).toHaveLength(2);
    const secret = onlyResource(template, 'AWS::SecretsManager::Secret');
    // Encrypted with a dedicated CMK (KmsKeyId present).
    expect((secret.Properties as Record<string, unknown>).KmsKeyId).toBeDefined();
    // Resource policy grants GetSecretValue to the reader account.
    const policy = onlyResource(
      template,
      'AWS::SecretsManager::ResourcePolicy',
    );
    const doc = JSON.stringify((policy.Properties as Record<string, unknown>).ResourcePolicy);
    expect(doc).toContain('secretsmanager:GetSecretValue');
    expect(doc).toContain('444444444444');
    // The client secret must NOT be read at synth (no CDK-generated Cognito
    // lookup custom resource); it is merged in by the named populator instead.
    const templateJson = JSON.stringify(template.toJSON());
    expect(templateJson).not.toContain('DescribeCognitoUserPoolClient');
    expect(templateJson).toContain('authorizationEndpoint');
    expect(templateJson).toContain('issuer');
    expect(templateJson).toContain('MetadataVersion');
    // The explicitly-named populator role exists (inside the AgenticAI* boundary).
    const roleNames = Object.values(
      template.findResources('AWS::IAM::Role'),
    ).map((r: any) => r.Properties?.RoleName);
    expect(
      roleNames.some(
        (n: unknown) =>
          typeof n === 'string' && n.startsWith('AgenticAI-InferenceM2mSecret'),
      ),
    ).toBe(true);
  });

  it('rejects a non-12-digit reader account id', () => {
    expect(() => synthWithReaders(['not-an-account'])).toThrow(
      /12-digit account IDs/,
    );
  });
});

describe('Phase 9 — server-side guardrail enforcement (REQUEST interceptor)', () => {
  it('attaches exactly one REQUEST interceptor to the Gateway and never passes headers', () => {
    const template = synth();
    const gateway = properties(
      onlyResource(template, 'AWS::BedrockAgentCore::Gateway'),
    );
    const functions = template.findResources('AWS::Lambda::Function');
    const functionLogicalId = Object.keys(functions)[0];
    expect(gateway.InterceptorConfigurations).toEqual([
      {
        Interceptor: {
          Lambda: { Arn: { 'Fn::GetAtt': [functionLogicalId, 'Arn'] } },
        },
        InterceptionPoints: ['REQUEST'],
        InputConfiguration: { PassRequestHeaders: false },
      },
    ]);
    expect(gateway.PolicyEngineConfiguration).toBeUndefined();
  });

  it('configures the interceptor with the guardrail identity and a bounded text budget', () => {
    const fn = properties(onlyResource(synth(), 'AWS::Lambda::Function'));
    expect(fn.FunctionName).toBe('agenticai-inference-guardrail-nonprod');
    expect(fn.Handler).toBe('index.handler');
    expect(fn.Runtime).toBe('python3.13');
    expect(fn.Timeout).toBe(25);
    expect((fn.Environment as any).Variables).toEqual({
      GUARDRAIL_IDENTIFIER: 'abcdef123456',
      GUARDRAIL_VERSION: 'DRAFT',
      MAX_GUARDED_CHARACTERS: '200000',
      ENV_NAME: 'nonprod',
    });
    // Shipped as a file asset (real handler), not an inline stub.
    expect((fn.Code as any).S3Bucket).toBeDefined();
    expect((fn.Code as any).ZipFile).toBeUndefined();
  });

  it('grants the interceptor role ApplyGuardrail on exactly the platform guardrail ARN', () => {
    const template = synth();
    const roles = Object.values(template.findResources('AWS::IAM::Role')).map(
      (resource) => properties(resource as Record<string, unknown>),
    );
    const role = roles.find(
      (candidate) => candidate.RoleName === 'AgenticAI-InferenceGuardrail-nonprod',
    ) as any;
    expect(role).toBeDefined();
    expect(role.AssumeRolePolicyDocument.Statement[0].Principal).toEqual({
      Service: 'lambda.amazonaws.com',
    });
    const statements = role.Policies[0].PolicyDocument.Statement;
    expect(statements).toEqual([
      {
        Sid: 'ApplyPlatformGuardrail',
        Effect: 'Allow',
        Action: 'bedrock:ApplyGuardrail',
        Resource: 'arn:aws:bedrock:us-west-2:123456789012:guardrail/abcdef123456',
      },
    ]);
    expect(JSON.stringify(role)).not.toContain('bedrock-mantle');
  });

  it('lets the Gateway role invoke exactly the interceptor function and orders the Gateway after that grant', () => {
    const template = synth();
    const invoke = policiesBySid(template).InvokeGuardrailInterceptor as any;
    const functionLogicalId = Object.keys(
      template.findResources('AWS::Lambda::Function'),
    )[0];
    const statement = invoke.PolicyDocument.Statement.find(
      (candidate: any) => candidate.Sid === 'InvokeGuardrailInterceptor',
    );
    expect(statement.Action).toBe('lambda:InvokeFunction');
    expect(statement.Resource).toEqual({
      'Fn::GetAtt': [functionLogicalId, 'Arn'],
    });
    expect(invoke.Roles).toEqual([
      { Ref: expect.stringMatching(/GatewayRole/) },
    ]);
    const gateway = onlyResource(template, 'AWS::BedrockAgentCore::Gateway');
    const policyLogicalIds = Object.entries(
      template.findResources('AWS::IAM::Policy'),
    )
      .filter(([, resource]) =>
        JSON.stringify(resource).includes('InvokeGuardrailInterceptor'),
      )
      .map(([logicalId]) => logicalId);
    expect(policyLogicalIds).toHaveLength(1);
    expect(gateway.DependsOn as string[]).toEqual(
      expect.arrayContaining([policyLogicalIds[0], functionLogicalId]),
    );
  });

  it('refuses to synthesize without a guardrail or with a blank guardrail field', () => {
    const build = (inputGuardrail: unknown) => () => {
      const stack = new Stack(new App(), 'NoGuardrail', {
        env: { account: '123456789012', region: 'us-west-2' },
      });
      return new PlatformInferenceGatewayConstruct(stack, 'Gateway', {
        envName: 'nonprod',
        applicationId: 'platform-inference',
        agentId: 'shared',
        tenantId: 'shared',
        costCentre: 'platform',
        modelRateLimits: MODEL_LIMITS,
        inputGuardrail: inputGuardrail as any,
      });
    };
    expect(build(undefined)).toThrow(/inputGuardrail is required/);
    expect(build({ ...GUARDRAIL, guardrailVersion: ' ' })).toThrow(
      /inputGuardrail\.guardrailVersion must be a non-empty string/,
    );
    expect(build({ ...GUARDRAIL, guardrailArn: '' })).toThrow(
      /inputGuardrail\.guardrailArn must be a non-empty string/,
    );
  });

  it('exposes the interceptor and enforced guardrail as stack outputs', () => {
    const stack = new InferenceGatewayStack(new App(), 'OutputsStack', {
      env: { account: '123456789012', region: 'us-west-2' },
      envName: 'prod',
      applicationId: 'platform-inference',
      agentId: 'shared',
      tenantId: 'shared',
      costCentre: 'platform',
      modelRateLimits: MODEL_LIMITS,
      inputGuardrail: GUARDRAIL,
    });
    const template = Template.fromStack(stack);
    template.hasOutput('GuardrailInterceptorFunctionArn', {});
    template.hasOutput('EnforcedGuardrailIdentifier', { Value: 'abcdef123456' });
    template.hasOutput('EnforcedGuardrailVersion', { Value: 'DRAFT' });
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'agenticai-inference-guardrail-prod',
    });
  });
});
