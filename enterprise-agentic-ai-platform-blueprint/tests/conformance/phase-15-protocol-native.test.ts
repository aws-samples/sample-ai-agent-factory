/**
 * Phase 15 conformance — A2A + MCP-native protocol support.
 *
 * Pins:
 *   - McpProbe Lambda runtime + locked protocol version env var.
 *   - EventBridge cadence (default 5 min).
 *   - cloudwatch:PutMetricData scoped to AgenticAI/MCP namespace.
 *   - Alarm fires on missing data + wires to the SNS failures topic.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Partial-2 + Partial-3 (CDK-side).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App, Stack } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { Topic } from 'aws-cdk-lib/aws-sns';

import { McpProbeConstruct } from '@agenticai/agent-protocols';

function synth() {
  const app = new App();
  const stack = new Stack(app, 'McpProbeTest', {
    env: { account: '111111111111', region: 'us-west-2' },
  });
  const topic = new Topic(stack, 'FailuresTopic', { topicName: 'agenticai-eval-failures-test' });
  new McpProbeConstruct(stack, 'Probe', {
    envName: 'prod',
    tenantId: 'demo',
    agentId: 'primary',
    gatewayUrl: 'https://gateway.example.com/a2a',
    failuresTopic: topic,
  });
  return Template.fromStack(stack);
}

describe('Phase 15 — McpProbeConstruct CFN shape', () => {
  it('emits the probe Lambda on Node 20 with the locked MCP version', () => {
    const t = synth();
    t.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'agenticai-mcp-probe-prod-demo-primary',
      Runtime: 'nodejs20.x',
      Environment: {
        Variables: Match.objectLike({
          MCP_PROTOCOL_HEADER: 'MCP-Protocol-Version',
          MCP_PROTOCOL_VERSION: '2025-06-18',
          METRIC_NAMESPACE: 'AgenticAI/MCP',
          GATEWAY_URL: 'https://gateway.example.com/a2a',
        }),
      },
    });
  });

  it('schedules the probe every 5 minutes', () => {
    const t = synth();
    t.hasResourceProperties('AWS::Events::Rule', {
      ScheduleExpression: 'rate(5 minutes)',
    });
  });

  // CUSTOM_JWT auth path: when Cognito props are supplied, the probe reads the
  // client secret (scoped) and carries the token-endpoint env vars.
  it('wires CUSTOM_JWT auth + scoped secret read when cognito props supplied', () => {
    const app = new App();
    const stack = new Stack(app, 'McpProbeJwt', { env: { account: '111111111111', region: 'us-west-2' } });
    const topic = new Topic(stack, 'FailuresTopic', { topicName: 'agenticai-eval-failures-test' });
    const secretArn = 'arn:aws:secretsmanager:us-west-2:111111111111:secret:cognito-client-abc123';
    new McpProbeConstruct(stack, 'Probe', {
      envName: 'prod', tenantId: 'demo', agentId: 'primary',
      gatewayUrl: 'https://gw123.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp',
      failuresTopic: topic,
      cognitoTokenUrl: 'https://d.auth.us-west-2.amazoncognito.com/oauth2/token',
      cognitoClientId: 'client123',
      cognitoClientSecretArn: secretArn,
      cognitoScope: 'mcp/invoke',
    });
    const t = Template.fromStack(stack);
    t.hasResourceProperties('AWS::Lambda::Function', {
      Environment: {
        Variables: Match.objectLike({
          COGNITO_TOKEN_URL: 'https://d.auth.us-west-2.amazoncognito.com/oauth2/token',
          COGNITO_CLIENT_ID: 'client123',
          COGNITO_CLIENT_SECRET_ARN: secretArn,
        }),
      },
    });
    // secret read scoped to the exact secret ARN
    t.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'ReadCognitoClientSecret',
            Action: 'secretsmanager:GetSecretValue',
            Resource: secretArn,
          }),
        ]),
      },
    });
  });

  it('cloudwatch:PutMetricData scoped to AgenticAI/MCP namespace', () => {
    const t = synth();
    const policies = t.findResources('AWS::IAM::Policy');
    const stmts = Object.values(policies).flatMap(
      (p: any) => p.Properties.PolicyDocument.Statement as Array<Record<string, unknown>>,
    );
    const put = stmts.find((s: any) =>
      (Array.isArray(s.Action) ? s.Action : [s.Action]).includes('cloudwatch:PutMetricData'),
    );
    expect(put).toBeDefined();
    expect(JSON.stringify(put!.Condition)).toContain('AgenticAI/MCP');
  });

  // Security review: InvokeGateway must NOT be account-wide '*'.
  it('bedrock-agentcore:InvokeGateway scoped to a gateway ARN, not "*"', () => {
    const t = synth();
    const policies = t.findResources('AWS::IAM::Policy');
    const stmts = Object.values(policies).flatMap(
      (p: any) => p.Properties.PolicyDocument.Statement as Array<Record<string, unknown>>,
    );
    const invoke = stmts.find((s: any) =>
      (Array.isArray(s.Action) ? s.Action : [s.Action]).includes('bedrock-agentcore:InvokeGateway'),
    );
    expect(invoke).toBeDefined();
    const resources = Array.isArray(invoke!.Resource) ? invoke!.Resource : [invoke!.Resource];
    for (const r of resources) {
      expect(r).not.toBe('*');
      // Should render a bedrock-agentcore gateway ARN.
      expect(JSON.stringify(r)).toContain('bedrock-agentcore');
      expect(JSON.stringify(r)).toContain('gateway');
    }
  });

  it('alarm treats missing data as breaching (probe silence == failure)', () => {
    const t = synth();
    t.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: 'agenticai-mcp-probe-prod-demo-primary-success',
      TreatMissingData: 'breaching',
      ComparisonOperator: 'LessThanThreshold',
      Threshold: 1,
    });
  });

  it('rejects HTTP gateway URLs at synth', () => {
    const app = new App();
    const stack = new Stack(app, 'Bad', { env: { account: '111111111111', region: 'us-west-2' } });
    const topic = new Topic(stack, 'Topic');
    expect(() =>
      new McpProbeConstruct(stack, 'P', {
        envName: 'prod',
        tenantId: 'demo',
        agentId: 'primary',
        gatewayUrl: 'http://insecure',
        failuresTopic: topic,
      }),
    ).toThrow(/HTTPS/);
  });
});
