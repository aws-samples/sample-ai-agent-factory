/**
 * WorkstreamRosterTable — DynamoDB table mapping
 *   permissionSetArn → workstreamId → developer email/principal
 *
 * Powers the audit dashboards and the EventBridge rule that opens an
 * auto-PR in workstream repos when a Registry record is deprecated. The
 * table is **append-only** at the application layer (no `Update*` IAM
 * granted to anything except the platform-account onboarding role) and
 * encrypted with a CMK supplied by the platform stack.
 *
 * Schema (single-table design):
 *   PK = `WS#<workstreamId>`
 *   SK = `PERSONA#<Developer|ReadOnly|Approver>#<developerEmail>`
 *   Attributes: permissionSetArn, principalId, accountIds (SS), addedAt (ISO).
 *
 * GSI1: PK1 = `PSA#<permissionSetArn>` for reverse lookup ("who has this PS?").
 *
 * ---
 * DATA PROTECTION / COMPLIANCE NOTE (personal data):
 * This table stores personal data — developer email addresses and IAM
 * Identity Center principal ids. Under the AWS shared responsibility model,
 * data protection in the cloud is the customer's responsibility. Customers
 * processing personal data of individuals in the EU/EEA (or other regulated
 * jurisdictions) must ensure their use of this construct complies with
 * applicable privacy law (e.g. GDPR), including:
 *   - lawful basis for processing and data minimisation (store only the
 *     identifiers you need for entitlement/audit);
 *   - data-subject rights — access, rectification, erasure ("right to be
 *     forgotten"), and portability. The table is append-only at the app
 *     layer for audit integrity, so erasure requests must be serviced by a
 *     deliberate, audited administrative deletion path (not the onboarding
 *     role);
 *   - a defined retention policy — set a TTL or scheduled purge appropriate
 *     to your audit-retention obligations rather than retaining indefinitely.
 * At-rest encryption uses a customer-managed KMS key (below). See the AWS
 * GDPR Center (https://aws.amazon.com/compliance/gdpr-center/) and the AWS
 * Data Privacy FAQ for guidance. This blueprint ships no personal data.
 * ---
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CfnOutput, RemovalPolicy } from 'aws-cdk-lib';
import { AttributeType, BillingMode, Table, TableEncryption } from 'aws-cdk-lib/aws-dynamodb';
import { IKey } from 'aws-cdk-lib/aws-kms';
import { Construct } from 'constructs';

export interface WorkstreamRosterTableProps {
  /**
   * Customer-managed KMS key for at-rest encryption. Required — the table
   * holds developer email addresses + Identity Center principal ids and
   * therefore must use CMK rather than AWS-owned keys.
   */
  readonly encryptionKey: IKey;

  /**
   * Optional name override. Default: `agenticai-workstream-roster-<envName>`.
   */
  readonly tableNameOverride?: string;

  /**
   * Environment slug (e.g., `nonprod`, `prod`). Used in the default table
   * name and in the `environment` tag.
   */
  readonly envName: string;
}

export class WorkstreamRosterTable extends Construct {
  readonly table: Table;

  constructor(scope: Construct, id: string, props: WorkstreamRosterTableProps) {
    super(scope, id);

    if (!/^[a-z][a-z0-9-]{1,16}$/.test(props.envName)) {
      throw new Error(
        `WorkstreamRosterTable: envName must match /^[a-z][a-z0-9-]{1,16}$/; got '${props.envName}'`,
      );
    }

    const tableName =
      props.tableNameOverride ?? `agenticai-workstream-roster-${props.envName}`;

    this.table = new Table(this, 'RosterTable', {
      tableName,
      partitionKey: { name: 'pk', type: AttributeType.STRING },
      sortKey: { name: 'sk', type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      encryption: TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.encryptionKey,
      pointInTimeRecovery: true,
      // Roster is operational state, not source-of-truth — destroy is fine in
      // nonprod tear-downs. Production stacks should override at the stack
      // level via `applyRemovalPolicy`.
      removalPolicy: RemovalPolicy.DESTROY,
    });

    this.table.addGlobalSecondaryIndex({
      indexName: 'gsi1-permission-set-arn',
      partitionKey: { name: 'gsi1pk', type: AttributeType.STRING },
      sortKey: { name: 'gsi1sk', type: AttributeType.STRING },
    });

    new CfnOutput(this, 'RosterTableName', {
      value: this.table.tableName,
      description: 'Workstream roster DynamoDB table — Identity Center permission-set assignments by workstream.',
      exportName: `AgenticAI-WorkstreamRosterTableName-${props.envName}`,
    });
    new CfnOutput(this, 'RosterTableArn', {
      value: this.table.tableArn,
      description: 'Workstream roster DynamoDB table ARN.',
      exportName: `AgenticAI-WorkstreamRosterTableArn-${props.envName}`,
    });
  }
}
