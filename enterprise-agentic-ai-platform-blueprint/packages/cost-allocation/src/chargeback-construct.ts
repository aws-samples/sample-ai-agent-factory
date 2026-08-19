/**
 * ChargebackConstruct — monthly per-team chargeback CSV export.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Missing-4 (chargeback half).
 * Showback (visibility) is provided by the existing per-app dashboard
 * widget; chargeback is the cron-emitted CSV that finance systems consume.
 *
 * Components:
 *   - S3 chargeback bucket: KMS-encrypted, versioned, TLS-only,
 *     Object Lock GOVERNANCE 24 months (retention reasonable for
 *     finance reconciliation cycles; not the COMPLIANCE 7y bucket).
 *   - DynamoDB index of monthly runs (CMK + PITR).
 *   - Lambda runner: queries Athena over the existing CUR S3 export,
 *     groups by `cost-centre` tag (set by AgenticApp), writes one CSV
 *     per cost-centre under `<YYYY-MM>/<cost-centre>.csv`, then signs
 *     URLs and posts an SES email to the configured distro list.
 *   - EventBridge schedule: 1st of every month at 02:00 UTC.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import {
  AttributeType,
  BillingMode,
  Table,
  TableEncryption,
} from 'aws-cdk-lib/aws-dynamodb';
import { Rule, Schedule } from 'aws-cdk-lib/aws-events';
import { LambdaFunction } from 'aws-cdk-lib/aws-events-targets';
import { AnyPrincipal, Effect, PolicyStatement, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import { Key } from 'aws-cdk-lib/aws-kms';
import { Code, Function, Runtime } from 'aws-cdk-lib/aws-lambda';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import {
  BlockPublicAccess,
  Bucket,
  BucketEncryption,
  ObjectLockRetention,
} from 'aws-cdk-lib/aws-s3';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

export interface ChargebackConstructProps {
  readonly envName: string;
  /** Existing CUR S3 path (`s3://bucket/prefix/`) registered as Athena table. */
  readonly curAthenaDatabase: string;
  readonly curAthenaTable: string;
  readonly chargebackEmailDistribution: readonly string[];
  /** Override the cron expression. Defaults to 1st @ 02:00 UTC. */
  readonly cronExpression?: string;
}

const RETENTION_MONTHS = 24;

/**
 * SQL identifier for an Athena table/database name — letters, digits and
 * underscores, optionally schema-qualified (`db.table`). Athena cannot bind an
 * identifier as a `?` parameter (only VALUES bind), so the identifier is
 * (a) validated at SYNTH time by this pattern — a non-conforming name fails the
 * CDK build and can never deploy — and (b) pinned at RUNTIME to the exact
 * synth-known name baked into the handler. No attacker-controllable surface
 * reaches the query string.
 */
const SQL_IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$/;

export class ChargebackConstruct extends Construct {
  readonly bucket: Bucket;
  readonly athenaResultsBucket: Bucket;
  readonly runsTable: Table;
  readonly runner: Function;
  readonly schedule: Rule;
  readonly kmsKey: Key;

