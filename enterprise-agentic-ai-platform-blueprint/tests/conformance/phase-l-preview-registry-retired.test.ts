/**
 * Phase L — the preview AgentCore Registry path stays retired.
 *
 * AWS moved Agent Registry to the GA `agent-registry-control` / `agent-registry`
 * namespaces on 2026-08-06 and ended preview Registry support on 2026-09-17.
 * The preview constructs, schema and IAM grants were removed from this
 * blueprint on 2026-10-06. This file is the regression guard that keeps them
 * out, and it replaces the former `phase-l-d03-platform-registry.test.ts`,
 * which pinned the preview path *alive*.
 *
 * The hard part of a guard like this is precision. `bedrock-agentcore` is still
 * the correct namespace for AgentCore Runtime, Gateway, Memory, Identity and
 * Policy, so a blanket ban on the string would be wrong and would fail on
 * perfectly good code. Only REGISTRY operations moved. Every pattern below is
 * therefore Registry-specific, and the final test asserts that legitimate
 * non-Registry AgentCore usage is still present, so nobody can satisfy this
 * file by deleting Gateway or Runtime calls.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { join, relative, sep } from 'node:path';

import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { D03PlatformCoreStack } from '../../apps/platform-account/lib/d03-platform-core-stack';

const ROOT = join(__dirname, '..', '..');

/**
 * Files permitted to mention the preview Registry surface, each for a stated
 * reason. The allowlist is itself checked below: if an entry stops containing
 * what it was excused for, the test fails rather than letting the exemption rot.
 */
const ALLOWLIST: ReadonlyArray<{ readonly path: string; readonly why: string }> = [
  {
    path: 'tests/conformance/phase-l-preview-registry-retired.test.ts',
    why: 'this guard, which must name the patterns it forbids',
  },
  {
    path: 'packages/organizations/src/scps/scp-11-registry-mutation-lockdown.ts',
    why: 'SCP-11 deliberately denies BOTH namespaces; the GA registry signs as `agent-registry` while the preview signed as `bedrock-agentcore`, so dropping either half would narrow the org-level deny',
  },
  {
    path: 'packages/organizations/src/scps/scps.test.ts',
    why: 'asserts SCP-11 keeps its dual-namespace coverage',
  },
  {
    path: 'tests/conformance/phase-22-ga-agent-registry.test.ts',
    why: 'asserts the GA stack emits none of the preview custom resources',
  },
  {
    path: 'scripts/live-agent-registry-spike/test_agent_registry_spike.py',
    why: 'asserts the live GA spike emits none of the preview custom resources',
  },
  {
    path: 'docs/AGENT_REGISTRY_GA_MIGRATION.md',
    why: 'the migration guide, which must name the preview surface it is migrating away from',
  },
];

