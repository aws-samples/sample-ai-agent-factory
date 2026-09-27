/**
 * Unit tests for @agenticai/tool-cedar-wrapper — Phase Q (v0.6.0).
 *
 * Covers:
 *   - evaluateCedar against the catalogue's composed Cedar grammar
 *   - extractPrincipalGroupsFromEvent across the three documented event shapes
 *   - withCedarEnforcement allow/deny semantics + fail-closed behaviour
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  composeCedarPolicyDocument,
  PLATFORM_TOOL_CATALOGUE,
  type ToolSpec,
} from '@agenticai/platform-tool-catalogue';

import {
  CedarDeniedError,
  evaluateCedar,
  extractPrincipalGroupsFromEvent,
  extractPrincipalSubjectFromEvent,
  withCedarEnforcement,
} from './index';

/** Build a subject-bound permit document (grammar the wrapper accepts). */
function subjectPermitDoc(toolId: string, subs: string[]): string {
  return subs
    .map(
      (s) =>
        `permit(principal == Developer::"${s}", action == Action::"InvokeTool", resource == Tool::"${toolId}");`,
    )
    .join('\n');
}

describe('evaluateCedar — unconditional permit (v0.5.0 back-compat)', () => {
  const subset = [PLATFORM_TOOL_CATALOGUE['tool-echo']];
  const doc = composeCedarPolicyDocument(subset);

  it('allows any principal when the catalogue tool has no allowedGroups', () => {
    const decision = evaluateCedar({
      toolId: 'tool-echo',
      cedarPolicyDocument: doc,
      principalGroups: [],
    });
    expect(decision.decision).toBe('allow');
  });

  it('denies a different toolId even when an unconditional permit exists', () => {
    const decision = evaluateCedar({
      toolId: 'tool-other',
      cedarPolicyDocument: doc,
      principalGroups: ['retail-developers'],
    });
    expect(decision.decision).toBe('deny');
  });
});

describe('evaluateCedar — group-bound permit (Phase Q entitlement)', () => {
  const entitledSpec: ToolSpec = {
    ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
    allowedGroups: ['retail-developers', 'platform-ai'],
  };
  const doc = composeCedarPolicyDocument([entitledSpec]);

  it('allows when principal groups intersect the allow-list', () => {
    const decision = evaluateCedar({
      toolId: 'tool-echo',
      cedarPolicyDocument: doc,
      principalGroups: ['retail-developers'],
    });
    expect(decision.decision).toBe('allow');
    if (decision.decision === 'allow') {
      expect(decision.reason).toContain('retail-developers');
    }
  });

  it('denies when principal groups do not intersect the allow-list', () => {
    const decision = evaluateCedar({
      toolId: 'tool-echo',
      cedarPolicyDocument: doc,
      principalGroups: ['hr-developers'],
    });
    expect(decision.decision).toBe('deny');
  });

  it('denies when principal has no groups at all', () => {
    const decision = evaluateCedar({
      toolId: 'tool-echo',
      cedarPolicyDocument: doc,
      principalGroups: [],
    });
    expect(decision.decision).toBe('deny');
  });

  it('fails closed when cedarPolicyDocument is missing', () => {
    const decision = evaluateCedar({
      toolId: 'tool-echo',
      cedarPolicyDocument: '',
      principalGroups: ['retail-developers'],
    });
    expect(decision.decision).toBe('deny');
  });

  it('fails closed when toolId is empty', () => {
    const decision = evaluateCedar({
      toolId: '',
      cedarPolicyDocument: doc,
      principalGroups: ['retail-developers'],
    });
    expect(decision.decision).toBe('deny');
  });
});

