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
import { DEV_TELEMETRY_LOG_GROUP_PREFIX, renderInlinePolicy } from './workstream-permission-sets';

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
    const consumerStmt = p.Statement.find((s) => s.Sid === 'AgentRegistryConsumer');
    expect(consumerStmt).toBeDefined();
    const resources = Array.isArray(consumerStmt!.Resource)
      ? consumerStmt!.Resource
      : [consumerStmt!.Resource ?? ''];
    expect(resources).toContain('arn:aws:agent-registry:*:222222222222:registry/*');
    expect(resources).toContain('arn:aws:agent-registry:*:222222222222:registry/*/record/*');
  });

  it('falls back to a wildcard account scope when platformAccountId is not supplied', () => {
    const p = asPolicy(renderInlinePolicy('Developer'));
    const consumerStmt = p.Statement.find((s) => s.Sid === 'AgentRegistryConsumer');
    const resources = Array.isArray(consumerStmt!.Resource)
      ? consumerStmt!.Resource
      : [consumerStmt!.Resource ?? ''];
    expect(resources).toContain('arn:aws:agent-registry:*:*:registry/*');
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
    // The GA Agent Registry signs as its own service, so the
    // `bedrock-agentcore:*` wildcards above do not reach it. Both namespaces
    // must be denied, the same reason SCP-11 covers both.
    expect(actions).toContain('agent-registry:Update*');
    expect(actions).toContain('agent-registry:Delete*');
    expect(actions).toContain('agent-registry:Create*');
    expect(actions).toContain('agent-registry:Put*');
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

  // Security review: pipeline write grants must be scoped to the workstream, not '*'.
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

  // CloudWatch Omni GA developer loop: IDE / local-run telemetry into the
  // nonprod cell, visible in the hub under the developer's account.
  describe('DeveloperTelemetryWrite (CloudWatch Omni developer loop)', () => {
    it('lets a developer write OTLP logs only under the workstream dev prefix, never a runtime group', () => {
      const p = asPolicy(renderInlinePolicy('Developer', { workstreamId: 'acme' }));
      const stmt = p.Statement.find((s) => s.Sid === 'DeveloperTelemetryWrite');
      expect(stmt).toBeDefined();
      expect(stmt!.Effect).toBe('Allow');
      const actions = Array.isArray(stmt!.Action) ? stmt!.Action : [stmt!.Action];
      expect(actions).toEqual(
        expect.arrayContaining([
          'logs:CreateLogGroup',
          'logs:CreateLogStream',
          'logs:PutLogEvents',
          'logs:PutRetentionPolicy',
        ]),
      );
      const resources = Array.isArray(stmt!.Resource) ? stmt!.Resource : [stmt!.Resource ?? ''];
      expect(resources).toEqual([`arn:aws:logs:*:*:log-group:${DEV_TELEMETRY_LOG_GROUP_PREFIX}/acme/*`]);
      // The runtime convention is /agenticai/<env>/<tenant>/<agent> with env in
      // {nonprod, prod}; `dev` is a slot no deployed stack uses.
      for (const r of resources) {
        expect(r).not.toBe('*');
        expect(r).not.toContain('/agenticai/nonprod');
        expect(r).not.toContain('/agenticai/prod');
        expect(r).toContain('/agenticai/dev/acme/');
      }
    });

    it('never hands the developer a Logs delete, resource-policy or delivery verb', () => {
      const p = asPolicy(renderInlinePolicy('Developer', { workstreamId: 'acme' }));
      const allowActions = p.Statement.filter((s) => s.Effect === 'Allow').flatMap((s) =>
        Array.isArray(s.Action) ? s.Action : [s.Action],
      );
      for (const forbidden of [
        'logs:DeleteLogGroup',
        'logs:DeleteLogStream',
        'logs:Delete*',
        'logs:PutResourcePolicy',
        'logs:PutSubscriptionFilter',
        'logs:CreateDelivery',
        'logs:*',
        'xray:UpdateTraceSegmentDestination',
        'xray:UpdateIndexingRule',
        'xray:*',
      ]) {
        expect(allowActions).not.toContain(forbidden);
      }
    });

    it('grants span export (PutSpans / PutTraceSegments) in a separate statement because X-Ray has no resource scoping', () => {
      const p = asPolicy(renderInlinePolicy('Developer', { workstreamId: 'acme' }));
      const stmt = p.Statement.find((s) => s.Sid === 'DeveloperTelemetrySpans');
      expect(stmt).toBeDefined();
      const actions = Array.isArray(stmt!.Action) ? stmt!.Action : [stmt!.Action];
      expect(actions).toEqual(
        expect.arrayContaining(['xray:PutSpans', 'xray:PutTraceSegments', 'xray:PutTelemetryRecords']),
      );
      expect(actions).toHaveLength(3);
      expect(stmt!.Resource).toBe('*');
    });

    it('pins both telemetry statements to the nonprod accounts with aws:PrincipalAccount when devTelemetryAccountIds is supplied', () => {
      const p = asPolicy(
        renderInlinePolicy('Developer', {
          workstreamId: 'acme',
          devTelemetryAccountIds: ['333333333333'],
        }),
      );
      for (const sid of ['DeveloperTelemetryWrite', 'DeveloperTelemetrySpans']) {
        const stmt = p.Statement.find((s) => s.Sid === sid);
        expect(stmt!.Condition).toEqual({
          StringEquals: { 'aws:PrincipalAccount': ['333333333333'] },
        });
      }
    });

    it('emits no account condition when devTelemetryAccountIds is omitted or empty', () => {
      for (const opts of [{ workstreamId: 'acme' }, { workstreamId: 'acme', devTelemetryAccountIds: [] }]) {
        const p = asPolicy(renderInlinePolicy('Developer', opts));
        for (const sid of ['DeveloperTelemetryWrite', 'DeveloperTelemetrySpans']) {
          const stmt = p.Statement.find((s) => s.Sid === sid);
          expect(stmt).toBeDefined();
          expect(stmt!.Condition).toBeUndefined();
        }
      }
    });

    it('falls back to a wildcard workstream slot (still under the dev prefix) when workstreamId is omitted', () => {
      const p = asPolicy(renderInlinePolicy('Developer'));
      const stmt = p.Statement.find((s) => s.Sid === 'DeveloperTelemetryWrite');
      expect(stmt!.Resource).toEqual([`arn:aws:logs:*:*:log-group:${DEV_TELEMETRY_LOG_GROUP_PREFIX}/*/*`]);
    });

    it('is a Developer-only grant: ReadOnly and Approver personas get no telemetry write', () => {
      for (const persona of ['ReadOnly', 'Approver'] as const) {
        const p = asPolicy(
          renderInlinePolicy(persona, { workstreamId: 'acme', approverPipelineNames: ['AgenticAI-acme-pipeline'] }),
        );
        expect(p.Statement.find((s) => s.Sid === 'DeveloperTelemetryWrite')).toBeUndefined();
        expect(p.Statement.find((s) => s.Sid === 'DeveloperTelemetrySpans')).toBeUndefined();
        const allowActions = p.Statement.filter((s) => s.Effect === 'Allow').flatMap((s) =>
          Array.isArray(s.Action) ? s.Action : [s.Action],
        );
        expect(allowActions).not.toContain('logs:PutLogEvents');
        expect(allowActions).not.toContain('xray:PutSpans');
        expect(allowActions).not.toContain('xray:PutTraceSegments');
      }
    });
  });
});

describe('renderInlinePolicy — ReadOnly', () => {
  it('grants observability + Registry read but NOT the active Discoverable search verb', () => {
    const p = asPolicy(renderInlinePolicy('ReadOnly', { platformAccountId: '222222222222' }));
    const consumerStmt = p.Statement.find((s) => s.Sid === 'AgentRegistryConsumerReadOnly');
    expect(consumerStmt).toBeDefined();
    const actions = Array.isArray(consumerStmt!.Action) ? consumerStmt!.Action : [consumerStmt!.Action];
    expect(actions).toContain('agent-registry:GetRegistry');
    expect(actions).toContain('agent-registry:GetRegistryRecord');
    expect(actions).toContain('agent-registry:ListDiscoverableRegistryRecords');
    // ReadOnly must not carry the active search verb the Developer persona has.
    expect(actions).not.toContain('agent-registry:SearchDiscoverableRegistryRecords');
    // And no preview-namespace action may survive the GA migration.
    expect(actions.some((a) => String(a).startsWith('bedrock-agentcore:'))).toBe(false);
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
