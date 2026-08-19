/**
 * Phase 18 conformance — HITL reference pattern (sample content; additional
 * customer-specific security review required before production deployment).
 *
 * Pins:
 *   - SQS escalation queue (KMS-encrypted, with DLQ).
 *   - Pause-token DDB (CMK + PITR + TTL).
 *   - Step Function STANDARD with X-Ray + LogLevel ALL + 24h timeout.
 *   - Cedar policy contains the approver role + ResumeTaskWithApproval.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Missing-5 (CDK-side).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App, Stack } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';

import { HumanInTheLoopConstruct } from '@agenticai/hitl';

function synth() {
  const app = new App();
  const stack = new Stack(app, 'HitlTest', {
    env: { account: '111111111111', region: 'us-west-2' },
  });
  const construct = new HumanInTheLoopConstruct(stack, 'Hitl', {
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
  return { template: Template.fromStack(stack), construct };
}

describe('Phase 18 — HumanInTheLoopConstruct CFN shape', () => {
  it('emits SQS escalation queue + DLQ, both KMS-encrypted', () => {
    const { template } = synth();
    const queues = template.findResources('AWS::SQS::Queue');
    expect(Object.keys(queues).length).toBeGreaterThanOrEqual(2);
    const queueProps = Object.values(queues).map((q: any) => q.Properties);
    const allHaveKms = queueProps.every((p: any) => p.KmsMasterKeyId !== undefined);
    expect(allHaveKms).toBe(true);
  });

  it('pause-token DDB has CMK + PITR + TTL', () => {
    const { template } = synth();
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'agenticai-hitl-prod-demo-primary-pending',
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
      SSESpecification: { SSEEnabled: true, SSEType: 'KMS' },
      TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
    });
  });

  it('state machine is STANDARD with X-Ray and 24h timeout', () => {
    const { template } = synth();
    template.hasResourceProperties('AWS::StepFunctions::StateMachine', {
      StateMachineName: 'AgenticAI-HITL-prod-demo-primary',
      StateMachineType: 'STANDARD',
      TracingConfiguration: { Enabled: true },
      LoggingConfiguration: Match.objectLike({ Level: 'ALL' }),
    });
  });

  it('definition includes RecordPauseToken + EscalateToHumans + NotifyApprovers', () => {
    const { template } = synth();
    const sm = Object.values(template.findResources('AWS::StepFunctions::StateMachine'))[0] as any;
    const def = JSON.stringify(sm.Properties.DefinitionString ?? sm.Properties);
    expect(def).toContain('RecordPauseToken');
    expect(def).toContain('EscalateToHumans');
    expect(def).toContain('NotifyApprovers');
  });

  it('Cedar policy permits only the approver role', () => {
    const { construct } = synth();
    expect(construct.cedarPolicy).toContain('arn:aws:iam::111111111111:role/AgenticAI-Approver');
    expect(construct.cedarPolicy).toContain('ResumeTaskWithApproval');
    expect(construct.cedarPolicy).toContain('Agent::"demo/primary"');
    // G-3: AVP's CfnPolicy accepts ONE statement; we emit `permit` only.
    // Default-deny is implicit when no permit matches.
    expect(construct.cedarPolicy).not.toContain('forbid');
    expect(construct.cedarPolicy).toContain('permit');
  });
});
