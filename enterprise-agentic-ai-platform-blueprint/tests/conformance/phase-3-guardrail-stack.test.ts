/**
 * Phase 3 conformance — GuardrailAdminRole + baseline guardrail.
 *
 * Spec §2.4.3 L1612-1634 (R-BED-011, R-BED-012) — segregation of duties.
 * Spec §2.4.4 L1645-1839 (R-BED-013..R-BED-028) — baseline profile.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { GuardrailStack } from '../../apps/platform-account/lib/guardrail-stack';

const PIPELINE_ROLE_ARN = 'arn:aws:iam::111111111111:role/AgenticAI-PlatformPipelineRole';

function synth() {
  const app = new App();
  const stack = new GuardrailStack(app, 'TestGuardrailStack', {
    env: { account: '111111111111', region: 'us-west-2' },
    pipelineRoleArn: PIPELINE_ROLE_ARN,
  });
  return Template.fromStack(stack);
}

describe('Phase 3 — GuardrailAdminRole segregation (R-BED-011/012)', () => {
  it('emits the admin role with the stable name AgenticAI-GuardrailAdmin', () => {
    const t = synth();
    t.hasResourceProperties('AWS::IAM::Role', {
      RoleName: 'AgenticAI-GuardrailAdmin',
    });
  });

  it('admin role trust policy trusts only the pipeline role ARN', () => {
    const t = synth();
    const roles = t.findResources('AWS::IAM::Role', {
      Properties: { RoleName: 'AgenticAI-GuardrailAdmin' },
    });
    const entries = Object.values(roles);
    expect(entries).toHaveLength(1);
    const trust = (entries[0].Properties as any).AssumeRolePolicyDocument;
    const principal = trust.Statement[0].Principal.AWS;
    expect(principal).toBe(PIPELINE_ROLE_ARN);
  });

  it('admin role inline policy grants only Guardrail admin actions', () => {
    const t = synth();
    const roles = t.findResources('AWS::IAM::Role', {
      Properties: { RoleName: 'AgenticAI-GuardrailAdmin' },
    });
    const props = (Object.values(roles)[0].Properties as any);
    const inline = props.Policies[0].PolicyDocument.Statement[0];
    const actions: string[] = Array.isArray(inline.Action) ? inline.Action : [inline.Action];
    expect(actions.sort()).toEqual([
      'bedrock:CreateGuardrail',
      'bedrock:CreateGuardrailVersion',
      'bedrock:DeleteGuardrail',
      'bedrock:GetGuardrail',
      'bedrock:ListGuardrails',
      'bedrock:UpdateGuardrail',
    ]);
  });

  it('rejects non-ARN pipeline role input at synth time', () => {
    expect(() => {
      const app = new App();
      new GuardrailStack(app, 'BadGuardrailStack', {
        env: { account: '111111111111', region: 'us-west-2' },
        pipelineRoleArn: 'not-an-arn',
      });
    }).toThrow(/must be an IAM role ARN/);
  });
});

describe('Phase 3 — PlatformBaselineGuardrail (R-BED-013..R-BED-028)', () => {
  it('emits a CfnGuardrail with the baseline profile name', () => {
    const t = synth();
    t.hasResourceProperties('AWS::Bedrock::Guardrail', {
      Name: 'agenticai-guardrail-baseline',
    });
  });

  it('sets HIGH strength on all content filters (R-BED-016)', () => {
    const t = synth();
    const guardrails = t.findResources('AWS::Bedrock::Guardrail');
    const entries = Object.values(guardrails);
    expect(entries).toHaveLength(1);
    const filters = (entries[0].Properties as any).ContentPolicyConfig.FiltersConfig as Array<{
      Type: string;
      InputStrength: string;
      OutputStrength: string;
    }>;

    // Every non-PROMPT_ATTACK filter must be HIGH on both input and output
    const applicable = filters.filter((f) => f.Type !== 'PROMPT_ATTACK');
    for (const f of applicable) {
      expect(f.InputStrength).toBe('HIGH');
      expect(f.OutputStrength).toBe('HIGH');
    }
    const promptAttack = filters.find((f) => f.Type === 'PROMPT_ATTACK');
    expect(promptAttack?.InputStrength).toBe('HIGH');
  });

  it('embeds AU-specific PII regexes for TFN, Medicare, BSB', () => {
    const t = synth();
    const guardrails = t.findResources('AWS::Bedrock::Guardrail');
    const regexes = ((Object.values(guardrails)[0].Properties as any).SensitiveInformationPolicyConfig.RegexesConfig) as Array<{
      Name: string;
    }>;
    const names = regexes.map((r) => r.Name).sort();
    expect(names).toEqual(['AU_BSB_Account', 'AU_Medicare', 'AU_TFN']);
  });

  it('denied topics include financial advice, credential exposure, and PII disclosure', () => {
    const t = synth();
    const guardrails = t.findResources('AWS::Bedrock::Guardrail');
    const topics = (Object.values(guardrails)[0].Properties as any).TopicPolicyConfig.TopicsConfig as Array<{
      Name: string;
      Type: string;
    }>;
    const names = topics.map((t) => t.Name);
    expect(names).toContain('UnapprovedFinancialAdvice');
    expect(names).toContain('CredentialExposure');
    expect(names).toContain('PiiDisclosure');
    for (const t of topics) {
      expect(t.Type).toBe('DENY');
    }
  });

  it('blocks credit-card, SSN, AWS keys; anonymises email/phone/IP', () => {
    const t = synth();
    const guardrails = t.findResources('AWS::Bedrock::Guardrail');
    const pii = (Object.values(guardrails)[0].Properties as any).SensitiveInformationPolicyConfig.PiiEntitiesConfig as Array<{
      Type: string;
      Action: string;
    }>;
    const byType: Record<string, string> = {};
    for (const p of pii) byType[p.Type] = p.Action;
    expect(byType['CREDIT_DEBIT_CARD_NUMBER']).toBe('BLOCK');
    expect(byType['US_SOCIAL_SECURITY_NUMBER']).toBe('BLOCK');
    expect(byType['AWS_ACCESS_KEY']).toBe('BLOCK');
    expect(byType['AWS_SECRET_KEY']).toBe('BLOCK');
    expect(byType['PASSWORD']).toBe('BLOCK');
    expect(byType['EMAIL']).toBe('ANONYMIZE');
    expect(byType['PHONE']).toBe('ANONYMIZE');
    expect(byType['IP_ADDRESS']).toBe('ANONYMIZE');
  });
});
