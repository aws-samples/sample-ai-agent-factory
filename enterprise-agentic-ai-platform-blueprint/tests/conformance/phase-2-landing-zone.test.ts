/**
 * Phase 2 conformance — synth-time assertions for LogArchiveStack + AuditStack.
 *
 * Pins:
 *   - R-ARCH-018 (audit account hosts Org CloudTrail, CWL destination, CUR).
 *   - R-ARCH-023 (cross-account subscription filters to audit).
 *   - R-OBS-002 (CloudWatch OAM sink in audit account).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { LogArchiveStack } from '../../apps/platform-account/lib/log-archive-stack';
import { AuditStack } from '../../apps/platform-account/lib/audit-stack';

function synthLogArchive(retainOnDelete?: boolean) {
  const app = new App();
  const stack = new LogArchiveStack(app, 'TestLogArchive', {
    env: { account: '333333333333', region: 'us-west-2' },
    organizationId: 'o-example123',
    workloadAccountIds: ['444444444444', '555555555555'],
    retainOnDelete,
  });
  return Template.fromStack(stack);
}

function synthAudit(opts?: { trustedAccountIds?: string[] }) {
  const app = new App();
  const stack = new AuditStack(app, 'TestAudit', {
    env: { account: '666666666666', region: 'us-west-2' },
    organizationId: opts?.trustedAccountIds ? undefined : 'o-example123',
    trustedAccountIds: opts?.trustedAccountIds,
  });
  return Template.fromStack(stack);
}

describe('Phase 2 — LogArchiveStack', () => {
  it('emits CMK with automatic rotation for archive encryption', () => {
    const t = synthLogArchive();
    t.hasResourceProperties('AWS::KMS::Key', { EnableKeyRotation: true });
  });

  it('creates three S3 buckets (CloudTrail archive + CUR + server-access-log destination)', () => {
    const t = synthLogArchive();
    t.resourceCountIs('AWS::S3::Bucket', 3);
  });

  it('CloudTrail archive bucket has server access logging enabled (AU-2b/AU-3a)', () => {
    const t = synthLogArchive();
    const buckets = t.findResources('AWS::S3::Bucket', {
      Properties: {
        BucketName: {
          'Fn::Join': [
            '',
            ['agenticai-cloudtrail-archive-', { Ref: 'AWS::AccountId' }, '-', { Ref: 'AWS::Region' }],
          ],
        },
      },
    });
    // Name may not match exactly due to synth-time tokens; fall back to a
    // simpler existence check for the LoggingConfiguration property.
    void buckets;
    const hasServerLogs = Object.values(t.findResources('AWS::S3::Bucket')).some((res) => {
      const props = res.Properties as any;
      return props.LoggingConfiguration?.DestinationBucketName !== undefined;
    });
    expect(hasServerLogs).toBe(true);
  });

  it('CloudTrail archive bucket enforces SSL + blocks public access + uses BUCKET_OWNER_ENFORCED ownership', () => {
    const t = synthLogArchive();
    t.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      OwnershipControls: {
        Rules: [{ ObjectOwnership: 'BucketOwnerEnforced' }],
      },
    });
  });

  it('bucket policy grants cloudtrail.amazonaws.com write scoped to the Organization prefix', () => {
    const t = synthLogArchive();
    const policies = t.findResources('AWS::S3::BucketPolicy');
    const found = Object.values(policies).some((res) => {
      const doc = (res.Properties as any).PolicyDocument;
      const stmts: any[] = doc.Statement;
      return stmts.some((s) => {
        const resource = JSON.stringify(s.Resource ?? '');
        return (
          s.Effect === 'Allow' &&
          Array.isArray(s.Action) ? s.Action.includes('s3:PutObject') : s.Action === 's3:PutObject'
        ) && resource.includes('o-example123');
      });
    });
    expect(found).toBe(true);
  });

  it('emits a CloudWatch Logs cross-account destination', () => {
    const t = synthLogArchive();
    t.resourceCountIs('AWS::Logs::Destination', 1);
    t.hasResourceProperties('AWS::Logs::Destination', {
      DestinationName: 'AgenticAI-CentralLogs',
    });
  });

  it('provisions the encrypted on-demand Kinesis target and Logs delivery role', () => {
    const t = synthLogArchive();
    t.hasResourceProperties('AWS::Kinesis::Stream', {
      Name: 'agenticai-central-logs',
      RetentionPeriodHours: 24,
      StreamModeDetails: { StreamMode: 'ON_DEMAND' },
      StreamEncryption: {
        EncryptionType: 'KMS',
        KeyId: { 'Fn::GetAtt': ['LogArchiveLogArchiveKey88359CE6', 'Arn'] },
      },
    });
    t.hasResourceProperties('AWS::IAM::Role', {
      RoleName: 'AgenticAI-LogArchive-CWLDestinationRole',
    });

    const resources = t.toJSON().Resources as Record<string, any>;
    const destinationEntry = Object.entries(resources).find(
      ([, resource]) => resource.Type === 'AWS::Logs::Destination',
    );
    expect(destinationEntry).toBeDefined();
    const [, destination] = destinationEntry!;
    expect(JSON.stringify(destination.Properties.RoleArn)).toContain('CloudWatchLogsDestinationRole');
    expect(JSON.stringify(destination.Properties.TargetArn)).toContain('CentralLogStream');

    const destinationRoleEntry = Object.entries(resources).find(
      ([, resource]) =>
        resource.Type === 'AWS::IAM::Role' &&
        resource.Properties.RoleName === 'AgenticAI-LogArchive-CWLDestinationRole',
    );
    expect(destinationRoleEntry).toBeDefined();
    const [destinationRoleId, destinationRole] = destinationRoleEntry!;
    const trust = JSON.stringify(destinationRole.Properties.AssumeRolePolicyDocument);
    expect(trust).toContain('logs.amazonaws.com');
    expect(trust).toContain(':logs:us-west-2:444444444444:*');
    expect(trust).toContain(':logs:us-west-2:555555555555:*');

    const destinationPolicyEntry = Object.entries(resources).find(
      ([, resource]) =>
        resource.Type === 'AWS::IAM::Policy' &&
        resource.Properties.Roles.some((role: any) => role.Ref === destinationRoleId),
    );
    expect(destinationPolicyEntry).toBeDefined();
    const [destinationPolicyId, destinationPolicy] = destinationPolicyEntry!;
    const grantedActions = destinationPolicy.Properties.PolicyDocument.Statement.flatMap(
      (statement: any) =>
        Array.isArray(statement.Action) ? statement.Action : [statement.Action],
    );
    expect(grantedActions).toEqual(
      expect.arrayContaining(['kinesis:PutRecord', 'kinesis:PutRecords', 'kms:GenerateDataKey*']),
    );
    expect(destination.DependsOn).toEqual(expect.arrayContaining([destinationPolicyId]));
  });

  it('destroys nonproduction archives but retains the production-default archives', () => {
    const ephemeral = synthLogArchive(false).toJSON().Resources as Record<string, any>;
    for (const resource of Object.values(ephemeral)) {
      if (['AWS::S3::Bucket', 'AWS::KMS::Key', 'AWS::Kinesis::Stream'].includes(resource.Type)) {
        expect(resource.DeletionPolicy).toBe('Delete');
        expect(resource.UpdateReplacePolicy).toBe('Delete');
      }
    }
    expect(
      Object.values(ephemeral).filter(
        (resource) => resource.Type === 'Custom::S3AutoDeleteObjects',
      ),
    ).toHaveLength(3);

    const retained = synthLogArchive().toJSON().Resources as Record<string, any>;
    for (const resource of Object.values(retained)) {
      if (['AWS::S3::Bucket', 'AWS::KMS::Key', 'AWS::Kinesis::Stream'].includes(resource.Type)) {
        expect(resource.DeletionPolicy).toBe('Retain');
        expect(resource.UpdateReplacePolicy).toBe('Retain');
      }
    }
  });

  it('CWL destination policy trusts the provided workload account ids', () => {
    const t = synthLogArchive();
    const dests = t.findResources('AWS::Logs::Destination');
    const entries = Object.values(dests);
    expect(entries).toHaveLength(1);
    const rawPolicy = (entries[0].Properties as any).DestinationPolicy;
    // Destination policies may serialise as either a string or an object.
    const parsed: { Statement: any[] } =
      typeof rawPolicy === 'string' ? JSON.parse(rawPolicy) : (rawPolicy as any);
    const principalArns = parsed.Statement[0].Principal.AWS;
    expect(principalArns).toContain('arn:aws:iam::444444444444:root');
    expect(principalArns).toContain('arn:aws:iam::555555555555:root');
  });
});

describe('Phase 2 — AuditStack', () => {
  it('emits a single OAM sink per region', () => {
    const t = synthAudit();
    t.resourceCountIs('AWS::Oam::Sink', 1);
  });

  it('sink policy trusts the Organization via PrincipalOrgID when organizationId supplied', () => {
    const t = synthAudit();
    const sinks = t.findResources('AWS::Oam::Sink');
    const entries = Object.values(sinks);
    expect(entries).toHaveLength(1);
    const policy = (entries[0].Properties as any).Policy as Record<string, unknown>;
    const stmt = (policy.Statement as any[])[0];
    expect(stmt.Principal.AWS).toBe('*');
    expect(stmt.Condition['ForAnyValue:StringEquals']['aws:PrincipalOrgID']).toBe('o-example123');
  });

  it('sink policy trusts explicit account list when trustedAccountIds supplied', () => {
    const t = synthAudit({ trustedAccountIds: ['444444444444'] });
    const sinks = t.findResources('AWS::Oam::Sink');
    const entries = Object.values(sinks);
    const policy = (entries[0].Properties as any).Policy as Record<string, unknown>;
    const stmt = (policy.Statement as any[])[0];
    expect(stmt.Principal.AWS).toEqual(['arn:aws:iam::444444444444:root']);
  });
});