describe('evaluateCedar — multi-tool composed bundle isolation', () => {
  const docMixed = composeCedarPolicyDocument([
    {
      ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
      allowedGroups: ['retail-developers'],
    },
    {
      ...PLATFORM_TOOL_CATALOGUE['tool-ping'],
      allowedGroups: ['platform-ai'],
    },
  ]);

  it('routes each toolId to its own permit set — echo allows retail-developers only', () => {
    expect(
      evaluateCedar({
        toolId: 'tool-echo',
        cedarPolicyDocument: docMixed,
        principalGroups: ['retail-developers'],
      }).decision,
    ).toBe('allow');
    expect(
      evaluateCedar({
        toolId: 'tool-echo',
        cedarPolicyDocument: docMixed,
        principalGroups: ['platform-ai'],
      }).decision,
    ).toBe('deny');
  });

  it('routes each toolId to its own permit set — ping allows platform-ai only', () => {
    expect(
      evaluateCedar({
        toolId: 'tool-ping',
        cedarPolicyDocument: docMixed,
        principalGroups: ['platform-ai'],
      }).decision,
    ).toBe('allow');
    expect(
      evaluateCedar({
        toolId: 'tool-ping',
        cedarPolicyDocument: docMixed,
        principalGroups: ['retail-developers'],
      }).decision,
    ).toBe('deny');
  });
});

describe('extractPrincipalGroupsFromEvent', () => {
  it('reads `cognito:groups` from event.identity.claims (preview shape)', () => {
    const groups = extractPrincipalGroupsFromEvent({
      identity: { claims: { 'cognito:groups': ['retail-developers', 'platform-ai'] } },
    });
    expect(groups).toEqual(['retail-developers', 'platform-ai']);
  });

  it('reads `cognito:groups` from event.requestContext.authorizer.jwt.claims (GA shape)', () => {
    const groups = extractPrincipalGroupsFromEvent({
      requestContext: {
        authorizer: { jwt: { claims: { 'cognito:groups': ['platform-ai'] } } },
      },
    });
    expect(groups).toEqual(['platform-ai']);
  });

  it('reads `cognito:groups` from event.claims (bare-Lambda test shape)', () => {
    const groups = extractPrincipalGroupsFromEvent({
      claims: { 'cognito:groups': ['hr-developers'] },
    });
    expect(groups).toEqual(['hr-developers']);
  });

  it('handles a comma-separated cognito:groups string', () => {
    const groups = extractPrincipalGroupsFromEvent({
      claims: { 'cognito:groups': 'retail-developers, platform-ai' },
    });
    expect(groups).toEqual(['retail-developers', 'platform-ai']);
  });

  it('returns an empty array when no claims are present', () => {
    expect(extractPrincipalGroupsFromEvent({})).toEqual([]);
    expect(extractPrincipalGroupsFromEvent(null)).toEqual([]);
    expect(extractPrincipalGroupsFromEvent(undefined)).toEqual([]);
  });

  it('returns an empty array when cognito:groups is absent or non-string-array', () => {
    expect(
      extractPrincipalGroupsFromEvent({ claims: { 'cognito:groups': [42, true] } }),
    ).toEqual([]);
    expect(extractPrincipalGroupsFromEvent({ claims: {} })).toEqual([]);
  });
});

