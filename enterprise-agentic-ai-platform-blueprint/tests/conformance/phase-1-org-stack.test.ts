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
import { spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import { App } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';

import {
  DEFAULT_ORGANIZATIONAL_UNIT_NAMES,
  type OrganizationalUnitKey,
} from '@agenticai/organizations';

import { OrgStack } from '../../apps/management-account/lib/org-stack';

const ADMIN_ROLE = 'arn:aws:iam::111111111111:role/AgenticAI-GuardrailAdmin';

function synth(opts: { attachToWorkloadsOu?: boolean; vpceIds?: boolean } = {}) {
  const app = new App();
  const withVpce = opts.vpceIds ?? true;
  const stack = new OrgStack(app, 'TestOrgStack', {
    env: { account: '222222222222', region: 'us-west-2' },
    platformGuardrailAdminRoleArn: ADMIN_ROLE,
    attachToWorkloadsOu: opts.attachToWorkloadsOu ?? false,
    approvedAgentCoreVpceIds: withVpce ? ['vpce-0a1b2c3d4e5f60001'] : undefined,
    approvedBedrockVpceIds: withVpce ? ['vpce-0a1b2c3d4e5f60002'] : undefined,
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
  it('emits exactly 8 Organizations::Policy resources when the VPC endpoint ids are supplied', () => {
    const t = synth();
    t.resourceCountIs('AWS::Organizations::Policy', 8);
  });

  it('omits the VPC-mode SCP-03/04/07 by default (public-endpoint workloads would be locked out)', () => {
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => {});
    try {
      const t = synth({ vpceIds: false });
      t.resourceCountIs('AWS::Organizations::Policy', 5);
      const names = Object.values(t.findResources('AWS::Organizations::Policy')).map(
        (r: Record<string, unknown>) => (r.Properties as Record<string, unknown>).Name as string,
      );
      expect(names).not.toContain('AgenticAI-SCP-03-EnforceAgentCoreVpce');
      expect(names).not.toContain('AgenticAI-SCP-04-EnforceBedrockVpce');
      expect(names).not.toContain('AgenticAI-SCP-07-DenyPublicAgentCore');
    } finally {
      warn.mockRestore();
    }
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

describe('Phase 1 — Adopting an existing Organization', () => {
  function synthImported() {
    const app = new App();
    const stack = new OrgStack(app, 'TestOrgStackImported', {
      env: { account: '222222222222', region: 'us-west-2' },
      platformGuardrailAdminRoleArn: ADMIN_ROLE,
      importExistingOrganization: true,
      existingRootId: 'r-abcd',
    });
    return Template.fromStack(stack);
  }

  it('does not emit AWS::Organizations::Organization when importing an existing one', () => {
    synthImported().resourceCountIs('AWS::Organizations::Organization', 0);
  });

  it('parents every OU under the supplied existing root id', () => {
    const t = synthImported();
    t.resourceCountIs('AWS::Organizations::OrganizationalUnit', 5);
    const ous = t.findResources('AWS::Organizations::OrganizationalUnit');
    for (const ou of Object.values(ous)) {
      expect(ou.Properties.ParentId).toBe('r-abcd');
    }
  });

  it('refuses to import without the existing root id', () => {
    const app = new App();
    expect(
      () =>
        new OrgStack(app, 'TestOrgStackNoRoot', {
          env: { account: '222222222222', region: 'us-west-2' },
          platformGuardrailAdminRoleArn: ADMIN_ROLE,
          importExistingOrganization: true,
        }),
    ).toThrow(/existingRootId is required/);
  });
});

describe('Phase 1 — OU name overrides for roots that already hold a same-named OU', () => {
  function synthNamed(names: Record<string, string>) {
    const app = new App();
    const stack = new OrgStack(app, 'TestOrgStackNamed', {
      env: { account: '222222222222', region: 'us-west-2' },
      platformGuardrailAdminRoleArn: ADMIN_ROLE,
      importExistingOrganization: true,
      existingRootId: 'r-abcd',
      organizationalUnitNames: names as Partial<Record<OrganizationalUnitKey, string>>,
    });
    return Template.fromStack(stack);
  }

  const ouNames = (t: Template) =>
    Object.values(t.findResources('AWS::Organizations::OrganizationalUnit'))
      .map((r: Record<string, unknown>) => (r.Properties as Record<string, unknown>).Name as string)
      .sort();

  it('renames only the overridden OU and keeps the other four defaults', () => {
    const t = synthNamed({ security: 'AgenticAI-Security' });
    t.resourceCountIs('AWS::Organizations::OrganizationalUnit', 5);
    expect(ouNames(t)).toEqual([
      'AgenticAI-Platform',
      'AgenticAI-Sandbox',
      'AgenticAI-Security',
      'AgenticAI-Workloads',
      'SharedServices',
    ]);
    expect(DEFAULT_ORGANIZATIONAL_UNIT_NAMES.security).toBe('Security');
  });

  it('keeps every SCP attached to the renamed Sandbox OU', () => {
    const t = synthNamed({ sandbox: 'AgenticAI-Soak' });
    const [sandboxLogicalId] = Object.keys(
      t.findResources('AWS::Organizations::OrganizationalUnit', { Properties: { Name: 'AgenticAI-Soak' } }),
    );
    expect(sandboxLogicalId).toBeDefined();
    const policies = Object.values(t.findResources('AWS::Organizations::Policy'));
    expect(policies.length).toBeGreaterThan(0);
    for (const policy of policies) {
      expect((policy.Properties as Record<string, unknown>).TargetIds).toEqual([
        { 'Fn::GetAtt': [sandboxLogicalId, 'Id'] },
      ]);
    }
  });

  it('rejects unknown keys, blank names and two OUs sharing a name', () => {
    expect(() => synthNamed({ securityOu: 'AgenticAI-Security' })).toThrow(/unknown OU key 'securityOu'/);
    expect(() => synthNamed({ security: '   ' })).toThrow(/non-empty string/);
    expect(() => synthNamed({ security: 'SharedServices' })).toThrow(/distinct names/);
  });
});

describe('Phase 1 — management stage entrypoint (bin/agentic-ai-platform.ts)', () => {
  const REPO = path.resolve(__dirname, '..', '..');
  const STACK = 'AgenticAI-Management-OrgStack';
  type Resource = { Type: string; Properties: Record<string, unknown> };

  // Runs the real app the way an operator does, so the context keys, their
  // validation and the OrgStack prop forwarding are exercised end to end.
  function synthEntrypoint(context: Record<string, string>) {
    const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'phase-1-management-'));
    const args = [
      'cdk',
      'synth',
      STACK,
      '--quiet',
      '--no-staging',
      '--no-notices',
      '--output',
      outDir,
      '--context',
      'stage=management',
      '--context',
      'agenticai/defaultRegion=us-west-2',
    ];
    for (const [key, value] of Object.entries(context)) {
      args.push('--context', `${key}=${value}`);
    }
    const result = spawnSync('npx', args, { cwd: REPO, encoding: 'utf8' });
    const templatePath = path.join(outDir, `${STACK}.template.json`);
    const template = fs.existsSync(templatePath)
      ? (JSON.parse(fs.readFileSync(templatePath, 'utf8')) as { Resources: Record<string, Resource> })
      : undefined;
    fs.rmSync(outDir, { recursive: true, force: true });
    return { status: result.status, stderr: result.stderr, template };
  }

  const ofType = (template: { Resources: Record<string, Resource> }, type: string) =>
    Object.values(template.Resources).filter((r) => r.Type === type);

  it('adopts an existing organization and applies OU name overrides from context', () => {
    const run = synthEntrypoint({
      'agenticai/importExistingOrganization': 'true',
      'agenticai/existingRootId': 'r-abcd',
      'agenticai/organizationalUnitNames': '{"security":"AgenticAI-Security"}',
    });
    expect(run.stderr).not.toMatch(/Error:/);
    expect(run.status).toBe(0);
    expect(run.template).toBeDefined();
    expect(ofType(run.template!, 'AWS::Organizations::Organization')).toHaveLength(0);
    const ous = ofType(run.template!, 'AWS::Organizations::OrganizationalUnit');
    expect(ous).toHaveLength(5);
    expect(ous.every((ou) => ou.Properties.ParentId === 'r-abcd')).toBe(true);
    expect(ous.map((ou) => ou.Properties.Name).sort()).toEqual([
      'AgenticAI-Platform',
      'AgenticAI-Sandbox',
      'AgenticAI-Security',
      'AgenticAI-Workloads',
      'SharedServices',
    ]);
  }, 180_000);

  it('creates the Organization by default and refuses to import without a root id', () => {
    const created = synthEntrypoint({});
    expect(created.status).toBe(0);
    expect(created.template).toBeDefined();
    expect(ofType(created.template!, 'AWS::Organizations::Organization')).toHaveLength(1);
    const ous = ofType(created.template!, 'AWS::Organizations::OrganizationalUnit');
    expect(ous.map((ou) => ou.Properties.Name).sort()).toEqual(
      Object.values(DEFAULT_ORGANIZATIONAL_UNIT_NAMES).sort(),
    );

    const refused = synthEntrypoint({ 'agenticai/importExistingOrganization': 'true' });
    expect(refused.status).not.toBe(0);
    expect(refused.stderr).toContain("requires context 'agenticai/existingRootId'");
    expect(refused.template).toBeUndefined();
  }, 240_000);
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
    // Allow-list lives on the resource (the old bedrock:FoundationModel condition key does not exist).
    const allow = body.Statement[0].NotResource;
    expect(JSON.stringify(body)).not.toContain('bedrock:FoundationModel');
    expect(allow).toContain(
      'arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0',
    );
    expect(allow).toContain(
      'arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0',
    );
  });
});

describe('Phase 1 — SCP-13 CloudWatch Omni telemetry lockdown (ADR-15)', () => {
  function synthWithOmni() {
    const app = new App();
    const stack = new OrgStack(app, 'TestOrgStackOmni', {
      env: { account: '222222222222', region: 'us-west-2' },
      platformGuardrailAdminRoleArn: ADMIN_ROLE,
      approvedAgentCoreVpceIds: ['vpce-0a1b2c3d4e5f60001'],
      approvedBedrockVpceIds: ['vpce-0a1b2c3d4e5f60002'],
      enableOmniTelemetryLockdown: true,
      omniBreakGlassRoleNames: ['AgenticAI-ObservabilityAdmin'],
    });
    return Template.fromStack(stack);
  }

  it('is absent by default and adds exactly one policy when enabled', () => {
    const names = (t: Template) =>
      Object.values(t.findResources('AWS::Organizations::Policy')).map(
        (r: Record<string, unknown>) => (r.Properties as Record<string, unknown>).Name as string,
      );
    expect(names(synth())).not.toContain('AgenticAI-SCP-13-OmniTelemetryLockdown');
    const t = synthWithOmni();
    t.resourceCountIs('AWS::Organizations::Policy', 9);
    expect(names(t)).toContain('AgenticAI-SCP-13-OmniTelemetryLockdown');
  });

  it('attaches SCP-13 to the same OU targets as the rest of the set and carries the break-glass exemption', () => {
    const t = synthWithOmni();
    t.hasResourceProperties('AWS::Organizations::Policy', {
      Name: 'AgenticAI-SCP-13-OmniTelemetryLockdown',
      Type: 'SERVICE_CONTROL_POLICY',
      Content: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'DenyTransactionSearchTamperingExceptPipeline',
            Action: Match.arrayWith(['xray:UpdateTraceSegmentDestination']),
            Condition: Match.objectLike({
              ArnNotLike: {
                'aws:PrincipalArn': Match.arrayWith(['arn:aws:iam::*:role/AgenticAI-ObservabilityAdmin']),
              },
            }),
          }),
        ]),
      }),
    });
  });
});
