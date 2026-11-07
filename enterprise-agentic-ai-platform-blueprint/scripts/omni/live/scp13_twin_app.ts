/**
 * SCP-13 live denial twin — attaches the blueprint's exact SCP-13 document
 * (`scp13OmniTelemetryLockdown`, the same function `OrganizationConstruct`
 * calls) to ONE organizational unit of a test organization, so the denials
 * and the pipeline / break-glass exemptions can be exercised against real
 * APIs without deploying the whole organization stack into an organization
 * that already has its own OUs.
 *
 * Usage (management account, any Region; Organizations is global):
 *
 *   npx cdk deploy --app "npx ts-node -r tsconfig-paths/register scripts/omni/live/scp13_twin_app.ts" \
 *     --profile <mgmt> -c account=<mgmt-account> -c region=us-east-1 -c targetOuId=ou-... \
 *     -c breakGlassRoleNames=AgenticAI-ObservabilityAdmin AgenticAI-Scp13Twin
 *
 * The policy type SERVICE_CONTROL_POLICY must be enabled on the root first
 * (`aws organizations enable-policy-type --root-id r-... --policy-type SERVICE_CONTROL_POLICY`).
 * Destroying the stack detaches and deletes the policy.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App, CfnOutput, CliCredentialsStackSynthesizer, Stack, Tags } from "aws-cdk-lib";
import { CfnPolicy } from "aws-cdk-lib/aws-organizations";

import { scp13OmniTelemetryLockdown } from "@agenticai/organizations/scps/scp-13-omni-telemetry-lockdown";

const app = new App();
const account = String(app.node.tryGetContext("account") ?? "");
const region = String(app.node.tryGetContext("region") ?? "us-east-1");
const targetOuId = String(app.node.tryGetContext("targetOuId") ?? "");
const breakGlassRaw = app.node.tryGetContext("breakGlassRoleNames");
const breakGlassRoleNames: string[] =
  typeof breakGlassRaw === "string" && breakGlassRaw.trim().length > 0
    ? breakGlassRaw.split(",").map((r) => r.trim()).filter(Boolean)
    : [];
if (!/^\d{12}$/.test(account)) {
  throw new Error("context 'account' must be the 12-digit management account id");
}
if (!/^ou-[0-9a-z]{4,32}-[0-9a-z]{8,32}$/.test(targetOuId)) {
  throw new Error("context 'targetOuId' must be an organizational unit id (ou-...)");
}

const stack = new Stack(app, "AgenticAI-Scp13Twin", {
  env: { account, region },
  synthesizer: new CliCredentialsStackSynthesizer(),
  description: "SCP-13 live denial twin: the blueprint's Omni telemetry lockdown attached to one test OU.",
});
Tags.of(stack).add("agenticai:owner", "platform");
Tags.of(stack).add("agenticai:purpose", "scp13-twin");

const scp = scp13OmniTelemetryLockdown({ breakGlassRoleNames });
// Same shape OrganizationConstruct hands to CfnPolicy (the body object; CloudFormation serialises it).
const policy = new CfnPolicy(stack, "Scp13", {
  content: scp.body,
  description: scp.description,
  name: `${scp.name}-twin`,
  type: "SERVICE_CONTROL_POLICY",
  targetIds: [targetOuId],
});

new CfnOutput(stack, "PolicyId", { value: policy.attrId });
new CfnOutput(stack, "PolicyName", { value: `${scp.name}-twin` });
new CfnOutput(stack, "BodyBytes", { value: String(Buffer.byteLength(JSON.stringify(scp.body), "utf8")) });
