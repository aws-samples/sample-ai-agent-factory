/**
 * @agenticai/agentic-app
 *
 * L3 composition: AgenticApp per tenant + agent. Wires together:
 *   - AgentCoreRuntime (execution role, ECR, log group)
 *   - AgentCoreMemory (namespace, CMK)
 *   - Application inference profile (R-TEN-013, R-TEN-029, R-TEN-030)
 *   - Per-app security group (R-TEN-022)
 *
 * Spec §2.5 tenancy per-app isolation — all resources carry the tenantId +
 * agentId tags and are scoped to per-app names so IAM scoping is mechanical.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export { AgenticApp, type AgenticAppProps } from './agentic-app';
