/**
 * Unit tests for SCP bodies.
 *
 * These guard the five downstream representations of the model allow-list
 * (SCP-01, Bedrock VPCE policy, LiteLLM config, IAM resource scope, SCP-06
 * region list) by pinning the JSON shape produced here.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  buildScpSet,
  SCP_BODY_HARD_LIMIT,
  SCP_BODY_SOFT_LIMIT,
} from './index';
import {
  allowedModelArns,
  PLATFORM_APPROVED_REGIONS,
} from '@agenticai/platform-baselines';

const PLATFORM_GUARDRAIL_ADMIN_ROLE_ARN =
  'arn:aws:iam::111111111111:role/AgenticAI-GuardrailAdmin';
const PLATFORM_ACCOUNT_ID = '222222222222';
const ALLOWED_TOOL_TARGET_ARNS = [
  'arn:aws:lambda:us-west-2:333333333333:function:tool-search',
  'arn:aws:lambda:us-west-2:333333333333:function:tool-weather',
];

function renderSet() {
  return buildScpSet({
    allowedModelArns: allowedModelArns('us-west-2'),
    approvedRegions: PLATFORM_APPROVED_REGIONS,
    platformGuardrailAdminRoleArn: PLATFORM_GUARDRAIL_ADMIN_ROLE_ARN,
  });
}

function renderFullSet() {
  return buildScpSet({
    allowedModelArns: allowedModelArns('us-west-2'),
    approvedRegions: PLATFORM_APPROVED_REGIONS,
    platformGuardrailAdminRoleArn: PLATFORM_GUARDRAIL_ADMIN_ROLE_ARN,
    platformAccountId: PLATFORM_ACCOUNT_ID,
    allowedToolTargetArns: ALLOWED_TOOL_TARGET_ARNS,
  });
}

// Silence the SCP-02/09/10 synth-time warnings emitted when the caller omits
// the optional approvedGuardrailIds / platformAccountId / allowedToolTargetArns
// inputs. The warnings themselves are asserted directly in the dedicated
// bypass-regression tests and in the opt-out tests below.
let warnSpy: jest.SpyInstance;
beforeEach(() => {
  warnSpy = jest.spyOn(console, 'warn').mockImplementation(() => {});
});
afterEach(() => {
  warnSpy.mockRestore();
});

describe('buildScpSet', () => {
  it('produces exactly 8 SCPs (SCP-01 through SCP-08)', () => {
    const set = renderSet();
    expect(set).toHaveLength(8);
    const ids = set.map((s) => s.id);
    expect(ids).toEqual([
      'scp-01',
      'scp-02',
      'scp-03',
      'scp-04',
      'scp-05',
      'scp-06',
      'scp-07',
      'scp-08',
    ]);
  });

  it('every SCP body is valid JSON and IAM-shaped', () => {
    const set = renderSet();
    for (const scp of set) {
      const parsed = scp.body as {
        Version: string;
        Statement: Array<{ Sid?: string; Effect: string } & Record<string, unknown>>;
      };
      expect(parsed.Version).toBe('2012-10-17');
      expect(Array.isArray(parsed.Statement)).toBe(true);
      expect(parsed.Statement.length).toBeGreaterThan(0);
      for (const stmt of parsed.Statement) {
        expect(stmt.Sid).toBeDefined();
        expect(['Allow', 'Deny']).toContain(stmt.Effect);
      }
    }
  });

  it('every SCP body stays under the 5000-char soft limit (R-SCP-017)', () => {
    const set = renderSet();
    for (const scp of set) {
      expect(scp.bodyJson.length).toBeLessThanOrEqual(SCP_BODY_SOFT_LIMIT);
      expect(scp.bodyJson.length).toBeLessThan(SCP_BODY_HARD_LIMIT);
    }
  });

  it('every SCP statement uses Effect=Deny (SCPs are deny-list)', () => {
    const set = renderSet();
    for (const scp of set) {
      const parsed = scp.body as {
        Version: string;
        Statement: Array<{ Sid?: string; Effect: string } & Record<string, unknown>>;
      };
      for (const stmt of parsed.Statement) {
        expect(stmt.Effect).toBe('Deny');
      }
    }
  });
});

describe('SCP-01 model allow-list', () => {
  it('embeds exactly the PLATFORM_ALLOWED_MODELS as foundation-model ARNs', () => {
    const set = renderSet();
    const scp01 = set[0];
    const parsed = scp01.body as any;
    const condition = parsed.Statement[0].Condition['ForAllValues:StringNotEquals'];
    expect(condition['bedrock:FoundationModel']).toEqual([
      'arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0',
      'arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0',
    ]);
  });

  it('covers the five Bedrock inference actions enumerated in spec §2.2.2', () => {
    const set = renderSet();
    const parsed = set[0].body as any;
    expect(parsed.Statement[0].Action.sort()).toEqual([
      'bedrock:Converse',
      'bedrock:ConverseStream',
      'bedrock:CreateModelInvocationJob',
      'bedrock:InvokeModel',
      'bedrock:InvokeModelWithResponseStream',
    ]);
  });

  it('rejects an empty allow-list at synth time', () => {
    expect(() =>
      buildScpSet({
        allowedModelArns: [],
        approvedRegions: PLATFORM_APPROVED_REGIONS,
        platformGuardrailAdminRoleArn: PLATFORM_GUARDRAIL_ADMIN_ROLE_ARN,
      }),
    ).toThrow(/must not be empty/);
  });
});

describe('SCP-02 enforce Guardrail', () => {
  it('denies Bedrock inference when GuardrailIdentifier is Null', () => {
    const scp02 = renderSet()[1];
    const parsed = scp02.body as any;
    const stmt = parsed.Statement[0];
    expect(stmt.Condition.Null['bedrock:GuardrailIdentifier']).toBe('true');
    expect(stmt.Action).toContain('bedrock:InvokeModel');
    expect(stmt.Action).toContain('bedrock:ConverseStream');
  });
});

describe('SCP-03 AgentCore VPCE enforcement', () => {
  it('uses SSM-resolved approved VPCE list', () => {
    const scp03 = renderSet()[2];
    const parsed = scp03.body as any;
    const stmt = parsed.Statement[0];
    expect(stmt.Action).toEqual(['bedrock-agentcore:*']);
    expect(stmt.Condition.StringNotEquals['aws:SourceVpce']).toContain('ssm:/agenticai/network');
  });
});

describe('SCP-04 Bedrock VPCE enforcement', () => {
  it('includes ApplyGuardrail in its action list per spec §2.2.5', () => {
    const scp04 = renderSet()[3];
    const parsed = scp04.body as any;
    expect(parsed.Statement[0].Action).toContain('bedrock:ApplyGuardrail');
  });
});

describe('SCP-05 Guardrail modification restriction', () => {
  it('pins Guardrail modification to the platform admin role ARN (role + assumed-role forms)', () => {
    const scp05 = renderSet()[4];
    const parsed = scp05.body as any;
    const allow: string[] = parsed.Statement[0].Condition.ArnNotLike['aws:PrincipalArn'];
    expect(allow).toContain(PLATFORM_GUARDRAIL_ADMIN_ROLE_ARN);
    expect(allow).toContain('arn:aws:sts::111111111111:assumed-role/AgenticAI-GuardrailAdmin/*');
  });

  it('rejects non-ARN admin role input', () => {
    expect(() =>
      buildScpSet({
        allowedModelArns: allowedModelArns('us-west-2'),
        approvedRegions: PLATFORM_APPROVED_REGIONS,
        platformGuardrailAdminRoleArn: 'not-an-arn',
      }),
    ).toThrow(/full IAM role ARN/);
  });
});

describe('SCP-06 region restriction', () => {
  it('embeds PLATFORM_APPROVED_REGIONS verbatim', () => {
    const scp06 = renderSet()[5];
    const parsed = scp06.body as any;
    expect(parsed.Statement[0].Condition.StringNotEquals['aws:RequestedRegion'].sort()).toEqual(
      [...PLATFORM_APPROVED_REGIONS].sort(),
    );
  });

  it('NotAction excludes global services so IAM/STS/Orgs etc. still work', () => {
    const scp06 = renderSet()[5];
    const parsed = scp06.body as any;
    const notAction: string[] = parsed.Statement[0].NotAction;
    expect(notAction).toContain('iam:*');
    expect(notAction).toContain('sts:*');
    expect(notAction).toContain('organizations:*');
    expect(notAction).toContain('support:*');
  });

  it('rejects empty approved-region list', () => {
    expect(() =>
      buildScpSet({
        allowedModelArns: allowedModelArns('us-west-2'),
        approvedRegions: [],
        platformGuardrailAdminRoleArn: PLATFORM_GUARDRAIL_ADMIN_ROLE_ARN,
      }),
    ).toThrow(/must not be empty/);
  });
});

describe('SCP-07 deny public AgentCore', () => {
  it('has two statements — one per required parameter (subnets + securityGroups)', () => {
    const scp07 = renderSet()[6];
    const parsed = scp07.body as any;
    expect(parsed.Statement).toHaveLength(2);
    expect(parsed.Statement[0].Condition.Null['bedrock-agentcore:subnets']).toBe('true');
    expect(parsed.Statement[1].Condition.Null['bedrock-agentcore:securityGroups']).toBe('true');
  });

  it('applies to all four Create/Update AgentCore-resource actions', () => {
    const scp07 = renderSet()[6];
    const parsed = scp07.body as any;
    for (const stmt of parsed.Statement) {
      expect(stmt.Action).toEqual(
        expect.arrayContaining([
          'bedrock-agentcore:CreateAgentRuntime',
          'bedrock-agentcore:UpdateAgentRuntime',
          'bedrock-agentcore:CreateBrowser',
          'bedrock-agentcore:CreateCodeInterpreter',
        ]),
      );
    }
  });
});

describe('SCP-08 deny ECR Public', () => {
  it('denies every ecr-public:* action', () => {
    const scp08 = renderSet()[7];
    const parsed = scp08.body as any;
    expect(parsed.Statement[0].Effect).toBe('Deny');
    expect(parsed.Statement[0].Action).toEqual(['ecr-public:*']);
    expect(parsed.Statement[0].Resource).toBe('*');
  });
});

describe('SCP-09 gateway mutation lockdown', () => {
  function getScp09() {
    const set = renderFullSet();
    const scp09 = set.find((s) => s.id === 'scp-09');
    expect(scp09).toBeDefined();
    return scp09!;
  }

  it('emits Deny with ArnNotLike on both role-ARN forms', () => {
    const parsed = getScp09().body as any;
    const stmt = parsed.Statement[0];
    expect(stmt.Effect).toBe('Deny');
    expect(stmt.Condition.ArnNotLike).toBeDefined();
    const arns: string[] = stmt.Condition.ArnNotLike['aws:PrincipalArn'];
    expect(arns).toEqual(
      expect.arrayContaining([
        `arn:aws:iam::${PLATFORM_ACCOUNT_ID}:role/AgenticAI-D03-GatewayAdmin`,
        `arn:aws:sts::${PLATFORM_ACCOUNT_ID}:assumed-role/AgenticAI-D03-GatewayAdmin/*`,
      ]),
    );
  });

  it('includes PrincipalIsAWSService=false guard', () => {
    const parsed = getScp09().body as any;
    const stmt = parsed.Statement[0];
    expect(stmt.Condition.BoolIfExists['aws:PrincipalIsAWSService']).toBe('false');
  });

  it('denies CreateGateway (not just Update/Delete) so rogue unmanaged Gateways are blocked', () => {
    const parsed = getScp09().body as any;
    const actions: string[] = parsed.Statement[0].Action;
    expect(actions).toContain('bedrock-agentcore:CreateGateway');
    expect(actions).toContain('bedrock-agentcore:UpdateGateway');
    expect(actions).toContain('bedrock-agentcore:DeleteGateway');
    expect(actions).toContain('bedrock-agentcore:CreateGatewayTarget');
    expect(actions).toContain('bedrock-agentcore:UpdateGatewayTarget');
    expect(actions).toContain('bedrock-agentcore:DeleteGatewayTarget');
    expect(actions).toContain('bedrock-agentcore:TagResource');
    expect(actions).toContain('bedrock-agentcore:UntagResource');
  });

  it('scopes Resource to arn:aws:bedrock-agentcore:*:*:gateway/*', () => {
    const parsed = getScp09().body as any;
    expect(parsed.Statement[0].Resource).toBe('arn:aws:bedrock-agentcore:*:*:gateway/*');
  });

  it('body stays under the 5000-char soft limit', () => {
    expect(getScp09().bodyJson.length).toBeLessThan(SCP_BODY_SOFT_LIMIT);
    expect(getScp09().bodyJson.length).toBeLessThan(SCP_BODY_HARD_LIMIT);
  });

  it('skipped (with warning) when platformAccountId is omitted', () => {
    warnSpy.mockClear();
    const set = renderSet();
    expect(set.find((s) => s.id === 'scp-09')).toBeUndefined();
    expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining('TODO-PLATFORM-ACCOUNT-ID'));
  });

  it('rejects a non-12-digit platformAccountId', () => {
    expect(() =>
      buildScpSet({
        allowedModelArns: allowedModelArns('us-west-2'),
        approvedRegions: PLATFORM_APPROVED_REGIONS,
        platformGuardrailAdminRoleArn: PLATFORM_GUARDRAIL_ADMIN_ROLE_ARN,
        platformAccountId: 'not-an-account-id',
      }),
    ).toThrow(/12-digit/);
  });
});

describe('SCP-10 tool-invoke allow-list', () => {
  function getScp10() {
    const set = renderFullSet();
    const scp10 = set.find((s) => s.id === 'scp-10');
    expect(scp10).toBeDefined();
    return scp10!;
  }

  it('emits Deny with NotResource listing exactly the supplied ARNs', () => {
    const parsed = getScp10().body as any;
    const stmt = parsed.Statement[0];
    expect(stmt.Effect).toBe('Deny');
    expect(stmt.NotResource).toEqual([...ALLOWED_TOOL_TARGET_ARNS]);
    expect(stmt.Action).toEqual(['lambda:InvokeFunction', 'lambda:InvokeAsync']);
  });

  it('narrows Condition to runtime-role principals via ArnLike', () => {
    const parsed = getScp10().body as any;
    const stmt = parsed.Statement[0];
    const arnLike: string[] = stmt.Condition.ArnLike['aws:PrincipalArn'];
    expect(arnLike).toEqual([
      'arn:aws:iam::*:role/AgenticAI-D03-*-runtime',
      'arn:aws:sts::*:assumed-role/AgenticAI-D03-*-runtime/*',
    ]);
    expect(stmt.Condition.BoolIfExists['aws:PrincipalIsAWSService']).toBe('false');
  });

  it('body stays under the 5000-char soft limit', () => {
    expect(getScp10().bodyJson.length).toBeLessThan(SCP_BODY_SOFT_LIMIT);
    expect(getScp10().bodyJson.length).toBeLessThan(SCP_BODY_HARD_LIMIT);
  });

  it('is a no-op (skipped with warning) when allowedToolTargetArns is empty', () => {
    warnSpy.mockClear();
    const set = buildScpSet({
      allowedModelArns: allowedModelArns('us-west-2'),
      approvedRegions: PLATFORM_APPROVED_REGIONS,
      platformGuardrailAdminRoleArn: PLATFORM_GUARDRAIL_ADMIN_ROLE_ARN,
      platformAccountId: PLATFORM_ACCOUNT_ID,
      allowedToolTargetArns: [],
    });
    expect(set.find((s) => s.id === 'scp-10')).toBeUndefined();
    expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining('TODO-TOOL-CATALOGUE'));
  });

  it('is a no-op (skipped with warning) when allowedToolTargetArns is omitted', () => {
    warnSpy.mockClear();
    const set = renderSet();
    expect(set.find((s) => s.id === 'scp-10')).toBeUndefined();
    expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining('TODO-TOOL-CATALOGUE'));
  });
});

describe('SCP-11 registry mutation lockdown', () => {
  function getScp11() {
    const set = buildScpSet({
      allowedModelArns: allowedModelArns('us-west-2'),
      approvedRegions: PLATFORM_APPROVED_REGIONS,
      platformGuardrailAdminRoleArn: PLATFORM_GUARDRAIL_ADMIN_ROLE_ARN,
      platformAccountId: PLATFORM_ACCOUNT_ID,
      allowedToolTargetArns: ALLOWED_TOOL_TARGET_ARNS,
      enableRegistryLockdown: true,
    });
    const scp11 = set.find((s) => s.id === 'scp-11');
    expect(scp11).toBeDefined();
    return scp11!;
  }

  it('emits Deny with ArnNotLike on both RegistryAdmin role-ARN forms', () => {
    const parsed = getScp11().body as any;
    const stmt = parsed.Statement[0];
    expect(stmt.Effect).toBe('Deny');
    const arns: string[] = stmt.Condition.ArnNotLike['aws:PrincipalArn'];
    expect(arns).toEqual(
      expect.arrayContaining([
        `arn:aws:iam::${PLATFORM_ACCOUNT_ID}:role/AgenticAI-RegistryAdmin`,
        `arn:aws:sts::${PLATFORM_ACCOUNT_ID}:assumed-role/AgenticAI-RegistryAdmin/*`,
      ]),
    );
  });

  it('denies the four registry-mutation actions and only those', () => {
    const parsed = getScp11().body as any;
    const actions: string[] = parsed.Statement[0].Action;
    expect(actions.sort()).toEqual([
      'bedrock-agentcore:CreateRegistry',
      'bedrock-agentcore:DeleteRegistry',
      'bedrock-agentcore:UpdateRegistry',
      'bedrock-agentcore:UpdateRegistryRecordStatus',
    ]);
  });

  it('does NOT deny CreateRegistryRecord or data-plane Search/Invoke', () => {
    const parsed = getScp11().body as any;
    const actions: string[] = parsed.Statement[0].Action;
    expect(actions).not.toContain('bedrock-agentcore:CreateRegistryRecord');
    expect(actions).not.toContain('bedrock-agentcore:UpdateRegistryRecord');
    expect(actions).not.toContain('bedrock-agentcore:SearchRegistryRecords');
    expect(actions).not.toContain('bedrock-agentcore:InvokeRegistryMcp');
  });

  it('scopes Resource to arn:aws:bedrock-agentcore:*:*:registry/*', () => {
    const parsed = getScp11().body as any;
    expect(parsed.Statement[0].Resource).toBe('arn:aws:bedrock-agentcore:*:*:registry/*');
  });

  it('includes PrincipalIsAWSService=false guard', () => {
    const parsed = getScp11().body as any;
    expect(parsed.Statement[0].Condition.BoolIfExists['aws:PrincipalIsAWSService']).toBe('false');
  });

  it('skipped (with warning) when enableRegistryLockdown=true but platformAccountId is omitted', () => {
    warnSpy.mockClear();
    const set = buildScpSet({
      allowedModelArns: allowedModelArns('us-west-2'),
      approvedRegions: PLATFORM_APPROVED_REGIONS,
      platformGuardrailAdminRoleArn: PLATFORM_GUARDRAIL_ADMIN_ROLE_ARN,
      enableRegistryLockdown: true,
    });
    expect(set.find((s) => s.id === 'scp-11')).toBeUndefined();
    expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining('TODO-PLATFORM-ACCOUNT-ID'));
  });

  it('not emitted when enableRegistryLockdown is unset', () => {
    const set = renderFullSet();
    expect(set.find((s) => s.id === 'scp-11')).toBeUndefined();
  });

  it('body stays under the 5000-char soft limit', () => {
    expect(getScp11().bodyJson.length).toBeLessThan(SCP_BODY_SOFT_LIMIT);
    expect(getScp11().bodyJson.length).toBeLessThan(SCP_BODY_HARD_LIMIT);
  });
});

describe('SCP-12 developer permission-set platform-tag deny', () => {
  function getScp12() {
    const set = buildScpSet({
      allowedModelArns: allowedModelArns('us-west-2'),
      approvedRegions: PLATFORM_APPROVED_REGIONS,
      platformGuardrailAdminRoleArn: PLATFORM_GUARDRAIL_ADMIN_ROLE_ARN,
      platformAccountId: PLATFORM_ACCOUNT_ID,
      allowedToolTargetArns: ALLOWED_TOOL_TARGET_ARNS,
      enableDeveloperPlatformTagDeny: true,
    });
    const scp12 = set.find((s) => s.id === 'scp-12');
    expect(scp12).toBeDefined();
    return scp12!;
  }

  it('fires only when ResourceTag agenticai:owner=platform', () => {
    const parsed = getScp12().body as any;
    const stmt = parsed.Statement[0];
    expect(stmt.Condition.StringEquals['aws:ResourceTag/agenticai:owner']).toBe('platform');
  });

  it('narrows Principal to AWSReservedSSO_AgenticAI-WS-Dev-* (role + assumed-role forms)', () => {
    const parsed = getScp12().body as any;
    const arnLike: string[] = parsed.Statement[0].Condition.ArnLike['aws:PrincipalArn'];
    expect(arnLike).toContain(
      'arn:aws:iam::*:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_AgenticAI-WS-Dev-*',
    );
    expect(arnLike).toContain(
      'arn:aws:sts::*:assumed-role/AWSReservedSSO_AgenticAI-WS-Dev-*/*',
    );
  });

  it('includes PrincipalIsAWSService=false guard', () => {
    const parsed = getScp12().body as any;
    expect(parsed.Statement[0].Condition.BoolIfExists['aws:PrincipalIsAWSService']).toBe('false');
  });

  it('denies lifecycle write actions across bedrock-agentcore + iam + lambda + kms', () => {
    const parsed = getScp12().body as any;
    const actions: string[] = parsed.Statement[0].Action;
    for (const expected of [
      'bedrock-agentcore:Update*',
      'bedrock-agentcore:Delete*',
      'iam:Update*',
      'iam:Delete*',
      'lambda:Update*',
      'lambda:Delete*',
      'kms:ScheduleKeyDeletion',
    ]) {
      expect(actions).toContain(expected);
    }
  });

  it('not emitted when enableDeveloperPlatformTagDeny is unset', () => {
    const set = renderFullSet();
    expect(set.find((s) => s.id === 'scp-12')).toBeUndefined();
  });

  it('body stays under the 5000-char soft limit', () => {
    expect(getScp12().bodyJson.length).toBeLessThan(SCP_BODY_SOFT_LIMIT);
    expect(getScp12().bodyJson.length).toBeLessThan(SCP_BODY_HARD_LIMIT);
  });
});
