/**
 * Phase Q (v0.6.0) conformance — per-developer tool entitlement.
 *
 * Pins the new behaviour added on top of the v0.5.0 Phase 10 D-03
 * per-workstream Gateway:
 *
 *   Q1 — `ToolSpec.allowedGroups` is validated at synth (catalogue SSOT)
 *   Q2 — `composeCedarPolicyDocument` emits principal-bound permits with no
 *        wildcards when `allowedGroups` is set; default forbid is preserved
 *   Q3 — `D03WorkstreamGatewayStack` (legacy path) throws at synth when any
 *        subscribed tool declares `allowedGroups` but `cognitoDiscoveryUrl`
 *        is missing — Cedar group binding has nothing to evaluate against
 *        without JWT claims
 *   Q4 — `evaluateCedar` denies a principal whose `cognito:groups` claim
 *        does not intersect a tool's allow-list (covered in the
 *        @agenticai/tool-cedar-wrapper unit tests; this file pins the
 *        catalogue + stack integration)
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import {
  PLATFORM_TOOL_CATALOGUE,
  composeCedarPolicyDocument,
  type ToolSpec,
} from '@agenticai/platform-tool-catalogue';
import {
  evaluateCedar,
  extractPrincipalGroupsFromEvent,
} from '@agenticai/tool-cedar-wrapper';

import { D03WorkstreamGatewayStack } from '../../apps/platform-account/lib/d03-workstream-gateway-stack';
import { D03PlatformCoreStack } from '../../apps/platform-account/lib/d03-platform-core-stack';

const PLATFORM_ACCOUNT_ID = '222222222222';
const WORKLOAD_ACCOUNT_ID = '333333333333';
const COGNITO_DISCOVERY =
  'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc/.well-known/openid-configuration';

describe('Phase Q — composed Cedar bundle shape', () => {
  it('produces principal-bound permits + default forbid when allowedGroups is set', () => {
    const subset: readonly ToolSpec[] = [
      { ...PLATFORM_TOOL_CATALOGUE['tool-echo'], allowedGroups: ['retail-developers'] },
    ];
    const doc = composeCedarPolicyDocument(subset);
    expect(doc).toContain(
      'permit(principal in CognitoGroup::"retail-developers", action == Action::"InvokeTool", resource == Tool::"tool-echo");',
    );
    expect(doc).toMatch(/forbid\(principal, action, resource\)/);
    expect(doc).toContain('Default forbid');
  });

  it('strips the unconditional permit when allowedGroups is set (no group bypass)', () => {
    const subset: readonly ToolSpec[] = [
      { ...PLATFORM_TOOL_CATALOGUE['tool-echo'], allowedGroups: ['retail-developers'] },
    ];
    const doc = composeCedarPolicyDocument(subset);
    expect(doc).not.toContain(
      'permit(principal, action == Action::"InvokeTool", resource == Tool::"tool-echo");',
    );
  });

  it('contains no Cedar wildcards (no `principal,` shorthand for entitled tools)', () => {
    const subset: readonly ToolSpec[] = [
      { ...PLATFORM_TOOL_CATALOGUE['tool-echo'], allowedGroups: ['retail-developers'] },
      { ...PLATFORM_TOOL_CATALOGUE['tool-ping'], allowedGroups: ['platform-ai'] },
    ];
    const doc = composeCedarPolicyDocument(subset);
    const permits = doc.match(/permit\([^)]+\)/g) ?? [];
    // Every permit clause must either bind to a CognitoGroup principal OR be
    // the catalogue's bare permit (which we strip when allowedGroups is set).
    for (const p of permits) {
      expect(p).toMatch(/principal in CognitoGroup::"/);
    }
  });
});

describe('Phase Q — D03WorkstreamGatewayStack legacy-path enforcement', () => {
  function synthLegacy(opts: {
    readonly toolWithGroups?: readonly string[];
    readonly cognitoDiscoveryUrl?: string;
  }): { template: Template; stack: D03WorkstreamGatewayStack } {
    const app = new App();
    // Inject the test-only entitled tool by mutating the catalogue subset
    // through a derived ToolSpec — but the production stack reads the
    // catalogue itself, so we synth using the existing tool-echo and
    // augment via a per-test override of the catalogue entry. The simpler
    // path is to use the publicly-exported catalogue and rely on the
    // synth-time validation reading it as-is. So the assertion strategy
    // below uses the documented stack-level error message instead.
    const stack = new D03WorkstreamGatewayStack(app, 'AgenticAI-D03-WorkstreamGateway-q-test', {
      env: { account: WORKLOAD_ACCOUNT_ID, region: 'us-east-1' },
      tenantId: 'q',
      agentId: 'test',
      envName: 'nonprod',
      workloadAccountId: WORKLOAD_ACCOUNT_ID,
      platformAccountId: PLATFORM_ACCOUNT_ID,
      allowedToolIds: ['tool-echo', 'tool-ping'],
      cognitoDiscoveryUrl: opts.cognitoDiscoveryUrl,
      cognitoAudience: opts.cognitoDiscoveryUrl ? ['aud-q'] : undefined,
    });
    return { template: Template.fromStack(stack), stack };
  }

  it('synth still succeeds when no subscribed tool declares allowedGroups (back-compat)', () => {
    expect(() => synthLegacy({})).not.toThrow();
  });

  it('synth still succeeds when allowedGroups is absent and CUSTOM_JWT is in use', () => {
    expect(() => synthLegacy({ cognitoDiscoveryUrl: COGNITO_DISCOVERY })).not.toThrow();
  });
});

describe('Phase Q — D03WorkstreamGatewayStack legacy-path: allowedGroups on subset throws without JWT', () => {
  // Spy the catalogue so we can simulate an entitled tool without forking the
  // SSOT export. We restore in `afterEach`.
  let originalEcho: ToolSpec | undefined;

  beforeEach(() => {
    originalEcho = PLATFORM_TOOL_CATALOGUE['tool-echo'];
    // Mutate the readonly record via cast — test-only, restored after.
    (PLATFORM_TOOL_CATALOGUE as Record<string, ToolSpec>)['tool-echo'] = {
      ...originalEcho!,
      allowedGroups: ['retail-developers'],
    };
  });

  afterEach(() => {
    if (originalEcho) {
      (PLATFORM_TOOL_CATALOGUE as Record<string, ToolSpec>)['tool-echo'] = originalEcho;
    }
  });

  it('throws at synth when an entitled tool is subscribed but no cognitoDiscoveryUrl is supplied', () => {
    const app = new App();
    expect(
      () =>
        new D03WorkstreamGatewayStack(app, 'AgenticAI-D03-WorkstreamGateway-q-throws', {
          env: { account: WORKLOAD_ACCOUNT_ID, region: 'us-east-1' },
          tenantId: 'q',
          agentId: 'throws',
          envName: 'nonprod',
          workloadAccountId: WORKLOAD_ACCOUNT_ID,
          platformAccountId: PLATFORM_ACCOUNT_ID,
          allowedToolIds: ['tool-echo'],
          // cognitoDiscoveryUrl deliberately omitted
        }),
    ).toThrow(/allowedGroups.*CUSTOM_JWT|cognitoDiscoveryUrl/);
  });

  it('error names the offending tool id so the developer can fix the subscription', () => {
    const app = new App();
    try {
      new D03WorkstreamGatewayStack(app, 'AgenticAI-D03-WorkstreamGateway-q-throws-2', {
        env: { account: WORKLOAD_ACCOUNT_ID, region: 'us-east-1' },
        tenantId: 'q',
        agentId: 'throws',
        envName: 'nonprod',
        workloadAccountId: WORKLOAD_ACCOUNT_ID,
        platformAccountId: PLATFORM_ACCOUNT_ID,
        allowedToolIds: ['tool-echo'],
      });
      fail('expected throw');
    } catch (err) {
      expect((err as Error).message).toContain('tool-echo');
    }
  });

  it('synth succeeds when the entitled tool is subscribed AND cognitoDiscoveryUrl is supplied', () => {
    const app = new App();
    expect(
      () =>
        new D03WorkstreamGatewayStack(app, 'AgenticAI-D03-WorkstreamGateway-q-jwt', {
          env: { account: WORKLOAD_ACCOUNT_ID, region: 'us-east-1' },
          tenantId: 'q',
          agentId: 'jwt',
          envName: 'nonprod',
          workloadAccountId: WORKLOAD_ACCOUNT_ID,
          platformAccountId: PLATFORM_ACCOUNT_ID,
          allowedToolIds: ['tool-echo'],
          cognitoDiscoveryUrl: COGNITO_DISCOVERY,
          cognitoAudience: ['aud-q'],
        }),
    ).not.toThrow();
  });

  it('PerTenantCedarPolicy output contains principal-bound permits when entitlement is in use', () => {
    const app = new App();
    const stack = new D03WorkstreamGatewayStack(
      app,
      'AgenticAI-D03-WorkstreamGateway-q-bundle',
      {
        env: { account: WORKLOAD_ACCOUNT_ID, region: 'us-east-1' },
        tenantId: 'q',
        agentId: 'bundle',
        envName: 'nonprod',
        workloadAccountId: WORKLOAD_ACCOUNT_ID,
        platformAccountId: PLATFORM_ACCOUNT_ID,
        allowedToolIds: ['tool-echo'],
        cognitoDiscoveryUrl: COGNITO_DISCOVERY,
      },
    );
    const template = Template.fromStack(stack);
    const outputs = template.findOutputs('PerTenantCedarPolicy');
    const value = (Object.values(outputs)[0] as { Value: unknown }).Value;
    const rendered = typeof value === 'string' ? value : JSON.stringify(value);
    expect(rendered).toContain(
      'permit(principal in CognitoGroup::"retail-developers", action == Action::"InvokeTool", resource == Tool::"tool-echo");',
    );
  });
});

describe('Phase Q — Cedar evaluator integration with the catalogue bundle', () => {
  it('denies a JWT whose cognito:groups do not intersect the tool allow-list', () => {
    const subset: readonly ToolSpec[] = [
      { ...PLATFORM_TOOL_CATALOGUE['tool-echo'], allowedGroups: ['retail-developers'] },
    ];
    const doc = composeCedarPolicyDocument(subset);
    const groups = extractPrincipalGroupsFromEvent({
      claims: { 'cognito:groups': ['hr-developers'] },
    });
    const decision = evaluateCedar({
      toolId: 'tool-echo',
      cedarPolicyDocument: doc,
      principalGroups: groups,
    });
    expect(decision.decision).toBe('deny');
  });

  it('allows a JWT whose cognito:groups intersect the tool allow-list', () => {
    const subset: readonly ToolSpec[] = [
      { ...PLATFORM_TOOL_CATALOGUE['tool-echo'], allowedGroups: ['retail-developers'] },
    ];
    const doc = composeCedarPolicyDocument(subset);
    const groups = extractPrincipalGroupsFromEvent({
      requestContext: {
        authorizer: { jwt: { claims: { 'cognito:groups': ['retail-developers'] } } },
      },
    });
    const decision = evaluateCedar({
      toolId: 'tool-echo',
      cedarPolicyDocument: doc,
      principalGroups: groups,
    });
    expect(decision.decision).toBe('allow');
  });
});

describe('Phase Q — D03PlatformCoreStack demo tool Lambda Cedar gate (inlined)', () => {
  function synthCore(): Template {
    const app = new App();
    const stack = new D03PlatformCoreStack(app, 'AgenticAI-D03-PlatformCoreStack-q-inline', {
      env: { account: PLATFORM_ACCOUNT_ID, region: 'us-east-1' },
      workloadAccountIds: [WORKLOAD_ACCOUNT_ID],
      externalId: 'phase-q-inline-eid',
      tenantAllocations: [
        {
          tenantId: 'demo',
          agentId: 'primary',
          workloadAccountId: WORKLOAD_ACCOUNT_ID,
          costCentre: 'platform',
          envName: 'nonprod',
        },
      ],
    });
    return Template.fromStack(stack);
  }

  function findToolFn(template: Template, toolFunctionName: string): { code: string; env: Record<string, unknown> } {
    const fns = template.findResources('AWS::Lambda::Function');
    for (const [, props] of Object.entries(fns)) {
      const p = props as { Properties: { FunctionName?: string; Code?: { ZipFile?: string }; Environment?: { Variables?: Record<string, unknown> } } };
      if (p.Properties.FunctionName === toolFunctionName) {
        return {
          code: p.Properties.Code?.ZipFile ?? '',
          env: p.Properties.Environment?.Variables ?? {},
        };
      }
    }
    throw new Error(`tool function ${toolFunctionName} not found in template`);
  }

  it('inlines the Cedar gate prologue + invokes the gate before the user body (echo)', () => {
    const tpl = synthCore();
    const echo = findToolFn(tpl, 'agenticai-d03-tool-echo');
    expect(echo.code).toContain('__agenticaiExtractGroups');
    expect(echo.code).toContain('__agenticaiEvaluateCedar');
    expect(echo.code).toContain('__agenticaiCedarDeniedError');
    expect(echo.code).toContain('await __agenticaiCedarGate(event);');
    expect(echo.code).toContain("name = 'CedarDeniedError'");
  });

  it('inlines the Cedar gate prologue + invokes the gate before the user body (ping)', () => {
    const tpl = synthCore();
    const ping = findToolFn(tpl, 'agenticai-d03-tool-ping');
    expect(ping.code).toContain('await __agenticaiCedarGate(event);');
    expect(ping.code).toContain('process.env.AGENTICAI_CEDAR_POLICY_DOCUMENT');
  });

  it('seeds AGENTICAI_TOOL_ID + AGENTICAI_CEDAR_POLICY_DOCUMENT env vars from the catalogue', () => {
    const tpl = synthCore();
    const echo = findToolFn(tpl, 'agenticai-d03-tool-echo');
    expect(echo.env.AGENTICAI_TOOL_ID).toBe('tool-echo');
    expect(typeof echo.env.AGENTICAI_CEDAR_POLICY_DOCUMENT).toBe('string');
    // v0.5.0 back-compat default: tool-echo has no allowedGroups in the
    // baseline catalogue, so the bundle is the unconditional permit.
    expect(echo.env.AGENTICAI_CEDAR_POLICY_DOCUMENT).toContain(
      'permit(principal, action == Action::"InvokeTool", resource == Tool::"tool-echo");',
    );
  });

  it('seeded Cedar bundle includes the default forbid for fail-closed semantics', () => {
    const tpl = synthCore();
    const echo = findToolFn(tpl, 'agenticai-d03-tool-echo');
    expect(echo.env.AGENTICAI_CEDAR_POLICY_DOCUMENT).toMatch(/forbid\(principal, action, resource\)/);
  });

  it('switches to principal-bound permits when the catalogue tool gains allowedGroups (live-tester rotation parity)', () => {
    const original = PLATFORM_TOOL_CATALOGUE['tool-echo'];
    (PLATFORM_TOOL_CATALOGUE as Record<string, ToolSpec>)['tool-echo'] = {
      ...original,
      allowedGroups: ['retail-developers'],
    };
    try {
      const tpl = synthCore();
      const echo = findToolFn(tpl, 'agenticai-d03-tool-echo');
      expect(echo.env.AGENTICAI_CEDAR_POLICY_DOCUMENT).toContain(
        'permit(principal in CognitoGroup::"retail-developers", action == Action::"InvokeTool", resource == Tool::"tool-echo");',
      );
      expect(echo.env.AGENTICAI_CEDAR_POLICY_DOCUMENT).not.toContain(
        'permit(principal, action == Action::"InvokeTool", resource == Tool::"tool-echo");',
      );
    } finally {
      (PLATFORM_TOOL_CATALOGUE as Record<string, ToolSpec>)['tool-echo'] = original;
    }
  });

  it('inline gate denies a non-member JWT when AGENTICAI_CEDAR_POLICY_DOCUMENT carries a group-bound permit', async () => {
    // Behaviour parity check: the inline gate string is functionally
    // equivalent to @agenticai/tool-cedar-wrapper. We exercise the inline
    // logic by extracting it from the synthesised Lambda code, eval-ing it
    // in a sandbox, and asserting the same allow/deny semantics. This pins
    // the inline gate against drift from the wrapper package.
    const tpl = synthCore();
    const echo = findToolFn(tpl, 'agenticai-d03-tool-echo');
    const principalBound = composeCedarPolicyDocument([
      { ...PLATFORM_TOOL_CATALOGUE['tool-echo'], allowedGroups: ['retail-developers'] },
    ]);
    const sandbox: Record<string, unknown> = {};
    const inlineCode = echo.code;
    // Strip the `exports.handler = ...` so we can call the inner gate
    // directly from our sandbox without invoking the user body.
    const prologueOnly = inlineCode.split('exports.handler')[0];
    const fn = new Function('process', `${prologueOnly}; return __agenticaiCedarGate;`)({
      env: {
        AGENTICAI_TOOL_ID: 'tool-echo',
        AGENTICAI_CEDAR_POLICY_DOCUMENT: principalBound,
      },
    });
    void sandbox;
    await expect(
      (fn as (e: unknown) => Promise<void>)({
        claims: { 'cognito:groups': ['hr-developers'] },
      }),
    ).rejects.toThrow(/CedarDeniedError|denied by Cedar/);
    await expect(
      (fn as (e: unknown) => Promise<void>)({
        claims: { 'cognito:groups': ['retail-developers'] },
      }),
    ).resolves.toBeUndefined();
  });
});
