/**
 * @agenticai/agentcore-runtime
 *
 * AgentCore Runtime-adjacent resources deployed per workload account:
 *   - Execution role (spec §2.4.3 IAM / R-BED-007..010)
 *     • Under D-01: scoped to reach the LiteLLM endpoint + ECR + CloudWatch,
 *       NOT direct Bedrock. Deny-on-null-guardrail still present for belt-
 *       and-braces even though LiteLLM applies the guardrail.
 *   - ECR repo for the agent container image
 *   - CloudWatch log group scoped to `/agenticai/<app>/*` (R-TEN-006)
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export {
  AgentCoreRuntimeConstruct,
  type AgentCoreRuntimeConstructProps,
} from './runtime-construct';
