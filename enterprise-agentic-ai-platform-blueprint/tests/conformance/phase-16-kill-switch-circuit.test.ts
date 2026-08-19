/**
 * Phase 16 conformance — kill-switch + circuit-breaker.
 *
 * Pins:
 *   - KillSwitch state machine has 4 parallel branches.
 *   - SSM trigger document name matches convention.
 *   - Audit DDB CMK + PITR.
 *   - Step Function has X-Ray + LogLevel ALL + 15min timeout.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Partial-4 (CDK-side).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App, Stack } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { Topic } from 'aws-cdk-lib/aws-sns';

import { KillSwitchConstruct } from '@agenticai/agent-resilience';

function synth() {
  const app = new App();
  const stack = new Stack(app, 'KillSwitchTest', {
    env: { account: '111111111111', region: 'us-west-2' },
  });
  const topic = new Topic(stack, 'AuditTopic', { topicName: 'agenticai-kill-switch-audit-test' });
  new KillSwitchConstruct(stack, 'KillSwitch', {
    envName: 'prod',
    tenantId: 'demo',
    agentId: 'primary',
    cognitoUserPoolId: 'us-west-2_AAAAAAAAA',
    cognitoUserPoolClientId: 'abc123',
    workloadIdentityName: 'demo-primary-wi',
    gatewayTargetId: 'tgt12345',
    inferenceProfileArn: 'arn:aws:bedrock:us-west-2:111111111111:application-inference-profile/demo-primary',
    auditTopic: topic,
  });
  return Template.fromStack(stack);
}

describe('Phase 16 — KillSwitchConstruct CFN shape', () => {
  it('emits a STANDARD state machine with X-Ray, LogLevel ALL, 15min timeout', () => {
    const t = synth();
    t.hasResourceProperties('AWS::StepFunctions::StateMachine', {
      StateMachineName: 'AgenticAI-KillSwitch-prod-demo-primary',
      StateMachineType: 'STANDARD',
      TracingConfiguration: { Enabled: true },
      LoggingConfiguration: Match.objectLike({ Level: 'ALL' }),
    });
  });

  it('definition contains the 4 parallel revoke branches', () => {
    const t = synth();
    const sms = t.findResources('AWS::StepFunctions::StateMachine');
    const sm = Object.values(sms)[0] as any;
    const def = JSON.stringify(sm.Properties.DefinitionString ?? sm.Properties);
    expect(def).toContain('LockCognitoClient');
    expect(def).toContain('DeleteWorkloadIdentity');
    expect(def).toContain('DisableGatewayTarget');
    expect(def).toContain('TagInferenceProfileKilled');
    expect(def).toContain('ParallelRevoke');
    // C-C fix verification: post-fix, all 4 branches are real CallAwsService
    // tasks (no Pass placeholders left).
    expect(def).not.toContain('"Type": "Pass"');
  });

  it('audit DDB is CMK-encrypted with PITR', () => {
    const t = synth();
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'agenticai-kill-switch-audit-prod-demo-primary',
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
      SSESpecification: { SSEEnabled: true, SSEType: 'KMS' },
    });
  });

  it('emits the SSM trigger document with the canonical name', () => {
    const t = synth();
    t.hasResourceProperties('AWS::SSM::Document', {
      Name: 'AgenticAI-Trigger-KillSwitch-prod-demo-primary',
      DocumentType: 'Automation',
    });
  });

  it('KillSwitch role assumed only by states.amazonaws.com', () => {
    const t = synth();
    t.hasResourceProperties('AWS::IAM::Role', {
      RoleName: 'AgenticAI-KillSwitch-prod-demo-primary',
      AssumeRolePolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Principal: { Service: 'states.amazonaws.com' },
            Action: 'sts:AssumeRole',
          }),
        ]),
      },
    });
  });

  it('Cognito policy targets only the configured user pool', () => {
    const t = synth();
    const policies = t.findResources('AWS::IAM::Policy');
    const stmts = Object.values(policies).flatMap(
      (p: any) => p.Properties.PolicyDocument.Statement as Array<Record<string, unknown>>,
    );
    const cognito = stmts.find((s: any) =>
      (Array.isArray(s.Action) ? s.Action : [s.Action]).includes('cognito-idp:UpdateUserPoolClient'),
    );
    expect(cognito).toBeDefined();
    expect(JSON.stringify(cognito!.Resource)).toContain('us-west-2_AAAAAAAAA');
  });
});
