/**
 * Phase 1 conformance — synth-time assertions that OrgStack emits the
 * resources required by spec §2.1.2 (OUs) and §2.2 (SCPs 01-08).
 *
 * These are the first conformance tests in the suite: they pin the OU tree and
 * the SCP set so that a drift in either fails at synth time rather than in a
 * live organization.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';

import { OrgStack } from '../../apps/management-account/lib/org-stack';

const ADMIN_ROLE = 'arn:aws:iam::111111111111:role/AgenticAI-GuardrailAdmin';

function synth(opts: { attachToWorkloadsOu?: boolean } = {}) {
  const app = new App();
  const stack = new OrgStack(app, 'TestOrgStack', {
    env: { account: '222222222222', region: 'us-west-2' },
    platformGuardrailAdminRoleArn: ADMIN_ROLE,
    attachToWorkloadsOu: opts.attachToWorkloadsOu ?? false,
  });
  return Template.fromStack(stack);
}

describe('Phase 1 — OU hierarchy (spec §2.1.2)', () => {
  it('emits exactly 5 OUs (Security, SharedServices, AgenticAI-Platform, AgenticAI-Workloads, Sandbox)', () => {
    const t = synth();
    t.resourceCountIs('AWS::Organizations::OrganizationalUnit', 5);
  });

  it('emits each OU with its spec-prescribed name', () => {
    const t = synth();
    t.hasResourceProperties('AWS::Organizations::OrganizationalUnit', { Name: 'Security' });
    t.hasResourceProperties('AWS::Organizations::OrganizationalUnit', { Name: 'SharedServices' });
    t.hasResourceProperties('AWS::Organizations::OrganizationalUnit', { Name: 'AgenticAI-Platform' });
    t.hasResourceProperties('AWS::Organizations::OrganizationalUnit', { Name: 'AgenticAI-Workloads' });
    t.hasResourceProperties('AWS::Organizations::OrganizationalUnit', { Name: 'AgenticAI-Sandbox' });
  });
});

describe('Phase 1 — SCPs 01-08 (spec §2.2)', () => {
  it('emits exactly 8 Organizations::Policy resources', () => {
    const t = synth();
    t.resourceCountIs('AWS::Organizations::Policy', 8);
  });

  it('each SCP is typed as SERVICE_CONTROL_POLICY', () => {
    const t = synth();
    const policies = t.findResources('AWS::Organizations::Policy');
    for (const logicalId of Object.keys(policies)) {
      expect(policies[logicalId].Properties.Type).toBe('SERVICE_CONTROL_POLICY');
    }
  });

  it('SCPs are named with the AgenticAI- prefix', () => {
    const t = synth();
    const policies = t.findResources('AWS::Organizations::Policy');
    const names = Object.values(policies).map(
      (r: Record<string, unknown>) => (r.Properties as Record<string, unknown>).Name as string,
    );
    for (const n of names) {
      expect(n).toMatch(/^AgenticAI-SCP-0/);
    }
    expect(names.sort()).toEqual([
      'AgenticAI-SCP-01-ModelAllowlist',
      'AgenticAI-SCP-02-EnforceGuardrail',
      'AgenticAI-SCP-03-EnforceAgentCoreVpce',
      'AgenticAI-SCP-04-EnforceBedrockVpce',
      'AgenticAI-SCP-05-DenyGuardrailModification',
      'AgenticAI-SCP-06-RestrictRegions',
      'AgenticAI-SCP-07-DenyPublicAgentCore',
      'AgenticAI-SCP-08-DenyEcrPublic',
    ]);
  });

  it('every SCP body is valid IAM JSON and under the 5000-char soft limit (R-SCP-017)', () => {
    const t = synth();
    const policies = t.findResources('AWS::Organizations::Policy');
    for (const res of Object.values(policies)) {
      const props = res.Properties as Record<string, unknown>;
      const content = props.Content;
      // CfnPolicy.content may be rendered as an object (when we pass an object)
      // or a JSON string (when a token resolves). Either is valid.
      const parsed: { Version: string; Statement: unknown[] } =
        typeof content === 'string' ? JSON.parse(content) : (content as { Version: string; Statement: unknown[] });
      expect(parsed.Version).toBe('2012-10-17');
      expect(Array.isArray(parsed.Statement)).toBe(true);
      const asJson = typeof content === 'string' ? content : JSON.stringify(content);
      expect(asJson.length).toBeLessThan(5000);
    }
  });
});

describe('Phase 1 — Sandbox-first attachment', () => {
  it('attaches to Sandbox OU only when attachToWorkloadsOu is false', () => {
    const t = synth({ attachToWorkloadsOu: false });
    const policies = t.findResources('AWS::Organizations::Policy');
    for (const res of Object.values(policies)) {
      const targets = (res.Properties as Record<string, unknown>).TargetIds as unknown[];
      expect(Array.isArray(targets)).toBe(true);
      expect(targets.length).toBe(1); // Sandbox only
    }
  });

  it('attaches to both Sandbox OU and AgenticAI-Workloads OU when attachToWorkloadsOu is true', () => {
    const t = synth({ attachToWorkloadsOu: true });
    const policies = t.findResources('AWS::Organizations::Policy');
    for (const res of Object.values(policies)) {
      const targets = (res.Properties as Record<string, unknown>).TargetIds as unknown[];
      expect(Array.isArray(targets)).toBe(true);
      expect(targets.length).toBe(2);
    }
  });
});

describe('Phase 1 — Organization resource', () => {
  it('creates CfnOrganization with ALL features by default', () => {
    const t = synth();
    t.hasResourceProperties('AWS::Organizations::Organization', {
      FeatureSet: 'ALL',
    });
  });
});

describe('Phase 1 — SCP-01 model allow-list comes from the SSOT', () => {
  it('embeds Claude Sonnet 4.5 + Claude Haiku 4.5 foundation-model ARNs', () => {
    const t = synth();
    const policies = t.findResources('AWS::Organizations::Policy', {
      Properties: { Name: 'AgenticAI-SCP-01-ModelAllowlist' },
    });
    const entries = Object.values(policies);
    expect(entries).toHaveLength(1);
    const policyProps = (entries[0] as Record<string, unknown>).Properties as Record<string, unknown>;
    const content = policyProps.Content;
    const body = typeof content === 'string' ? JSON.parse(content) : (content as any);
    const allow = body.Statement[0].Condition['ForAllValues:StringNotEquals']['bedrock:FoundationModel'];
    expect(allow).toContain(
      'arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0',
    );
    expect(allow).toContain(
      'arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0',
    );
  });
});

// Silence Jest unused-import lint for Match; re-exported for future phases.
void Match;
