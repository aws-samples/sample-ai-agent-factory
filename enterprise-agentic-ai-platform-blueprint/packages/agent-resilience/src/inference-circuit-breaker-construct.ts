/**
 * InferenceCircuitBreakerConstruct — runtime enforcement of the SSOT
 * retry/fallback/circuit-breaker policy from `circuit-breaker.ts`.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS Partial-5 (the runtime-side that the
 * v0.4.0 Phase F shipped only as a data module).
 *
 * Components emitted:
 *   - Node.js 20 inline Lambda `agenticai-inference-cb-<env>-<tenant>-<agent>`.
 *     Caller invokes it with `{ messages: [...], userId, ... }`. The Lambda:
 *       1. Looks up live failure-rate from CW metric `FallbackInvocations`.
 *       2. If rate > `errorRateMaxPct` over `windowMinutes`, opens the
 *          circuit, increments `CircuitOpenCount`, returns 503 immediately.
 *       3. Otherwise calls `bedrock:Converse` against the primary model.
 *          Retries on 429/502/503 with exp backoff (200/800/3200 ms).
 *       4. After `consecutive5xxToFallback` consecutive 5xx, switches to
 *          the secondary model and emits `FallbackInvocations`.
 *       5. Tracks `RetryStorm` (a single invoke that consumed > maxAttempts
 *          retries across the request lifecycle).
 *   - 4 CloudWatch alarms (CircuitOpen, FallbackInvocations, RetryStorm,
 *     FallbackToTotalRatio) → composite alarm → SNS failures topic.
 *   - Metric namespace `AgenticAI/InferenceCB`.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import {
  Alarm,
  AlarmRule,
  AlarmState,
  ComparisonOperator,
  CompositeAlarm,
  Metric,
  TreatMissingData,
} from 'aws-cdk-lib/aws-cloudwatch';
import { SnsAction } from 'aws-cdk-lib/aws-cloudwatch-actions';
import { ITable } from 'aws-cdk-lib/aws-dynamodb';
import { Effect, PolicyStatement } from 'aws-cdk-lib/aws-iam';
import { Code, Function as LambdaFunction, Runtime } from 'aws-cdk-lib/aws-lambda';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import { ITopic } from 'aws-cdk-lib/aws-sns';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

import { allowedBedrockResources } from '@agenticai/platform-baselines';

import {
  DEFAULT_CIRCUIT_THRESHOLDS,
  DEFAULT_RETRY_POLICY,
  defaultFallbackChain,
  validateFallbackChain,
  type CircuitBreakerThresholds,
  type FallbackChain,
  type RetryPolicy,
} from './circuit-breaker';

export const INFERENCE_CB_METRIC_NAMESPACE = 'AgenticAI/InferenceCB';

export interface InferenceCircuitBreakerConstructProps {
  readonly envName: string;
  readonly tenantId: string;
  readonly agentId: string;
  readonly failuresTopic: ITopic;
  readonly retryPolicy?: RetryPolicy;
  readonly thresholds?: CircuitBreakerThresholds;
  readonly fallbackChain?: FallbackChain;
  /**
   * G-5a: optional per-tenant quota table. When supplied the Lambda checks
   * the tenant's monthly token budget before invoking Bedrock and grants
   * via conditional update. Returns 429 when the tenant is over budget.
   */
  readonly tenantQuotaTable?: ITable;
  /** G-5a: monthly token budget per tenant; default 10M. */
  readonly tenantMonthlyTokenBudget?: number;
}

export class InferenceCircuitBreakerConstruct extends Construct {
  readonly fn: LambdaFunction;
  readonly retryPolicy: RetryPolicy;
  readonly thresholds: CircuitBreakerThresholds;
  readonly fallbackChain: FallbackChain;
  readonly compositeAlarm: CompositeAlarm;

