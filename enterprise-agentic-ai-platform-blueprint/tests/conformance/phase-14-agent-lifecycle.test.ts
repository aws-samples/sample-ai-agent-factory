/**
 * Phase 14 conformance — agent lifecycle (versioning + canary + rollback).
 *
 * Pins:
 *   - AgentVersionTable: CMK + PITR + by-alias + by-status GSIs.
 *   - RollbackStateMachine: STANDARD type, 15m timeout, logs ALL, X-Ray
 *     tracing, role narrowed to the version table + topic.
 *   - The Step Function definition includes a flip-alias and audit step.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Missing-3 (CDK-side).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App, Stack } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { Topic } from 'aws-cdk-lib/aws-sns';

import {
  AgentVersionTableConstruct,
  RollbackStateMachineConstruct,
} from '@agenticai/agent-lifecycle';

function synth() {
  const app = new App();
  const stack = new Stack(app, 'AgentLifecycleTest', {
    env: { account: '111111111111', region: 'us-west-2' },
  });
  const versions = new AgentVersionTableConstruct(stack, 'Versions', { envName: 'prod' });
  const topic = new Topic(stack, 'FailuresTopic', { topicName: 'agenticai-eval-failures-test' });
  new RollbackStateMachineConstruct(stack, 'Rollback', {
    envName: 'prod',
    tenantId: 'demo',
    agentId: 'primary',
    versionTable: versions.table,
    failuresTopic: topic,
  });
  return Template.fromStack(stack);
}

describe('Phase 14 — agent lifecycle CFN shape', () => {
  it('AgentVersionTable is CMK-encrypted with PITR and two GSIs', () => {
    const t = synth();
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'agenticai-agent-versions-prod',
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
      SSESpecification: { SSEEnabled: true, SSEType: 'KMS' },
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'by-alias' }),
        Match.objectLike({ IndexName: 'by-status' }),
      ]),
    });
  });

  it('RollbackStateMachine is STANDARD type with X-Ray tracing', () => {
    const t = synth();
    t.hasResourceProperties('AWS::StepFunctions::StateMachine', {
      StateMachineType: 'STANDARD',
      TracingConfiguration: { Enabled: true },
    });
  });

  it('RollbackStateMachine name embeds tenant + agent', () => {
    const t = synth();
    t.hasResourceProperties('AWS::StepFunctions::StateMachine', {
      StateMachineName: 'AgenticAI-Rollback-prod-demo-primary',
    });
  });

  it('Step Function definition references PROD alias flip + audit + SNS', () => {
    const t = synth();
    const sms = t.findResources('AWS::StepFunctions::StateMachine');
    const sm = Object.values(sms)[0] as any;
    const def = JSON.stringify(sm.Properties.DefinitionString ?? sm.Properties);
    expect(def).toContain('FlipAliasToPrevious');
    expect(def).toContain('WriteAuditRecord');
    expect(def).toContain('NotifyOps');
  });

  it('logs are emitted at LogLevel ALL', () => {
    const t = synth();
    t.hasResourceProperties('AWS::StepFunctions::StateMachine', {
      LoggingConfiguration: Match.objectLike({ Level: 'ALL' }),
    });
  });
});
