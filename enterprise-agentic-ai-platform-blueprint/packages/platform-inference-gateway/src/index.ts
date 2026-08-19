/**
 * @agenticai/platform-inference-gateway
 *
 * D-03 centralised-platform PrivateLink primitive (see README §3.3).
 *
 * Emits an internal NLB + a VpcEndpointService with `acceptanceRequired: false`
 * and `AllowedPrincipals` locked to the supplied workload account roots. The
 * NLB forwards TCP/TLS :443 to the platform's LiteLLM ALB when one is passed;
 * otherwise it carries an empty target group so the D-03 current shape
 * (AssumeRole → Bedrock direct) still synths.
 *
 * Typical wiring:
 *
 *   // platform account
 *   const gw = new PlatformInferenceGatewayConstruct(this, 'Gw', {
 *     vpc: platformVpc,
 *     workloadAccountIds: ['111111111111', '222222222222'],
 *     targetAlb: litellm.alb,      // optional
 *   });
 *
 *   // workload account (pass gw.endpointServiceName via context)
 *   new InterfaceVpcEndpoint(this, 'PlatformInferenceVpce', {
 *     vpc,
 *     service: new InterfaceVpcEndpointService(platformInferenceServiceName, 443),
 *     ...
 *   });
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export {
  PlatformInferenceGatewayConstruct,
  type PlatformInferenceGatewayConstructProps,
} from './platform-inference-gateway-construct';
