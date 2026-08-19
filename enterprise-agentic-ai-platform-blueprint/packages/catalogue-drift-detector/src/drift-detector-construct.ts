/**
 * CatalogueDriftDetectorConstruct — scheduled Lambda that compares the
 * platform-tool-catalogue SSOT vs. the live AgentCore Gateway target list,
 * emits CW metrics under `AgenticAI/Catalogue/*`, alarms on drift > 0.
 *
 * G-5d: wires `@agenticai/catalogue-drift-detector` (pure-fn module) into
 * a real scheduled execution path.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import {
  Alarm,
  ComparisonOperator,
  Metric,
  TreatMissingData,
} from 'aws-cdk-lib/aws-cloudwatch';
import { SnsAction } from 'aws-cdk-lib/aws-cloudwatch-actions';
import { Rule, Schedule } from 'aws-cdk-lib/aws-events';
import { LambdaFunction as EventsLambdaTarget } from 'aws-cdk-lib/aws-events-targets';
import { Effect, PolicyStatement, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import { Key } from 'aws-cdk-lib/aws-kms';
import { Code, Function as LambdaFunction, Runtime } from 'aws-cdk-lib/aws-lambda';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import { ITopic } from 'aws-cdk-lib/aws-sns';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

export const CATALOGUE_DRIFT_NAMESPACE = 'AgenticAI/Catalogue';

export interface CatalogueDriftDetectorConstructProps {
  readonly envName: string;
  readonly tenantId: string;
  readonly agentId: string;
  readonly failuresTopic: ITopic;
  /** SSOT-declared tool ids the gateway should carry. */
  readonly catalogueIds: readonly string[];
  /** Optional override; defaults to 1/day. */
  readonly cadence?: Duration;
}

export class CatalogueDriftDetectorConstruct extends Construct {
  readonly fn: LambdaFunction;
  readonly schedule: Rule;
  readonly driftAlarm: Alarm;
  readonly kmsKey: Key;

  constructor(scope: Construct, id: string, props: CatalogueDriftDetectorConstructProps) {
    super(scope, id);
    const stack = Stack.of(this);
    const dim = { TenantId: props.tenantId, AgentId: props.agentId, Env: props.envName };

    // SEC (Holmes CSR): CMK-encrypt the Lambda log group — matches the CMK
    // log-group idiom used across the blueprint (e.g. online-evaluation).
    this.kmsKey = new Key(this, 'LogKey', {
      alias: `alias/agenticai/catalogue-drift-${props.envName}-${props.tenantId}-${props.agentId}`,
      description: `CMK for catalogue-drift logs (${props.envName}/${props.tenantId}/${props.agentId}).`,
      enableKeyRotation: true,
      pendingWindow: Duration.days(30),
      removalPolicy: RemovalPolicy.DESTROY,
    });
    // CloudWatch Logs needs kms:GenerateDataKey* etc., scoped by log-group ARN.
    this.kmsKey.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowCloudWatchLogs',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal(`logs.${stack.region}.amazonaws.com`)],
        actions: ['kms:Encrypt*', 'kms:Decrypt*', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:Describe*'],
        resources: ['*'],
        conditions: {
          ArnLike: { 'kms:EncryptionContext:aws:logs:arn': `arn:aws:logs:${stack.region}:${stack.account}:*` },
        },
      }),
    );

    const logGroup = new LogGroup(this, 'Logs', {
      logGroupName: `/agenticai/catalogue-drift/${props.envName}/${props.tenantId}/${props.agentId}`,
      retention: RetentionDays.SIX_MONTHS,
      encryptionKey: this.kmsKey,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    this.fn = new LambdaFunction(this, 'Fn', {
      functionName: `agenticai-catalogue-drift-${props.envName}-${props.tenantId}-${props.agentId}`,
      runtime: Runtime.NODEJS_20_X,
      handler: 'index.handler',
      timeout: Duration.minutes(5),
      memorySize: 256,
      logGroup,
      environment: {
        ENV_NAME: props.envName,
        TENANT_ID: props.tenantId,
        AGENT_ID: props.agentId,
        METRIC_NAMESPACE: CATALOGUE_DRIFT_NAMESPACE,
        CATALOGUE_IDS: JSON.stringify(props.catalogueIds),
      },
      description: 'Catalogue drift detector — compares SSOT vs live Gateway targets.',
      code: Code.fromInline(DRIFT_HANDLER),
    });

    this.fn.addToRolePolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ['bedrock-agentcore:ListGatewayTargets', 'bedrock-agentcore:ListGateways'],
        resources: ['*'],
      }),
    );
    this.fn.addToRolePolicy(
      new PolicyStatement({
        sid: 'PutDriftMetrics',
        effect: Effect.ALLOW,
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'],
        conditions: { StringEquals: { 'cloudwatch:namespace': CATALOGUE_DRIFT_NAMESPACE } },
      }),
    );

    NagSuppressions.addResourceSuppressions(
      this.fn,
      [
        { id: 'AwsSolutions-IAM5', reason: 'SEC-025: AgentCore list ops do not yet support resource-level scoping; cw scoped via namespace condition.' },
        { id: 'NIST.800.53.R5-LambdaConcurrency', reason: 'SEC-007: cadence-driven daily.' },
        { id: 'NIST.800.53.R5-LambdaDLQ', reason: 'SEC-008: failure path emits drift metric + composite alarm.' },
        { id: 'NIST.800.53.R5-LambdaInsideVPC', reason: 'SEC-009: control-plane only.' },
      ],
      true,
    );

    this.schedule = new Rule(this, 'Schedule', {
      ruleName: `agenticai-catalogue-drift-${props.envName}-${props.tenantId}-${props.agentId}`,
      schedule: Schedule.rate(props.cadence ?? Duration.days(1)),
      description: 'Catalogue drift detector daily run.',
    });
    this.schedule.addTarget(new EventsLambdaTarget(this.fn));

    this.driftAlarm = new Alarm(this, 'DriftAlarm', {
      alarmName: `agenticai-catalogue-drift-${props.envName}-${props.tenantId}-${props.agentId}`,
      metric: new Metric({
        namespace: CATALOGUE_DRIFT_NAMESPACE,
        metricName: 'DriftCount',
        dimensionsMap: dim,
        statistic: 'Maximum',
        period: Duration.hours(24),
      }),
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      threshold: 0,
      evaluationPeriods: 1,
      treatMissingData: TreatMissingData.NOT_BREACHING,
    });
    this.driftAlarm.addAlarmAction(new SnsAction(props.failuresTopic));
  }
}

