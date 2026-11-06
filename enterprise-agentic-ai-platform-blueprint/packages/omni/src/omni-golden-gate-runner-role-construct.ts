/**
 * OmniGoldenGateRunnerRoleConstruct — the hub-account role the workload
 * pipeline assumes to run the golden-dataset promotion gate
 * (`scripts/golden_dataset_gate.py export|gate`).
 *
 * A batch evaluation (`StartBatchEvaluation`) takes no execution role: the
 * service checks the CALLER's permissions, so the gate must run under a role
 * that can read the candidate's centralized copies, read the dataset version
 * it is judged against, start and poll the batch evaluation, write its result
 * events and metrics, and decrypt the hub CMK through CloudWatch Logs. That
 * role lives in the hub account (it is the only account that holds the
 * copies and the dataset) and is assumed cross-account by the pipeline's
 * evaluation role, never by a person.
 *
 * Scope: reads are pinned to the centralized copies of the named source
 * accounts (`/centralized/<account>/...`), writes to one result log-group
 * prefix and one metrics namespace, KMS to the hub centralized-logs key via
 * the Logs service only. `replay` (invoking the candidate) is NOT granted
 * here: that side runs under the cell's evaluation invoker role.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CfnOutput, Duration, Stack, Tags } from 'aws-cdk-lib';
import {
  AccountPrincipal,
  CompositePrincipal,
  Effect,
  IPrincipal,
  PolicyDocument,
  PolicyStatement,
  Role,
} from 'aws-cdk-lib/aws-iam';
import { IKey } from 'aws-cdk-lib/aws-kms';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

import { assertAccountId } from './naming';

const ROLE_ARN_PATTERN = /^arn:aws[a-z-]*:iam::(\d{12}):role\/[\w+=,.@\-/]+$/;

export interface OmniGoldenGateRunnerRoleConstructProps {
  /**
   * The pipeline principals allowed to assume the runner: the workload
   * pipeline's evaluation role ARN(s), in the pipeline account(s). The trust
   * policy names the account and pins `aws:PrincipalArn` to these ARNs, so a
   * role recreated with the same name keeps working and no other principal
   * of that account can assume the runner.
   */
  readonly trustedPrincipalArns: readonly string[];
  /** The hub CMK that encrypts the centralized copies (and the result group). */
  readonly centralizedLogsKey: IKey;
  /**
   * Source (cell) account ids whose centralized copies the runner may query.
   * Default: every `/centralized/*` group in the hub. A per-workstream
   * pipeline should name only its own cell accounts.
   */
  readonly sourceAccountIds?: readonly string[];
  /** Log-group prefix the gate writes batch results under. Default `/agenticai/golden-gate/`. */
  readonly resultLogGroupPrefix?: string;
  /** Metrics namespace the batch evaluation publishes to. Default `agenticai/golden-gate`. */
  readonly metricsNamespace?: string;
  /** Default `AgenticAIGoldenGateRunner-<region>`. */
  readonly roleName?: string;
  readonly tags?: Record<string, string>;
}

export class OmniGoldenGateRunnerRoleConstruct extends Construct {
  readonly role: Role;
  readonly resultLogGroupPrefix: string;
  readonly metricsNamespace: string;

