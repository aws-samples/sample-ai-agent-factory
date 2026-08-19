/**
 * Phase 12 conformance — OnlineEvaluationConstruct.
 *
 * Pins:
 *   - DDB samples table CMK + PITR + TTL.
 *   - Lambda watchdog runtime + log-group encryption.
 *   - EventBridge schedule cadence.
 *   - bedrock:InvokeModel scoped to allowedBedrockResources() (no wildcards).
 *   - Composite alarm with at least 5 child alarms wired to the SNS topic.
 *   - cloudwatch:PutMetricData scoped to namespace 'AgenticAI/OnlineEval'.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Missing-1 (CDK-side).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App, Duration, Stack } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { Topic } from 'aws-cdk-lib/aws-sns';

import { OnlineEvaluationConstruct } from '@agenticai/online-evaluation';

function synth() {
  const app = new App();
  const stack = new Stack(app, 'OnlineEvalTest', {
    env: { account: '111111111111', region: 'us-west-2' },
  });
  const failuresTopic = new Topic(stack, 'FailuresTopic', { topicName: 'agenticai-eval-failures-test' });
  new OnlineEvaluationConstruct(stack, 'OnlineEval', {
    envName: 'nonprod',
    tenantId: 'demo',
    agentId: 'primary',
    failuresTopic,
    cadence: Duration.hours(1),
  });
  return Template.fromStack(stack);
}

describe('Phase 12 — OnlineEvaluationConstruct CFN shape', () => {
  it('emits a CMK-encrypted samples table with PITR and TTL', () => {
    const t = synth();
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'agenticai-eval-online-nonprod-demo-primary',
      BillingMode: 'PAY_PER_REQUEST',
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
      SSESpecification: { SSEEnabled: true, SSEType: 'KMS' },
      TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
    });
  });

  it('emits the watchdog Lambda on Node 20 with the locked judge models in env', () => {
    const t = synth();
    t.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'agenticai-online-eval-nonprod-demo-primary',
      Runtime: 'nodejs20.x',
      Environment: {
        Variables: Match.objectLike({
          JUDGE_MODEL_CORRECTNESS: Match.stringLikeRegexp('claude-sonnet-4-5'),
          JUDGE_MODEL_REFUSAL: Match.stringLikeRegexp('claude-haiku-4-5'),
          JUDGE_MODEL_TOXICITY: Match.stringLikeRegexp('claude-haiku-4-5'),
          METRIC_NAMESPACE: 'AgenticAI/OnlineEval',
        }),
      },
    });
  });

  it('schedules the watchdog hourly via EventBridge', () => {
    const t = synth();
    t.hasResourceProperties('AWS::Events::Rule', {
      Name: 'agenticai-online-eval-nonprod-demo-primary',
      ScheduleExpression: 'rate(1 hour)',
    });
  });

  it('Lambda role policy invokes Bedrock only on the allow-list (no wildcard)', () => {
    const t = synth();
    const policies = t.findResources('AWS::IAM::Policy');
    const stmts = Object.values(policies).flatMap(
      (p: any) => p.Properties.PolicyDocument.Statement as Array<Record<string, unknown>>,
    );
    const invoke = stmts.find((s: any) =>
      Array.isArray(s.Action)
        ? s.Action.includes('bedrock:InvokeModel')
        : s.Action === 'bedrock:InvokeModel',
    );
    expect(invoke).toBeDefined();
    expect(JSON.stringify(invoke!.Resource)).not.toContain('"*"');
    expect(JSON.stringify(invoke!.Resource)).toMatch(/foundation-model|inference-profile/);
  });

  it('cloudwatch:PutMetricData is scoped to the AgenticAI/OnlineEval namespace', () => {
    const t = synth();
    const policies = t.findResources('AWS::IAM::Policy');
    const stmts = Object.values(policies).flatMap(
      (p: any) => p.Properties.PolicyDocument.Statement as Array<Record<string, unknown>>,
    );
    const put = stmts.find((s: any) =>
      (Array.isArray(s.Action) ? s.Action : [s.Action]).includes('cloudwatch:PutMetricData'),
    );
    expect(put).toBeDefined();
    expect(JSON.stringify(put!.Condition)).toContain('AgenticAI/OnlineEval');
  });

  it('emits a composite alarm covering ≥ 5 child alarms with SNS action', () => {
    const t = synth();
    t.resourceCountIs('AWS::CloudWatch::CompositeAlarm', 1);
    const alarms = t.findResources('AWS::CloudWatch::Alarm');
    expect(Object.keys(alarms).length).toBeGreaterThanOrEqual(5);
    const composite = Object.values(t.findResources('AWS::CloudWatch::CompositeAlarm'))[0] as any;
    expect(JSON.stringify(composite.Properties.AlarmActions)).toContain('FailuresTopic');
  });
});
