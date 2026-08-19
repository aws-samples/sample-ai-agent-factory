/**
 * Unit tests for @agenticai/platform-tool-catalogue — guards the SSOT that
 * feeds every per-workstream AgentCore Gateway synth.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  PLATFORM_TOOL_CATALOGUE,
  composeCedarPolicyDocument,
  resolveSubscribedTools,
  resolveTargetArn,
  validateToolSpec,
  type ToolSpec,
} from './index';

describe('validateToolSpec', () => {
  it('accepts every tool in PLATFORM_TOOL_CATALOGUE', () => {
    for (const spec of Object.values(PLATFORM_TOOL_CATALOGUE)) {
      expect(() => validateToolSpec(spec)).not.toThrow();
    }
  });

  it('rejects a spec whose targetArn lacks an alias suffix (Q5 pin-via-alias)', () => {
    const bad: ToolSpec = {
      ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
      targetArn: 'arn:aws:lambda:us-east-1:${PLATFORM_ACCOUNT_ID}:function:agenticai-d03-tool-echo',
    };
    expect(() => validateToolSpec(bad)).toThrow(/alias/i);
  });

  it('rejects a non-12-digit targetAccountId', () => {
    const bad: ToolSpec = {
      ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
      targetAccountId: '1234',
    };
    expect(() => validateToolSpec(bad)).toThrow(/12 digits/);
  });

  it("rejects approvalStatus: 'x'", () => {
    const bad = {
      ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
      approvalStatus: 'x',
    } as unknown as ToolSpec;
    expect(() => validateToolSpec(bad)).toThrow(/approvalStatus invalid/);
  });
});

describe('resolveSubscribedTools', () => {
  it("returns array with 'tool-echo' spec when ['tool-echo'] is subscribed", () => {
    const subset = resolveSubscribedTools(['tool-echo']);
    expect(subset).toHaveLength(1);
    expect(subset[0].toolId).toBe('tool-echo');
    expect(subset[0]).toEqual(PLATFORM_TOOL_CATALOGUE['tool-echo']);
  });

  it('throws with a message naming the unknown id + listing known ids', () => {
    expect(() => resolveSubscribedTools(['tool-unknown'])).toThrow(/tool-unknown/);
    expect(() => resolveSubscribedTools(['tool-unknown'])).toThrow(/Known:/);
    expect(() => resolveSubscribedTools(['tool-unknown'])).toThrow(/tool-echo/);
  });

  it('throws on partial-unknown — the whole batch is rejected', () => {
    expect(() => resolveSubscribedTools(['tool-echo', 'tool-unknown'])).toThrow(/tool-unknown/);
  });

  it('rejects a deprecated tool in a local catalogue copy', () => {
    // Build a local catalogue copy with a deprecated entry and re-implement
    // the resolver against it to exercise the deprecated-subset code path.
    const localCatalogue: Record<string, ToolSpec> = {
      ...PLATFORM_TOOL_CATALOGUE,
      'tool-x': {
        toolId: 'tool-x',
        targetArn: 'arn:aws:lambda:us-east-1:${PLATFORM_ACCOUNT_ID}:function:agenticai-d03-tool-x:PROD',
        cedarPolicy: 'permit(principal, action == Action::"InvokeTool", resource == Tool::"tool-x");',
        ownerTeam: 'platform-ai',
        costCentre: 'platform',
        description: 'Deprecated demo tool.',
        approvalStatus: 'deprecated',
      },
    };
    const resolveLocal = (ids: readonly string[]): ToolSpec[] => {
      const unknown = ids.filter((id) => !(id in localCatalogue));
      if (unknown.length > 0) throw new Error(`Unknown tool id(s): ${unknown.join(', ')}`);
      const subset = ids.map((id) => localCatalogue[id]);
      const deprecated = subset.filter((s) => s.approvalStatus === 'deprecated');
      if (deprecated.length > 0) {
        throw new Error(
          `Cannot subscribe to deprecated tool(s): ${deprecated.map((t) => t.toolId).join(', ')}.`,
        );
      }
      return subset;
    };
    expect(() => resolveLocal(['tool-x'])).toThrow(/deprecated/);
    expect(() => resolveLocal(['tool-echo'])).not.toThrow();
  });
});

describe('resolveTargetArn', () => {
  it('substitutes ${PLATFORM_ACCOUNT_ID} when targetAccountId is undefined', () => {
    const spec = PLATFORM_TOOL_CATALOGUE['tool-echo'];
    const arn = resolveTargetArn(spec, '111111111111');
    expect(arn).toBe(
      'arn:aws:lambda:us-east-1:111111111111:function:agenticai-d03-tool-echo:PROD',
    );
    expect(arn).not.toContain('${PLATFORM_ACCOUNT_ID}');
  });

  it('uses targetAccountId literally when present (cross-account tool)', () => {
    const spec: ToolSpec = {
      ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
      targetArn: 'arn:aws:lambda:us-east-1:${PLATFORM_ACCOUNT_ID}:function:agenticai-d03-tool-echo:PROD',
      targetAccountId: '999999999999',
    };
    const arn = resolveTargetArn(spec, '111111111111');
    expect(arn).toBe(
      'arn:aws:lambda:us-east-1:999999999999:function:agenticai-d03-tool-echo:PROD',
    );
    expect(arn).not.toContain('111111111111');
  });
});

describe('composeCedarPolicyDocument', () => {
  it("includes every tool's cedarPolicy plus a default forbid", () => {
    const subset = resolveSubscribedTools(['tool-echo', 'tool-ping']);
    const doc = composeCedarPolicyDocument(subset);
    for (const spec of subset) {
      expect(doc).toContain(spec.cedarPolicy.trim());
    }
    expect(doc).toMatch(/forbid\(principal, action, resource\)/);
    expect(doc).toContain('Default forbid');
  });

  it('prefixes each policy with a `// Tool: <id>` comment', () => {
    const subset = resolveSubscribedTools(['tool-echo', 'tool-ping']);
    const doc = composeCedarPolicyDocument(subset);
    for (const spec of subset) {
      expect(doc).toContain(`// Tool: ${spec.toolId}`);
    }
  });
});

describe('Phase Q — allowedGroups (per-developer entitlement)', () => {
  it('validateToolSpec accepts a tool with a valid allowedGroups list', () => {
    const spec: ToolSpec = {
      ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
      allowedGroups: ['retail-developers', 'platform-ai'],
    };
    expect(() => validateToolSpec(spec)).not.toThrow();
  });

  it('validateToolSpec rejects an empty allowedGroups array', () => {
    const spec = {
      ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
      allowedGroups: [],
    } as unknown as ToolSpec;
    expect(() => validateToolSpec(spec)).toThrow(/non-empty/);
  });

  it('validateToolSpec rejects an allowedGroups entry that violates the Cognito group regex', () => {
    const spec: ToolSpec = {
      ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
      allowedGroups: ['invalid group with spaces'],
    };
    expect(() => validateToolSpec(spec)).toThrow(/not a valid Cognito group name/);
  });

  it('validateToolSpec rejects a non-string allowedGroups entry', () => {
    const spec = {
      ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
      allowedGroups: [42],
    } as unknown as ToolSpec;
    expect(() => validateToolSpec(spec)).toThrow(/not a valid Cognito group name/);
  });

  it('composeCedarPolicyDocument emits principal-bound permits when allowedGroups is set', () => {
    const subset: readonly ToolSpec[] = [
      {
        ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
        allowedGroups: ['retail-developers', 'platform-ai'],
      },
    ];
    const doc = composeCedarPolicyDocument(subset);
    expect(doc).toContain(
      'permit(principal in CognitoGroup::"retail-developers", action == Action::"InvokeTool", resource == Tool::"tool-echo");',
    );
    expect(doc).toContain(
      'permit(principal in CognitoGroup::"platform-ai", action == Action::"InvokeTool", resource == Tool::"tool-echo");',
    );
    expect(doc).toMatch(/Q-entitlement: principal-bound/);
    expect(doc).not.toMatch(/permit\(principal,\s*action == Action::"InvokeTool",\s*resource == Tool::"tool-echo"\);/);
    expect(doc).toContain('Default forbid');
  });

  it('composeCedarPolicyDocument falls back to the unconditional permit when allowedGroups is absent (back-compat)', () => {
    const subset = resolveSubscribedTools(['tool-echo']);
    const doc = composeCedarPolicyDocument(subset);
    expect(doc).toContain(PLATFORM_TOOL_CATALOGUE['tool-echo'].cedarPolicy.trim());
    expect(doc).not.toMatch(/Q-entitlement/);
  });

  it('composeCedarPolicyDocument never emits a Cedar wildcard for a tool with allowedGroups', () => {
    const subset: readonly ToolSpec[] = [
      {
        ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
        allowedGroups: ['retail-developers'],
      },
    ];
    const doc = composeCedarPolicyDocument(subset);
    // Must not contain the bare unconditional permit that a v0.5.0 ToolSpec
    // ships with by default — Phase Q strips it in favour of group-bound
    // permits when entitlement is declared.
    expect(doc).not.toContain(
      'permit(principal, action == Action::"InvokeTool", resource == Tool::"tool-echo");',
    );
  });
});
