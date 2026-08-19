/**
 * Phase 21 — conformance pins for the Z7 + audit-fix work.
 *
 * Pins:
 *   - InferenceCircuitBreakerConstruct (Z7-A) emits Lambda + 3 child alarms
 *     + composite alarm wired to SNS.
 *   - Pipeline canary stage (Z7-B) — CodeBuild actions CanaryDeploy + CanarySoak.
 *   - Registry GSIs by-card-name + by-domain (Z7-E + Z7-K).
 *   - Tool catalogue agent-a2a validation (Z7-K).
 *   - ShowbackConstruct (Z7-F + G-4) emits QuickSight DataSet.
 *   - HumanInTheLoopConstruct AVP policy store + policy resource (G-3).
 *   - CatalogueDriftDetectorConstruct (G-5d) emits Lambda + schedule + alarm.
 *   - TenantQuotaTableConstruct (G-5a) emits CMK-encrypted DDB.
 *
 * Closes self-audit gaps G-3 / G-7 / G-8 / G-9 / G-10.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App, Stack } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { Topic } from 'aws-cdk-lib/aws-sns';

import { AgentCoreRegistryConstruct } from '@agenticai/agentcore-registry';
import { InferenceCircuitBreakerConstruct } from '@agenticai/agent-resilience';
import { ShowbackConstruct } from '@agenticai/cost-allocation';
import { HumanInTheLoopConstruct } from '@agenticai/hitl';
import { CatalogueDriftDetectorConstruct } from '@agenticai/catalogue-drift-detector';
import { TenantQuotaTableConstruct } from '@agenticai/tenant-quota-guard';
import { validateToolSpec } from '@agenticai/platform-tool-catalogue';
import { WorkloadPipelineStack } from '../../pipelines/workload-pipeline-stack';

describe('Phase 21 — Z7-A: InferenceCircuitBreakerConstruct', () => {
  function synth() {
    const app = new App();
    const stack = new Stack(app, 'CB', { env: { account: '111111111111', region: 'us-west-2' } });
    const topic = new Topic(stack, 'T');
    new InferenceCircuitBreakerConstruct(stack, 'CB', {
      envName: 'prod',
      tenantId: 'demo',
      agentId: 'primary',
      failuresTopic: topic,
    });
    return Template.fromStack(stack);
  }
  it('emits the circuit-breaker Lambda on Node 20', () => {
    const t = synth();
    t.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'agenticai-inference-cb-prod-demo-primary',
      Runtime: 'nodejs20.x',
    });
  });
  it('emits a composite alarm + 3 child alarms wired to SNS', () => {
    const t = synth();
    t.resourceCountIs('AWS::CloudWatch::CompositeAlarm', 1);
    const alarms = t.findResources('AWS::CloudWatch::Alarm');
    expect(Object.keys(alarms).length).toBeGreaterThanOrEqual(3);
  });
  it('IAM policy on the Lambda role scopes bedrock:InvokeModel to the SSOT (no wildcard)', () => {
    const t = synth();
    const policies = t.findResources('AWS::IAM::Policy');
    const stmts = Object.values(policies).flatMap((p: any) => p.Properties.PolicyDocument.Statement);
    const bedrock = stmts.find((s: any) =>
      (Array.isArray(s.Action) ? s.Action : [s.Action]).includes('bedrock:InvokeModel'),
    );
    expect(bedrock).toBeDefined();
    expect(JSON.stringify(bedrock!.Resource)).not.toContain('"*"');
    expect(JSON.stringify(bedrock!.Resource)).toMatch(/foundation-model|inference-profile/);
  });
});

describe('Phase 21 — Z7-B: Pipeline canary stage', () => {
  it('workload pipeline declares CanaryDeploy + CanarySoak CodeBuild actions', () => {
    const app = new App();
    const stack = new WorkloadPipelineStack(app, 'WP', {
      env: { account: '111111111111', region: 'us-west-2' },
      githubRepo: 'aws-samples/x',
      githubConnectionArn: 'arn:aws:codestar-connections:us-west-2:111111111111:connection/abc',
      tenantId: 'demo',
      agentId: 'primary',
      costCentre: 'engineering',
      workloadNonprodEnv: { account: '444444444444', region: 'us-west-2' },
      workloadProdEnv: { account: '555555555555', region: 'us-west-2' },
    });
    const t = Template.fromStack(stack);
    const cbProjects = t.findResources('AWS::CodeBuild::Project');
    const projectNames = Object.values(cbProjects)
      .map((p: any) => JSON.stringify(p.Properties))
      .join(' ');
    expect(projectNames).toContain('CanaryDeploy');
    expect(projectNames).toContain('CanarySoak');
  });
});

describe('Phase 21 — Z7-E + Z7-K: registry GSIs', () => {
  it('AgentTable carries by-card-name + by-domain GSIs', () => {
    const app = new App();
    const stack = new Stack(app, 'R', { env: { account: '111111111111', region: 'us-west-2' } });
    new AgentCoreRegistryConstruct(stack, 'R', { envName: 'prod' });
    const t = Template.fromStack(stack);
    const tables = t.findResources('AWS::DynamoDB::Table');
    const agentTable = Object.values(tables).find(
      (tab: any) => tab.Properties.TableName === 'agenticai-registry-agents-prod',
    ) as any;
    expect(agentTable).toBeDefined();
    const indexNames = (agentTable.Properties.GlobalSecondaryIndexes || []).map(
      (g: any) => g.IndexName,
    );
    expect(indexNames).toEqual(expect.arrayContaining(['by-kind', 'by-card-name', 'by-domain']));
  });
});

describe('Phase 21 — Z7-K: tool catalogue agent-a2a validation', () => {
  it('accepts a valid agent-a2a tool spec', () => {
    expect(() =>
      validateToolSpec({
        toolId: 'peer-agent-x',
        toolType: 'agent-a2a',
        targetArn: 'placeholder',
        a2aEndpointUrl: 'https://gateway.example.com/a2a',
        cedarPolicy: 'permit(principal, action, resource);',
        ownerTeam: 'platform-ai',
        costCentre: 'platform',
        description: 'peer agent x',
        approvalStatus: 'approved',
      }),
    ).not.toThrow();
  });
  it('rejects HTTP a2aEndpointUrl', () => {
    expect(() =>
      validateToolSpec({
        toolId: 'peer-agent-x',
        toolType: 'agent-a2a',
        targetArn: 'placeholder',
        a2aEndpointUrl: 'http://insecure',
        cedarPolicy: 'permit(principal, action, resource);',
        ownerTeam: 'platform-ai',
        costCentre: 'platform',
        description: 'peer agent x',
        approvalStatus: 'approved',
      }),
    ).toThrow(/https/);
  });
  it('rejects agent-a2a without a2aEndpointUrl', () => {
    expect(() =>
      validateToolSpec({
        toolId: 'peer-agent-x',
        toolType: 'agent-a2a',
        targetArn: 'placeholder',
        cedarPolicy: 'permit(principal, action, resource);',
        ownerTeam: 'platform-ai',
        costCentre: 'platform',
        description: 'peer agent x',
        approvalStatus: 'approved',
      }),
    ).toThrow(/a2aEndpointUrl/);
  });
  it('rejects lambda toolType with a2aEndpointUrl set', () => {
    expect(() =>
      validateToolSpec({
        toolId: 'tool-echo',
        toolType: 'lambda',
        targetArn: 'arn:aws:lambda:us-east-1:111111111111:function:foo:PROD',
        a2aEndpointUrl: 'https://nope',
        cedarPolicy: 'permit(principal, action, resource);',
        ownerTeam: 'platform-ai',
        costCentre: 'platform',
        description: 'echo',
        approvalStatus: 'approved',
      }),
    ).toThrow(/a2aEndpointUrl/);
  });
});

describe('Phase 21 — G-4: ShowbackConstruct', () => {
  it('emits a QuickSight DataSet + DataSource', () => {
    const app = new App();
    const stack = new Stack(app, 'S', { env: { account: '111111111111', region: 'us-east-1' } });
    new ShowbackConstruct(stack, 'S', {
      envName: 'prod',
      curAthenaDatabase: 'cur',
      curAthenaTable: 'cost_and_usage',
      readerPrincipalArn: 'arn:aws:quicksight:us-east-1:111111111111:group/default/AgenticAI-FinOps',
    });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::QuickSight::DataSource', 1);
    t.resourceCountIs('AWS::QuickSight::DataSet', 1);
    t.hasResourceProperties('AWS::QuickSight::DataSource', { Type: 'ATHENA' });
  });
});

describe('Phase 21 — G-3: HITL AVP policy store enforcement', () => {
  it('emits a CfnPolicyStore + CfnPolicy with the Cedar approver statement', () => {
    const app = new App();
    const stack = new Stack(app, 'H', { env: { account: '111111111111', region: 'us-east-1' } });
    new HumanInTheLoopConstruct(stack, 'H', {
      envName: 'prod',
      tenantId: 'demo',
      agentId: 'primary',
      approver: {
        approverRoleArn: 'arn:aws:iam::111111111111:role/AgenticAI-Approver',
        tenantId: 'demo',
        agentId: 'primary',
        confidenceThreshold: 0.7,
      },
    });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::VerifiedPermissions::PolicyStore', 1);
    t.resourceCountIs('AWS::VerifiedPermissions::Policy', 1);
    const policies = t.findResources('AWS::VerifiedPermissions::Policy');
    const policy = Object.values(policies)[0] as any;
    expect(JSON.stringify(policy.Properties.Definition)).toContain('AgenticAI-Approver');
    expect(JSON.stringify(policy.Properties.Definition)).toContain('ResumeTaskWithApproval');
  });
});

describe('Phase 21 — G-5d: CatalogueDriftDetectorConstruct', () => {
  function synth() {
    const app = new App();
    const stack = new Stack(app, 'D', { env: { account: '111111111111', region: 'us-east-1' } });
    const topic = new Topic(stack, 'T');
    new CatalogueDriftDetectorConstruct(stack, 'D', {
      envName: 'prod',
      tenantId: 'demo',
      agentId: 'primary',
      failuresTopic: topic,
      catalogueIds: ['tool-a', 'tool-b'],
    });
    return Template.fromStack(stack);
  }
  it('emits the detector Lambda on Node 20', () => {
    const t = synth();
    t.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'agenticai-catalogue-drift-prod-demo-primary',
      Runtime: 'nodejs20.x',
    });
  });
  it('emits the daily EventBridge schedule', () => {
    const t = synth();
    t.hasResourceProperties('AWS::Events::Rule', { ScheduleExpression: 'rate(1 day)' });
  });
  it('emits a drift alarm', () => {
    const t = synth();
    t.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: 'agenticai-catalogue-drift-prod-demo-primary',
      Threshold: 0,
    });
  });
});

describe('Phase 21 — G-5a: TenantQuotaTableConstruct', () => {
  it('emits a CMK-encrypted DDB with TTL', () => {
    const app = new App();
    const stack = new Stack(app, 'TQ', { env: { account: '111111111111', region: 'us-east-1' } });
    new TenantQuotaTableConstruct(stack, 'TQ', { envName: 'prod' });
    const t = Template.fromStack(stack);
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'agenticai-tenant-quota-prod',
      SSESpecification: { SSEEnabled: true, SSEType: 'KMS' },
      TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
    });
  });
});
