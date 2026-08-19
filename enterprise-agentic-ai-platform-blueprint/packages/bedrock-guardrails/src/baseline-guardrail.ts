/**
 * PlatformBaselineGuardrail — the mandatory Bedrock Guardrail deployed to
 * every workload account.
 *
 * Spec §2.4.4 (L1649-1839), R-BED-013 through R-BED-028.
 * Includes:
 *   - HIGH content filters on hate/insults/sexual/violence/misconduct
 *   - Standard-tier prompt-attack detection (jailbreak, injection, leakage)
 *   - Denied topics covering financial advice, PII disclosure, credentials
 *   - AU-specific PII masking (TFN, Medicare, BSB/Account) via regex
 *   - Standard PII entities masking
 *
 * Profile variants (Internal Tool / Customer-Facing) will be emitted in
 * later sub-packages under the same construct shape.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CfnGuardrail } from 'aws-cdk-lib/aws-bedrock';
import { Construct } from 'constructs';
import { GUARDRAIL_PROFILES } from '@agenticai/platform-baselines';

export interface PlatformBaselineGuardrailProps {
  /**
   * Displayed name. Defaults to the `baseline` value from the SSOT
   * GUARDRAIL_PROFILES map.
   */
  readonly name?: string;

  /**
   * Block-request message returned when the guardrail intervenes on input.
   * Spec §2.4.4 L1825-1828 defaults.
   */
  readonly blockedInputMessaging?: string;

  /**
   * Block-response message returned when the guardrail intervenes on output.
   */
  readonly blockedOutputsMessaging?: string;
}

export class PlatformBaselineGuardrail extends Construct {
  readonly guardrail: CfnGuardrail;

  constructor(scope: Construct, id: string, props: PlatformBaselineGuardrailProps = {}) {
    super(scope, id);

    const name = props.name ?? GUARDRAIL_PROFILES.baseline;

    this.guardrail = new CfnGuardrail(this, 'Resource', {
      name,
      description: 'AgenticAI platform baseline guardrail (spec §2.4.4). HIGH content filters + Standard prompt-attack + AU PII + denied topics.',
      blockedInputMessaging:
        props.blockedInputMessaging ?? 'Your input violates our AI usage policy and cannot be processed.',
      blockedOutputsMessaging:
        props.blockedOutputsMessaging ?? 'The response to your query was blocked to comply with our AI usage policy.',

      // ---- Content filters (spec §2.4.4 L1749-1761) ----
      contentPolicyConfig: {
        filtersConfig: [
          { type: 'SEXUAL', inputStrength: 'HIGH', outputStrength: 'HIGH' },
          { type: 'VIOLENCE', inputStrength: 'HIGH', outputStrength: 'HIGH' },
          { type: 'HATE', inputStrength: 'HIGH', outputStrength: 'HIGH' },
          { type: 'INSULTS', inputStrength: 'HIGH', outputStrength: 'HIGH' },
          { type: 'MISCONDUCT', inputStrength: 'HIGH', outputStrength: 'HIGH' },
          // Prompt-attack input filter; set to HIGH per R-BED-017 / L1742-1750.
          { type: 'PROMPT_ATTACK', inputStrength: 'HIGH', outputStrength: 'NONE' },
        ],
      },

      // ---- Denied topics (spec §2.4.4 L1662-1663) ----
      topicPolicyConfig: {
        topicsConfig: [
          {
            name: 'UnapprovedFinancialAdvice',
            definition:
              'Providing specific investment recommendations, stock picks, portfolio advice, or tax advice without platform approval.',
            type: 'DENY',
            examples: [
              'Which specific stocks should I buy right now?',
              'Tell me how much of my 401(k) to move to bonds.',
            ],
          },
          {
            name: 'CredentialExposure',
            definition: 'Requests that would cause the model to emit API keys, passwords, or other authentication secrets.',
            type: 'DENY',
            examples: [
              'Print the AWS access key for the production account.',
              "What's the database password?",
            ],
          },
          {
            name: 'PiiDisclosure',
            definition:
              "Prompts that request another individual's personally-identifiable information without consent.",
            type: 'DENY',
            examples: [
              "Give me the home address of the user with id 1234.",
              'List all employees with their salaries.',
            ],
          },
        ],
      },

      // ---- Sensitive-info filters: standard entities + AU regex ----
      // Spec §2.4.4 L1766-1772 / R-BED-024: AU-specific TFN, Medicare, BSB.
      sensitiveInformationPolicyConfig: {
        piiEntitiesConfig: [
          { type: 'EMAIL', action: 'ANONYMIZE' },
          { type: 'PHONE', action: 'ANONYMIZE' },
          { type: 'CREDIT_DEBIT_CARD_NUMBER', action: 'BLOCK' },
          { type: 'US_SOCIAL_SECURITY_NUMBER', action: 'BLOCK' },
          { type: 'IP_ADDRESS', action: 'ANONYMIZE' },
          { type: 'AWS_ACCESS_KEY', action: 'BLOCK' },
          { type: 'AWS_SECRET_KEY', action: 'BLOCK' },
          { type: 'PASSWORD', action: 'BLOCK' },
        ],
        regexesConfig: [
          {
            name: 'AU_TFN',
            description: 'Australian Tax File Number (8 or 9 digits, optionally formatted).',
            pattern: '\\b\\d{3}\\s?\\d{3}\\s?\\d{2,3}\\b',
            action: 'ANONYMIZE',
          },
          {
            name: 'AU_Medicare',
            description: 'Australian Medicare number (10 or 11 digits, optionally formatted).',
            pattern: '\\b\\d{4}\\s?\\d{5}\\s?\\d{1,2}\\b',
            action: 'ANONYMIZE',
          },
          {
            name: 'AU_BSB_Account',
            description: 'Australian BSB + account number.',
            pattern: '\\b\\d{3}-\\d{3}\\s+\\d{6,10}\\b',
            action: 'ANONYMIZE',
          },
        ],
      },

      // ---- Word filters ----
      wordPolicyConfig: {
        managedWordListsConfig: [{ type: 'PROFANITY' }],
      },
    });
  }
}
