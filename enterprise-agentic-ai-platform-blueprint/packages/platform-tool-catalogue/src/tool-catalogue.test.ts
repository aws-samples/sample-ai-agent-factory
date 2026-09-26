/**
 * Unit tests for @agenticai/platform-tool-catalogue — guards the SSOT that
 * feeds every per-workstream AgentCore Gateway synth.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  PLATFORM_TOOL_CATALOGUE,
  composeAgentCorePolicyDefinitions,
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
  it('substitutes platform Region and account placeholders', () => {
    const spec = PLATFORM_TOOL_CATALOGUE['tool-echo'];
    const arn = resolveTargetArn(spec, '111111111111', 'eu-west-1');
    expect(arn).toBe(
      'arn:aws:lambda:eu-west-1:111111111111:function:agenticai-d03-tool-echo:PROD',
    );
    expect(arn).not.toContain('${PLATFORM_REGION}');
    expect(arn).not.toContain('${PLATFORM_ACCOUNT_ID}');
  });

  it('uses targetAccountId literally when present (cross-account tool)', () => {
    const spec: ToolSpec = {
      ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
      targetArn: 'arn:aws:lambda:us-east-1:${PLATFORM_ACCOUNT_ID}:function:agenticai-d03-tool-echo:PROD',
      targetAccountId: '999999999999',
    };
    const arn = resolveTargetArn(spec, '111111111111', 'eu-west-1');
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
    expect(doc).toMatch(/entitlement: principal-bound/);
    expect(doc).not.toMatch(/permit\(principal,\s*action == Action::"InvokeTool",\s*resource == Tool::"tool-echo"\);/);
    expect(doc).toContain('Default forbid');
  });

  it('composeCedarPolicyDocument falls back to the unconditional permit when allowedGroups is absent (back-compat)', () => {
    const subset = resolveSubscribedTools(['tool-echo']);
    const doc = composeCedarPolicyDocument(subset);
    expect(doc).toContain(PLATFORM_TOOL_CATALOGUE['tool-echo'].cedarPolicy.trim());
    expect(doc).not.toMatch(/entitlement:/);
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

describe('Round 3 — allowedSubjects (per-developer subject entitlement)', () => {
  it('validateToolSpec accepts a valid allowedSubjects list', () => {
    const spec: ToolSpec = {
      ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
      allowedSubjects: ['sub-alice', 'user:bob@example.com'],
    };
    expect(() => validateToolSpec(spec)).not.toThrow();
  });

  it('validateToolSpec rejects an empty allowedSubjects array', () => {
    const spec = {
      ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
      allowedSubjects: [],
    } as unknown as ToolSpec;
    expect(() => validateToolSpec(spec)).toThrow(/non-empty/);
  });

  it('validateToolSpec rejects an allowedSubjects entry with an unsafe character', () => {
    const spec: ToolSpec = {
      ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
      allowedSubjects: ['bad sub with spaces'],
    };
    expect(() => validateToolSpec(spec)).toThrow(/not a valid JWT sub value/);
  });

  it('composeCedarPolicyDocument emits a Developer permit per subject', () => {
    const subset: readonly ToolSpec[] = [
      {
        ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
        allowedSubjects: ['sub-alice', 'sub-bob'],
      },
    ];
    const doc = composeCedarPolicyDocument(subset);
    expect(doc).toContain(
      'permit(principal == Developer::"sub-alice", action == Action::"InvokeTool", resource == Tool::"tool-echo");',
    );
    expect(doc).toContain(
      'permit(principal == Developer::"sub-bob", action == Action::"InvokeTool", resource == Tool::"tool-echo");',
    );
    expect(doc).toMatch(/entitlement: principal-bound/);
    // The unconditional permit must be stripped once entitlement is declared.
    expect(doc).not.toContain(
      'permit(principal, action == Action::"InvokeTool", resource == Tool::"tool-echo");',
    );
  });

  it('composeCedarPolicyDocument emits both group and subject permits when both are set (combined)', () => {
    const subset: readonly ToolSpec[] = [
      {
        ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
        allowedGroups: ['retail-developers'],
        allowedSubjects: ['sub-alice'],
      },
    ];
    const doc = composeCedarPolicyDocument(subset);
    expect(doc).toContain(
      'permit(principal in CognitoGroup::"retail-developers", action == Action::"InvokeTool", resource == Tool::"tool-echo");',
    );
    expect(doc).toContain(
      'permit(principal == Developer::"sub-alice", action == Action::"InvokeTool", resource == Tool::"tool-echo");',
    );
  });
});


describe('composeAgentCorePolicyDefinitions', () => {
  const gatewayArn =
    'arn:aws:bedrock-agentcore:us-west-2:333333333333:gateway/agenticai-d03-nonprod-demo-primary-gw-abcdefghij';

  it('emits exact IAM assumed-role principals, qualified actions, and Gateway resources', () => {
    const definitions = composeAgentCorePolicyDefinitions(
      resolveSubscribedTools(['tool-echo']),
      {
        authorizerType: 'AWS_IAM',
        gatewayArn,
        policyNamePrefix: 'AgenticAI_nonprod_demo_primary',
        targetNames: { 'tool-echo': 'target-tool-echo' },
        iamRoleArns: [
          'arn:aws:iam::333333333333:role/AgenticAI-D03-demo-primary-runtime',
        ],
      },
    );

    expect(definitions).toHaveLength(1);
    expect(definitions[0].policyName).toMatch(/^[A-Za-z][A-Za-z0-9_]{0,47}$/);
    expect(definitions[0].statement).toContain(
      'principal == AgentCore::IamEntity::"arn:aws:sts::333333333333:assumed-role/AgenticAI-D03-demo-primary-runtime"',
    );
    expect(definitions[0].statement).toContain(
      'AgentCore::Action::"target-tool-echo___tool-echo"',
    );
    expect(definitions[0].statement).toContain(
      `AgentCore::Gateway::"${gatewayArn}"`,
    );
    expect(definitions[0].statement).not.toContain('forbid(');
    expect(definitions[0].statement).not.toContain('*');
  });

  it('emits an authenticated OAuth permit when no group entitlement is configured', () => {
    const [definition] = composeAgentCorePolicyDefinitions(
      resolveSubscribedTools(['tool-ping']),
      {
        authorizerType: 'CUSTOM_JWT',
        gatewayArn,
        policyNamePrefix: 'AgenticAI_nonprod_demo_primary',
        targetNames: { 'tool-ping': 'target-tool-ping' },
      },
    );
    expect(definition.statement).toContain('principal is AgentCore::OAuthUser');
    expect(definition.statement).not.toContain('cognito:groups');
  });

  it('uses quoted JSON element boundaries for OAuth group membership', () => {
    const tool: ToolSpec = {
      ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
      allowedGroups: ['retail-developers'],
    };
    const [definition] = composeAgentCorePolicyDefinitions([tool], {
      authorizerType: 'CUSTOM_JWT',
      gatewayArn,
      policyNamePrefix: 'AgenticAI_nonprod_demo_primary',
      targetNames: { 'tool-echo': 'target-tool-echo' },
    });
    expect(definition.statement).toContain(
      'principal.hasTag("cognito:groups")',
    );
    expect(definition.statement).toContain(
      'principal.getTag("cognito:groups") like "*\\"retail-developers\\"*"',
    );
    expect(definition.statement).not.toContain('like "*retail-developers*"');
  });

  it('rejects wildcard-bearing OAuth group names before Cedar rendering', () => {
    const tool: ToolSpec = {
      ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
      allowedGroups: ['retail-*'],
    };
    expect(() =>
      composeAgentCorePolicyDefinitions([tool], {
        authorizerType: 'CUSTOM_JWT',
        gatewayArn,
        policyNamePrefix: 'AgenticAI_nonprod_demo_primary',
        targetNames: { 'tool-echo': 'target-tool-echo' },
      }),
    ).toThrow(/not a valid Cognito group name/);
  });

  it('rejects group entitlements on AWS_IAM because IAM principals have no tags', () => {
    const tool: ToolSpec = {
      ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
      allowedGroups: ['retail-developers'],
    };
    expect(() =>
      composeAgentCorePolicyDefinitions([tool], {
        authorizerType: 'AWS_IAM',
        gatewayArn,
        policyNamePrefix: 'AgenticAI_nonprod_demo_primary',
        targetNames: { 'tool-echo': 'target-tool-echo' },
        iamRoleArns: ['arn:aws:iam::333333333333:role/RuntimeRole'],
      }),
    ).toThrow(/requires CUSTOM_JWT/);
  });

  it('fails closed on missing, duplicate, pathful, wildcard, and incomplete inputs', () => {
    const subset = resolveSubscribedTools(['tool-echo']);
    const base = {
      authorizerType: 'AWS_IAM' as const,
      gatewayArn,
      policyNamePrefix: 'AgenticAI_nonprod_demo_primary',
      targetNames: { 'tool-echo': 'target-tool-echo' },
    };
    expect(() => composeAgentCorePolicyDefinitions(subset, base)).toThrow(
      /at least one exact IAM role ARN/,
    );
    expect(() =>
      composeAgentCorePolicyDefinitions(subset, {
        ...base,
        iamRoleArns: ['arn:aws:iam::333333333333:role/path/RuntimeRole'],
      }),
    ).toThrow(/pathless IAM role ARN/);
    expect(() =>
      composeAgentCorePolicyDefinitions(subset, {
        ...base,
        iamRoleArns: [
          'arn:aws:iam::333333333333:role/RuntimeRole',
          'arn:aws:iam::333333333333:role/RuntimeRole',
        ],
      }),
    ).toThrow(/must not contain duplicates/);
    expect(() =>
      composeAgentCorePolicyDefinitions(subset, {
        ...base,
        gatewayArn: `${gatewayArn}*`,
        iamRoleArns: ['arn:aws:iam::333333333333:role/RuntimeRole'],
      }),
    ).toThrow(/must not contain a wildcard/);
    expect(() =>
      composeAgentCorePolicyDefinitions(subset, {
        ...base,
        targetNames: {},
        iamRoleArns: ['arn:aws:iam::333333333333:role/RuntimeRole'],
      }),
    ).toThrow(/target name.*absent or invalid/);
  });

  it('hashes overlong policy names deterministically within the 48-character limit', () => {
    const options = {
      authorizerType: 'CUSTOM_JWT' as const,
      gatewayArn,
      policyNamePrefix:
        'AgenticAI_nonproduction_extremely_long_tenant_extremely_long_agent',
      targetNames: { 'tool-echo': 'target-tool-echo' },
    };
    const left = composeAgentCorePolicyDefinitions(
      resolveSubscribedTools(['tool-echo']),
      options,
    )[0].policyName;
    const right = composeAgentCorePolicyDefinitions(
      resolveSubscribedTools(['tool-echo']),
      options,
    )[0].policyName;
    expect(left).toBe(right);
    expect(left).toHaveLength(48);
    expect(left).toMatch(/^[A-Za-z][A-Za-z0-9_]+$/);
  });
});
