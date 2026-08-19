/**
 * AgenticDashboardConstruct — per-app CloudWatch dashboard.
 *
 * Covers Bedrock invocation count / token counts / guardrail interventions,
 * LiteLLM ECS CPU/mem, AgentCore Runtime execution duration, per-inference-
 * profile cost metrics.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Dashboard, GraphWidget, Metric, TextWidget } from 'aws-cdk-lib/aws-cloudwatch';
import { Duration } from 'aws-cdk-lib';
import { Construct } from 'constructs';

export interface AgenticDashboardConstructProps {
  readonly envName: string;
  readonly tenantId: string;
  readonly agentId: string;
  readonly inferenceProfileName: string;
}

export class AgenticDashboardConstruct extends Construct {
  readonly dashboard: Dashboard;

  constructor(scope: Construct, id: string, props: AgenticDashboardConstructProps) {
    super(scope, id);

    this.dashboard = new Dashboard(this, 'Dashboard', {
      dashboardName: `agenticai-${props.envName}-${props.tenantId}-${props.agentId}`,
      defaultInterval: Duration.hours(6),
    });

    this.dashboard.addWidgets(
      new TextWidget({
        markdown: `# Agentic AI — ${props.envName}/${props.tenantId}/${props.agentId}\nInference profile: **${props.inferenceProfileName}**.`,
        width: 24,
        height: 2,
      }),
    );

    const bedrockDim = { InferenceProfileId: props.inferenceProfileName };

    this.dashboard.addWidgets(
      new GraphWidget({
        title: 'Bedrock Invocations',
        left: [
          new Metric({
            namespace: 'AWS/Bedrock',
            metricName: 'Invocations',
            dimensionsMap: bedrockDim,
            statistic: 'Sum',
          }),
        ],
        width: 12,
      }),
      new GraphWidget({
        title: 'Input / Output Tokens',
        left: [
          new Metric({
            namespace: 'AWS/Bedrock',
            metricName: 'InputTokenCount',
            dimensionsMap: bedrockDim,
            statistic: 'Sum',
          }),
          new Metric({
            namespace: 'AWS/Bedrock',
            metricName: 'OutputTokenCount',
            dimensionsMap: bedrockDim,
            statistic: 'Sum',
          }),
        ],
        width: 12,
      }),
      new GraphWidget({
        title: 'Guardrail Interventions',
        left: [
          new Metric({
            namespace: 'AWS/Bedrock',
            metricName: 'GuardrailInvocations',
            dimensionsMap: bedrockDim,
            statistic: 'Sum',
          }),
        ],
        width: 12,
      }),
      new GraphWidget({
        title: 'Invocation Latency (p50 / p99)',
        left: [
          new Metric({
            namespace: 'AWS/Bedrock',
            metricName: 'InvocationLatency',
            dimensionsMap: bedrockDim,
            statistic: 'p50',
          }),
          new Metric({
            namespace: 'AWS/Bedrock',
            metricName: 'InvocationLatency',
            dimensionsMap: bedrockDim,
            statistic: 'p99',
          }),
        ],
        width: 12,
      }),
    );

    // Z7-F: showback spend widgets. The CUR-derived `EstimatedCharges` is
    // emitted by AWS Billing in `us-east-1` only at 6h granularity. We
    // surface a coarse running-total under the per-tenant `application-id`
    // dimension. Customers wanting per-agent splits use the QuickSight
    // showback dataset (ShowbackConstruct).
    this.dashboard.addWidgets(
      new GraphWidget({
        title: 'Estimated month-to-date spend (USD) — per-tenant',
        left: [
          new Metric({
            namespace: 'AWS/Billing',
            metricName: 'EstimatedCharges',
            dimensionsMap: { Currency: 'USD', ApplicationId: props.tenantId },
            statistic: 'Maximum',
            region: 'us-east-1',
            period: Duration.hours(6),
          }),
        ],
        width: 12,
      }),
      new TextWidget({
        markdown: `**Showback dataset:** QuickSight \`agenticai-showback-${props.envName}\` (deployed by ShowbackConstruct). For per-agent / per-cost-centre rollups query the dataset directly.`,
        width: 12,
        height: 2,
      }),
    );
  }
}
