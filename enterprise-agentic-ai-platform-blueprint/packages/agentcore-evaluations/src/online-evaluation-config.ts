/**
 * AgentCoreOnlineEvaluationConstruct
 *
 * An Amazon Bedrock AgentCore online evaluation configuration that samples an
 * agent's live traces from CloudWatch Logs and scores them continuously with
 * catalog evaluators. Results land as OpenTelemetry GenAI evaluation events in
 * a dedicated CloudWatch log group and as CloudWatch metrics, so the existing
 * alarm and SNS fan-out contract of the retired Lambda watchdog is preserved.
 *
 * Option A (CloudWatch Omni) creates the configuration inside the hub space
 * (web UI or API); option B (AgentCore native) deploys THIS construct in the
 * account that owns the spans. Both read the same evaluator catalog and write
 * the same result shape (ADR-17).
 *
 * The pinned aws-cdk-lib L1 lags the CloudFormation schema in two places, so
 * `OutputConfig` and `LogGroupNamePrefixes` are applied as property overrides
 * against the documented AWS::BedrockAgentCore::OnlineEvaluationConfig schema.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Duration, Token } from 'aws-cdk-lib';
import { CfnOnlineEvaluationConfig } from 'aws-cdk-lib/aws-bedrockagentcore';
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
import { IRole } from 'aws-cdk-lib/aws-iam';
import { ITopic } from 'aws-cdk-lib/aws-sns';
import { Construct } from 'constructs';

import { assertEvaluationResourceName, assertEvaluatorId } from './evaluator-catalog';

export interface OnlineEvaluationFilter {
  /** Trace attribute key, for example `gen_ai.agent.name`. */
  readonly key: string;
  readonly operator:
    | 'Equals'
    | 'NotEquals'
    | 'GreaterThan'
    | 'LessThan'
    | 'GreaterThanOrEqual'
    | 'LessThanOrEqual'
    | 'Contains'
    | 'NotContains';
  readonly value: string | number | boolean;
}

export interface OnlineEvaluationScoreAlarm {
  /**
   * CloudWatch metric name as published by the service for the evaluator
   * (verified live per evaluator; the service publishes one metric per
   * numerical evaluator or dimension).
   */
  readonly metricName: string;
  /** Metric dimensions to pin the series to this configuration. */
  readonly dimensions: Record<string, string>;
  /** Alarm when the average score over `periodMinutes` drops below this value. */
  readonly minimumAverageScore: number;
  /** Evaluation period in minutes. Default 60. */
  readonly periodMinutes?: number;
}

export interface AgentCoreOnlineEvaluationConstructProps {
  /** Service-compliant configuration name (`^[a-zA-Z][a-zA-Z0-9_]{0,47}$`). */
  readonly configName: string;
  /** Execution role (see EvaluationExecutionRoleConstruct). */
  readonly executionRole: IRole;
  /**
   * OpenTelemetry `service.name` of the agent, as it appears in the span log
   * group. For an AgentCore Runtime this is `<runtime-name>.<endpoint>`, for
   * example `my_agent-abc123.DEFAULT`. The service accepts exactly one.
   */
  readonly serviceName: string;
  /** Exact log group names holding the spans. Default `['aws/spans']`. */
  readonly logGroupNames?: readonly string[];
  /** Log group name prefixes (schema extension; applied as a property override). */
  readonly logGroupNamePrefixes?: readonly string[];
  /** Evaluator ids from the catalog (built-in, third-party or custom). 1-25. */
  readonly evaluatorIds: readonly string[];
  /** Percentage of matching traces to evaluate (0.01-100). */
  readonly samplingPercentage: number;
  /** Idle minutes after which a session is complete and session evaluators run (1-1440). */
  readonly sessionTimeoutMinutes?: number;
  /** Up to five trace filters. */
  readonly filters?: readonly OnlineEvaluationFilter[];
  /** Start enabled (default) or create disabled. */
  readonly enabled?: boolean;
  /** Dedicated result log group name. Default: the service's `/aws/bedrock-agentcore/evaluations/results/<id>`. */
  readonly resultLogGroupName?: string;
  /** Custom metrics namespace (must not start with `AWS/`). Default `Bedrock-AgentCore/Evaluations`. */
  readonly metricsNamespace?: string;
  /** Optional score alarms, fanned out to `failuresTopic` through one composite alarm. */
  readonly scoreAlarms?: readonly OnlineEvaluationScoreAlarm[];
  /** SNS topic receiving the composite alarm (the evaluation-gates failures topic). */
  readonly failuresTopic?: ITopic;
  readonly description?: string;
}

export class AgentCoreOnlineEvaluationConstruct extends Construct {
  readonly config: CfnOnlineEvaluationConfig;
  readonly alarms: Alarm[] = [];
  readonly compositeAlarm?: CompositeAlarm;

