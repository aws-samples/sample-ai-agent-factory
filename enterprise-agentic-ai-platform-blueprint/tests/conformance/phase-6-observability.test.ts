/**
 * Phase 6 conformance — observability + cost.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { WorkloadNetworkStack } from '../../apps/workload-account/lib/workload-network-stack';
import { WorkloadAppStack } from '../../apps/workload-account/lib/workload-app-stack';

function synth() {
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
    auditOamSinkArn: 'arn:aws:oam:us-west-2:666666666666:sink/abc',
    notificationEmail: 'finops@example.com',
    monthlyBudgetUsd: 1000,
  });
  return Template.fromStack(stack);
}

describe('Phase 6 — OAM source link', () => {
  it('emits an AWS::Oam::Link to the audit-account sink', () => {
    const t = synth();
    t.resourceCountIs('AWS::Oam::Link', 1);
    t.hasResourceProperties('AWS::Oam::Link', {
      SinkIdentifier: 'arn:aws:oam:us-west-2:666666666666:sink/abc',
    });
  });

  it('shares all three resource types (Metric + LogGroup + Trace)', () => {
    const t = synth();
    const links = t.findResources('AWS::Oam::Link');
    const types = (Object.values(links)[0].Properties as any).ResourceTypes as string[];
    expect(types).toEqual(
      expect.arrayContaining([
        'AWS::CloudWatch::Metric',
        'AWS::Logs::LogGroup',
        'AWS::XRay::Trace',
      ]),
    );
  });
});

describe('Phase 6 — Dashboards', () => {
  it('emits a CloudWatch dashboard named per agent', () => {
    const t = synth();
    t.hasResourceProperties('AWS::CloudWatch::Dashboard', {
      DashboardName: 'agenticai-nonprod-demo-primary',
    });
  });
});

describe('Phase 6 — Alarms', () => {
  it('emits guardrail-violation + latency alarms', () => {
    const t = synth();
    const alarms = t.findResources('AWS::CloudWatch::Alarm');
    const names = Object.values(alarms).map((r) => (r.Properties as any).AlarmName as string);
    expect(names).toContain('agenticai-nonprod-demo-primary-guardrail-violations');
    expect(names).toContain('agenticai-nonprod-demo-primary-first-token-p99');
  });

  it('latency alarm defaults to 1500ms p99', () => {
    const t = synth();
    const alarms = t.findResources('AWS::CloudWatch::Alarm', {
      Properties: { AlarmName: 'agenticai-nonprod-demo-primary-first-token-p99' },
    });
    const props = Object.values(alarms)[0].Properties as any;
    expect(props.Threshold).toBe(1500);
    expect(props.ExtendedStatistic || props.Statistic).toBe('p99');
  });
});

describe('Phase 6 — Per-app Budget', () => {
  it('emits an AWS::Budgets::Budget filtered by the application-id tag', () => {
    const t = synth();
    t.resourceCountIs('AWS::Budgets::Budget', 1);
    t.hasResourceProperties('AWS::Budgets::Budget', {
      Budget: {
        BudgetName: 'agenticai-nonprod-demo',
        BudgetLimit: { Amount: 1000, Unit: 'USD' },
      },
    });
  });

  it('Budget carries ACTUAL + FORECASTED notifications', () => {
    const t = synth();
    const budgets = t.findResources('AWS::Budgets::Budget');
    const props = Object.values(budgets)[0].Properties as any;
    const notifications: any[] = props.NotificationsWithSubscribers;
    expect(notifications.length).toBe(2);
    const types = notifications.map((n) => n.Notification.NotificationType);
    expect(types).toEqual(expect.arrayContaining(['ACTUAL', 'FORECASTED']));
  });
});
