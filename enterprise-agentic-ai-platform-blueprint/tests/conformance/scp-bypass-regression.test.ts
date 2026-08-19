/**
 * SCP bypass-regression tests.
 *
 * These lock down the condition shapes introduced by the production-blocker
 * security review. Each assertion corresponds to a specific bypass that
 * slipped through the previous SCP bodies:
 *
 *   - SCP-02: `Null`-only gate allowed empty-string GuardrailIdentifier —
 *             now paired with an allow-list `ForAllValues:StringNotEquals`.
 *   - SCP-03: `StringNotEqualsIfExists` on `aws:SourceVpce` made the key's
 *             absence satisfy the condition → public calls skipped the
 *             deny — now accompanied by a `Null: aws:SourceVpce = true`
 *             twin deny.
 *   - SCP-04: same bypass / same fix as SCP-03.
 *   - SCP-05: exact-match `StringNotEquals` on `aws:PrincipalArn` ignored
 *             the STS assumed-role session form — now `ArnNotLike` against
 *             both role and assumed-role patterns.
 *
 * Every fixed SCP must also carry a `PrincipalIsAWSService=false` guard so
 * AWS-owned service principals (Config, GuardDuty, CloudTrail, …) are not
 * self-denied.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  buildScpSet,
  SCP_BODY_SOFT_LIMIT,
} from '../../packages/organizations/src/scps/index';
import {
  allowedModelArns,
  PLATFORM_APPROVED_REGIONS,
} from '@agenticai/platform-baselines';

const PLATFORM_GUARDRAIL_ADMIN_ROLE_ARN =
  'arn:aws:iam::111111111111:role/AgenticAI-GuardrailAdmin';
const APPROVED_GUARDRAIL_IDS = [
  'arn:aws:bedrock:us-west-2:111111111111:guardrail/platform-default',
];
const PLATFORM_ACCOUNT_ID = '222222222222';
const ALLOWED_TOOL_TARGET_ARNS = [
  'arn:aws:lambda:us-west-2:333333333333:function:tool-search',
  'arn:aws:lambda:us-west-2:333333333333:function:tool-weather',
];

function renderSet(
  opts: {
    approvedGuardrailIds?: readonly string[];
    platformAccountId?: string;
    allowedToolTargetArns?: readonly string[];
  } = {},
) {
  return buildScpSet({
    allowedModelArns: allowedModelArns('us-west-2'),
    approvedRegions: PLATFORM_APPROVED_REGIONS,
    platformGuardrailAdminRoleArn: PLATFORM_GUARDRAIL_ADMIN_ROLE_ARN,
    approvedGuardrailIds: opts.approvedGuardrailIds,
    platformAccountId: opts.platformAccountId,
    allowedToolTargetArns: opts.allowedToolTargetArns,
  });
}

/**
 * IAM condition-evaluation model used by the bypass-regression tests below.
 *
 * `ArnNotLike` / `ArnLike` on `aws:PrincipalArn` treats the candidate ARN as
 * a glob-matched string (standard-globs only: `*` and `?`). These helpers
 * mirror that semantics so the regression tests do not depend on live IAM.
 */
