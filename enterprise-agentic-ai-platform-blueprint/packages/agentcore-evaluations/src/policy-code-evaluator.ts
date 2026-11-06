/**
 * PolicyCodeEvaluatorConstruct
 *
 * A code-based AgentCore evaluator backed by the blueprint's deterministic
 * policy Lambda (`lambda/policy-evaluator`): refusal-on-adversarial-input,
 * no system-prompt echo, bounded output, and tool allow-list. Model-free, so
 * it never crosses the D-01 inference boundary and costs no judge tokens.
 *
 * The function name carries the `cloudwatchCodeEvaluator` prefix because the
 * CloudWatch Omni space access policy may only invoke code evaluators named
 * `cloudwatchCodeEvaluator*`; the AgentCore-native option has no such
 * restriction, so one function serves both options.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import * as path from 'path';

import { Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import { CfnEvaluator } from 'aws-cdk-lib/aws-bedrockagentcore';
import { ManagedPolicy, Role, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import { Code, Function as LambdaFunction, Runtime } from 'aws-cdk-lib/aws-lambda';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

import { assertEvaluationResourceName } from './evaluator-catalog';

export interface PolicyCodeEvaluatorConstructProps {
  /** Service-compliant evaluator name (`^[a-zA-Z][a-zA-Z0-9_]{0,47}$`). */
  readonly evaluatorName: string;
  /** Short unique suffix for the Lambda function name (letters, digits, hyphens). */
  readonly functionNameSuffix: string;
  /** Qualified or leaf tool names the agent is subscribed to. Empty disables the tool-scope check. */
  readonly allowedTools?: readonly string[];
  /** Character budget for the assistant output. Default 8000. */
  readonly maxOutputChars?: number;
  /** Substring that must never appear in the assistant output. Default `SYSTEM PROMPT`. */
  readonly systemPromptMarker?: string;
  /** Override the adversarial corpus regex (case-insensitive). */
  readonly adversarialPattern?: string;
  /** Lambda invocation timeout the service applies (1-300 s). Default 60. */
  readonly evaluatorTimeoutSeconds?: number;
  /** Optional customer managed key ARN for the evaluator's stored configuration. */
  readonly kmsKeyArn?: string;
  /** Log retention for the evaluator function. Default one month. */
  readonly logRetention?: RetentionDays;
}

export class PolicyCodeEvaluatorConstruct extends Construct {
  readonly fn: LambdaFunction;
  readonly evaluator: CfnEvaluator;
  /** The service-assigned evaluator id, as referenced by online evaluation configurations. */
  readonly evaluatorId: string;

  constructor(scope: Construct, id: string, props: PolicyCodeEvaluatorConstructProps) {
    super(scope, id);

    assertEvaluationResourceName('evaluator', props.evaluatorName);
    if (!/^[A-Za-z0-9-]{1,30}$/.test(props.functionNameSuffix)) {
      throw new Error(
        `PolicyCodeEvaluatorConstruct functionNameSuffix '${props.functionNameSuffix}' must be 1-30 letters, digits or hyphens.`,
      );
    }
    const timeoutSeconds = props.evaluatorTimeoutSeconds ?? 60;
    if (timeoutSeconds < 1 || timeoutSeconds > 300) {
      throw new Error('PolicyCodeEvaluatorConstruct evaluatorTimeoutSeconds must be between 1 and 300.');
    }

    const stack = Stack.of(this);
    const functionName = `cloudwatchCodeEvaluator-${props.functionNameSuffix}`;

    const logGroup = new LogGroup(this, 'LogGroup', {
      logGroupName: `/aws/lambda/${functionName}`,
      retention: props.logRetention ?? RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const role = new Role(this, 'Role', {
      assumedBy: new ServicePrincipal('lambda.amazonaws.com'),
      description: 'Execution role for the AgenticAI policy code evaluator (logs only; no AWS data-plane access).',
      managedPolicies: [ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole')],
    });

    const environment: Record<string, string> = {
      POLICY_MAX_OUTPUT_CHARS: String(props.maxOutputChars ?? 8000),
      POLICY_SYSTEM_PROMPT_MARKER: props.systemPromptMarker ?? 'SYSTEM PROMPT',
    };
    if (props.allowedTools && props.allowedTools.length > 0) {
      environment.POLICY_ALLOWED_TOOLS = JSON.stringify([...props.allowedTools]);
    }
    if (props.adversarialPattern) {
      environment.POLICY_ADVERSARIAL_RE = props.adversarialPattern;
    }

    this.fn = new LambdaFunction(this, 'Function', {
      functionName,
      description:
        'AgentCore code-based evaluator: deterministic AgenticAI policy checks (refusal, system-prompt echo, output budget, tool scope).',
      runtime: Runtime.PYTHON_3_13,
      handler: 'index.handler',
      timeout: Duration.seconds(Math.min(timeoutSeconds, 300)),
      memorySize: 256,
      role,
      logGroup,
      code: Code.fromAsset(path.join(__dirname, '..', 'lambda', 'policy-evaluator'), {
        exclude: ['test_*.py', '__pycache__', '.pytest_cache'],
      }),
      environment,
    });

    this.evaluator = new CfnEvaluator(this, 'Evaluator', {
      evaluatorName: props.evaluatorName,
      description: 'AgenticAI deterministic policy checks (code-based; model-free).',
      level: 'TRACE',
      kmsKeyArn: props.kmsKeyArn,
      evaluatorConfig: {
        codeBased: {
          lambdaConfig: {
            lambdaArn: this.fn.functionArn,
            lambdaTimeoutInSeconds: timeoutSeconds,
          },
        },
      },
    });
    this.evaluatorId = this.evaluator.attrEvaluatorId;

    NagSuppressions.addResourceSuppressions(
      role,
      [
        {
          id: 'AwsSolutions-IAM4',
          reason: 'SEC-031: AWSLambdaBasicExecutionRole is the documented minimal logging policy; the evaluator holds no other permissions.',
        },
      ],
      true,
    );
    NagSuppressions.addResourceSuppressions(this.fn, [
      {
        id: 'AwsSolutions-L1',
        reason: 'Python 3.13 is the latest runtime supported by the pinned aws-cdk-lib; the evaluator has no third-party dependencies.',
      },
    ]);
    void stack;
  }
}