  constructor(scope: Construct, id: string, props: OmniGoldenGateRunnerRoleConstructProps) {
    super(scope, id);
    const stack = Stack.of(this);

    if (props.trustedPrincipalArns.length === 0) {
      throw new Error('OmniGoldenGateRunnerRoleConstruct: trustedPrincipalArns must name at least one pipeline role ARN.');
    }
    const byAccount = new Map<string, string[]>();
    for (const arn of props.trustedPrincipalArns) {
      const match = ROLE_ARN_PATTERN.exec(arn);
      if (!match) {
        throw new Error(
          `OmniGoldenGateRunnerRoleConstruct: trusted principal '${arn}' must be an IAM role ARN (arn:aws:iam::<account>:role/<name>).`,
        );
      }
      byAccount.set(match[1], [...(byAccount.get(match[1]) ?? []), arn]);
    }
    for (const account of props.sourceAccountIds ?? []) {
      assertAccountId('OmniGoldenGateRunnerRoleConstruct sourceAccountIds', account);
    }
    this.resultLogGroupPrefix = props.resultLogGroupPrefix ?? '/agenticai/golden-gate/';
    this.metricsNamespace = props.metricsNamespace ?? 'agenticai/golden-gate';
    if (!/^\/[\w./-]+\/$/.test(this.resultLogGroupPrefix)) {
      throw new Error(
        `OmniGoldenGateRunnerRoleConstruct: resultLogGroupPrefix '${this.resultLogGroupPrefix}' must start and end with '/'.`,
      );
    }

    const trusted: IPrincipal[] = [...byAccount.entries()].map(([account, arns]) =>
      new AccountPrincipal(account).withConditions({ ArnEquals: { 'aws:PrincipalArn': arns } }),
    );

    const sourceLogGroupArns = (props.sourceAccountIds && props.sourceAccountIds.length > 0
      ? props.sourceAccountIds.map((account) => `/centralized/${account}/*`)
      : ['/centralized/*']
    ).map((pattern) => `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:${pattern}`);
    const resultLogGroupArns = [
      `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:${this.resultLogGroupPrefix}*`,
    ];

    this.role = new Role(this, 'Role', {
      roleName: props.roleName ?? `AgenticAIGoldenGateRunner-${stack.region}`,
      description:
        'Assumed by the workload pipeline to run the golden-dataset promotion gate in the hub: dataset read, batch evaluation, result write.',
      assumedBy: new CompositePrincipal(...trusted),
      maxSessionDuration: Duration.hours(1),
      inlinePolicies: {
        GoldenGate: new PolicyDocument({
          statements: [
            new PolicyStatement({
              sid: 'DatasetVersionRead',
              effect: Effect.ALLOW,
              actions: ['bedrock-agentcore:GetDataset', 'bedrock-agentcore:ListDatasetExamples', 'bedrock-agentcore:ListDatasetVersions'],
              resources: [`arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:dataset/*`],
            }),
            new PolicyStatement({
              sid: 'BatchEvaluationLifecycle',
              effect: Effect.ALLOW,
              // The documented minimum policy for batch evaluations scopes these
              // actions to `batch-evaluate/*` in the account and Region
              // (AgentCore developer guide, batch evaluation prerequisites).
              actions: [
                'bedrock-agentcore:StartBatchEvaluation',
                'bedrock-agentcore:GetBatchEvaluation',
                'bedrock-agentcore:ListBatchEvaluations',
                'bedrock-agentcore:StopBatchEvaluation',
                'bedrock-agentcore:DeleteBatchEvaluation',
              ],
              resources: [`arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:batch-evaluate/*`],
            }),
            new PolicyStatement({
              sid: 'SourceCopiesQuery',
              effect: Effect.ALLOW,
              actions: ['logs:StartQuery', 'logs:GetLogEvents', 'logs:DescribeLogStreams', 'logs:FilterLogEvents'],
              resources: [...sourceLogGroupArns, ...resultLogGroupArns, ...resultLogGroupArns.map((a) => `${a}:*`)],
            }),
            new PolicyStatement({
              sid: 'QueryLifecycleNoResourceType',
              effect: Effect.ALLOW,
              actions: ['logs:GetQueryResults', 'logs:StopQuery', 'logs:DescribeLogGroups', 'logs:DescribeQueries'],
              resources: ['*'],
            }),
            new PolicyStatement({
              sid: 'ResultGroupWrite',
              effect: Effect.ALLOW,
              actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents', 'logs:PutRetentionPolicy', 'logs:AssociateKmsKey'],
              resources: [...resultLogGroupArns, ...resultLogGroupArns.map((a) => `${a}:*`)],
            }),
            new PolicyStatement({
              sid: 'ResultMetrics',
              effect: Effect.ALLOW,
              actions: ['cloudwatch:PutMetricData'],
              resources: ['*'],
              conditions: { StringEquals: { 'cloudwatch:namespace': this.metricsNamespace } },
            }),
            new PolicyStatement({
              sid: 'HubKeyThroughLogs',
              effect: Effect.ALLOW,
              actions: ['kms:Decrypt', 'kms:Encrypt', 'kms:GenerateDataKey*', 'kms:DescribeKey'],
              resources: [props.centralizedLogsKey.keyArn],
              conditions: { StringEquals: { 'kms:ViaService': `logs.${stack.region}.amazonaws.com` } },
            }),
          ],
        }),
      },
    });
    for (const [key, value] of Object.entries(props.tags ?? {})) {
      Tags.of(this.role).add(key, value);
    }
    Tags.of(this.role).add('agenticai:owner', 'platform');

    new CfnOutput(this, 'OmniGoldenGateRunnerRoleArn', {
      value: this.role.roleArn,
      description: 'Hub role the workload pipeline assumes for scripts/golden_dataset_gate.py export|gate.',
    });

    NagSuppressions.addResourceSuppressions(
      this.role,
      [
        {
          id: 'AwsSolutions-IAM5',
          reason:
            'SEC-037: dataset reads are scoped to this account\'s dataset ARNs and batch-evaluation actions to its batch-evaluate/* ARNs (the documented minimum policy); logs:GetQueryResults/StopQuery/DescribeLogGroups/DescribeQueries have no resource type; cloudwatch:PutMetricData is pinned to one namespace; log-group wildcards cover only the named source accounts\' centralized copies and the result prefix.',
        },
        {
          id: 'NIST.800.53.R5-IAMNoInlinePolicy',
          reason:
            'SEC-037: the policy names this hub\'s exact copies, result prefix, namespace and key and is deleted with the role; it is not reusable by any other principal.',
        },
      ],
      true,
    );
  }
}
