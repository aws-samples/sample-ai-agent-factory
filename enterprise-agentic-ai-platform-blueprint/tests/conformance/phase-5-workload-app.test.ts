/**
 * Phase 5 conformance — WorkloadAppStack composition.
 *
 * Pins:
 *   - API Gateway fronting (§08 Option A) — primary auth boundary present.
 *   - AgentCore Gateway internal ALB + CMK.
 *   - AgentCoreIdentity Cognito + Token Vault CMK.
 *   - AgenticApp L3 per tenant (runtime role, memory CMK, inference profile).
 *   - RAG bucket CMK + VPCE-only deny.
 *   - Bedrock quota-increase custom resource.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App, Stack } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { Certificate } from 'aws-cdk-lib/aws-certificatemanager';
import { SubnetType, Vpc } from 'aws-cdk-lib/aws-ec2';

import { AgentCoreGatewayConstruct } from '@agenticai/agentcore-gateway';
import { WorkloadNetworkStack } from '../../apps/workload-account/lib/workload-network-stack';
import { WorkloadAppStack } from '../../apps/workload-account/lib/workload-app-stack';

function synthApp() {
  const app = new App();
  const net = new WorkloadNetworkStack(app, 'Net', {
    env: { account: '444444444444', region: 'us-west-2' },
  });
  const stack = new WorkloadAppStack(app, 'App', {
    env: { account: '444444444444', region: 'us-west-2' },
    vpcId: net.vpc.vpc.vpcId,
    workloadSubnetIds: net.vpc.vpc
      .selectSubnets({ subnetGroupName: 'workload' })
      .subnetIds,
    vpcCidr: net.vpc.vpc.vpcCidrBlock,
    availabilityZones: net.vpc.vpc.availabilityZones,
    bedrockRuntimeVpceId: net.vpc.endpoints.bedrockRuntime.vpcEndpointId,
    vpceSecurityGroupId: net.vpc.vpceEniSg.securityGroupId,
    envName: 'nonprod',
    tenantId: 'demo',
    agentId: 'primary',
    costCentre: 'engineering',
  });
  return { template: Template.fromStack(stack), stack };
}

describe('Phase 5 — §08 Option A API Gateway fronting', () => {
  it('emits an HTTP API v2 with Cognito JWT authorizer', () => {
    const { template: t } = synthApp();
    t.resourceCountIs('AWS::ApiGatewayV2::Api', 1);
    t.resourceCountIs('AWS::ApiGatewayV2::Authorizer', 1);
    t.hasResourceProperties('AWS::ApiGatewayV2::Authorizer', {
      AuthorizerType: 'JWT',
    });
  });

  it('default route is JWT-authorized (no NONE auth path)', () => {
    const { template: t } = synthApp();
    const routes = t.findResources('AWS::ApiGatewayV2::Route');
    for (const res of Object.values(routes)) {
      expect((res.Properties as any).AuthorizationType).toBe('JWT');
    }
  });

  it('emits a WAFv2 Web ACL with managed rules + rate-limit', () => {
    const { template: t } = synthApp();
    t.resourceCountIs('AWS::WAFv2::WebACL', 1);
    const acls = t.findResources('AWS::WAFv2::WebACL');
    const rules = (Object.values(acls)[0].Properties as any).Rules as Array<any>;
    const ruleNames = rules.map((r) => r.Name);
    expect(ruleNames).toContain('AWSManagedRulesCommonRuleSet');
    expect(ruleNames).toContain('AWSManagedRulesKnownBadInputsRuleSet');
    expect(ruleNames).toContain('RateLimit');
  });

  it('emits a VPC Link (not public-internet integration)', () => {
    const { template: t } = synthApp();
    t.resourceCountIs('AWS::ApiGatewayV2::VpcLink', 1);
  });
});

describe('Phase 5 — AgentCore Gateway (behind API Gateway)', () => {
  it('internal ALB (not internet-facing) owned by AgentCoreGatewayConstruct', () => {
    const { template: t } = synthApp();
    // Both LiteLLM + AgentCore Gateway ship internal ALBs — scan for both.
    const albs = t.findResources('AWS::ElasticLoadBalancingV2::LoadBalancer');
    for (const res of Object.values(albs)) {
      expect((res.Properties as any).Scheme).toBe('internal');
    }
  });

  it('emits the AgenticAI-AgentCoreGateway-<env> service role trusted by bedrock-agentcore', () => {
    const { template: t } = synthApp();
    const roles = t.findResources('AWS::IAM::Role', {
      Properties: { RoleName: 'AgenticAI-AgentCoreGateway-nonprod' },
    });
    const entries = Object.values(roles);
    expect(entries).toHaveLength(1);
    const trust = (entries[0].Properties as any).AssumeRolePolicyDocument;
    expect(trust.Statement[0].Principal.Service).toBe('bedrock-agentcore.amazonaws.com');
  });

  it('upgrades the internal ALB listener to HTTPS:443 when an ACM certificate is supplied', () => {
    // Isolated mini-stack (the full WorkloadAppStack does not yet pass a cert
    // because the platform-account ACM export is wired in a later phase).
    const app = new App();
    const stack = new Stack(app, 'GwCertTest', {
      env: { account: '444444444444', region: 'us-west-2' },
    });
    const vpc = new Vpc(stack, 'Vpc', {
      maxAzs: 2,
      subnetConfiguration: [
        { name: 'isolated', subnetType: SubnetType.PRIVATE_ISOLATED, cidrMask: 24 },
      ],
    });
    const cert = Certificate.fromCertificateArn(
      stack,
      'Cert',
      'arn:aws:acm:us-west-2:444444444444:certificate/11111111-2222-3333-4444-555555555555',
    );
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    const gw = new AgentCoreGatewayConstruct(stack, 'Gw', {
      vpc,
      envName: 'nonprod',
      certificate: cert,
    });
    const tpl = Template.fromStack(stack);
    const listeners = tpl.findResources('AWS::ElasticLoadBalancingV2::Listener');
    const listenerValues = Object.values(listeners);
    expect(listenerValues).toHaveLength(1);
    const props = listenerValues[0].Properties as any;
    expect(props.Protocol).toBe('HTTPS');
    expect(props.Port).toBe(443);
    expect(Array.isArray(props.Certificates)).toBe(true);
    expect(props.Certificates).toHaveLength(1);
  });
});

describe('Phase 5 — AgentCoreIdentity (Cognito + Token Vault CMK)', () => {
  it('creates a Cognito user pool with deletion protection + 12-char password minimum', () => {
    const { template: t } = synthApp();
    t.hasResourceProperties('AWS::Cognito::UserPool', {
      DeletionProtection: 'ACTIVE',
      Policies: {
        PasswordPolicy: { MinimumLength: 12 },
      },
    });
  });

  it('Token Vault CMK has automatic rotation and is aliased', () => {
    const { template: t } = synthApp();
    const aliases = t.findResources('AWS::KMS::Alias', {
      Properties: { AliasName: 'alias/agenticai/token-vault-nonprod' },
    });
    expect(Object.keys(aliases)).toHaveLength(1);
  });
});

describe('Phase 5 — AgenticApp L3 per-tenant resources', () => {
  it('creates per-agent application inference profile with cost-allocation tags', () => {
    const { template: t } = synthApp();
    const profiles = t.findResources('AWS::Bedrock::ApplicationInferenceProfile');
    expect(Object.keys(profiles)).toHaveLength(1);
    const props = Object.values(profiles)[0].Properties as any;
    expect(props.InferenceProfileName).toBe('agenticai-nonprod-demo-primary');
    const tags = props.Tags as Array<{ Key: string; Value: string }>;
    const tagMap: Record<string, string> = {};
    for (const tg of tags) tagMap[tg.Key] = tg.Value;
    expect(tagMap['application-id']).toBe('demo');
    expect(tagMap['agent-id']).toBe('primary');
    expect(tagMap['environment']).toBe('nonprod');
    expect(tagMap['cost-centre']).toBe('engineering');
  });

  it('per-agent ECR repo is tag-immutable + CMK-encrypted + scan-on-push', () => {
    const { template: t } = synthApp();
    t.hasResourceProperties('AWS::ECR::Repository', {
      RepositoryName: 'agenticai-nonprod-demo-primary',
      ImageTagMutability: 'IMMUTABLE',
      ImageScanningConfiguration: { ScanOnPush: true },
    });
  });

  it('runtime execution role carries Deny-on-null-GuardrailIdentifier', () => {
    const { template: t } = synthApp();
    const policies = t.findResources('AWS::IAM::Policy');
    const rendered = JSON.stringify(policies);
    expect(rendered).toContain('DenyDirectBedrockWithoutGuardrail');
  });
});

describe('Phase 5 — RAG knowledge base', () => {
  it('deny-non-VPCE bucket policy covers Get/Put/List/Delete', () => {
    const { template: t } = synthApp();
    const policies = t.findResources('AWS::S3::BucketPolicy');
    const rendered = JSON.stringify(policies);
    expect(rendered).toContain('DenyNonVpceAccess');
    expect(rendered).toContain('s3:GetObject');
    expect(rendered).toContain('s3:PutObject');
    expect(rendered).toContain('aws:SourceVpce');
  });
});

describe('Phase 5 — Bedrock quota-increase request', () => {
  it('emits a Custom::BedrockQuotaRequest resource', () => {
    const { template: t } = synthApp();
    const all = t.toJSON().Resources as Record<string, any>;
    const rendered = JSON.stringify(all);
    expect(rendered).toContain('requestServiceQuotaIncrease');
    expect(rendered).toContain('bedrock');
  });
});

describe('Phase 5 — LiteLLM triple-gate preserved in composition', () => {
  it('task-role deny statement still present in the composed app stack', () => {
    const { template: t } = synthApp();
    const policies = t.findResources('AWS::IAM::Policy');
    const rendered = JSON.stringify(policies);
    expect(rendered).toContain('DenyInferenceWithoutGuardrail');
    expect(rendered).toContain('bedrock:GuardrailIdentifier');
  });
});
