/**
 * @agenticai/platform-inference-gateway
 *
 * Pipeline-owned central inference path built from native AgentCore Gateway,
 * GatewayTarget and GatewayRateLimit CloudFormation resources. Cognito issues
 * client-credentials JWTs and the target uses the Bedrock Mantle connector.
 * A REQUEST interceptor applies the platform baseline Bedrock Guardrail to
 * every request body before the model is called and fails closed.
 *
 * Generated agents use the Gateway's `/inference/v1` OpenAI-compatible route
 * through Strands `LiteLLMModel`; they do not call Bedrock directly.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export {
  PlatformInferenceGatewayConstruct,
  allowedMantleModelIds,
  type InferenceInputGuardrail,
  type InferenceModelRateLimit,
  type PlatformInferenceGatewayConstructProps,
} from './platform-inference-gateway-construct';
