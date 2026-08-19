/**
 * @agenticai/agentcore-gateway
 *
 * AgentCore Gateway + API Gateway fronting (§08 Option A).
 *
 * The API Gateway HTTP v2 is the PRIMARY auth boundary (spec §3.2 four-way
 * MUST). AgentCore Gateway behind it is the observability + configuration
 * point + Cedar micro-policies.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export { ApiGatewayFronting, type ApiGatewayFrontingProps } from './api-gateway-fronting';
export { AgentCoreGatewayConstruct, type AgentCoreGatewayConstructProps } from './agentcore-gateway-construct';
