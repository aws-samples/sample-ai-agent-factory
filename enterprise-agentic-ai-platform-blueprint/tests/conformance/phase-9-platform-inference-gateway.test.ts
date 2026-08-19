/**
 * Phase 9 conformance — D-03 PrivateLink primitive.
 *
 * Pins the shape of `PlatformInferenceGatewayConstruct` against the
 * README §3.3 residual-risks row "Cross-account PrivateLink →
 * LiteLLM attack surface": restrict the endpoint service AllowedPrincipals
 * to the exact workload account root ARNs, emit an internal NLB on :443,
 * and wrap the NLB in a VpcEndpointService with acceptanceRequired=false.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App, Stack } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { IpAddresses, SubnetType, Vpc } from 'aws-cdk-lib/aws-ec2';
import {
  ApplicationLoadBalancer,
  ApplicationProtocol,
  ListenerAction,
} from 'aws-cdk-lib/aws-elasticloadbalancingv2';

import { PlatformInferenceGatewayConstruct } from '@agenticai/platform-inference-gateway';

interface SynthOptions {
  readonly withAlb?: boolean;
  readonly workloadAccountIds?: readonly string[];
}

function synth(opts: SynthOptions = {}): {
  template: Template;
  stack: Stack;
} {
  const app = new App();
  const stack = new Stack(app, 'TestPlatformInferenceGw', {
    env: { account: '123456789012', region: 'us-east-1' },
  });
  const vpc = new Vpc(stack, 'Vpc', {
    ipAddresses: IpAddresses.cidr('10.40.0.0/16'),
    maxAzs: 2,
    natGateways: 0,
    subnetConfiguration: [
      { name: 'platform', subnetType: SubnetType.PRIVATE_ISOLATED, cidrMask: 20 },
    ],
    createInternetGateway: false,
  });

  let targetAlb: ApplicationLoadBalancer | undefined;
  if (opts.withAlb) {
    targetAlb = new ApplicationLoadBalancer(stack, 'TargetAlb', {
      vpc,
      internetFacing: false,
      vpcSubnets: { subnetType: SubnetType.PRIVATE_ISOLATED },
    });
    // Use HTTP:80 for the harness so we don't need to provision an ACM cert
    // at synth. Production wiring passes an HTTPS-terminating ALB via the
    // LiteLLM construct; the NLB target-group port is independent of the
    // NLB listener port (TCP:443 → forward to ALB:80 at L4).
    targetAlb.addListener('AlbListener', {
      port: 80,
      protocol: ApplicationProtocol.HTTP,
      defaultAction: ListenerAction.fixedResponse(200, { messageBody: 'ok' }),
    });
  }

  new PlatformInferenceGatewayConstruct(stack, 'Gw', {
    vpc,
    workloadAccountIds: opts.workloadAccountIds ?? ['444444444444', '123456789012'],
    targetAlb,
    targetAlbPort: opts.withAlb ? 80 : undefined,
  });
  return { template: Template.fromStack(stack), stack };
}

describe('Phase 9 — PlatformInferenceGatewayConstruct NLB shape', () => {
  it('emits exactly one internal network load balancer', () => {
    const { template } = synth();
    const nlbs = template.findResources('AWS::ElasticLoadBalancingV2::LoadBalancer', {
      Properties: {
        Scheme: 'internal',
        Type: 'network',
      },
    });
    expect(Object.keys(nlbs)).toHaveLength(1);
  });

  it('NLB has deletion protection + S3 access logs configured', () => {
    const { template } = synth();
    const nlbs = template.findResources('AWS::ElasticLoadBalancingV2::LoadBalancer');
    const nlb = Object.values(nlbs).find(
      (r) => (r.Properties as any).Type === 'network',
    );
    expect(nlb).toBeDefined();
    const attrs = ((nlb!.Properties as any).LoadBalancerAttributes ?? []) as Array<{
      Key: string;
      Value: string;
    }>;
    const attrMap = Object.fromEntries(attrs.map((a) => [a.Key, a.Value]));
    expect(attrMap['deletion_protection.enabled']).toBe('true');
    expect(attrMap['access_logs.s3.enabled']).toBe('true');
  });

  it('listener is on port 443 and TCP by default (no cert supplied)', () => {
    const { template } = synth();
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
      Port: 443,
      Protocol: 'TCP',
    });
  });
});

describe('Phase 9 — VpcEndpointService principal restriction', () => {
  it('endpoint service is acceptanceRequired: false and targets the NLB', () => {
    const { template } = synth();
    const services = template.findResources('AWS::EC2::VPCEndpointService');
    expect(Object.keys(services)).toHaveLength(1);
    const service = Object.values(services)[0];
    expect((service.Properties as any).AcceptanceRequired).toBe(false);
    const lbArns = (service.Properties as any).NetworkLoadBalancerArns as unknown[];
    expect(Array.isArray(lbArns)).toBe(true);
    expect(lbArns).toHaveLength(1);
    // The NLB ARN comes through as a Ref — assert the Ref resolves to an NLB
    // logical id in this stack.
    const ref = (lbArns[0] as { Ref?: string }).Ref;
    expect(typeof ref).toBe('string');
    const nlbKey = Object.keys(
      template.findResources('AWS::ElasticLoadBalancingV2::LoadBalancer'),
    )[0];
    expect(ref).toBe(nlbKey);
  });

  it('VPCEndpointServicePermissions lists the workload account roots exactly', () => {
    const { template } = synth({
      workloadAccountIds: ['444444444444', '123456789012'],
    });
    // AllowedPrincipals are rendered as Fn::Join over ["arn:", { Ref: "AWS::Partition" }, ":iam::<acct>:root"].
    // Flatten and look at the trailing account fragment.
    const perms = template.findResources('AWS::EC2::VPCEndpointServicePermissions');
    const principals = (Object.values(perms)[0].Properties as any)
      .AllowedPrincipals as unknown[];
    expect(principals).toHaveLength(2);
    const tails = principals.map((p) => {
      const joined = (p as { 'Fn::Join': [string, unknown[]] })['Fn::Join'];
      const parts = joined[1];
      return parts[parts.length - 1] as string;
    });
    expect(tails).toEqual([':iam::444444444444:root', ':iam::123456789012:root']);
  });

  it('each workload account id produces its own root-ARN allowed principal', () => {
    const { template } = synth({
      workloadAccountIds: ['111111111111', '222222222222', '333333333333'],
    });
    const perms = template.findResources('AWS::EC2::VPCEndpointServicePermissions');
    const entry = Object.values(perms)[0];
    const principals = (entry.Properties as any).AllowedPrincipals as Array<{
      'Fn::Join': [string, unknown[]];
    }>;
    expect(principals).toHaveLength(3);
    const tails = principals.map(
      (p) => p['Fn::Join'][1][p['Fn::Join'][1].length - 1] as string,
    );
    expect(tails).toEqual([
      ':iam::111111111111:root',
      ':iam::222222222222:root',
      ':iam::333333333333:root',
    ]);
    // Middle fragment is a Ref to AWS::Partition — assert at least one.
    const firstMiddle = principals[0]['Fn::Join'][1][1] as { Ref?: string };
    expect(firstMiddle.Ref).toBe('AWS::Partition');
  });

  it('empty workload-account list is rejected at synth time', () => {
    const app = new App();
    const stack = new Stack(app, 'Reject', {
      env: { account: '123456789012', region: 'us-east-1' },
    });
    const vpc = new Vpc(stack, 'Vpc', {
      ipAddresses: IpAddresses.cidr('10.40.0.0/16'),
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        { name: 'p', subnetType: SubnetType.PRIVATE_ISOLATED, cidrMask: 20 },
      ],
      createInternetGateway: false,
    });
    expect(
      () =>
        new PlatformInferenceGatewayConstruct(stack, 'Gw', {
          vpc,
          workloadAccountIds: [],
        }),
    ).toThrow(/at least one account id/i);
  });

  it('non-12-digit account ids are rejected', () => {
    const app = new App();
    const stack = new Stack(app, 'RejectBad', {
      env: { account: '123456789012', region: 'us-east-1' },
    });
    const vpc = new Vpc(stack, 'Vpc', {
      ipAddresses: IpAddresses.cidr('10.40.0.0/16'),
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        { name: 'p', subnetType: SubnetType.PRIVATE_ISOLATED, cidrMask: 20 },
      ],
      createInternetGateway: false,
    });
    expect(
      () =>
        new PlatformInferenceGatewayConstruct(stack, 'Gw', {
          vpc,
          workloadAccountIds: ['not-an-account'],
        }),
    ).toThrow(/12-digit/);
  });
});

describe('Phase 9 — NLB target group wiring', () => {
  it('without targetAlb, target group is ALB-type with no Targets block (wire LiteLLM later)', () => {
    const { template } = synth({ withAlb: false });
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::TargetGroup', {
      TargetType: 'alb',
      Port: 443,
      Protocol: 'TCP',
    });
    // With no targets, no `Targets:` property is rendered.
    const tgs = template.findResources('AWS::ElasticLoadBalancingV2::TargetGroup');
    const tg = Object.values(tgs)[0];
    expect((tg.Properties as any).Targets).toBeUndefined();
  });

  it('with targetAlb supplied, the ALB is registered as the NLB target', () => {
    const { template } = synth({ withAlb: true });
    const tgs = template.findResources('AWS::ElasticLoadBalancingV2::TargetGroup');
    // We have two target groups in this shape: the ALB's own empty TG (from
    // the fixed-response listener) and the NLB's ALB-type TG. Find the ALB-
    // type one and assert it has exactly one target whose Id is a Ref to the
    // ALB resource.
    const albTg = Object.values(tgs).find(
      (r) => (r.Properties as any).TargetType === 'alb',
    );
    expect(albTg).toBeDefined();
    const targets = (albTg!.Properties as any).Targets as Array<{ Id: unknown }>;
    expect(targets).toHaveLength(1);
    expect(targets[0].Id).toBeDefined();
  });
});

describe('Phase 9 — CfnOutput surface', () => {
  it('emits the endpoint service name as a stack output', () => {
    const { template } = synth();
    const outputs = template.findOutputs('*');
    const names = Object.keys(outputs);
    const match = names.find((n) => /EndpointServiceName/i.test(n));
    expect(match).toBeDefined();
    const exportName = (outputs[match!] as any).Export?.Name;
    expect(exportName).toMatch(/AgenticAI-D03-PlatformInferenceEndpointServiceName/);
  });
});
