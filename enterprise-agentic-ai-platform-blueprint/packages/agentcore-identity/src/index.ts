/**
 * @agenticai/agentcore-identity
 *
 * AgentCore Identity per spec §3.3:
 *   - Cognito User Pool + client (Phase 1 enterprise token exchange;
 *     Phase 2 OBO via custom Lambda; Phase 3 Entra deferred to v2 per DECISIONS.md Q-ENTRA)
 *   - Token Vault CMK (spec §3.3.5 / R-ID-020..023)
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export { AgentCoreIdentityConstruct, type AgentCoreIdentityConstructProps } from './identity-construct';