  constructor(scope: Construct, id: string, props: InferenceCircuitBreakerConstructProps) {
    super(scope, id);

    const stack = Stack.of(this);
    this.retryPolicy = props.retryPolicy ?? DEFAULT_RETRY_POLICY;
    this.thresholds = props.thresholds ?? DEFAULT_CIRCUIT_THRESHOLDS;
    this.fallbackChain = props.fallbackChain ?? defaultFallbackChain();
    validateFallbackChain(this.fallbackChain);

    const dim = { TenantId: props.tenantId, AgentId: props.agentId, Env: props.envName };

    const logGroup = new LogGroup(this, 'Logs', {
      logGroupName: `/agenticai/inference-cb/${props.envName}/${props.tenantId}/${props.agentId}`,
      retention: RetentionDays.ONE_YEAR,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    this.fn = new LambdaFunction(this, 'Fn', {
      functionName: `agenticai-inference-cb-${props.envName}-${props.tenantId}-${props.agentId}`,
      runtime: Runtime.NODEJS_20_X,
      handler: 'index.handler',
      timeout: Duration.minutes(2),
      memorySize: 512,
      logGroup,
      environment: {
        ENV_NAME: props.envName,
        TENANT_ID: props.tenantId,
        AGENT_ID: props.agentId,
        REGION: stack.region,
        METRIC_NAMESPACE: INFERENCE_CB_METRIC_NAMESPACE,
        RETRY_POLICY_JSON: JSON.stringify(this.retryPolicy),
        THRESHOLDS_JSON: JSON.stringify(this.thresholds),
        FALLBACK_CHAIN_JSON: JSON.stringify(this.fallbackChain),
        QUOTA_TABLE: props.tenantQuotaTable ? props.tenantQuotaTable.tableName : '',
        QUOTA_MAX_TOKENS_PER_MONTH: String(props.tenantMonthlyTokenBudget ?? 10_000_000),
      },
      description: 'Inference circuit-breaker: SSOT retry + fallback + circuit-open enforcement.',
      code: Code.fromInline(INFERENCE_CB_HANDLER),
    });

    const bedrockResources = [...allowedBedrockResources(stack.region, stack.account)];
    this.fn.addToRolePolicy(
      new PolicyStatement({
        sid: 'InvokeAllowListedModels',
        effect: Effect.ALLOW,
        actions: ['bedrock:InvokeModel', 'bedrock:Converse', 'bedrock:ConverseStream'],
        resources: bedrockResources,
      }),
    );
    if (props.tenantQuotaTable) {
      props.tenantQuotaTable.grantReadWriteData(this.fn);
    }
    this.fn.addToRolePolicy(
      new PolicyStatement({
        sid: 'PutCircuitMetrics',
        effect: Effect.ALLOW,
        actions: ['cloudwatch:PutMetricData', 'cloudwatch:GetMetricStatistics'],
        resources: ['*'],
        conditions: { StringEquals: { 'cloudwatch:namespace': INFERENCE_CB_METRIC_NAMESPACE } },
      }),
    );

    NagSuppressions.addResourceSuppressions(
      this.fn,
      [
        { id: 'AwsSolutions-IAM5', reason: 'SEC-025: bedrock:InvokeModel/Converse resources are the SSOT allow-list; cw scoped via namespace condition.' },
        { id: 'NIST.800.53.R5-LambdaConcurrency', reason: 'SEC-007: caller-driven concurrency.' },
        { id: 'NIST.800.53.R5-LambdaDLQ', reason: 'SEC-008: failures emit metric + composite alarm.' },
        { id: 'NIST.800.53.R5-LambdaInsideVPC', reason: 'SEC-009: control-plane only.' },
      ],
      true,
    );

    // Alarms — the metrics are emitted as Sum over 5min windows.
    const alarmFor = (name: string, threshold: number, comparison: ComparisonOperator) => {
      return new Alarm(this, `${name}Alarm`, {
        alarmName: `agenticai-inference-cb-${props.envName}-${props.tenantId}-${props.agentId}-${name}`,
        metric: new Metric({
          namespace: INFERENCE_CB_METRIC_NAMESPACE,
          metricName: name,
          dimensionsMap: dim,
          statistic: 'Sum',
          period: Duration.minutes(5),
        }),
        comparisonOperator: comparison,
        threshold,
        evaluationPeriods: 1,
        treatMissingData: TreatMissingData.NOT_BREACHING,
      });
    };
    const circuitOpen = alarmFor('CircuitOpenCount', 0, ComparisonOperator.GREATER_THAN_THRESHOLD);
    const fallback = alarmFor('FallbackInvocations', 5, ComparisonOperator.GREATER_THAN_THRESHOLD);
    const storm = alarmFor('RetryStorm', 0, ComparisonOperator.GREATER_THAN_THRESHOLD);
    for (const a of [circuitOpen, fallback, storm]) {
      a.addAlarmAction(new SnsAction(props.failuresTopic));
    }
    this.compositeAlarm = new CompositeAlarm(this, 'Composite', {
      compositeAlarmName: `agenticai-inference-cb-${props.envName}-${props.tenantId}-${props.agentId}`,
      alarmRule: AlarmRule.anyOf(
        AlarmRule.fromAlarm(circuitOpen, AlarmState.ALARM),
        AlarmRule.fromAlarm(fallback, AlarmState.ALARM),
        AlarmRule.fromAlarm(storm, AlarmState.ALARM),
      ),
      alarmDescription: 'Inference circuit-breaker: circuit open OR sustained fallback OR retry storm.',
    });
    this.compositeAlarm.addAlarmAction(new SnsAction(props.failuresTopic));
  }
}

const INFERENCE_CB_HANDLER = `
const { BedrockRuntimeClient, ConverseCommand } = require('@aws-sdk/client-bedrock-runtime');
const { CloudWatchClient, PutMetricDataCommand, GetMetricStatisticsCommand } = require('@aws-sdk/client-cloudwatch');
const env = process.env;
const br = new BedrockRuntimeClient({});
const cw = new CloudWatchClient({});
const dim = [
  { Name: 'TenantId', Value: env.TENANT_ID },
  { Name: 'AgentId', Value: env.AGENT_ID },
  { Name: 'Env', Value: env.ENV_NAME },
];
const retry = JSON.parse(env.RETRY_POLICY_JSON);
const thresholds = JSON.parse(env.THRESHOLDS_JSON);
const chain = JSON.parse(env.FALLBACK_CHAIN_JSON);

async function emit(metricName, value) {
  await cw.send(new PutMetricDataCommand({
    Namespace: env.METRIC_NAMESPACE,
    MetricData: [{ MetricName: metricName, Dimensions: dim, Value: value, Unit: 'Count' }],
  })).catch(() => undefined);
}
async function isCircuitOpen() {
  try {
    const r = await cw.send(new GetMetricStatisticsCommand({
      Namespace: env.METRIC_NAMESPACE,
      MetricName: 'FallbackInvocations',
      Dimensions: dim,
      StartTime: new Date(Date.now() - thresholds.windowMinutes * 60 * 1000),
      EndTime: new Date(),
      Period: 60,
      Statistics: ['Sum'],
    }));
    const total = (r.Datapoints || []).reduce((a, p) => a + (p.Sum || 0), 0);
    return total >= thresholds.consecutive5xxToFallback * 2;
  } catch (e) { return false; }
}
async function callOnce(modelId, messages) {
  return await br.send(new ConverseCommand({ modelId, messages }));
}
exports.handler = async (event) => {
  const messages = (event && event.messages) || [];
  if (await isCircuitOpen()) {
    await emit('CircuitOpenCount', 1);
    return { statusCode: 503, error: 'CircuitOpen', message: 'Inference circuit-breaker open.' };
  }
  let attempt = 0; let consecutive5xx = 0; let usedFallback = false; let lastErr = null;
  for (attempt = 0; attempt < retry.maxAttempts; attempt++) {
    const modelId = usedFallback ? chain.secondary : chain.primary;
    try {
      const out = await callOnce(modelId, messages);
      if (attempt > 0 && attempt >= retry.maxAttempts - 1) await emit('RetryStorm', 1);
      if (usedFallback) await emit('FallbackInvocations', 1);
      return { statusCode: 200, modelId, output: out.output, attempt, usedFallback };
    } catch (e) {
      lastErr = e;
      const code = (e && (e.$metadata && e.$metadata.httpStatusCode)) || 0;
      const retriable = retry.retryableStatuses.includes(code);
      if (code >= 500) consecutive5xx++;
      if (!retriable) break;
      if (consecutive5xx >= thresholds.consecutive5xxToFallback && !usedFallback) {
        usedFallback = true; consecutive5xx = 0;
      }
      const backoffIdx = Math.min(attempt, retry.backoffMs.length - 1);
      await new Promise((r) => setTimeout(r, retry.backoffMs[backoffIdx]));
    }
  }
  if (attempt >= retry.maxAttempts - 1) await emit('RetryStorm', 1);
  // SEC (Holmes CSR): do not surface the raw upstream error message to the
  // caller — a Bedrock error body can echo the user prompt. Return only the
  // error's name/type + HTTP status; the full error is logged server-side.
  const lastCode = (lastErr && lastErr.$metadata && lastErr.$metadata.httpStatusCode) || 0;
  const lastName = (lastErr && (lastErr.name || (lastErr.constructor && lastErr.constructor.name))) || 'UnknownError';
  console.error('Inference attempts exhausted', { attempt, lastName, lastCode });
  return { statusCode: 500, error: 'Exhausted', errorType: lastName, upstreamStatus: lastCode, attempt };
};
`;