/** Registry-specific preview patterns. Deliberately NOT a ban on `bedrock-agentcore`. */
const FORBIDDEN: ReadonlyArray<{ readonly label: string; readonly re: RegExp }> = [
  {
    label: 'preview Registry IAM action (bedrock-agentcore:<verb>Registry…)',
    re: /bedrock-agentcore:[A-Za-z]*Registry[A-Za-z]*/,
  },
  {
    label: 'preview Registry ARN shape (arn:aws:bedrock-agentcore:…:registry/…)',
    re: /arn:aws:bedrock-agentcore:[^'"`\s]*:registry\//,
  },
  {
    label: 'preview Registry custom-resource type (Custom::BedrockAgentCoreRegistry…)',
    re: /Custom::BedrockAgentCoreRegistry/,
  },
  {
    label: 'preview record schema field (descriptorType)',
    re: /\bdescriptorType\b/,
  },
  {
    label: 'preview Registry construct (PlatformRegistryConstruct / RegistryRecordConstruct)',
    // Word-boundary-ish on the left so GaPlatformRegistryConstruct does not match.
    re: /(?<![A-Za-z])(PlatformRegistryConstruct|RegistryRecordConstruct|grantRegistryConsumer)(?![A-Za-z])/,
  },
];

/** Every git-tracked source file under the blueprint, as repo-relative paths. */
function trackedSourceFiles(): readonly string[] {
  const out = execFileSync(
    'git',
    [
      'ls-files',
      '-z',
      // `--others --exclude-standard` adds untracked-but-not-ignored files, so a
      // reintroduction fails this guard before it is ever committed. `.gitignore`
      // still keeps node_modules and dist out.
      '--cached',
      '--others',
      '--exclude-standard',
      '--',
      '*.ts',
      '*.py',
      '*.sh',
      '*.json',
      '*.yaml',
      '*.yml',
      '*.md',
    ],
    { cwd: ROOT, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 },
  );
  return out
    .split('\0')
    .filter((f) => f.length > 0)
    .filter((f) => !f.split(sep).includes('node_modules'));
}

const allowed = new Set(ALLOWLIST.map((e) => e.path));

describe('Phase L — preview AgentCore Registry stays retired', () => {
  const files = trackedSourceFiles();

  it('finds source files to scan (guards against a broken glob silently passing)', () => {
    expect(files.length).toBeGreaterThan(200);
    expect(files).toContain('apps/platform-account/lib/d03-platform-core-stack.ts');
  });

  for (const { label, re } of FORBIDDEN) {
    it(`no file reintroduces: ${label}`, () => {
      const offenders: string[] = [];
      for (const f of files) {
        if (allowed.has(f)) continue;
        const text = readFileSync(join(ROOT, f), 'utf8');
        const m = re.exec(text);
        if (m) {
          const line = text.slice(0, m.index).split('\n').length;
          offenders.push(`${f}:${line} — ${m[0]}`);
        }
      }
      expect(offenders).toEqual([]);
    });
  }

  it('the four deleted preview modules are absent from the package', () => {
    const gone = [
      'packages/agent-registry/src/platform-registry-construct.ts',
      'packages/agent-registry/src/registry-record-construct.ts',
      'packages/agent-registry/src/registry-consumer-grant.ts',
      'packages/agent-registry/src/registry-record-spec.ts',
    ];
    expect(files.filter((f) => gone.includes(f))).toEqual([]);
  });

  it('D03PlatformCoreStack synthesizes no preview Registry resources at all', () => {
    const app = new App();
    const stack = new D03PlatformCoreStack(app, 'TestD03', {
      env: { account: '111111111111', region: 'us-west-2' },
      workloadAccountIds: ['222222222222'],
      externalId: 'test-external-id',
    });
    const json = Template.fromStack(stack).toJSON() as {
      Resources: Record<string, { Type: string }>;
      Outputs?: Record<string, unknown>;
    };
    const previewTypes = Object.values(json.Resources)
      .map((r) => r.Type)
      .filter((t) => t.startsWith('Custom::BedrockAgentCoreRegistry'));
    expect(previewTypes).toEqual([]);
    // The two exports the preview path used to publish are gone. Neither ever
    // had an Fn::ImportValue consumer, so removing them breaks no stack.
    expect(Object.keys(json.Outputs ?? {})).not.toContain('AgentRegistryId');
    expect(Object.keys(json.Outputs ?? {})).not.toContain('AgentRegistryArn');
  });

  it('every allowlist entry still earns its exemption', () => {
    for (const { path, why } of ALLOWLIST) {
      expect(files).toContain(path);
      const text = readFileSync(join(ROOT, path), 'utf8');
      const hits = FORBIDDEN.filter(({ re }) => re.test(text));
      // If a file no longer mentions the preview surface, it should be removed
      // from the allowlist rather than left as a standing exemption.
      expect(hits.length).toBeGreaterThan(0);
      expect(typeof why).toBe('string');
    }
  });

  it('legitimate non-Registry AgentCore usage is untouched', () => {
    // The guard above must not be satisfiable by deleting Gateway, Runtime,
    // Memory or Identity calls, which correctly remain on `bedrock-agentcore`.
    const gatewayStack = readFileSync(
      join(ROOT, 'apps/platform-account/lib/d03-workstream-gateway-stack.ts'),
      'utf8',
    );
    expect(gatewayStack).toContain('bedrock-agentcore');
    // …and the Registry calls in that same file are on the GA namespace.
    expect(gatewayStack).toContain('agent-registry-control.');
    expect(gatewayStack).toContain("const service = 'agent-registry';");
  });

  it('the GA Registry path is the one that remains', () => {
    const index = readFileSync(join(ROOT, 'packages/agent-registry/src/index.ts'), 'utf8');
    expect(index).toContain('GaPlatformRegistryConstruct');
    expect(index).not.toContain('./registry-record-spec');
    const ga = readFileSync(
      join(ROOT, 'packages/agent-registry/src/ga-platform-registry-construct.ts'),
      'utf8',
    );
    expect(ga).toContain('AWS::AgentRegistry::Registry');
    expect(ga).toContain('AWS::AgentRegistry::RegistryRecord');
  });

  it('developer and read-only permission sets grant only the GA Registry surface', () => {
    const rel = relative(
      ROOT,
      join(ROOT, 'packages/developer-access/src/workstream-permission-sets.ts'),
    );
    const text = readFileSync(join(ROOT, rel), 'utf8');
    expect(text).toContain("'agent-registry:GetRegistry'");
    expect(text).toContain("'agent-registry:SearchDiscoverableRegistryRecords'");
    expect(text).toContain('arn:aws:agent-registry:');
    // Both namespaces must stay denied: the GA service is not covered by a
    // `bedrock-agentcore:*` wildcard.
    expect(text).toContain("'agent-registry:Delete*'");
    expect(text).toContain("'bedrock-agentcore:Delete*'");
  });
});
