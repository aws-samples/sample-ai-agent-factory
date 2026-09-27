/**
 * Standard-tier Bedrock Mantle token prices used by the generated-agent
 * evaluation gate. Values are USD per 1,000 tokens from AWS Price List,
 * queried 2026-09-26. Unknown model/Region pairs fail closed so a price change
 * or model expansion cannot silently under-report evaluation cost.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
export interface GatewayModelPrice {
  readonly inputUsdPer1kTokens: number;
  readonly outputUsdPer1kTokens: number;
  readonly effectiveDate: string;
}

const GPT_OSS_120B = "openai.gpt-oss-120b";
const EFFECTIVE_DATE = "2026-09-26";

export const GATEWAY_MODEL_PRICES: Readonly<
  Record<string, Readonly<Record<string, GatewayModelPrice>>>
> = {
  "us-east-1": {
    [GPT_OSS_120B]: {
      inputUsdPer1kTokens: 0.00015,
      outputUsdPer1kTokens: 0.0006,
      effectiveDate: EFFECTIVE_DATE,
    },
  },
  "us-west-2": {
    [GPT_OSS_120B]: {
      inputUsdPer1kTokens: 0.00015,
      outputUsdPer1kTokens: 0.0006,
      effectiveDate: EFFECTIVE_DATE,
    },
  },
  "eu-west-1": {
    [GPT_OSS_120B]: {
      inputUsdPer1kTokens: 0.00018,
      outputUsdPer1kTokens: 0.0007,
      effectiveDate: EFFECTIVE_DATE,
    },
  },
} as const;

export function gatewayModelPrice(
  region: string,
  targetQualifiedModelId: string,
): GatewayModelPrice {
  const modelId = targetQualifiedModelId.split("/").at(-1) ?? "";
  const price = GATEWAY_MODEL_PRICES[region]?.[modelId];
  if (!price) {
    throw new Error(
      `No reviewed standard Mantle price for ${modelId || "<empty>"} in ${region}.`,
    );
  }
  return price;
}