function arnLikeMatch(pattern: string, candidate: string): boolean {
  // Escape regex metacharacters, then replace glob wildcards.
  const re = new RegExp(
    '^' +
      pattern
        .replace(/[-[\]{}()+.,\\^$|#\s]/g, '\\$&')
        .replace(/\*/g, '.*')
        .replace(/\?/g, '.') +
      '$',
  );
  return re.test(candidate);
}
function arnNotLikeFires(patterns: readonly string[], candidate: string): boolean {
  // Deny fires iff the candidate does NOT match ANY of the allowed patterns.
  return !patterns.some((p) => arnLikeMatch(p, candidate));
}
function arnLikeFires(patterns: readonly string[], candidate: string): boolean {
  return patterns.some((p) => arnLikeMatch(p, candidate));
}

describe('SCP bypass regression — SCP-02 (empty-string GuardrailIdentifier)', () => {
  it('with approvedGuardrailIds emits both the Null gate and the allow-list Deny', () => {
    const scp02 = renderSet({ approvedGuardrailIds: APPROVED_GUARDRAIL_IDS })[1];
    const parsed = scp02.body as any;

    const nullStmt = parsed.Statement.find(
      (s: any) => s.Condition?.Null?.['bedrock:GuardrailIdentifier'] === 'true',
    );
    expect(nullStmt).toBeDefined();

    const allowListStmt = parsed.Statement.find(
      (s: any) => s.Condition?.['ForAllValues:StringNotEquals']?.['bedrock:GuardrailIdentifier'],
    );
    expect(allowListStmt).toBeDefined();
    expect(
      allowListStmt.Condition['ForAllValues:StringNotEquals']['bedrock:GuardrailIdentifier'],
    ).toEqual(APPROVED_GUARDRAIL_IDS);
  });

  it('falls back to Null-only gate when approvedGuardrailIds is omitted (with warning)', () => {
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => {});
    try {
      const scp02 = renderSet()[1];
      const parsed = scp02.body as any;
      expect(parsed.Statement).toHaveLength(1);
      expect(parsed.Statement[0].Condition.Null['bedrock:GuardrailIdentifier']).toBe('true');
      expect(warn).toHaveBeenCalledWith(expect.stringContaining('TODO-APPROVED-GUARDRAILS'));
    } finally {
      warn.mockRestore();
    }
  });
});

describe('SCP bypass regression — SCP-03 (IfExists on aws:SourceVpce)', () => {
  it('emits both the StringNotEquals VPCE deny AND the Null-source-VPCE twin deny', () => {
    const scp03 = renderSet()[2];
    const parsed = scp03.body as any;

    const vpceDeny = parsed.Statement.find(
      (s: any) => s.Condition?.StringNotEquals?.['aws:SourceVpce'],
    );
    expect(vpceDeny).toBeDefined();
    // No IfExists suffix — absence must no longer skip the deny.
    expect(vpceDeny.Condition.StringNotEqualsIfExists).toBeUndefined();

    const nullDeny = parsed.Statement.find(
      (s: any) => s.Condition?.Null?.['aws:SourceVpce'] === 'true',
    );
    expect(nullDeny).toBeDefined();
    expect(nullDeny.Action).toEqual(['bedrock-agentcore:*']);
  });
});

describe('SCP bypass regression — SCP-04 (IfExists on aws:SourceVpce)', () => {
  it('emits both the StringNotEquals VPCE deny AND the Null-source-VPCE twin deny', () => {
    const scp04 = renderSet()[3];
    const parsed = scp04.body as any;

    const vpceDeny = parsed.Statement.find(
      (s: any) => s.Condition?.StringNotEquals?.['aws:SourceVpce'],
    );
    expect(vpceDeny).toBeDefined();
    expect(vpceDeny.Condition.StringNotEqualsIfExists).toBeUndefined();

    const nullDeny = parsed.Statement.find(
      (s: any) => s.Condition?.Null?.['aws:SourceVpce'] === 'true',
    );
    expect(nullDeny).toBeDefined();
    expect(nullDeny.Action).toContain('bedrock:ApplyGuardrail');
  });
});

describe('SCP bypass regression — SCP-05 (PrincipalArn session form)', () => {
  it('uses ArnNotLike with both role and assumed-role forms', () => {
    const scp05 = renderSet()[4];
    const parsed = scp05.body as any;
    const stmt = parsed.Statement[0];
    expect(stmt.Condition.ArnNotLike).toBeDefined();
    expect(stmt.Condition.StringNotEquals).toBeUndefined();
    const arns: string[] = stmt.Condition.ArnNotLike['aws:PrincipalArn'];
    expect(arns).toEqual(
      expect.arrayContaining([
        'arn:aws:iam::111111111111:role/AgenticAI-GuardrailAdmin',
        'arn:aws:sts::111111111111:assumed-role/AgenticAI-GuardrailAdmin/*',
      ]),
    );
  });
});

describe('SCP bypass regression — AWS-service principal self-denial guard', () => {
  it('SCPs 02, 03, 04, 05 all carry PrincipalIsAWSService=false on every statement', () => {
    const set = renderSet({ approvedGuardrailIds: APPROVED_GUARDRAIL_IDS });
    const targets = [set[1], set[2], set[3], set[4]]; // SCP-02, 03, 04, 05
    for (const scp of targets) {
      const parsed = scp.body as any;
      for (const stmt of parsed.Statement) {
        const guard = stmt.Condition?.BoolIfExists?.['aws:PrincipalIsAWSService'];
        expect(guard).toBe('false');
      }
    }
  });
});

describe('SCP bypass regression — size budget', () => {
  it('every rewritten SCP body stays within the 5000-char soft limit', () => {
    const set = renderSet({
      approvedGuardrailIds: APPROVED_GUARDRAIL_IDS,
      platformAccountId: PLATFORM_ACCOUNT_ID,
      allowedToolTargetArns: ALLOWED_TOOL_TARGET_ARNS,
    });
    for (const scp of set) {
      expect(scp.bodyJson.length).toBeLessThanOrEqual(SCP_BODY_SOFT_LIMIT);
    }
  });
});

describe('SCP bypass regression — SCP-09 (Gateway mutation lockdown)', () => {
  function getScp09() {
    const set = renderSet({ platformAccountId: PLATFORM_ACCOUNT_ID });
    const scp09 = set.find((s) => s.id === 'scp-09')!;
    expect(scp09).toBeDefined();
    return scp09;
  }

  it('ArnNotLike fires when the evaluated aws:PrincipalArn is the literal empty string', () => {
    // Bypass scenario: a malformed principal evaluation surfaces "" as the
    // aws:PrincipalArn value. ArnNotLike must still treat "" as not-like any
    // allowed admin ARN, so the Deny fires.
    const parsed = getScp09().body as any;
    const allowed: string[] = parsed.Statement[0].Condition.ArnNotLike['aws:PrincipalArn'];
    expect(arnNotLikeFires(allowed, '')).toBe(true);
  });

  it('role-name case-difference matches via ArnNotLike (so case-altered forgeries still trip Deny)', () => {
    // IAM ARNs are technically case-sensitive in string comparisons, but
    // ArnNotLike against the canonical spelling means a case-different forgery
    // (e.g. "AGENTICAI-d03-GATEWAYADMIN") is NOT a match → Deny fires.
    const parsed = getScp09().body as any;
    const allowed: string[] = parsed.Statement[0].Condition.ArnNotLike['aws:PrincipalArn'];
    const forged = `arn:aws:sts::${PLATFORM_ACCOUNT_ID}:assumed-role/AGENTICAI-d03-GATEWAYADMIN/session-1`;
    expect(arnNotLikeFires(allowed, forged)).toBe(true);
  });

  it('AWS-service principals are not self-denied (BoolIfExists guard)', () => {
    // AgentCore Gateway may be acted on internally by an AWS service
    // principal (e.g. pipeline Lambda with service principal). The
    // PrincipalIsAWSService=false guard prevents the Deny from firing.
    const parsed = getScp09().body as any;
    const stmt = parsed.Statement[0];
    expect(stmt.Condition.BoolIfExists['aws:PrincipalIsAWSService']).toBe('false');
    // Defence-in-depth: the action list does not include any wildcard that
    // would sweep in unrelated Bedrock calls.
    const actions: string[] = stmt.Action;
    expect(actions.every((a) => a.startsWith('bedrock-agentcore:'))).toBe(true);
  });

  it('admin session ARN matches and plain role ARN matches — Deny does NOT fire for either', () => {
    const parsed = getScp09().body as any;
    const allowed: string[] = parsed.Statement[0].Condition.ArnNotLike['aws:PrincipalArn'];

    const sessionArn = `arn:aws:sts::${PLATFORM_ACCOUNT_ID}:assumed-role/AgenticAI-D03-GatewayAdmin/session-abc123`;
    const roleArn = `arn:aws:iam::${PLATFORM_ACCOUNT_ID}:role/AgenticAI-D03-GatewayAdmin`;

    // Both admin forms are ArnLike (match) one of the allowed patterns, so
    // ArnNotLike is FALSE and the Deny does NOT fire.
    expect(arnNotLikeFires(allowed, sessionArn)).toBe(false);
    expect(arnNotLikeFires(allowed, roleArn)).toBe(false);

    // But a workload runtime-role session in the SAME account does trip Deny.
    const workloadSession = `arn:aws:sts::${PLATFORM_ACCOUNT_ID}:assumed-role/AgenticAI-D03-foo-runtime/session-xyz`;
    expect(arnNotLikeFires(allowed, workloadSession)).toBe(true);
  });
});

describe('SCP bypass regression — SCP-10 (Tool-invoke allow-list)', () => {
  function getScp10() {
    const set = renderSet({
      platformAccountId: PLATFORM_ACCOUNT_ID,
      allowedToolTargetArns: ALLOWED_TOOL_TARGET_ARNS,
    });
    const scp10 = set.find((s) => s.id === 'scp-10')!;
    expect(scp10).toBeDefined();
    return scp10;
  }

  it('runtime-role invoking a catalogued Lambda is NOT denied (NotResource exempts it)', () => {
    const parsed = getScp10().body as any;
    const stmt = parsed.Statement[0];
    const notResource: string[] = stmt.NotResource;
    const runtimePrincipalPatterns: string[] = stmt.Condition.ArnLike['aws:PrincipalArn'];

    const runtimeSession =
      'arn:aws:sts::333333333333:assumed-role/AgenticAI-D03-myapp-runtime/session-1';
    const cataloguedTarget = ALLOWED_TOOL_TARGET_ARNS[0];

    // Principal matches the runtime-role ArnLike condition …
    expect(arnLikeFires(runtimePrincipalPatterns, runtimeSession)).toBe(true);
    // … but the resource IS on the NotResource list, so the Deny does not
    // fire. Simulate this: the Deny applies only when the resource is NOT in
    // the NotResource list.
    const resourceTriggersDeny = !notResource.includes(cataloguedTarget);
    expect(resourceTriggersDeny).toBe(false);
  });

  it('runtime-role invoking a non-catalogued Lambda IS denied', () => {
    const parsed = getScp10().body as any;
    const stmt = parsed.Statement[0];
    const notResource: string[] = stmt.NotResource;
    const runtimePrincipalPatterns: string[] = stmt.Condition.ArnLike['aws:PrincipalArn'];

    const runtimeSession =
      'arn:aws:sts::333333333333:assumed-role/AgenticAI-D03-myapp-runtime/session-1';
    const rogueTarget =
      'arn:aws:lambda:us-west-2:333333333333:function:exfiltrate-to-attacker';

    // Principal matches the runtime-role guard …
    expect(arnLikeFires(runtimePrincipalPatterns, runtimeSession)).toBe(true);
    // … and the resource is NOT on the NotResource list → Deny fires.
    const resourceTriggersDeny = !notResource.includes(rogueTarget);
    expect(resourceTriggersDeny).toBe(true);

    // Non-runtime principal (pipeline role) invoking the same rogue target
    // should NOT be self-denied — the ArnLike narrows to runtime roles only.
    const pipelineRole = 'arn:aws:iam::333333333333:role/AgenticAI-PlatformPipelineRole';
    expect(arnLikeFires(runtimePrincipalPatterns, pipelineRole)).toBe(false);
  });
});
