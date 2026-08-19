/**
 * @agenticai/litellm-gateway
 *
 * D-01 compensating implementation (see README §3).
 *
 * Agents in the workload account call this LiteLLM endpoint instead of
 * Bedrock directly. LiteLLM applies:
 *   - Virtual-key budgets
 *   - default_on: true guardrails
 *   - Tag propagation (application-id, cost-centre, environment, agent-id)
 *
 * The construct emits:
 *   - ECS Fargate service (single task: LiteLLM + sidecar Postgres)
 *   - Internal ALB (no public DNS)
 *   - IAM task role with Deny on `bedrock:InvokeModel`* when GuardrailIdentifier is Null
 *   - ECS task config + log group CMK-encrypted
 *
 * This is a minimal, conformance-test-driven implementation. Phase 5 follow-ons
 * will add the API Gateway fronting + VpcLink + WAF + Cognito authorizer.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export { LiteLLMGatewayConstruct, type LiteLLMGatewayConstructProps } from './litellm-gateway-construct';
