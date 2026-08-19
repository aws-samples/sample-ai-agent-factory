/**
 * AgenticAlarmsConstruct — per-app CloudWatch alarms.
 *
 * Default thresholds are the same ones the CI evaluation gate enforces between
 * workload-nonprod and workload-prod — see `scripts/evaluation_gate.py`.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Alarm, ComparisonOperator, Metric, TreatMissingData } from 'aws-cdk-lib/aws-cloudwatch';
import { SnsAction } from 'aws-cdk-lib/aws-cloudwatch-actions';
import { Topic } from 'aws-cdk-lib/aws-sns';
import { Duration, Stack } from 'aws-cdk-lib';
import { Key } from 'aws-cdk-lib/aws-kms';
import { Effect, PolicyStatement, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export interface AgenticAlarmsConstructProps {
  readonly envName: string;
  readonly tenantId: string;
  readonly agentId: string;
  readonly inferenceProfileName: string;
  /** Guardrail-violation alarm threshold (violations per 5 minutes). Default 5. */
  readonly guardrailViolationThreshold?: number;
  /** First-token p99 latency alarm threshold (ms). Default 1500 per eval-gate SLO. */
  readonly firstTokenLatencyP99Ms?: number;
}

export class AgenticAlarmsConstruct extends Construct {
  readonly guardrailViolationAlarm: Alarm;
  readonly latencyAlarm: Alarm;
  readonly topic: Topic;
  readonly topicKey: Key;

  constructor(scope: Construct, id: string, props: AgenticAlarmsConstructProps) {
    super(scope, id);

    const stack = Stack.of(this);
    const bedrockDim = { InferenceProfileId: props.inferenceProfileName };
    const base = `agenticai-${props.envName}-${props.tenantId}-${props.agentId}`;

    // CMK for the alarm topic.
    this.topicKey = new Key(this, 'TopicKey', {
      alias: `alias/agenticai/alarms-${props.envName}-${props.tenantId}-${props.agentId}`,
      description: 'CMK for per-app CloudWatch alarm SNS topic.',
      enableKeyRotation: true,
    });
    this.topicKey.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowCloudWatchAlarms',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal('cloudwatch.amazonaws.com')],
        actions: ['kms:Decrypt', 'kms:GenerateDataKey*'],
        resources: ['*'],
      }),
    );

    this.topic = new Topic(this, 'AlarmTopic', {
      topicName: `${base}-alarms`,
      displayName: `AgenticAI alarms ${base}`,
      masterKey: this.topicKey,
      enforceSSL: true,
    });

    void stack;

    this.guardrailViolationAlarm = new Alarm(this, 'GuardrailViolations', {
      alarmName: `${base}-guardrail-violations`,
      alarmDescription: 'Spike in Bedrock Guardrail interventions — content policy anomaly.',
      metric: new Metric({
        namespace: 'AWS/Bedrock',
        metricName: 'GuardrailInterventionCount',
        dimensionsMap: bedrockDim,
        statistic: 'Sum',
        period: Duration.minutes(5),
      }),
      threshold: props.guardrailViolationThreshold ?? 5,
      evaluationPeriods: 1,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: TreatMissingData.NOT_BREACHING,
    });
    this.guardrailViolationAlarm.addAlarmAction(new SnsAction(this.topic));
    this.guardrailViolationAlarm.addOkAction(new SnsAction(this.topic));

    this.latencyAlarm = new Alarm(this, 'FirstTokenLatencyP99', {
      alarmName: `${base}-first-token-p99`,
      alarmDescription: 'First-token p99 latency exceeded the SLO (eval-gate threshold).',
      metric: new Metric({
        namespace: 'AWS/Bedrock',
        metricName: 'InvocationLatency',
        dimensionsMap: bedrockDim,
        statistic: 'p99',
        period: Duration.minutes(5),
      }),
      threshold: props.firstTokenLatencyP99Ms ?? 1500,
      evaluationPeriods: 3,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: TreatMissingData.NOT_BREACHING,
    });
    this.latencyAlarm.addAlarmAction(new SnsAction(this.topic));
    this.latencyAlarm.addOkAction(new SnsAction(this.topic));
  }
}