describe('withCedarEnforcement', () => {
  const entitledSpec: ToolSpec = {
    ...PLATFORM_TOOL_CATALOGUE['tool-echo'],
    allowedGroups: ['retail-developers'],
  };
  const cedarDoc = composeCedarPolicyDocument([entitledSpec]);

  it('invokes the inner handler when the principal is allowed', async () => {
    const inner = jest.fn().mockResolvedValue({ ok: true });
    const wrapped = withCedarEnforcement(
      { toolId: 'tool-echo', cedarPolicyDocument: cedarDoc },
      inner,
    );
    const result = await wrapped({
      claims: { 'cognito:groups': ['retail-developers'] },
      message: 'hi',
    });
    expect(result).toEqual({ ok: true });
    expect(inner).toHaveBeenCalledTimes(1);
  });

  it('throws CedarDeniedError without invoking the inner handler when denied', async () => {
    const inner = jest.fn();
    const wrapped = withCedarEnforcement(
      { toolId: 'tool-echo', cedarPolicyDocument: cedarDoc },
      inner,
    );
    await expect(
      wrapped({ claims: { 'cognito:groups': ['hr-developers'] } }),
    ).rejects.toBeInstanceOf(CedarDeniedError);
    expect(inner).not.toHaveBeenCalled();
  });

  it('reads the Cedar document from process.env.AGENTICAI_CEDAR_POLICY_DOCUMENT when not passed in options', async () => {
    const original = process.env.AGENTICAI_CEDAR_POLICY_DOCUMENT;
    process.env.AGENTICAI_CEDAR_POLICY_DOCUMENT = cedarDoc;
    try {
      const inner = jest.fn().mockResolvedValue('ok');
      const wrapped = withCedarEnforcement({ toolId: 'tool-echo' }, inner);
      const result = await wrapped({ claims: { 'cognito:groups': ['retail-developers'] } });
      expect(result).toBe('ok');
    } finally {
      if (original === undefined) {
        delete process.env.AGENTICAI_CEDAR_POLICY_DOCUMENT;
      } else {
        process.env.AGENTICAI_CEDAR_POLICY_DOCUMENT = original;
      }
    }
  });

  it('fails closed when neither options.cedarPolicyDocument nor env var are set', async () => {
    const original = process.env.AGENTICAI_CEDAR_POLICY_DOCUMENT;
    delete process.env.AGENTICAI_CEDAR_POLICY_DOCUMENT;
    try {
      const inner = jest.fn();
      const wrapped = withCedarEnforcement({ toolId: 'tool-echo' }, inner);
      await expect(
        wrapped({ claims: { 'cognito:groups': ['retail-developers'] } }),
      ).rejects.toBeInstanceOf(CedarDeniedError);
      expect(inner).not.toHaveBeenCalled();
    } finally {
      if (original !== undefined) {
        process.env.AGENTICAI_CEDAR_POLICY_DOCUMENT = original;
      }
    }
  });

  it('throws synchronously at wrap time when toolId is missing', () => {
    expect(() =>
      withCedarEnforcement({ toolId: '' as unknown as string }, async () => undefined),
    ).toThrow(/toolId is required/);
  });
});

describe('evaluateCedar — per-subject entitlement (Round 3)', () => {
  const doc = subjectPermitDoc('tool-echo', ['sub-alice', 'sub-bob']);

  it('allows when principal subject matches a Developer permit', () => {
    const d = evaluateCedar({
      toolId: 'tool-echo',
      cedarPolicyDocument: doc,
      principalGroups: [],
      principalSubject: 'sub-alice',
    });
    expect(d.decision).toBe('allow');
  });

  it('denies a non-matching subject even in the same group', () => {
    const d = evaluateCedar({
      toolId: 'tool-echo',
      cedarPolicyDocument: doc,
      principalGroups: ['retail-developers'],
      principalSubject: 'sub-carol',
    });
    expect(d.decision).toBe('deny');
  });

  it('fails closed when no subject is presented but subject permits exist', () => {
    const d = evaluateCedar({
      toolId: 'tool-echo',
      cedarPolicyDocument: doc,
      principalGroups: [],
      principalSubject: undefined,
    });
    expect(d.decision).toBe('deny');
  });

  it('routes subject permits per-tool (isolation)', () => {
    const mixed =
      subjectPermitDoc('tool-echo', ['sub-alice']) +
      '\n' +
      subjectPermitDoc('tool-ping', ['sub-bob']);
    expect(
      evaluateCedar({
        toolId: 'tool-echo',
        cedarPolicyDocument: mixed,
        principalGroups: [],
        principalSubject: 'sub-bob',
      }).decision,
    ).toBe('deny');
    expect(
      evaluateCedar({
        toolId: 'tool-ping',
        cedarPolicyDocument: mixed,
        principalGroups: [],
        principalSubject: 'sub-bob',
      }).decision,
    ).toBe('allow');
  });
});