  constructor(scope: Construct, id: string, props: AgentCoreOnlineEvaluationConstructProps) {
    super(scope, id);

    assertEvaluationResourceName('online evaluation configuration', props.configName);
    if (props.evaluatorIds.length < 1 || props.evaluatorIds.length > 25) {
      throw new Error('AgentCoreOnlineEvaluationConstruct requires between 1 and 25 evaluator ids.');
    }
    for (const evaluatorId of props.evaluatorIds) {
      // A custom evaluator id is a deploy-time attribute (CfnEvaluator.attrEvaluatorId);
      // only literal ids can be validated at synth.
      if (!Token.isUnresolved(evaluatorId)) {
        assertEvaluatorId(evaluatorId);
      }
    }
    if (props.samplingPercentage < 0.01 || props.samplingPercentage > 100) {
      throw new Error('AgentCoreOnlineEvaluationConstruct samplingPercentage must be between 0.01 and 100.');
    }
    if (
      props.sessionTimeoutMinutes !== undefined &&
      (props.sessionTimeoutMinutes < 1 || props.sessionTimeoutMinutes > 1440)
    ) {
      throw new Error('AgentCoreOnlineEvaluationConstruct sessionTimeoutMinutes must be between 1 and 1440.');
    }
    if (props.filters && props.filters.length > 5) {
      throw new Error('AgentCoreOnlineEvaluationConstruct accepts at most 5 filters.');
    }
    if (props.metricsNamespace && /^AWS\//.test(props.metricsNamespace)) {
      throw new Error('AgentCoreOnlineEvaluationConstruct metricsNamespace must not begin with AWS/.');
    }
    if (props.resultLogGroupName && props.resultLogGroupName.startsWith('/aws/bedrock-agentcore/evaluations/')) {
      throw new Error(
        'AgentCoreOnlineEvaluationConstruct resultLogGroupName must not use the reserved /aws/bedrock-agentcore/evaluations/ prefix.',
      );
    }
    const logGroupNames = props.logGroupNames ?? (props.logGroupNamePrefixes?.length ? [] : ['aws/spans']);

    this.config = new CfnOnlineEvaluationConfig(this, 'Config', {
      onlineEvaluationConfigName: props.configName,
      description: props.description ?? 'AgenticAI continuous production evaluation (ADR-17).',
      evaluationExecutionRoleArn: props.executionRole.roleArn,
      executionStatus: props.enabled === false ? 'DISABLED' : 'ENABLED',
      dataSourceConfig: {
        cloudWatchLogs: {
          logGroupNames: [...logGroupNames],
          serviceNames: [props.serviceName],
        },
      },
      evaluators: props.evaluatorIds.map((evaluatorId) => ({ evaluatorId })),
      rule: {
        samplingConfig: { samplingPercentage: props.samplingPercentage },
        ...(props.sessionTimeoutMinutes !== undefined
          ? { sessionConfig: { sessionTimeoutMinutes: props.sessionTimeoutMinutes } }
          : {}),
        ...(props.filters && props.filters.length > 0
          ? {
              filters: props.filters.map((f) => ({
                key: f.key,
                operator: f.operator,
                value:
                  typeof f.value === 'boolean'
                    ? { booleanValue: f.value }
                    : typeof f.value === 'number'
                      ? { doubleValue: f.value }
                      : { stringValue: f.value },
              })),
            }
          : {}),
      },
    });

    if (props.logGroupNamePrefixes && props.logGroupNamePrefixes.length > 0) {
      this.config.addPropertyOverride('DataSourceConfig.CloudWatchLogs.LogGroupNamePrefixes', [
        ...props.logGroupNamePrefixes,
      ]);
      if (logGroupNames.length === 0) {
        this.config.addPropertyDeletionOverride('DataSourceConfig.CloudWatchLogs.LogGroupNames');
      }
    }

    const cloudWatchOutput: Record<string, string> = {};
    if (props.resultLogGroupName) {
      cloudWatchOutput.ResultDestination = 'DEDICATED_LOG_GROUP';
      cloudWatchOutput.LogGroupName = props.resultLogGroupName;
    }
    if (props.metricsNamespace) {
      cloudWatchOutput.MetricsNamespace = props.metricsNamespace;
    }
    if (Object.keys(cloudWatchOutput).length > 0) {
      this.config.addPropertyOverride('OutputConfig.CloudWatchConfig', cloudWatchOutput);
    }

    if (props.scoreAlarms && props.scoreAlarms.length > 0) {
      const namespace = props.metricsNamespace ?? 'Bedrock-AgentCore/Evaluations';
      for (const [index, spec] of props.scoreAlarms.entries()) {
        const metric = new Metric({
          namespace,
          metricName: spec.metricName,
          dimensionsMap: spec.dimensions,
          statistic: 'Average',
          period: Duration.minutes(spec.periodMinutes ?? 60),
        });
        const alarm = new Alarm(this, `ScoreAlarm${index}`, {
          alarmName: `agenticai-online-eval-${props.configName}-${spec.metricName.replace(/[^A-Za-z0-9]/g, '-')}`,
          alarmDescription: `Average ${spec.metricName} below ${spec.minimumAverageScore} for ${props.configName}.`,
          metric,
          threshold: spec.minimumAverageScore,
          comparisonOperator: ComparisonOperator.LESS_THAN_THRESHOLD,
          evaluationPeriods: 1,
          treatMissingData: TreatMissingData.NOT_BREACHING,
        });
        this.alarms.push(alarm);
      }
      this.compositeAlarm = new CompositeAlarm(this, 'Composite', {
        compositeAlarmName: `agenticai-online-eval-${props.configName}`,
        alarmDescription: 'Any configured evaluation score below its floor. Fans out to the evaluation-gates failures topic.',
        alarmRule: AlarmRule.anyOf(
          ...this.alarms.map((alarm) => AlarmRule.fromAlarm(alarm, AlarmState.ALARM)),
        ),
      });
      if (props.failuresTopic) {
        this.compositeAlarm.addAlarmAction(new SnsAction(props.failuresTopic));
      }
    }
  }
}