const DRIFT_HANDLER = `
const { BedrockAgentCoreControlClient, ListGatewayTargetsCommand, ListGatewaysCommand } = require('@aws-sdk/client-bedrock-agentcore-control');
const { CloudWatchClient, PutMetricDataCommand } = require('@aws-sdk/client-cloudwatch');
const env = process.env;
const acc = new BedrockAgentCoreControlClient({});
const cw = new CloudWatchClient({});
const dim = [
  { Name: 'TenantId', Value: env.TENANT_ID },
  { Name: 'AgentId', Value: env.AGENT_ID },
  { Name: 'Env', Value: env.ENV_NAME },
];
function setDifference(a, b) { return a.filter((x) => !b.includes(x)).sort(); }
exports.handler = async () => {
  const catalogue = JSON.parse(env.CATALOGUE_IDS);
  let liveTargetIds = [];
  try {
    const gws = await acc.send(new ListGatewaysCommand({}));
    for (const g of (gws.items || [])) {
      const t = await acc.send(new ListGatewayTargetsCommand({ gatewayIdentifier: g.gatewayIdentifier || g.name }));
      liveTargetIds = liveTargetIds.concat((t.items || []).map((it) => it.name || it.targetId));
    }
  } catch (e) {
    console.log(JSON.stringify({ warn: 'list-gateways/targets failed; assuming empty live set', err: String(e && e.name || e) }));
  }
  const missingFromGateway = setDifference(catalogue, liveTargetIds);
  const missingFromCatalogue = setDifference(liveTargetIds, catalogue);
  const driftCount = missingFromGateway.length + missingFromCatalogue.length;
  const inSync = driftCount === 0 ? 1 : 0;
  await cw.send(new PutMetricDataCommand({
    Namespace: env.METRIC_NAMESPACE,
    MetricData: [
      { MetricName: 'DriftCount', Dimensions: dim, Value: driftCount, Unit: 'Count' },
      { MetricName: 'MissingFromGateway', Dimensions: dim, Value: missingFromGateway.length, Unit: 'Count' },
      { MetricName: 'MissingFromCatalogue', Dimensions: dim, Value: missingFromCatalogue.length, Unit: 'Count' },
      { MetricName: 'InSync', Dimensions: dim, Value: inSync, Unit: 'Count' },
    ],
  }));
  return { ok: true, driftCount, missingFromGateway, missingFromCatalogue };
};
`;
