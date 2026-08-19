/**
 * @agenticai/bedrock-quotas
 *
 * Submits Bedrock quota-increase requests per workload account on stack
 * deploy. Spec §2.4.6 L1944-1963 / R-BED-034 — non-prod 100 RPM, prod 500.
 *
 * Requests go through `service-quotas:RequestServiceQuotaIncrease` via a
 * CDK custom resource. The request id is written to an SSM parameter so a
 * smoke test can verify approval.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export { BedrockQuotaRequestConstruct, type BedrockQuotaRequestConstructProps } from './quota-request-construct';
