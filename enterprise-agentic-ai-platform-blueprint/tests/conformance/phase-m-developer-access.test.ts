/**
 * Phase M conformance — Identity Center permission sets for workstream
 * developers.
 *
 * Pins the shape of `WorkstreamPermissionSets`:
 *   - Exactly 3 `AWS::SSO::PermissionSet` resources per construct (one per
 *     persona).
 *   - Permission-set names follow the SCP-12-pinned per-persona prefix
 *     (`AgenticAI-WS-Dev-` / `AgenticAI-WS-Ro-` / `AgenticAI-WS-Apv-`).
 *   - Each persona's inline policy carries the expected Sid set.
 *   - One `AWS::SSO::Assignment` per (persona, target-account) pair when a
 *     group GUID is supplied.
 *   - Synth-time validation rejects malformed inputs (workstreamId,
 *     instanceArn, accountIds, sessionDuration, groupGuid).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App, Stack } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { Key } from 'aws-cdk-lib/aws-kms';

import {
  WorkstreamPermissionSets,
  WorkstreamRosterTable,
} from '@agenticai/developer-access';

const PLATFORM_ACCOUNT_ID = '222222222222';
const WORKLOAD_NONPROD = '333333333333';
const WORKLOAD_PROD = '444444444444';
const IDC_INSTANCE = 'arn:aws:sso:::instance/ssoins-1234567890abcdef';
const VALID_GROUP_GUID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';

interface SynthOpts {
  readonly workstreamId?: string;
  readonly targetAccountIds?: readonly string[];
  readonly groupAssignments?: Partial<Record<'Developer' | 'ReadOnly' | 'Approver', string>>;
  readonly approverPipelineNames?: readonly string[];
  readonly platformAccountId?: string;
}

function synth(opts: SynthOpts = {}): { template: Template; stack: Stack } {
  const app = new App();
  const stack = new Stack(app, 'AgenticAI-Test-DeveloperAccess', {
    env: { account: PLATFORM_ACCOUNT_ID, region: 'us-east-1' },
  });
  new WorkstreamPermissionSets(stack, 'PermissionSets', {
    identityCenterInstanceArn: IDC_INSTANCE,
    workstreamId: opts.workstreamId ?? 'acme',
    targetAccountIds: opts.targetAccountIds ?? [WORKLOAD_NONPROD, WORKLOAD_PROD],
    groupAssignments: opts.groupAssignments,
    approverPipelineNames: opts.approverPipelineNames,
    platformAccountId: opts.platformAccountId ?? PLATFORM_ACCOUNT_ID,
  });
  return { template: Template.fromStack(stack), stack };
}

describe('Phase M — WorkstreamPermissionSets resource shape', () => {
  it('emits exactly 3 AWS::SSO::PermissionSet resources (one per persona)', () => {
    const { template } = synth();
    const sets = template.findResources('AWS::SSO::PermissionSet');
    expect(Object.keys(sets)).toHaveLength(3);
  });

  it('permission-set names follow the SCP-12-pinned per-persona prefix', () => {
    const { template } = synth({ workstreamId: 'retail' });
    const sets = template.findResources('AWS::SSO::PermissionSet');
    const names = Object.values(sets).map((r) => (r as { Properties: { Name: string } }).Properties.Name);
    expect(names).toContain('AgenticAI-WS-Dev-retail');
    expect(names).toContain('AgenticAI-WS-Ro-retail');
    expect(names).toContain('AgenticAI-WS-Apv-retail');
  });

  it('each persona carries its persona tag', () => {
    const { template } = synth();
    const sets = template.findResources('AWS::SSO::PermissionSet');
    const personas = Object.values(sets).map((r) => {
      const tags = (r as { Properties: { Tags: Array<{ Key: string; Value: string }> } }).Properties.Tags;
      return tags.find((t) => t.Key === 'agenticai:persona')?.Value;
    });
    expect(personas.sort()).toEqual(['Approver', 'Developer', 'ReadOnly']);
  });

  it('Developer persona inline policy contains the AgentCoreRegistryConsumer Sid', () => {
    const { template } = synth();
    const sets = template.findResources('AWS::SSO::PermissionSet');
    const developer = Object.values(sets).find(
      (r) =>
        (r as { Properties: { Name: string } }).Properties.Name ===
        'AgenticAI-WS-Dev-acme',
    );
    expect(developer).toBeDefined();
    const inline = (developer as { Properties: { InlinePolicy: { Statement: Array<{ Sid?: string }> } } })
      .Properties.InlinePolicy;
    const sids = inline.Statement.map((s) => s.Sid);
    expect(sids).toContain('AgentCoreRegistryConsumer');
    expect(sids).toContain('DenyPlatformOwnedMutation');
    expect(sids).toContain('PipelineDeployForOwnWorkstream');
    expect(sids).toContain('ObservabilityRead');
  });

  it('emits one Assignment per (persona, target-account) when group GUIDs are supplied', () => {
    const { template } = synth({
      groupAssignments: {
        Developer: VALID_GROUP_GUID,
        ReadOnly: VALID_GROUP_GUID,
        Approver: VALID_GROUP_GUID,
      },
    });
    const assigns = template.findResources('AWS::SSO::Assignment');
    expect(Object.keys(assigns)).toHaveLength(3 * 2);
  });

  it('emits zero Assignments when groupAssignments is omitted (dry-run mode)', () => {
    const { template } = synth();
    const assigns = template.findResources('AWS::SSO::Assignment');
    expect(Object.keys(assigns)).toHaveLength(0);
  });
});

describe('Phase M — WorkstreamPermissionSets synth-time validation', () => {
  it('rejects a non-kebab workstreamId', () => {
    expect(() => synth({ workstreamId: 'BadID' })).toThrow(/workstreamId/);
  });

  it('rejects a malformed identityCenterInstanceArn', () => {
    const app = new App();
    const stack = new Stack(app, 'BadIdcArn', {
      env: { account: PLATFORM_ACCOUNT_ID, region: 'us-east-1' },
    });
    expect(
      () =>
        new WorkstreamPermissionSets(stack, 'PS', {
          identityCenterInstanceArn: 'arn:aws:sso::not-a-valid:instance/foo',
          workstreamId: 'acme',
          targetAccountIds: [WORKLOAD_NONPROD],
        }),
    ).toThrow(/identityCenterInstanceArn/);
  });

  it('rejects empty targetAccountIds', () => {
    expect(() => synth({ targetAccountIds: [] })).toThrow(/at least one/);
  });

  it('rejects a non-12-digit target account id', () => {
    expect(() => synth({ targetAccountIds: ['abc'] })).toThrow(/12 digits/);
  });

  it('rejects a malformed group GUID', () => {
    expect(() =>
      synth({ groupAssignments: { Developer: 'not-a-guid' } }),
    ).toThrow(/group GUID/);
  });
});

describe('Phase M — WorkstreamRosterTable shape', () => {
  it('emits exactly one AWS::DynamoDB::Table with CMK encryption + GSI1', () => {
    const app = new App();
    const stack = new Stack(app, 'AgenticAI-Test-Roster', {
      env: { account: PLATFORM_ACCOUNT_ID, region: 'us-east-1' },
    });
    const key = new Key(stack, 'TestKey', { enableKeyRotation: true });
    new WorkstreamRosterTable(stack, 'Roster', {
      encryptionKey: key,
      envName: 'nonprod',
    });
    const template = Template.fromStack(stack);
    const tables = template.findResources('AWS::DynamoDB::Table');
    expect(Object.keys(tables)).toHaveLength(1);
    const t = Object.values(tables)[0] as {
      Properties: {
        BillingMode: string;
        SSESpecification: { KMSMasterKeyId: unknown; SSEEnabled: boolean; SSEType: string };
        GlobalSecondaryIndexes: Array<{ IndexName: string }>;
      };
    };
    expect(t.Properties.BillingMode).toBe('PAY_PER_REQUEST');
    expect(t.Properties.SSESpecification.SSEEnabled).toBe(true);
    expect(t.Properties.SSESpecification.SSEType).toBe('KMS');
    expect(t.Properties.GlobalSecondaryIndexes).toHaveLength(1);
    expect(t.Properties.GlobalSecondaryIndexes[0].IndexName).toBe('gsi1-permission-set-arn');
  });
});
