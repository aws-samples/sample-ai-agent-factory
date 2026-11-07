/**
 * Live-validation CDK app for the Workstream side of the CloudWatch Omni hub
 * model (ADR-15, ADR-17): one throwaway stack in a cell account that
 *
 *   - opts the account into Transaction Search (spans land in `aws/spans`,
 *     which the organization centralization rule copies to the hub);
 *   - deploys option B of the production-evaluation contract: the model-free
 *     policy code evaluator, the evaluation execution role and an AgentCore
 *     online evaluation configuration sampling the validation agent's spans;
 *   - creates one blueprint-prefixed log group the validation agent writes to,
 *     so log centralization is proven alongside span centralization.
 *
 * Everything is DESTROY on delete; the stack exists only while the live gates
 * run (docs/CLOUDWATCH_OMNI_INTEGRATION.md, "Live validation").
 *
 *   npx cdk deploy --app "npx ts-node -r tsconfig-paths/register scripts/omni/live/cell_validation_app.ts" \
 *     --profile <cell-profile> -c account=<cell-account> -c region=eu-west-1 \
 *     -c serviceName=omni-validation-agent AgenticAI-OmniValidation-Cell
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App, CfnOutput, CliCredentialsStackSynthesizer, DefaultStackSynthesizer, RemovalPolicy, Stack, Tags } from "aws-cdk-lib";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";

import {
  AgentCoreOnlineEvaluationConstruct,
  EvaluationExecutionRoleConstruct,
  PolicyCodeEvaluatorConstruct,
} from "@agenticai/agentcore-evaluations";
import { TransactionSearchConstruct } from "@agenticai/omni";

const app = new App();
const account = String(app.node.tryGetContext("account") ?? "");
const region = String(app.node.tryGetContext("region") ?? "eu-west-1");
const serviceName = String(app.node.tryGetContext("serviceName") ?? "omni-validation-agent");
const manageTransactionSearch = app.node.tryGetContext("transactionSearch") !== "false";
if (!/^\d{12}$/.test(account)) {
  throw new Error("Context 'account' must be the 12-digit cell account id.");
}

const stack = new Stack(app, "AgenticAI-OmniValidation-Cell", {
  env: { account, region },
  // `-c synthesizer=bootstrap` deploys through the CDK bootstrap roles, so CloudFormation runs the
  // resource handlers as cdk-*-cfn-exec-role-* (the principal SCP-13 exempts); the default keeps the
  // operator's own credentials, which is what SCP-13 must deny for Transaction Search changes.
  synthesizer:
    app.node.tryGetContext("synthesizer") === "bootstrap"
      ? new DefaultStackSynthesizer()
      : new CliCredentialsStackSynthesizer(),
  description: "Throwaway CloudWatch Omni live-validation resources in a Workstream cell (delete after the gates).",
});

// ADR-13 amended: Transaction Search is opt-in per cell and required for Omni-observed accounts.
if (manageTransactionSearch) {
  new TransactionSearchConstruct(stack, "TransactionSearch", { indexingPercentage: 1 });
}

// Blueprint-prefixed log group the validation agent writes to (log centralization evidence).
const agentLogGroup = new LogGroup(stack, "AgentLogGroup", {
  logGroupName: `/agenticai/omni-validation/${serviceName}`,
  retention: RetentionDays.ONE_DAY,
  removalPolicy: RemovalPolicy.DESTROY,
});

// Option B: AgentCore Evaluations native, in the account that owns the spans.
const policy = new PolicyCodeEvaluatorConstruct(stack, "PolicyEvaluator", {
  evaluatorName: "agenticai_omni_validation_policy",
  functionNameSuffix: "omni-validation",
  allowedTools: ["current_time", "word_count"],
  maxOutputChars: 4000,
  logRetention: RetentionDays.ONE_DAY,
});
const executionRole = new EvaluationExecutionRoleConstruct(stack, "EvaluationRole", {
  nameSuffix: "omni-validation",
  spanLogGroupArns: [
    `arn:aws:logs:${region}:${account}:log-group:aws/spans`,
    `arn:aws:logs:${region}:${account}:log-group:aws/spans:*`,
  ],
  codeEvaluatorFunctionArns: [policy.fn.functionArn],
});
const online = new AgentCoreOnlineEvaluationConstruct(stack, "OnlineEvaluation", {
  configName: "agenticai_omni_validation",
  executionRole: executionRole.role,
  serviceName,
  // Spans AND the log group that receives the GenAI prompt/completion log events (for an agent on
  // Runtime this is its runtime log group); the evaluator reads both and fails the trace without the events.
  logGroupNames: ["aws/spans", agentLogGroup.logGroupName],
  evaluatorIds: [policy.evaluatorId],
  samplingPercentage: 100,
  sessionTimeoutMinutes: 5,
  description: "CloudWatch Omni live validation: model-free policy evaluator over the validation agent's spans.",
});
online.node.addDependency(policy.evaluator);

for (const [key, value] of Object.entries({
  "application-id": "omni-validation",
  "agent-id": serviceName,
  "tenant-id": "validation",
  "cost-centre": "platform",
  environment: "validation",
})) {
  Tags.of(stack).add(key, value);
}

new CfnOutput(stack, "AgentLogGroupName", { value: agentLogGroup.logGroupName });
new CfnOutput(stack, "PolicyEvaluatorId", { value: policy.evaluatorId });
new CfnOutput(stack, "PolicyEvaluatorFunctionName", { value: policy.fn.functionName });
new CfnOutput(stack, "EvaluationExecutionRoleArn", { value: executionRole.role.roleArn });
new CfnOutput(stack, "OnlineEvaluationConfigId", { value: online.config.attrOnlineEvaluationConfigId });
new CfnOutput(stack, "OnlineEvaluationConfigArn", { value: online.config.attrOnlineEvaluationConfigArn });
new CfnOutput(stack, "ServiceName", { value: serviceName });
