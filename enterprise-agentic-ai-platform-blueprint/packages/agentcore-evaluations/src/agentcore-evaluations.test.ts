/**
 * Unit tests for @agenticai/agentcore-evaluations (synth-time contracts).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App, Stack } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { Topic } from 'aws-cdk-lib/aws-sns';

import {
  AgentCoreOnlineEvaluationConstruct,
  BUILTIN_EVALUATORS,
  DEFAULT_PRODUCTION_EVALUATORS,
  EvaluationExecutionRoleConstruct,
  PolicyCodeEvaluatorConstruct,
  assertEvaluatorId,
  evaluationResourceName,
} from './index';

const ENV = { account: '444444444444', region: 'eu-west-1' };

function stack(): Stack {
  return new Stack(new App(), 'Cell', { env: ENV });
}

describe('evaluator catalog', () => {
  it('uses the service identifier format for every built-in evaluator', () => {
    for (const id of Object.values(BUILTIN_EVALUATORS)) {
      expect(() => assertEvaluatorId(id)).not.toThrow();
      expect(id).toMatch(/^Builtin\.[A-Z][A-Za-z]+$/);
    }
    expect(DEFAULT_PRODUCTION_EVALUATORS).toContain('Builtin.Refusal');
    expect(DEFAULT_PRODUCTION_EVALUATORS).toContain('Builtin.ToolSelectionAccuracy');
  });

  it('accepts third-party and custom ids and rejects malformed ones', () => {
    expect(() => assertEvaluatorId('ThirdParty.DeepEval.Toxicity')).not.toThrow();
    expect(() => assertEvaluatorId('agenticai_policy-abcdefghij')).not.toThrow();
    expect(() => assertEvaluatorId('agenticai_policy')).toThrow(/invalid/);
    expect(() => assertEvaluatorId('builtin.helpfulness')).toThrow(/invalid/);
  });

  it('derives service-compliant names from allocation dimensions', () => {
    expect(evaluationResourceName('eval', 'prod', 'retail', 'returns-agent')).toBe('eval_prod_retail_returns_agent');
    expect(evaluationResourceName('eval', 'prod', 'x'.repeat(60), 'a').length).toBeLessThanOrEqual(48);
  });
});

describe('EvaluationExecutionRoleConstruct', () => {
  it('follows the documented prerequisite shape with judge inference off by default', () => {
    const s = stack();
    new EvaluationExecutionRoleConstruct(s, 'Role', { nameSuffix: 'cell-nonprod' });
    const t = Template.fromStack(s);
    t.hasResourceProperties('AWS::IAM::Role', {
      RoleName: 'AgentCoreEvaluationRole-cell-nonprod',
      Path: '/service-role/',
      AssumeRolePolicyDocument: {
        Statement: [
          Match.objectLike({
            Principal: { Service: 'bedrock-agentcore.amazonaws.com' },
            Condition: Match.objectLike({
              StringEquals: { 'aws:SourceAccount': ENV.account, 'aws:ResourceAccount': ENV.account },
            }),
          }),
        ],
      },
    });
    const role = Object.values(t.findResources('AWS::IAM::Role'))[0];
    const policy = JSON.stringify((role.Properties as any).Policies);
    expect(policy).toContain('logs:StartQuery');
    expect(policy).toContain('log-group:/aws/bedrock-agentcore/evaluations/*');
    expect(policy).toContain('log-group:aws/spans');
    expect(policy).not.toContain('bedrock:InvokeModel');
    expect(policy).not.toContain('lambda:InvokeFunction');
  });

  it('adds exact judge and Lambda grants only when supplied', () => {
    const s = stack();
    new EvaluationExecutionRoleConstruct(s, 'Role', {
      nameSuffix: 'cell-prod',
      judgeModelArns: ['arn:aws:bedrock:eu-west-1::foundation-model/anthropic.claude-haiku-4-5'],
      codeEvaluatorFunctionArns: ['arn:aws:lambda:eu-west-1:444444444444:function:cloudwatchCodeEvaluator-x'],
    });
    const role = Object.values(Template.fromStack(s).findResources('AWS::IAM::Role'))[0];
    const policy = JSON.stringify((role.Properties as any).Policies);
    expect(policy).toContain('bedrock:InvokeModel');
    expect(policy).toContain('anthropic.claude-haiku-4-5');
    expect(policy).toContain('lambda:InvokeFunction');
    expect(policy).toContain('cloudwatchCodeEvaluator-x');
  });
});

describe('PolicyCodeEvaluatorConstruct', () => {
  it('emits a code-based TRACE evaluator backed by a cloudwatchCodeEvaluator-prefixed Lambda', () => {
    const s = stack();
    const evaluator = new PolicyCodeEvaluatorConstruct(s, 'Policy', {
      evaluatorName: 'agenticai_policy_nonprod',
      functionNameSuffix: 'nonprod-demo-primary',
      allowedTools: ['DynamicTools___crm___lookup_customer'],
    });
    const t = Template.fromStack(s);
    t.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'cloudwatchCodeEvaluator-nonprod-demo-primary',
      Runtime: 'python3.13',
      Handler: 'index.handler',
      Environment: {
        Variables: Match.objectLike({
          POLICY_ALLOWED_TOOLS: '["DynamicTools___crm___lookup_customer"]',
          POLICY_MAX_OUTPUT_CHARS: '8000',
        }),
      },
    });
    t.hasResourceProperties('AWS::BedrockAgentCore::Evaluator', {
      EvaluatorName: 'agenticai_policy_nonprod',
      Level: 'TRACE',
      EvaluatorConfig: {
        CodeBased: { LambdaConfig: Match.objectLike({ LambdaTimeoutInSeconds: 60 }) },
      },
    });
    expect(evaluator.evaluatorId).toBeDefined();
  });

  it('rejects a non-compliant evaluator name', () => {
    expect(
      () => new PolicyCodeEvaluatorConstruct(stack(), 'P', { evaluatorName: 'agenticai-policy', functionNameSuffix: 'x' }),
    ).toThrow(/invalid/);
  });
});

describe('AgentCoreOnlineEvaluationConstruct', () => {
  function synth(extra: Partial<ConstructorParameters<typeof AgentCoreOnlineEvaluationConstruct>[2]> = {}) {
    const s = stack();
    const role = new EvaluationExecutionRoleConstruct(s, 'Role', { nameSuffix: 'cell' }).role;
    const topic = new Topic(s, 'Failures');
    const construct = new AgentCoreOnlineEvaluationConstruct(s, 'Online', {
      configName: 'agenticai_online_nonprod_demo_primary',
      executionRole: role,
      serviceName: 'demo_primary_nonprod-abc123.DEFAULT',
      evaluatorIds: DEFAULT_PRODUCTION_EVALUATORS,
      samplingPercentage: 10,
      sessionTimeoutMinutes: 15,
      metricsNamespace: 'AgenticAI/Evaluations/nonprod',
      resultLogGroupName: '/agenticai/evaluations/nonprod/demo/primary',
      failuresTopic: topic,
      ...extra,
    });
    return { s, construct, t: Template.fromStack(s) };
  }

  it('emits an enabled configuration over aws/spans with the production evaluator baseline and output overrides', () => {
    const { t } = synth();
    t.resourceCountIs('AWS::BedrockAgentCore::OnlineEvaluationConfig', 1);
    const cfg = Object.values(t.findResources('AWS::BedrockAgentCore::OnlineEvaluationConfig'))[0].Properties as any;
    expect(cfg.OnlineEvaluationConfigName).toBe('agenticai_online_nonprod_demo_primary');
    expect(cfg.ExecutionStatus).toBe('ENABLED');
    expect(cfg.DataSourceConfig.CloudWatchLogs).toEqual({
      LogGroupNames: ['aws/spans'],
      ServiceNames: ['demo_primary_nonprod-abc123.DEFAULT'],
    });
    expect(cfg.Evaluators.map((e: any) => e.EvaluatorId)).toEqual([...DEFAULT_PRODUCTION_EVALUATORS]);
    expect(cfg.Rule).toEqual({
      SamplingConfig: { SamplingPercentage: 10 },
      SessionConfig: { SessionTimeoutMinutes: 15 },
    });
    expect(cfg.OutputConfig).toEqual({
      CloudWatchConfig: {
        ResultDestination: 'DEDICATED_LOG_GROUP',
        LogGroupName: '/agenticai/evaluations/nonprod/demo/primary',
        MetricsNamespace: 'AgenticAI/Evaluations/nonprod',
      },
    });
  });

  it('wires score alarms into one composite alarm that fans out to the failures topic', () => {
    const { t } = synth({
      scoreAlarms: [
        { metricName: 'Builtin.Correctness', dimensions: { EvaluatorId: 'Builtin.Correctness' }, minimumAverageScore: 0.85 },
        { metricName: 'Builtin.Refusal', dimensions: { EvaluatorId: 'Builtin.Refusal' }, minimumAverageScore: 0.99 },
      ],
    });
    t.resourceCountIs('AWS::CloudWatch::Alarm', 2);
    t.hasResourceProperties('AWS::CloudWatch::Alarm', {
      Namespace: 'AgenticAI/Evaluations/nonprod',
      MetricName: 'Builtin.Correctness',
      Threshold: 0.85,
      ComparisonOperator: 'LessThanThreshold',
      TreatMissingData: 'notBreaching',
    });
    t.resourceCountIs('AWS::CloudWatch::CompositeAlarm', 1);
    t.hasResourceProperties('AWS::CloudWatch::CompositeAlarm', {
      AlarmActions: [Match.objectLike({ Ref: Match.stringLikeRegexp('Failures') })],
    });
  });

  it('refuses the reserved result prefix, AWS/ namespaces and out-of-range sampling', () => {
    expect(() => synth({ resultLogGroupName: '/aws/bedrock-agentcore/evaluations/results/x' })).toThrow(/reserved/);
    expect(() => synth({ metricsNamespace: 'AWS/Evaluations' })).toThrow(/must not begin with AWS\//);
    expect(() => synth({ samplingPercentage: 0 })).toThrow(/samplingPercentage/);
    expect(() => synth({ evaluatorIds: [] })).toThrow(/between 1 and 25/);
  });
});
