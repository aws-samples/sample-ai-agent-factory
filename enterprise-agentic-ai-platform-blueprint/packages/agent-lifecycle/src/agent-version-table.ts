/**
 * AgentVersionTableConstruct — DDB schema extension for agent version history.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Missing-3 (storage side).
 *
 * Schema:
 *   - PK: tenantId
 *   - SK: agentId#vN#<gitSha>     (immutable; new row per build)
 *   - GSI by-alias: aliasName (PROD | CANARY | PREVIOUS) → tenantId#agentId
 *   - GSI by-status: status (LIVE | PROMOTING | ROLLED_BACK) → emittedAt
 *
 * The pre-existing `AgentCoreRegistryConstruct` keeps the
 * `agenticai-registry-agents-<env>` table as the present-state lookup; this
 * new table is the **history** record consumed by the canary + rollback
 * Step Function.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Duration, RemovalPolicy } from 'aws-cdk-lib';
import {
  AttributeType,
  BillingMode,
  StreamViewType,
  Table,
  TableEncryption,
} from 'aws-cdk-lib/aws-dynamodb';
import { IKey, Key } from 'aws-cdk-lib/aws-kms';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

export interface AgentVersionTableConstructProps {
  readonly envName: string;
  readonly kmsKey?: IKey;
}

export class AgentVersionTableConstruct extends Construct {
  readonly table: Table;
  readonly kmsKey: IKey;

  constructor(scope: Construct, id: string, props: AgentVersionTableConstructProps) {
    super(scope, id);

    this.kmsKey =
      props.kmsKey ??
      new Key(this, 'Key', {
        alias: `alias/agenticai/agent-versions-${props.envName}`,
        description: `CMK for agent-version history (${props.envName}).`,
        enableKeyRotation: true,
        pendingWindow: Duration.days(30),
        removalPolicy: RemovalPolicy.RETAIN,
      });

    this.table = new Table(this, 'Table', {
      tableName: `agenticai-agent-versions-${props.envName}`,
      partitionKey: { name: 'tenantId', type: AttributeType.STRING },
      sortKey: { name: 'sk', type: AttributeType.STRING }, // agentId#vN#gitSha
      billingMode: BillingMode.PAY_PER_REQUEST,
      encryption: TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: this.kmsKey,
      pointInTimeRecovery: true,
      removalPolicy: RemovalPolicy.RETAIN,
      stream: StreamViewType.NEW_AND_OLD_IMAGES,
    });
    this.table.addGlobalSecondaryIndex({
      indexName: 'by-alias',
      partitionKey: { name: 'aliasName', type: AttributeType.STRING },
      sortKey: { name: 'tenantAgent', type: AttributeType.STRING },
    });
    this.table.addGlobalSecondaryIndex({
      indexName: 'by-status',
      partitionKey: { name: 'status', type: AttributeType.STRING },
      sortKey: { name: 'emittedAt', type: AttributeType.STRING },
    });

    NagSuppressions.addResourceSuppressions(
      this.table,
      [{ id: 'NIST.800.53.R5-DynamoDBInBackupPlan', reason: 'SEC-023: PITR is enabled; AWS Backup plan is a customer opt-in.' }],
      true,
    );
  }
}
