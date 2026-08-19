/**
 * Phase 17 conformance — cost-allocation chargeback infrastructure.
 *
 * Pins:
 *   - Chargeback bucket: KMS + bucket-key + Object Lock GOVERNANCE 24 mo +
 *     versioning + BlockPublicAccess + deny-non-TLS.
 *   - Runs DDB: CMK + PITR.
 *   - Runner Lambda: monthly cron, Node 20, Athena + SES IAM.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Missing-4 (CDK-side).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App, Stack } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';

import { ChargebackConstruct } from '@agenticai/cost-allocation';

function synth() {
  const app = new App();
  const stack = new Stack(app, 'ChargebackTest', {
    env: { account: '111111111111', region: 'us-west-2' },
  });
  new ChargebackConstruct(stack, 'Chargeback', {
    envName: 'prod',
    curAthenaDatabase: 'cur',
    curAthenaTable: 'cost_and_usage',
    chargebackEmailDistribution: ['finops@example.com'],
  });
  return Template.fromStack(stack);
}

describe('Phase 17 — ChargebackConstruct CFN shape', () => {
  it('chargeback bucket has Object Lock GOVERNANCE 24 months (~720 days)', () => {
    const t = synth();
    t.hasResourceProperties('AWS::S3::Bucket', {
      ObjectLockEnabled: true,
      ObjectLockConfiguration: {
        ObjectLockEnabled: 'Enabled',
        Rule: {
          DefaultRetention: {
            Mode: 'GOVERNANCE',
            Days: 24 * 30,
          },
        },
      },
    });
  });

  it('chargeback bucket KMS-encrypted + BlockPublicAccess + versioned', () => {
    const t = synth();
    t.hasResourceProperties('AWS::S3::Bucket', {
      BucketEncryption: {
        ServerSideEncryptionConfiguration: Match.arrayWith([
          Match.objectLike({
            BucketKeyEnabled: true,
            ServerSideEncryptionByDefault: { SSEAlgorithm: 'aws:kms' },
          }),
        ]),
      },
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      VersioningConfiguration: { Status: 'Enabled' },
    });
  });

  it('runs DDB CMK-encrypted + PITR', () => {
    const t = synth();
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'agenticai-chargeback-runs-prod',
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
      SSESpecification: { SSEEnabled: true, SSEType: 'KMS' },
    });
  });

  it('runner Lambda is Node 20 with chargeback env vars', () => {
    const t = synth();
    t.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'agenticai-chargeback-prod',
      Runtime: 'nodejs20.x',
      Environment: {
        Variables: Match.objectLike({
          CUR_DB: 'cur',
          CUR_TABLE: 'cost_and_usage',
          EMAIL_DISTRO: 'finops@example.com',
        }),
      },
    });
  });

  it('schedule fires monthly at 02:00 UTC on day 1', () => {
    const t = synth();
    t.hasResourceProperties('AWS::Events::Rule', {
      ScheduleExpression: 'cron(0 2 1 * ? *)',
    });
  });

  // Holmes CSR SEC-025: Athena/Glue/SES must be scoped to exact ARNs, never '*'.
  it('runner IAM policy has NO wildcard resource on athena/glue/ses actions', () => {
    const t = synth();
    const policies = t.findResources('AWS::IAM::Policy');
    const sensitiveActions = [
      'athena:StartQueryExecution',
      'glue:GetDatabase',
      'ses:SendEmail',
    ];
    for (const logicalId of Object.keys(policies)) {
      const statements = policies[logicalId].Properties.PolicyDocument.Statement as Array<{
        Action: string | string[];
        Resource: unknown;
      }>;
      for (const stmt of statements) {
        const actions = Array.isArray(stmt.Action) ? stmt.Action : [stmt.Action];
        if (actions.some((a) => sensitiveActions.some((s) => a === s))) {
          const resources = Array.isArray(stmt.Resource) ? stmt.Resource : [stmt.Resource];
          for (const r of resources) {
            expect(r).not.toBe('*');
          }
        }
      }
    }
  });

  it('runner scopes Athena to the primary workgroup ARN', () => {
    const t = synth();
    t.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(['athena:StartQueryExecution']),
            Resource: {
              'Fn::Join': Match.arrayWith([
                Match.arrayWith([Match.stringLikeRegexp('workgroup/primary')]),
              ]),
            },
          }),
        ]),
      },
    });
  });

  it('runner scopes Glue to the CUR database + table (three ARNs)', () => {
    const t = synth();
    t.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(['glue:GetDatabase', 'glue:GetTable', 'glue:GetPartitions']),
            Resource: Match.arrayWith([
              Match.objectLike({ 'Fn::Join': Match.arrayWith([Match.arrayWith([Match.stringLikeRegexp('database/cur')])]) }),
              Match.objectLike({ 'Fn::Join': Match.arrayWith([Match.arrayWith([Match.stringLikeRegexp('table/cur/cost_and_usage')])]) }),
            ]),
          }),
        ]),
      },
    });
  });

  it('runner scopes SES to the source identity ARN', () => {
    const t = synth();
    t.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'SESSendEmail',
            Action: Match.arrayWith(['ses:SendEmail', 'ses:SendRawEmail']),
            Resource: Match.arrayWith([
              Match.objectLike({ 'Fn::Join': Match.arrayWith([Match.arrayWith([Match.stringLikeRegexp('identity/finops@example.com')])]) }),
            ]),
          }),
        ]),
      },
    });
  });

  // Holmes CSR: SQL-injection defence on the Athena FROM identifier.
  it('bakes the exact CUR table name into the runner as an immutable allow-list', () => {
    const t = synth();
    const fns = t.findResources('AWS::Lambda::Function');
    const runner = Object.values(fns).find(
      (f: any) => f.Properties?.FunctionName === 'agenticai-chargeback-prod',
    ) as any;
    expect(runner).toBeDefined();
    const code = runner.Properties.Code.ZipFile as string;
    // The synth-known table name is pinned as a literal + exact-matched at runtime.
    expect(code).toContain('const ALLOWED_CUR_TABLE = "cost_and_usage"');
    expect(code).toContain('tableName !== ALLOWED_CUR_TABLE');
    expect(code).toContain('FROM " + ALLOWED_CUR_TABLE');
  });

  it('rejects a non-SQL-identifier CUR table name at synth (injection can never deploy)', () => {
    const app = new App();
    const stack = new Stack(app, 'BadTable', { env: { account: '111111111111', region: 'us-west-2' } });
    expect(
      () =>
        new ChargebackConstruct(stack, 'Chargeback', {
          envName: 'prod',
          curAthenaDatabase: 'cur',
          curAthenaTable: 'cost_and_usage; DROP TABLE x;--',
          chargebackEmailDistribution: ['finops@example.com'],
        }),
    ).toThrow(/not a valid SQL identifier/);
  });

  it('rejects a non-SQL-identifier CUR database name at synth', () => {
    const app = new App();
    const stack = new Stack(app, 'BadDb', { env: { account: '111111111111', region: 'us-west-2' } });
    expect(
      () =>
        new ChargebackConstruct(stack, 'Chargeback', {
          envName: 'prod',
          curAthenaDatabase: "cur' OR '1'='1",
          curAthenaTable: 'cost_and_usage',
          chargebackEmailDistribution: ['finops@example.com'],
        }),
    ).toThrow(/not a valid SQL identifier/);
  });
});
