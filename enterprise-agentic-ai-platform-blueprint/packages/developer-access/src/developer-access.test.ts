/**
 * Unit tests for @agenticai/developer-access.
 *
 * Pins the inline policy renderer for the three personas + the construct's
 * synth-time validation. Construct-level CFN shape is exercised by
 * `tests/conformance/phase-m-developer-access.test.ts`.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { renderInlinePolicy } from './workstream-permission-sets';

interface PolicyDoc {
  readonly Version: string;
  readonly Statement: ReadonlyArray<{
    readonly Sid?: string;
    readonly Effect: 'Allow' | 'Deny';
    readonly Action: string | readonly string[];
    readonly Resource?: string | readonly string[];
    readonly Condition?: Record<string, unknown>;
  }>;
}

function asPolicy(p: Record<string, unknown>): PolicyDoc {
  return p as unknown as PolicyDoc;
}

describe('renderInlinePolicy — Developer', () => {
  it('grants Registry consumer permissions scoped to the platform-account registry ARN when supplied', () => {
    const p = asPolicy(renderInlinePolicy('Developer', { platformAccountId: '222222222222' }));
    const consumerStmt = p.Statement.find((s) => s.Sid === 'AgentCoreRegistryConsumer');
    expect(consumerStmt).toBeDefined();
    const resources = Array.isArray(consumerStmt!.Resource)
      ? consumerStmt!.Resource
      : [consumerStmt!.Resource ?? ''];
    expect(resources).toContain('arn:aws:bedrock-agentcore:*:222222222222:registry/*');
    expect(resources).toContain('arn:aws:bedrock-agentcore:*:222222222222:registry/*/record/*');
  });

  it('falls back to a wildcard account scope when platformAccountId is not supplied', () => {
    const p = asPolicy(renderInlinePolicy('Developer'));
    const consumerStmt = p.Statement.find((s) => s.Sid === 'AgentCoreRegistryConsumer');
    const resources = Array.isArray(consumerStmt!.Resource)
      ? consumerStmt!.Resource
      : [consumerStmt!.Resource ?? ''];
    expect(resources).toContain('arn:aws:bedrock-agentcore:*:*:registry/*');
  });

  it('emits a Deny statement guarding agenticai:owner=platform tagged resources (defense-in-depth for SCP-12)', () => {
    const p = asPolicy(renderInlinePolicy('Developer'));
    const denyStmt = p.Statement.find((s) => s.Sid === 'DenyPlatformOwnedMutation');
    expect(denyStmt).toBeDefined();
    expect(denyStmt!.Effect).toBe('Deny');
    expect(denyStmt!.Condition).toEqual({
      StringEquals: { 'aws:ResourceTag/agenticai:owner': 'platform' },
    });
    const actions = Array.isArray(denyStmt!.Action) ? denyStmt!.Action : [denyStmt!.Action];
    expect(actions).toContain('bedrock-agentcore:Update*');
    expect(actions).toContain('bedrock-agentcore:Delete*');
    expect(actions).toContain('iam:Update*');
    expect(actions).toContain('lambda:Update*');
    expect(actions).toContain('kms:ScheduleKeyDeletion');
    expect(actions).toContain('organizations:*');
  });

  it('grants pipeline deploy actions (CodePipeline + CodeBuild + CodeCommit) for the workload pipeline', () => {
    const p = asPolicy(renderInlinePolicy('Developer', { workstreamId: 'acme' }));
    const pipeStmt = p.Statement.find((s) => s.Sid === 'PipelineDeployForOwnWorkstream');
    const repoStmt = p.Statement.find((s) => s.Sid === 'RepoAccessForOwnWorkstream');
    const buildStmt = p.Statement.find((s) => s.Sid === 'BuildForOwnWorkstream');
    expect(pipeStmt).toBeDefined();
    expect(repoStmt).toBeDefined();
    expect(buildStmt).toBeDefined();
    const pipeActions = Array.isArray(pipeStmt!.Action) ? pipeStmt!.Action : [pipeStmt!.Action];
    const repoActions = Array.isArray(repoStmt!.Action) ? repoStmt!.Action : [repoStmt!.Action];
    const buildActions = Array.isArray(buildStmt!.Action) ? buildStmt!.Action : [buildStmt!.Action];
    expect(pipeActions).toContain('codepipeline:StartPipelineExecution');
    expect(buildActions).toContain('codebuild:StartBuild');
    expect(repoActions).toContain('codecommit:GitPush');
  });

  // Holmes CSR: pipeline write grants must be scoped to the workstream, not '*'.
  it('scopes pipeline/repo/build write grants to the workstream id, never "*"', () => {
    const p = asPolicy(renderInlinePolicy('Developer', { workstreamId: 'acme' }));
    for (const sid of ['PipelineDeployForOwnWorkstream', 'RepoAccessForOwnWorkstream', 'BuildForOwnWorkstream']) {
      const stmt = p.Statement.find((s) => s.Sid === sid);
      expect(stmt).toBeDefined();
      const resources = Array.isArray(stmt!.Resource) ? stmt!.Resource : [stmt!.Resource ?? ''];
      expect(resources.length).toBeGreaterThan(0);
      for (const r of resources) {
        expect(r).not.toBe('*');
        expect(r).toContain('acme');
      }
    }
  });

  it('grants observability read across CloudWatch + X-Ray + Logs', () => {
    const p = asPolicy(renderInlinePolicy('Developer'));
    const obsStmt = p.Statement.find((s) => s.Sid === 'ObservabilityRead');
    expect(obsStmt).toBeDefined();
    const actions = Array.isArray(obsStmt!.Action) ? obsStmt!.Action : [obsStmt!.Action];
    expect(actions).toContain('xray:Get*');
    expect(actions).toContain('cloudwatch:Get*');
    expect(actions).toContain('logs:FilterLogEvents');
  });
});

describe('renderInlinePolicy — ReadOnly', () => {
  it('grants observability + Registry read but NOT InvokeRegistryMcp', () => {
    const p = asPolicy(renderInlinePolicy('ReadOnly', { platformAccountId: '222222222222' }));
    const consumerStmt = p.Statement.find((s) => s.Sid === 'AgentCoreRegistryConsumerReadOnly');
    expect(consumerStmt).toBeDefined();
    const actions = Array.isArray(consumerStmt!.Action) ? consumerStmt!.Action : [consumerStmt!.Action];
    expect(actions).toContain('bedrock-agentcore:SearchRegistryRecords');
    expect(actions).toContain('bedrock-agentcore:GetRegistryRecord');
    expect(actions).not.toContain('bedrock-agentcore:InvokeRegistryMcp');
  });

  it('does not grant any pipeline or codebuild actions', () => {
    const p = asPolicy(renderInlinePolicy('ReadOnly'));
    const allActions = p.Statement.flatMap((s) =>
      Array.isArray(s.Action) ? s.Action : [s.Action],
    );
    for (const a of allActions) {
      expect(a).not.toMatch(/^codepipeline:/);
      expect(a).not.toMatch(/^codebuild:/);
      expect(a).not.toMatch(/^codecommit:/);
    }
  });
});

describe('renderInlinePolicy — Approver', () => {
  it('only grants PutApprovalResult on the supplied pipeline list, no wildcards', () => {
    const p = asPolicy(
      renderInlinePolicy('Approver', { approverPipelineNames: ['agenticai-workload-acme'] }),
    );
    const approveStmt = p.Statement.find((s) => s.Sid === 'ApproverApprove');
    expect(approveStmt).toBeDefined();
    const actions = Array.isArray(approveStmt!.Action) ? approveStmt!.Action : [approveStmt!.Action];
    expect(actions).toEqual(['codepipeline:PutApprovalResult']);
    const resources = Array.isArray(approveStmt!.Resource)
      ? approveStmt!.Resource
      : [approveStmt!.Resource ?? ''];
    expect(resources).toEqual(['arn:aws:codepipeline:*:*:agenticai-workload-acme/*']);
    for (const r of resources) {
      expect(r).not.toMatch(/^arn:aws:codepipeline:\*:\*:\*$/);
    }
  });

  it('falls back to a placeholder pipeline ARN when approverPipelineNames is empty', () => {
    const p = asPolicy(renderInlinePolicy('Approver', { approverPipelineNames: [] }));
    const approveStmt = p.Statement.find((s) => s.Sid === 'ApproverApprove');
    const resources = Array.isArray(approveStmt!.Resource)
      ? approveStmt!.Resource
      : [approveStmt!.Resource ?? ''];
    expect(resources[0]).toContain('__no_pipeline_configured__');
  });

  it('does not grant any write actions to bedrock-agentcore, iam, lambda, or kms', () => {
    const p = asPolicy(renderInlinePolicy('Approver', { approverPipelineNames: ['x'] }));
    const allActions = p.Statement.flatMap((s) =>
      Array.isArray(s.Action) ? s.Action : [s.Action],
    );
    for (const a of allActions) {
      expect(a).not.toMatch(/^bedrock-agentcore:(Create|Update|Delete|Put)/);
      expect(a).not.toMatch(/^iam:(Create|Update|Delete|Put|Attach|Detach)/);
      expect(a).not.toMatch(/^lambda:(Create|Update|Delete|Put)/);
      expect(a).not.toMatch(/^kms:(Schedule|Disable|Update|Put)/);
    }
  });
});
