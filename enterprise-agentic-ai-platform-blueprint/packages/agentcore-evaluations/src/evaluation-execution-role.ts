/**
 * EvaluationExecutionRoleConstruct
 *
 * The IAM role Amazon Bedrock AgentCore Evaluations assumes to read agent
 * spans, write evaluation results and (optionally) invoke a judge model or a
 * code-based evaluator Lambda. The same role shape serves:
 *   - a CloudWatch Omni space (`AgentCoreEvaluationRoleArn` on the space), and
 *   - an AgentCore-native online evaluation configuration
 *     (`EvaluationExecutionRoleArn`).
 *
 * Shape follows the AgentCore Evaluations prerequisites page (trust on
 * bedrock-agentcore.amazonaws.com with SourceAccount / ResourceAccount /
 * SourceArn conditions; log read, scoped log write, span index policy; judge
 * invoke only when granted). The role name keeps the `AgentCoreEvaluationRole`
 * prefix under `/service-role/` because the CloudWatch Omni space access policy
 * only passes `service-role/AgentCoreEvaluationRole*` to the service.
 *
 * ADR-16: judge-model inference is OFF unless `judgeModelArns` is supplied.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Stack } from 'aws-cdk-lib';
import {
  Effect,
  PolicyDocument,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from 'aws-cdk-lib/aws-iam';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

export interface EvaluationExecutionRoleConstructProps {
  /**
   * Suffix appended to the mandatory `AgentCoreEvaluationRole` prefix, so one
   * account can host several roles (for example one per environment).
   * Letters, digits, hyphens; at most 40 characters.
   */
  readonly nameSuffix: string;

  /**
   * Log groups holding the agent spans this role may read and index. Defaults
   * to the account's `aws/spans` group. For a CloudWatch Omni hub fed by
   * centralization rules, pass the centralized span group ARNs as well.
   */
  readonly spanLogGroupArns?: readonly string[];

  /**
   * Bedrock foundation-model or inference-profile ARNs a custom
   * model-as-judge evaluator may invoke. Leave undefined to keep judge
   * inference disabled (ADR-16 default).
   */
  readonly judgeModelArns?: readonly string[];

  /** Lambda function ARNs of code-based evaluators this role may invoke. */
  readonly codeEvaluatorFunctionArns?: readonly string[];

  /**
   * Custom result log group names (without the reserved
   * `/aws/bedrock-agentcore/evaluations/` prefix) the role may create and
   * write to. The reserved prefix is always allowed.
   */
  readonly additionalResultLogGroupNames?: readonly string[];
}

export class EvaluationExecutionRoleConstruct extends Construct {
  readonly role: Role;

  constructor(scope: Construct, id: string, props: EvaluationExecutionRoleConstructProps) {
    super(scope, id);

    if (!/^[A-Za-z0-9-]{1,40}$/.test(props.nameSuffix)) {
      throw new Error(
        `EvaluationExecutionRoleConstruct nameSuffix '${props.nameSuffix}' must be 1-40 letters, digits or hyphens.`,
      );
    }

    const stack = Stack.of(this);
    const spanLogGroupArns = props.spanLogGroupArns ?? [
      `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:aws/spans`,
      `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:aws/spans:*`,
    ];

    const resultLogGroupArns = [
      `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:/aws/bedrock-agentcore/evaluations/*`,
      ...(props.additionalResultLogGroupNames ?? []).flatMap((name) => [
        `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:${name}`,
        `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:${name}:*`,
      ]),
    ];

    const statements: PolicyStatement[] = [
      new PolicyStatement({
        sid: 'CloudWatchLogReadStatement',
        effect: Effect.ALLOW,
        actions: ['logs:DescribeLogGroups', 'logs:GetQueryResults', 'logs:StartQuery'],
        resources: ['*'],
      }),
      new PolicyStatement({
        sid: 'CloudWatchLogWriteStatement',
        effect: Effect.ALLOW,
        actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: resultLogGroupArns,
      }),
      new PolicyStatement({
        sid: 'CloudWatchIndexPolicyStatement',
        effect: Effect.ALLOW,
        actions: ['logs:DescribeIndexPolicies', 'logs:PutIndexPolicy'],
        resources: [...spanLogGroupArns],
      }),
    ];

    if (props.judgeModelArns && props.judgeModelArns.length > 0) {
      statements.push(
        new PolicyStatement({
          sid: 'BedrockInvokeStatement',
          effect: Effect.ALLOW,
          actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
          resources: [...props.judgeModelArns],
        }),
      );
    }

    if (props.codeEvaluatorFunctionArns && props.codeEvaluatorFunctionArns.length > 0) {
      statements.push(
        new PolicyStatement({
          sid: 'LambdaInvokeStatement',
          effect: Effect.ALLOW,
          actions: ['lambda:InvokeFunction', 'lambda:GetFunction'],
          resources: [...props.codeEvaluatorFunctionArns],
        }),
      );
    }

    this.role = new Role(this, 'Role', {
      roleName: `AgentCoreEvaluationRole-${props.nameSuffix}`,
      path: '/service-role/',
      description:
        'Amazon Bedrock AgentCore Evaluations execution role: reads agent spans, writes evaluation results, invokes only the judges and code evaluators granted here.',
      assumedBy: new ServicePrincipal('bedrock-agentcore.amazonaws.com', {
        conditions: {
          StringEquals: {
            'aws:SourceAccount': stack.account,
            'aws:ResourceAccount': stack.account,
          },
          ArnLike: {
            'aws:SourceArn': [
              `arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:evaluator/*`,
              `arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:online-evaluation-config/*`,
            ],
          },
        },
      }),
      inlinePolicies: {
        AgentCoreEvaluationsExecution: new PolicyDocument({ statements }),
      },
    });

    NagSuppressions.addResourceSuppressions(
      this.role,
      [
        {
          id: 'AwsSolutions-IAM5',
          reason:
            'SEC-030: logs:DescribeLogGroups/StartQuery/GetQueryResults have no resource-level scoping that the AgentCore Evaluations service accepts (documented prerequisite shape); result writes are scoped to /aws/bedrock-agentcore/evaluations/* and explicitly named groups; index-policy actions are scoped to the span log groups; judge and Lambda grants are exact ARNs and absent by default.',
        },
        {
          id: 'NIST.800.53.R5-IAMNoInlinePolicy',
          reason:
            'SEC-030: the execution policy is role-specific (exact span groups, result groups, judge and Lambda ARNs) and is deleted with the role; a shared managed policy would widen reuse of these grants.',
        },
      ],
      true,
    );
  }
}