  constructor(scope: Construct, id: string, props: ChargebackConstructProps) {
    super(scope, id);

    // SECURITY (SQL injection defence): the CUR database + table names are
    // concatenated into an Athena query (identifiers cannot be bound as '?').
    // Fail the CDK build if either is not a strict SQL identifier, so an
    // injectable name can never be deployed. The runtime handler additionally
    // pins CUR_TABLE to this exact synth-time value (see buildChargebackRunner).
    if (!SQL_IDENTIFIER.test(props.curAthenaTable)) {
      throw new Error(
        `ChargebackConstruct: curAthenaTable '${props.curAthenaTable}' is not a valid SQL identifier (^[A-Za-z_][A-Za-z0-9_]*(\\.[A-Za-z_][A-Za-z0-9_]*)?$).`,
      );
    }
    if (!SQL_IDENTIFIER.test(props.curAthenaDatabase)) {
      throw new Error(
        `ChargebackConstruct: curAthenaDatabase '${props.curAthenaDatabase}' is not a valid SQL identifier.`,
      );
    }

    const stack = Stack.of(this);

    this.kmsKey = new Key(this, 'Key', {
      alias: `alias/agenticai/chargeback-${props.envName}`,
      description: `Chargeback CSV CMK (${props.envName}).`,
      enableKeyRotation: true,
      pendingWindow: Duration.days(30),
      removalPolicy: RemovalPolicy.RETAIN,
    });
    this.kmsKey.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowS3Service',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal('s3.amazonaws.com')],
        actions: ['kms:Encrypt', 'kms:Decrypt', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:DescribeKey'],
        resources: ['*'],
        conditions: { StringEquals: { 'aws:SourceAccount': stack.account } },
      }),
    );

    // CRIT-D companion fix: split into two buckets — finance-CSV bucket is
    // Object-Locked GOVERNANCE 24mo (the audit boundary), Athena temp
    // results live in a separate bucket without Object Lock so they can be
    // garbage-collected.
    this.bucket = new Bucket(this, 'ChargebackBucket', {
      bucketName: `agenticai-chargeback-${props.envName}-${stack.account}-${stack.region}`,
      encryption: BucketEncryption.KMS,
      encryptionKey: this.kmsKey,
      bucketKeyEnabled: true,
      enforceSSL: true,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      versioned: true,
      objectLockEnabled: true,
      objectLockDefaultRetention: ObjectLockRetention.governance(Duration.days(RETENTION_MONTHS * 30)),
      removalPolicy: RemovalPolicy.RETAIN,
    });
    this.athenaResultsBucket = new Bucket(this, 'AthenaResultsBucket', {
      bucketName: `agenticai-chargeback-athena-${props.envName}-${stack.account}-${stack.region}`,
      encryption: BucketEncryption.KMS,
      encryptionKey: this.kmsKey,
      bucketKeyEnabled: true,
      enforceSSL: true,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      versioned: false,
      lifecycleRules: [{ id: 'expire-athena-tmp', expiration: Duration.days(7) }],
      removalPolicy: RemovalPolicy.RETAIN,
    });
    // L-A note: enforceSSL: true on the L2 Bucket already adds a TLS-deny
    // statement automatically. We do NOT add a second one (the duplicate
    // was a v0.4.0 finding from the security agent + bug-bash).

    NagSuppressions.addResourceSuppressions(
      this.bucket,
      [
        { id: 'AwsSolutions-S1', reason: 'SEC-001: chargeback bucket; access logs deferred to v2.' },
        { id: 'NIST.800.53.R5-S3BucketLoggingEnabled', reason: 'SEC-001: same.' },
        { id: 'NIST.800.53.R5-S3BucketReplicationEnabled', reason: 'SEC-002: CRR deferred to v2.' },
      ],
      true,
    );
    NagSuppressions.addResourceSuppressions(
      this.athenaResultsBucket,
      [
        { id: 'AwsSolutions-S1', reason: 'SEC-001: Athena temporary results bucket; lifecycled to 7d expiry, no business data.' },
        { id: 'NIST.800.53.R5-S3BucketLoggingEnabled', reason: 'SEC-001: same — temporary results.' },
        { id: 'NIST.800.53.R5-S3BucketReplicationEnabled', reason: 'SEC-002: temporary; CRR deferred.' },
        { id: 'NIST.800.53.R5-S3BucketVersioningEnabled', reason: 'SEC-022: Athena results are derived data with 7-day expiry; versioning would conflict with the lifecycle expiry rule and inflate cost on every chargeback run.' },
      ],
      true,
    );

    this.runsTable = new Table(this, 'RunsTable', {
      tableName: `agenticai-chargeback-runs-${props.envName}`,
      partitionKey: { name: 'pk', type: AttributeType.STRING },          // YYYY-MM
      sortKey: { name: 'costCentre', type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      encryption: TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: this.kmsKey,
      pointInTimeRecovery: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    NagSuppressions.addResourceSuppressions(
      this.runsTable,
      [{ id: 'NIST.800.53.R5-DynamoDBInBackupPlan', reason: 'SEC-023: PITR is enabled.' }],
      true,
    );

    const logGroup = new LogGroup(this, 'RunnerLogs', {
      logGroupName: `/agenticai/chargeback/${props.envName}`,
      retention: RetentionDays.ONE_YEAR,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    this.runner = new Function(this, 'Runner', {
      functionName: `agenticai-chargeback-${props.envName}`,
      runtime: Runtime.NODEJS_20_X,
      handler: 'index.handler',
      timeout: Duration.minutes(15),
      memorySize: 512,
      logGroup,
      environment: {
        ENV_NAME: props.envName,
        CUR_DB: props.curAthenaDatabase,
        CUR_TABLE: props.curAthenaTable,
        BUCKET: this.bucket.bucketName,
        ATHENA_RESULTS_BUCKET: this.athenaResultsBucket.bucketName,
        RUNS_TABLE: this.runsTable.tableName,
        EMAIL_DISTRO: props.chargebackEmailDistribution.join(','),
      },
      description: 'Monthly chargeback CSV runner — Athena → S3 → SES.',
      code: Code.fromInline(buildChargebackRunner(props.curAthenaTable)),
    });

    // CRIT-D fix: Athena needs s3:GetBucketLocation + s3:ListBucket +
    // s3:GetObject on the result bucket; previously only `grantWrite` was
    // applied which causes "Unable to verify/create output bucket" failures.
    this.bucket.grantReadWrite(this.runner);
    this.athenaResultsBucket.grantReadWrite(this.runner);
    this.kmsKey.grantEncryptDecrypt(this.runner);
    this.runsTable.grantWriteData(this.runner);

    // SEC-025 (Holmes CSR): scope Athena/Glue/SES to exact resource ARNs.
    // The runner submits queries against the default "primary" workgroup
    // (no WorkGroup param on StartQueryExecution), reads only the CUR
    // database/table, and sends mail only from the first distro address
    // (used as the SES Source). All three services support resource-level
    // ARNs — the prior wildcard was over-broad.
    const athenaWorkgroupArn = stack.formatArn({
      service: 'athena',
      resource: 'workgroup',
      resourceName: 'primary',
    });
    this.runner.addToRolePolicy(
      new PolicyStatement({
        sid: 'AthenaChargebackQuery',
        effect: Effect.ALLOW,
        actions: ['athena:StartQueryExecution', 'athena:GetQueryExecution', 'athena:GetQueryResults'],
        resources: [athenaWorkgroupArn],
      }),
    );
    // Glue Data Catalog: catalog + the specific CUR database + its table.
    const glueCatalogArn = stack.formatArn({ service: 'glue', resource: 'catalog' });
    const glueDatabaseArn = stack.formatArn({
      service: 'glue',
      resource: 'database',
      resourceName: props.curAthenaDatabase,
    });
    const glueTableArn = stack.formatArn({
      service: 'glue',
      resource: 'table',
      resourceName: `${props.curAthenaDatabase}/${props.curAthenaTable}`,
    });
    this.runner.addToRolePolicy(
      new PolicyStatement({
        sid: 'GlueCatalogRead',
        effect: Effect.ALLOW,
        actions: ['glue:GetDatabase', 'glue:GetTable', 'glue:GetPartitions'],
        resources: [glueCatalogArn, glueDatabaseArn, glueTableArn],
      }),
    );
    // SES: only the Source identity (first distro entry). Support both the
    // email-identity ARN and the parent domain identity so a verified-domain
    // sender also works without re-widening to '*'.
    const sesSource = props.chargebackEmailDistribution[0];
    const sesSourceDomain = sesSource?.includes('@') ? sesSource.split('@')[1] : undefined;
    const sesResources = [
      stack.formatArn({ service: 'ses', resource: 'identity', resourceName: sesSource }),
      ...(sesSourceDomain
        ? [stack.formatArn({ service: 'ses', resource: 'identity', resourceName: sesSourceDomain })]
        : []),
    ];
    this.runner.addToRolePolicy(
      new PolicyStatement({
        sid: 'SESSendEmail',
        effect: Effect.ALLOW,
        actions: ['ses:SendEmail', 'ses:SendRawEmail'],
        resources: sesResources,
      }),
    );

    NagSuppressions.addResourceSuppressions(
      this.runner,
      [
        { id: 'NIST.800.53.R5-LambdaConcurrency', reason: 'SEC-007: cadence-driven monthly.' },
        { id: 'NIST.800.53.R5-LambdaDLQ', reason: 'SEC-008: failures emit CW logs and stack-event SNS.' },
        { id: 'NIST.800.53.R5-LambdaInsideVPC', reason: 'SEC-009: control-plane only.' },
      ],
      true,
    );

    this.schedule = new Rule(this, 'Schedule', {
      ruleName: `agenticai-chargeback-${props.envName}`,
      schedule: Schedule.expression(props.cronExpression ?? 'cron(0 2 1 * ? *)'),
      description: 'Monthly chargeback CSV export cadence.',
    });
    this.schedule.addTarget(new LambdaFunction(this.runner));
  }
}

/**
 * Build the chargeback runner handler with the operator-configured CUR table
 * name baked in as an immutable allow-list literal. The runner refuses to run
 * unless `CUR_TABLE` EXACTLY equals this synth-time value — so rotating the env
 * var out-of-band (e.g. via update-function-configuration) cannot smuggle a
 * different identifier into the FROM clause.
 */
function buildChargebackRunner(allowedTable: string): string {
  return `
const ALLOWED_CUR_TABLE = ${JSON.stringify(allowedTable)};
const { AthenaClient, StartQueryExecutionCommand, GetQueryExecutionCommand, GetQueryResultsCommand } = require('@aws-sdk/client-athena');
const { S3Client, PutObjectCommand, GetObjectCommand } = require('@aws-sdk/client-s3');
const { getSignedUrl } = require('@aws-sdk/s3-request-presigner');
const { SESClient, SendEmailCommand } = require('@aws-sdk/client-ses');
const { DynamoDBClient } = require('@aws-sdk/client-dynamodb');
const { DynamoDBDocumentClient, PutCommand } = require('@aws-sdk/lib-dynamodb');

const env = process.env;
const athena = new AthenaClient({});
const s3 = new S3Client({});
const ses = new SESClient({});
const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));

async function runQuery(sql, executionParameters) {
  const start = await athena.send(new StartQueryExecutionCommand({
    QueryString: sql,
    QueryExecutionContext: { Database: env.CUR_DB },
    ResultConfiguration: { OutputLocation: 's3://' + env.ATHENA_RESULTS_BUCKET + '/' },
    ExecutionParameters: executionParameters,
  }));
  for (let i = 0; i < 60; i++) {
    await new Promise((r) => setTimeout(r, 5000));
    const ex = await athena.send(new GetQueryExecutionCommand({ QueryExecutionId: start.QueryExecutionId }));
    if (ex.QueryExecution.Status.State === 'SUCCEEDED') break;
    if (['FAILED', 'CANCELLED'].includes(ex.QueryExecution.Status.State)) {
      throw new Error('Athena query ' + ex.QueryExecution.Status.State + ': ' + ex.QueryExecution.Status.StateChangeReason);
    }
  }
  return athena.send(new GetQueryResultsCommand({ QueryExecutionId: start.QueryExecutionId }));
}

function csvEscape(s) {
  if (s == null) return '';
  const str = String(s);
  return /[",\\n]/.test(str) ? '"' + str.replace(/"/g, '""') + '"' : str;
}

async function recordRunRow(month, costCentre, status, reason) {
  await ddb.send(new PutCommand({
    TableName: env.RUNS_TABLE,
    Item: { pk: month, costCentre, status, reason: reason || '', createdAt: new Date().toISOString() },
  }));
}

exports.handler = async () => {
  const today = new Date();
  const month = today.toISOString().slice(0, 7); // YYYY-MM
  // SEC (Holmes CSR): prevent SQL injection.
  //  - The month VALUE is bound via Athena ExecutionParameters ('?'), never
  //    interpolated into the query string.
  //  - The table IDENTIFIER cannot be a bind parameter in Athena, so it is
  //    validated against a strict allow-list pattern (optionally
  //    schema-qualified) before use. A non-conforming CUR_TABLE aborts.
  const tableName = env.CUR_TABLE || '';
  // SECURITY (SQL injection defence — do NOT remove or weaken):
  // The table IDENTIFIER cannot be an Athena bind parameter (only VALUES bind
  // via '?'), so the FROM clause must concatenate a name. That name is pinned
  // to ALLOWED_CUR_TABLE — the exact value baked into this handler at CDK synth
  // (itself validated as a strict SQL identifier at synth time). We require an
  // EXACT match, not a pattern match, so nothing attacker-controllable — not
  // even a rotated CUR_TABLE env var — can reach the query string.
  if (tableName !== ALLOWED_CUR_TABLE) {
    await recordRunRow(month, '__chargeback_runner__', 'FAILED', 'CUR_TABLE does not match the synth-time allow-listed table');
    return { ok: false, month, reason: 'CUR_TABLE not allow-listed' };
  }
  const sql = "SELECT line_item_resource_id, sum(line_item_unblended_cost) AS cost, resource_tags_user_application_id, resource_tags_user_cost_centre " +
              // SECURITY: ALLOWED_CUR_TABLE is the synth-baked, exact-matched identifier above; the month VALUE is bound via '?' (ExecutionParameters), never concatenated.
              "FROM " + ALLOWED_CUR_TABLE + " " +
              "WHERE date_format(line_item_usage_start_date, '%Y-%m') = ? " +
              "GROUP BY 1, 3, 4";
  // CRIT-D fix: catch Athena failures (CUR table missing, IAM gap, workgroup
  // missing) and persist a runs DDB row so an operator can see what happened.
  let r;
  try {
    r = await runQuery(sql, [month]);
  } catch (err) {
    await recordRunRow(month, '__chargeback_runner__', 'FAILED', String(err && err.message || err));
    return { ok: false, month, reason: String(err && err.message || err) };
  }
  const rows = (r.ResultSet && r.ResultSet.Rows) || [];
  const byCC = new Map();
  for (let i = 1; i < rows.length; i++) {
    const cells = rows[i].Data.map((d) => d.VarCharValue || '');
    const cc = cells[3] || 'unknown';
    if (!byCC.has(cc)) byCC.set(cc, []);
    byCC.get(cc).push(cells);
  }
  const distro = (env.EMAIL_DISTRO || '').split(',').filter(Boolean);
  for (const [cc, ccRows] of byCC.entries()) {
    const csv = 'resource_id,cost,application_id,cost_centre\\n' +
                ccRows.map((c) => c.map(csvEscape).join(',')).join('\\n');
    const key = month + '/' + cc + '.csv';
    await s3.send(new PutObjectCommand({
      Bucket: env.BUCKET, Key: key, Body: csv, ContentType: 'text/csv',
    }));
    const url = await getSignedUrl(s3, new GetObjectCommand({ Bucket: env.BUCKET, Key: key }), { expiresIn: 7 * 24 * 3600 });
    await ddb.send(new PutCommand({
      TableName: env.RUNS_TABLE,
      Item: { pk: month, costCentre: cc, key, rowCount: ccRows.length, createdAt: today.toISOString() },
    }));
    if (distro.length) {
      await ses.send(new SendEmailCommand({
        Source: distro[0],
        Destination: { ToAddresses: distro },
        Message: {
          Subject: { Data: 'AgenticAI chargeback ' + month + ' / ' + cc },
          Body: { Text: { Data: 'CSV: ' + url + '\\nRows: ' + ccRows.length } },
        },
      }));
    }
  }
  return { ok: true, month, costCentres: [...byCC.keys()] };
};
`;
}
