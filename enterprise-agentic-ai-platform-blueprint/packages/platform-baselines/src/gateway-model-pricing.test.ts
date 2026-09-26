/**
 * Gateway model pricing contract tests.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { gatewayModelPrice } from "./gateway-model-pricing";

describe("gatewayModelPrice", () => {
  it.each([
    ["us-east-1", 0.00015, 0.0006],
    ["us-west-2", 0.00015, 0.0006],
    ["eu-west-1", 0.00018, 0.0007],
  ])("pins gpt-oss-120b standard pricing in %s", (region, input, output) => {
    expect(
      gatewayModelPrice(
        region,
        "agenticai-inference-nonprod-bedrock/openai.gpt-oss-120b",
      ),
    ).toEqual({
      inputUsdPer1kTokens: input,
      outputUsdPer1kTokens: output,
      effectiveDate: "2026-09-26",
    });
  });

  it.each([
    ["eu-west-1", "openai.unknown"],
    ["eu-central-1", "openai.gpt-oss-120b"],
  ])("fails closed for %s / %s", (region, modelId) => {
    expect(() => gatewayModelPrice(region, modelId)).toThrow(
      /No reviewed standard Mantle price/,
    );
  });
});
