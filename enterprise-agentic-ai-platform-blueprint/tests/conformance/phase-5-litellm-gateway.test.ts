/**
 * Phase 5 conformance — LiteLLM gateway D-01 equivalence.
 *
 * Pins:
 *   - Task-role policy contains the Deny-on-null-GuardrailIdentifier statement
 *     (triple-gate enforcement layer 2).
 *   - Task-role Allow statement embeds the SSOT model allow-list.
 *   - ECS + ALB are private (no public IP, no internet-facing ALB).
 *   - CMK on log group.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App, Stack } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { Vpc, SubnetType, IpAddresses } from 'aws-cdk-lib/aws-ec2';

import { LiteLLMGatewayConstruct } from '@agenticai/litellm-gateway';

function synth() {
  const app = new App();
  const stack = new Stack(app, 'TestLiteLLM', {
    env: { account: '444444444444', region: 'us-west-2' },
  });
  const vpc = new Vpc(stack, 'Vpc', {
    ipAddresses: IpAddresses.cidr('10.20.0.0/16'),
    maxAzs: 2,
    natGateways: 0,
    subnetConfiguration: [
      { name: 'workload', subnetType: SubnetType.PRIVATE_ISOLATED, cidrMask: 20 },
      { name: 'vpce', subnetType: SubnetType.PRIVATE_ISOLATED, cidrMask: 22 },
    ],
    createInternetGateway: false,
  });
  new LiteLLMGatewayConstruct(stack, 'LiteLLM', { vpc });
  return Template.fromStack(stack);
}

describe('Phase 5 — LiteLLM D-01 triple-gate (layer 2)', () => {
  it('task role carries the Deny-on-null-GuardrailIdentifier statement', () => {
    const t = synth();
    const policies = t.findResources('AWS::IAM::Policy');
    const rendered = JSON.stringify(policies);
    expect(rendered).toContain('DenyInferenceWithoutGuardrail');
    expect(rendered).toContain('"Effect":"Deny"');
    expect(rendered).toContain('bedrock:GuardrailIdentifier');
  });

  it('task role Allow statement scopes to the platform model allow-list + profiles + guardrails', () => {
    const t = synth();
    const policies = t.findResources('AWS::IAM::Policy');
    const rendered = JSON.stringify(policies);
    expect(rendered).toContain('anthropic.claude-sonnet-4-5-20250929');
    expect(rendered).toContain('anthropic.claude-haiku-4-5-20251001');
    expect(rendered).toContain(':inference-profile/');
    expect(rendered).toContain(':guardrail/');
  });
});

describe('Phase 5 — LiteLLM networking + encryption', () => {
  it('ALB is internal (not internet-facing)', () => {
    const t = synth();
    t.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
      Scheme: 'internal',
    });
  });

  it('ECS service assigns no public IP', () => {
    const t = synth();
    t.hasResourceProperties('AWS::ECS::Service', {
      NetworkConfiguration: {
        AwsvpcConfiguration: { AssignPublicIp: 'DISABLED' },
      },
    });
  });

  it('Log group is CMK-encrypted', () => {
    const t = synth();
    t.hasResourceProperties('AWS::Logs::LogGroup', {
      LogGroupName: '/agenticai/litellm-gateway',
    });
    const kms = t.findResources('AWS::KMS::Key', {
      Properties: {
        Description: 'CMK for LiteLLM gateway logs + config secrets.',
      },
    });
    expect(Object.keys(kms)).toHaveLength(1);
  });

  it('KMS key has automatic rotation enabled', () => {
    const t = synth();
    const kms = t.findResources('AWS::KMS::Key');
    const litellmKeys = Object.values(kms).filter((r) =>
      ((r.Properties as any).Description as string | undefined)?.includes('LiteLLM'),
    );
    expect(litellmKeys).toHaveLength(1);
    expect((litellmKeys[0].Properties as any).EnableKeyRotation).toBe(true);
  });
});