describe('evaluateCedar — combined subject + group semantics', () => {
  // Two developers in the SAME group, only one subject-entitled.
  const combined =
    'permit(principal in CognitoGroup::"retail-developers", action == Action::"InvokeTool", resource == Tool::"tool-echo");\n' +
    subjectPermitDoc('tool-ping', ['sub-alice']);

  it('group permit still allows any group member for the group-scoped tool', () => {
    expect(
      evaluateCedar({
        toolId: 'tool-echo',
        cedarPolicyDocument: combined,
        principalGroups: ['retail-developers'],
        principalSubject: 'sub-bob',
      }).decision,
    ).toBe('allow');
  });

  it('subject-scoped tool allows only the entitled subject, even for a group peer', () => {
    // Alice: entitled by subject.
    expect(
      evaluateCedar({
        toolId: 'tool-ping',
        cedarPolicyDocument: combined,
        principalGroups: ['retail-developers'],
        principalSubject: 'sub-alice',
      }).decision,
    ).toBe('allow');
    // Bob: same group, different subject -> denied on the subject-scoped tool.
    expect(
      evaluateCedar({
        toolId: 'tool-ping',
        cedarPolicyDocument: combined,
        principalGroups: ['retail-developers'],
        principalSubject: 'sub-bob',
      }).decision,
    ).toBe('deny');
  });
});

describe('extractPrincipalSubjectFromEvent', () => {
  it('reads sub from the preview identity.claims shape', () => {
    expect(
      extractPrincipalSubjectFromEvent({ identity: { claims: { sub: 'sub-alice' } } }),
    ).toBe('sub-alice');
  });

  it('reads sub from the GA requestContext.authorizer.jwt.claims shape', () => {
    expect(
      extractPrincipalSubjectFromEvent({
        requestContext: { authorizer: { jwt: { claims: { sub: 'sub-bob' } } } },
      }),
    ).toBe('sub-bob');
  });

  it('reads sub from the bare-Lambda claims shape', () => {
    expect(extractPrincipalSubjectFromEvent({ claims: { sub: 'sub-carol' } })).toBe(
      'sub-carol',
    );
  });

  it('returns undefined when sub is absent, empty, or non-string (forged/missing)', () => {
    expect(extractPrincipalSubjectFromEvent({ claims: {} })).toBeUndefined();
    expect(extractPrincipalSubjectFromEvent({ claims: { sub: '' } })).toBeUndefined();
    expect(extractPrincipalSubjectFromEvent({ claims: { sub: 42 } })).toBeUndefined();
    expect(extractPrincipalSubjectFromEvent(null)).toBeUndefined();
  });
});

describe('withCedarEnforcement — subject rotation/revocation', () => {
  it('a rotated-out subject is denied once the permit no longer names it', async () => {
    // Old doc entitled sub-alice; after rotation the doc entitles sub-alice-2.
    const rotatedDoc = subjectPermitDoc('tool-echo', ['sub-alice-2']);
    const inner = jest.fn();
    const wrapped = withCedarEnforcement(
      { toolId: 'tool-echo', cedarPolicyDocument: rotatedDoc },
      inner,
    );
    await expect(
      wrapped({ claims: { sub: 'sub-alice' } }),
    ).rejects.toBeInstanceOf(CedarDeniedError);
    expect(inner).not.toHaveBeenCalled();
  });

  it('the rotated-in subject is allowed', async () => {
    const rotatedDoc = subjectPermitDoc('tool-echo', ['sub-alice-2']);
    const inner = jest.fn().mockResolvedValue('ok');
    const wrapped = withCedarEnforcement(
      { toolId: 'tool-echo', cedarPolicyDocument: rotatedDoc },
      inner,
    );
    await expect(wrapped({ claims: { sub: 'sub-alice-2' } })).resolves.toBe('ok');
  });
});
