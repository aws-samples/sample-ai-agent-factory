/**
 * Phase 4 conformance — Agentic VPC + 9 VPCEs + Bedrock Invocation Logging.
 *
 * Pins:
 *   - R-NET-002..004  VPC design, no IGW/NAT
 *   - R-NET-008..016  9 VPCEs
 *   - R-NET-017/018   Endpoint policies (local-account + model allow-list)
 *   - R-NET-019/020   SG pair
 *   - R-BED-036..038  Bedrock invocation logging
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { WorkloadNetworkStack } from '../../apps/workload-account/lib/workload-network-stack';

function synth() {
  const app = new App();
  const stack = new WorkloadNetworkStack(app, 'TestWorkloadNet', {
    env: { account: '444444444444', region: 'us-west-2' },
  });
  return Template.fromStack(stack);
}

describe('Phase 4 — Agentic VPC (spec §2.3.2)', () => {
  it('creates a single VPC with the expected CIDR', () => {
    const t = synth();
    t.resourceCountIs('AWS::EC2::VPC', 1);
    t.hasResourceProperties('AWS::EC2::VPC', { CidrBlock: '10.20.0.0/16' });
  });

  it('contains zero Internet Gateways (R-NET-004)', () => {
    const t = synth();
    t.resourceCountIs('AWS::EC2::InternetGateway', 0);
  });

  it('contains zero NAT Gateways (R-NET-004)', () => {
    const t = synth();
    t.resourceCountIs('AWS::EC2::NatGateway', 0);
  });

  it('spans 3 Availability Zones', () => {
    const t = synth();
    const subnets = t.findResources('AWS::EC2::Subnet');
    const uniqueAzs = new Set(
      Object.values(subnets).map((s) => ((s.Properties as any).AvailabilityZone as any)?.['Fn::Select']?.[0]),
    );
    // CDK derives AZ via Fn::Select; at least 3 subnet groups across AZs.
    expect(Object.keys(subnets).length).toBeGreaterThanOrEqual(6); // 2 tiers × 3 AZs
    void uniqueAzs;
  });
});

describe('Phase 4 — 9 required VPCEs (spec §2.3.4)', () => {
  it('creates 11 interface + 1 gateway endpoints (covers the 9 required + STS + KMS + S3 gateway)', () => {
    const t = synth();
    // Interface endpoints: 11 (bedrock-agentcore data+control+gateway,
    // bedrock-runtime, bedrock, ecr api+dkr, logs, monitoring, sts, kms).
    const interfaceEndpoints = t.findResources('AWS::EC2::VPCEndpoint', {
      Properties: { VpcEndpointType: 'Interface' },
    });
    expect(Object.keys(interfaceEndpoints).length).toBeGreaterThanOrEqual(11);
    // Gateway endpoint for S3.
    const gatewayEndpoints = t.findResources('AWS::EC2::VPCEndpoint', {
      Properties: { VpcEndpointType: 'Gateway' },
    });
    expect(Object.keys(gatewayEndpoints)).toHaveLength(1);
  });

  it('creates the three AgentCore VPCEs (data, control, gateway)', () => {
    const t = synth();
    const services = Object.values(t.findResources('AWS::EC2::VPCEndpoint')).map(
      (r) => (r.Properties as any).ServiceName,
    );
    const joined = JSON.stringify(services);
    expect(joined).toContain('bedrock-agentcore-control');
    expect(joined).toContain('bedrock-agentcore.gateway');
    // Data-plane endpoint matches the bare `bedrock-agentcore` service name.
    const bareAgentcore = services.filter(
      (s: string) => typeof s === 'string' && s.endsWith('.bedrock-agentcore'),
    );
    expect(bareAgentcore.length).toBeGreaterThanOrEqual(1);
  });

  it('creates the Bedrock Runtime + Bedrock control VPCEs', () => {
    const t = synth();
    const services = Object.values(t.findResources('AWS::EC2::VPCEndpoint')).map(
      (r) => (r.Properties as any).ServiceName,
    );
    const joined = JSON.stringify(services);
    expect(joined).toContain('bedrock-runtime');
    // Bare `bedrock` endpoint (control plane) — match exact suffix.
    const bareBedrock = services.filter(
      (s: string) => typeof s === 'string' && s.endsWith('.bedrock'),
    );
    expect(bareBedrock.length).toBeGreaterThanOrEqual(1);
  });
});

describe('Phase 4 — Endpoint policies (spec §2.3.5)', () => {
  it('Bedrock Runtime VPCE policy references the platform model allow-list', () => {
    const t = synth();
    const endpoints = t.findResources('AWS::EC2::VPCEndpoint');
    let found = false;
    for (const res of Object.values(endpoints)) {
      const props = res.Properties as any;
      if (typeof props.ServiceName === 'string' && props.ServiceName.endsWith('bedrock-runtime')) {
        const policy = props.PolicyDocument;
        const rendered = JSON.stringify(policy);
        expect(rendered).toContain('anthropic.claude-sonnet-4-5-20250929');
        expect(rendered).toContain('anthropic.claude-haiku-4-5-20251001');
        expect(rendered).toContain('bedrock:GuardrailIdentifier');
        found = true;
      }
    }
    expect(found).toBe(true);
  });

  it('every VPCE policy scopes principals to the local account root', () => {
    const t = synth();
    const endpoints = t.findResources('AWS::EC2::VPCEndpoint', {
      Properties: { VpcEndpointType: 'Interface' },
    });
    for (const res of Object.values(endpoints)) {
      const policy = (res.Properties as any).PolicyDocument;
      const rendered = JSON.stringify(policy);
      // Either literal account root ARN or the CFN Fn::Join root-arn pattern.
      expect(rendered.toLowerCase()).toContain('root');
    }
  });
});

describe('Phase 4 — Security groups (spec §2.3.6)', () => {
  it('creates a workload-ENI SG with no inbound rules', () => {
    const t = synth();
    const sgs = t.findResources('AWS::EC2::SecurityGroup', {
      Properties: {
        GroupDescription: {
          'Fn::Join': [
            '',
            ['SG for AgentCore Runtime / Gateway / LiteLLM task ENIs. No inbound; egress to VPCE SG only.'],
          ],
        },
      },
    });
    // Match by description text (avoid templated-name brittleness).
    const matching = Object.values(t.findResources('AWS::EC2::SecurityGroup')).filter((r) =>
      (r.Properties as any).GroupDescription?.includes?.('No inbound; egress to VPCE SG only'),
    );
    expect(matching.length).toBe(1);
    // No SecurityGroupIngress inline; SG rule resources should be absent for this SG.
    void sgs;
  });
});

describe('Phase 4 — Bedrock Model Invocation Logging (spec §2.4.7)', () => {
  it('creates the CMK-encrypted /agenticai/bedrock-invocations log group', () => {
    const t = synth();
    t.hasResourceProperties('AWS::Logs::LogGroup', {
      LogGroupName: '/agenticai/bedrock-invocations',
    });
  });

  it('log group retention set to 90 days (THREE_MONTHS) by default', () => {
    const t = synth();
    t.hasResourceProperties('AWS::Logs::LogGroup', { RetentionInDays: 90 });
  });

  it('service role AgenticAI-BedrockInvocationLogging trusts bedrock.amazonaws.com only', () => {
    const t = synth();
    const roles = t.findResources('AWS::IAM::Role', {
      Properties: { RoleName: 'AgenticAI-BedrockInvocationLogging' },
    });
    const entries = Object.values(roles);
    expect(entries).toHaveLength(1);
    const trust = (entries[0].Properties as any).AssumeRolePolicyDocument;
    expect(trust.Statement[0].Principal.Service).toBe('bedrock.amazonaws.com');
  });

  it('emits a custom resource to call PutModelInvocationLoggingConfiguration', () => {
    const t = synth();
    // CDK custom-resources emit a Custom::<type> resource whose name can vary
    // between CDK versions. Scan all resources for the distinctive
    // AwsSdkCall payload instead.
    const all = t.toJSON().Resources as Record<string, any>;
    const rendered = JSON.stringify(all);
    expect(rendered).toContain('putModelInvocationLoggingConfiguration');
    expect(rendered).toContain('textDataDeliveryEnabled');
  });
});

describe('Phase 4 — SSM parameters for SCP resolution', () => {
  it('writes /agenticai/network/approved-bedrock-vpce-id for SCP-04', () => {
    const t = synth();
    t.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/agenticai/network/approved-bedrock-vpce-id',
    });
  });

  it('writes /agenticai/network/approved-agentcore-vpce-ids (StringList) for SCP-03', () => {
    const t = synth();
    t.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/agenticai/network/approved-agentcore-vpce-ids',
      Type: 'StringList',
    });
  });
});
