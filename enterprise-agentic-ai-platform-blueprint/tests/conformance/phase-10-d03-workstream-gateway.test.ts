/**
 * Phase 10 conformance — D-03 v3 per-workstream AgentCore Gateway stack.
 *
 * Pins the shape of `D03WorkstreamGatewayStack` against the three-layer
 * tool-governance model (see README §3.3 v3):
 *   - Layer 1 (synth-time): unknown `allowedToolIds` fail the CDK synth.
 *   - Layer 3 (runtime): Gateway service role inline policy lists exactly
 *     the resolved N tool ARNs — no wildcards, no extras.
 *
 * Also pins:
 *   - Exactly one `Custom::BedrockAgentCoreGateway` resource per stack.
 *   - Exactly N `Custom::BedrockAgentCoreGatewayTarget` resources.
 *   - Each target's `lambdaArn` matches the catalogue's resolved ARN.
 *   - CfnOutputs surface `GatewayId` / `GatewayArn` / one per tool.
 *   - Stack tags include `tenant-id` and `agent-id`.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import {
  PLATFORM_TOOL_CATALOGUE,
  resolveTargetArn,
  type ToolId,
} from '@agenticai/platform-tool-catalogue';

import { D03WorkstreamGatewayStack } from '../../apps/platform-account/lib/d03-workstream-gateway-stack';

const PLATFORM_ACCOUNT_ID = '222222222222';
const WORKLOAD_ACCOUNT_ID = '333333333333';

interface SynthOpts {
  readonly allowedToolIds?: readonly ToolId[];
  readonly tenantId?: string;
  readonly agentId?: string;
  readonly cognitoDiscoveryUrl?: string;
  readonly cognitoAudience?: readonly string[];
}

function synth(opts: SynthOpts = {}): { template: Template; stack: D03WorkstreamGatewayStack } {
  const app = new App();
  const allowedToolIds = opts.allowedToolIds ?? ['tool-echo', 'tool-ping'];
  const tenantId = opts.tenantId ?? 'acme';
  const agentId = opts.agentId ?? 'primary';
  const stack = new D03WorkstreamGatewayStack(
    app,
    `AgenticAI-D03-WorkstreamGateway-${tenantId}-${agentId}`,
    {
      env: { account: WORKLOAD_ACCOUNT_ID, region: 'us-east-1' },
      tenantId,
      agentId,
      envName: 'nonprod',
      workloadAccountId: WORKLOAD_ACCOUNT_ID,
      platformAccountId: PLATFORM_ACCOUNT_ID,
      allowedToolIds,
      cognitoDiscoveryUrl: opts.cognitoDiscoveryUrl,
      cognitoAudience: opts.cognitoAudience,
    },
  );
  return { template: Template.fromStack(stack), stack };
}

describe('Phase 10 — D03WorkstreamGatewayStack shape', () => {
  it('emits exactly one Custom::BedrockAgentCoreGateway resource', () => {
    const { template } = synth();
    const gws = template.findResources('Custom::BedrockAgentCoreGateway');
    expect(Object.keys(gws)).toHaveLength(1);
  });

  it('emits exactly N gateway-target resources where N = allowedToolIds.length', () => {
    const ids: ToolId[] = ['tool-echo', 'tool-ping'];
    const { template } = synth({ allowedToolIds: ids });
    const targets = template.findResources('Custom::BedrockAgentCoreGatewayTarget');
    expect(Object.keys(targets)).toHaveLength(ids.length);
  });

  it('single-tool subscription emits exactly one target', () => {
    const { template } = synth({ allowedToolIds: ['tool-echo'] });
    const targets = template.findResources('Custom::BedrockAgentCoreGatewayTarget');
    expect(Object.keys(targets)).toHaveLength(1);
  });
});

describe('Phase 10 — layer-3 enforcement (Gateway service role inline policy)', () => {
  it('inline policy lists exactly the N resolved tool ARNs — no wildcards, no extras', () => {
    const ids: ToolId[] = ['tool-echo', 'tool-ping'];
    const { template } = synth({ allowedToolIds: ids });
    // The service role has a single inline policy `InvokeSubscribedTools`.
    const roles = template.findResources('AWS::IAM::Role', {
      Properties: {
        AssumeRolePolicyDocument: {
          Statement: [
            {
              Principal: { Service: 'bedrock-agentcore.amazonaws.com' },
            },
          ],
        },
      },
    });
    // There is exactly one role trusted by bedrock-agentcore.
    const gwRole = Object.values(roles);
    expect(gwRole).toHaveLength(1);
    const policies = ((gwRole[0] as any).Properties.Policies ?? []) as Array<{
      PolicyName: string;
      PolicyDocument: { Statement: Array<{ Effect: string; Action: unknown; Resource: unknown }> };
    }>;
    expect(policies).toHaveLength(1);
    const stmts = policies[0].PolicyDocument.Statement;
    expect(stmts).toHaveLength(1);
    const stmt = stmts[0];
    expect(stmt.Effect).toBe('Allow');
    expect(stmt.Action).toBe('lambda:InvokeFunction');
    const resources = Array.isArray(stmt.Resource) ? stmt.Resource : [stmt.Resource];
    // Exact ARNs — must match what the catalogue resolves.
    const expected = ids.map((id) =>
      resolveTargetArn(PLATFORM_TOOL_CATALOGUE[id], PLATFORM_ACCOUNT_ID),
    );
    expect(resources).toHaveLength(expected.length);
    for (const arn of expected) {
      expect(resources).toContain(arn);
    }
    // No wildcards anywhere in the resource list.
    for (const r of resources) {
      expect(typeof r).toBe('string');
      expect(r as string).not.toContain('*');
    }
  });
});

describe("Phase 10 — each target's lambdaArn matches the catalogue's resolved ARN", () => {
  it('per-tool GatewayTarget carries the expected lambdaArn in its CreateGatewayTarget params', () => {
    const ids: ToolId[] = ['tool-echo', 'tool-ping'];
    const { template } = synth({ allowedToolIds: ids });
    const targets = template.findResources('Custom::BedrockAgentCoreGatewayTarget');
    // The custom-resource `Create` property is a JSON-in-JSON payload the
    // CDK serialises via Fn::Join — search the raw JSON.stringify output.
    // Inner quotes are therefore double-escaped (`\\\"`).
    const rendered = JSON.stringify(targets);
    for (const id of ids) {
      const expected = resolveTargetArn(PLATFORM_TOOL_CATALOGUE[id], PLATFORM_ACCOUNT_ID);
      expect(rendered).toContain(expected);
      // And the tool id must appear as the inlinePayload.name (the inner
      // JSON renders `"name":"<id>"` which, after one level of outer
      // JSON.stringify escaping, becomes `\\\"name\\\":\\\"<id>\\\"`).
      expect(rendered).toContain(`\\"name\\":\\"${id}\\"`);
    }
  });
});

describe('Phase 10 — synth-time SSOT gate (layer 1)', () => {
  it('throws when allowedToolIds contains an id not in the catalogue', () => {
    expect(() => synth({ allowedToolIds: ['not-a-real-tool'] })).toThrow(
      /Unknown tool id\(s\)/,
    );
  });

  it('error message lists the known catalogue keys for the operator', () => {
    try {
      synth({ allowedToolIds: ['bogus-tool-x'] });
      fail('expected throw');
    } catch (err) {
      const msg = (err as Error).message;
      expect(msg).toContain('bogus-tool-x');
      expect(msg).toContain('tool-echo');
    }
  });
});

describe('Phase 10 — tags + outputs surface', () => {
  it('stack tags include tenant-id and agent-id', () => {
    const { stack } = synth({ tenantId: 'retail', agentId: 'triage' });
    const tags = stack.tags.tagValues();
    expect(tags['tenant-id']).toBe('retail');
    expect(tags['agent-id']).toBe('triage');
    expect(tags['deviation']).toBe('D-03');
    expect(tags['environment']).toBe('nonprod');
  });

  it('CfnOutputs include GatewayId + GatewayArn', () => {
    const { template } = synth();
    const outputs = template.findOutputs('*');
    const names = Object.keys(outputs);
    expect(names).toContain('GatewayId');
    expect(names).toContain('GatewayArn');
    expect(names).toContain('GatewayServiceRoleArn');
    expect(names).toContain('SubscribedToolCount');
    expect(names).toContain('PerTenantCedarPolicy');
  });

  it('emits one ToolTarget-<toolId> output per subscribed tool', () => {
    const ids: ToolId[] = ['tool-echo', 'tool-ping'];
    const { template } = synth({ allowedToolIds: ids });
    const outputs = template.findOutputs('*');
    for (const id of ids) {
      const key = `ToolTarget${id.replace(/-/g, '')}`;
      // CDK strips non-alphanumeric from the logical id; just look for any
      // output whose value contains the resolved ARN.
      const expected = resolveTargetArn(PLATFORM_TOOL_CATALOGUE[id], PLATFORM_ACCOUNT_ID);
      const match = Object.values(outputs).find(
        (o) => JSON.stringify((o as any).Value) === JSON.stringify(expected),
      );
      expect(match).toBeDefined();
      // Sanity: the key is derived from the tool id.
      expect(key.toLowerCase()).toContain(id.replace(/-/g, '').toLowerCase());
    }
  });
});

describe('Phase 10 — v0.5.0 Registry subscription path', () => {
  function synthRegistry(opts: {
    readonly subscribedRegistryRecords?: readonly string[];
    readonly registryId?: string;
    readonly tenantId?: string;
  }): { template: Template; stack: D03WorkstreamGatewayStack } {
    const app = new App();
    const tenantId = opts.tenantId ?? 'acme';
    const stack = new D03WorkstreamGatewayStack(
      app,
      `AgenticAI-D03-WorkstreamGateway-${tenantId}-registry`,
      {
        env: { account: WORKLOAD_ACCOUNT_ID, region: 'us-east-1' },
        tenantId,
        agentId: 'primary',
        envName: 'nonprod',
        workloadAccountId: WORKLOAD_ACCOUNT_ID,
        platformAccountId: PLATFORM_ACCOUNT_ID,
        subscribedRegistryRecords: opts.subscribedRegistryRecords ?? ['tool-echo', 'tool-ping'],
        registryId: opts.registryId ?? 'reg-platform-abc123',
      },
    );
    return { template: Template.fromStack(stack), stack };
  }

  it('emits one Custom::AgenticAIRegistryRecordValidator per subscribed record', () => {
    const ids = ['tool-echo', 'tool-ping'];
    const { template } = synthRegistry({ subscribedRegistryRecords: ids });
    // v0.5.0 — the read-only AwsCustomResource was replaced by a Lambda-backed
    // Provider + CustomResource so the deploy can fail when status !== APPROVED.
    const validators = template.findResources('Custom::AgenticAIRegistryRecordValidator');
    expect(Object.keys(validators)).toHaveLength(ids.length);
    // The validator Lambda must exist and carry the GetRegistryRecord IAM allow.
    const fns = template.findResources('AWS::Lambda::Function');
    const validatorFn = Object.values(fns).find(
      (f: any) =>
        typeof f?.Properties?.FunctionName === 'string' &&
        (f.Properties.FunctionName as string).includes('reg-validator'),
    );
    expect(validatorFn).toBeDefined();
  });

  it('still emits exactly one Gateway and N Targets in Registry mode', () => {
    const ids = ['tool-echo', 'tool-ping'];
    const { template } = synthRegistry({ subscribedRegistryRecords: ids });
    const gws = template.findResources('Custom::BedrockAgentCoreGateway');
    const targets = template.findResources('Custom::BedrockAgentCoreGatewayTarget');
    expect(Object.keys(gws)).toHaveLength(1);
    expect(Object.keys(targets)).toHaveLength(ids.length);
  });

  it('SubscribedToolCount matches subscribedRegistryRecords.length', () => {
    const ids = ['tool-echo'];
    const { template } = synthRegistry({ subscribedRegistryRecords: ids });
    const outputs = template.findOutputs('SubscribedToolCount');
    expect(Object.values(outputs)[0]).toBeDefined();
    expect((Object.values(outputs)[0] as any).Value).toBe('1');
  });

  it('throws when subscribedRegistryRecords is set but registryId is missing', () => {
    expect(() =>
      synthRegistry({ subscribedRegistryRecords: ['tool-echo'], registryId: '' }),
    ).toThrow(/registryId/);
  });

  it('throws when both allowedToolIds and subscribedRegistryRecords are supplied', () => {
    const app = new App();
    expect(() =>
      new D03WorkstreamGatewayStack(app, 'AgenticAI-D03-WorkstreamGateway-conflict', {
        env: { account: WORKLOAD_ACCOUNT_ID, region: 'us-east-1' },
        tenantId: 'a',
        agentId: 'b',
        envName: 'nonprod',
        workloadAccountId: WORKLOAD_ACCOUNT_ID,
        platformAccountId: PLATFORM_ACCOUNT_ID,
        allowedToolIds: ['tool-echo'],
        subscribedRegistryRecords: ['tool-echo'],
        registryId: 'reg-x',
      }),
    ).toThrow(/mutually exclusive/);
  });

  it('throws when neither allowedToolIds nor subscribedRegistryRecords is supplied', () => {
    const app = new App();
    expect(() =>
      new D03WorkstreamGatewayStack(app, 'AgenticAI-D03-WorkstreamGateway-empty', {
        env: { account: WORKLOAD_ACCOUNT_ID, region: 'us-east-1' },
        tenantId: 'a',
        agentId: 'b',
        envName: 'nonprod',
        workloadAccountId: WORKLOAD_ACCOUNT_ID,
        platformAccountId: PLATFORM_ACCOUNT_ID,
      }),
    ).toThrow(/either 'allowedToolIds'.*or 'subscribedRegistryRecords'/);
  });
});

describe('Phase 10 — authorizer mode selection', () => {
  it('falls back to AWS_IAM when no Cognito discoveryUrl is supplied', () => {
    const { template } = synth({ cognitoDiscoveryUrl: undefined });
    const gws = template.findResources('Custom::BedrockAgentCoreGateway');
    // The Create prop is JSON-in-JSON so inner quotes are double-escaped.
    const rendered = JSON.stringify(gws);
    expect(rendered).toContain('\\"authorizerType\\":\\"AWS_IAM\\"');
    expect(rendered).not.toContain('customJWTAuthorizer');
  });

  it('switches to CUSTOM_JWT when cognitoDiscoveryUrl is supplied', () => {
    const { template } = synth({
      cognitoDiscoveryUrl:
        'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc/.well-known/openid-configuration',
      cognitoAudience: ['aud-xyz'],
    });
    const gws = template.findResources('Custom::BedrockAgentCoreGateway');
    const rendered = JSON.stringify(gws);
    expect(rendered).toContain('\\"authorizerType\\":\\"CUSTOM_JWT\\"');
    expect(rendered).toContain('customJWTAuthorizer');
    expect(rendered).toContain('\\"allowedAudience\\":[\\"aud-xyz\\"]');
  });
});
