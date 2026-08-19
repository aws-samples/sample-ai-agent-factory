/**
 * AgentCoreRegistryConstruct — Agent + Tool Registry tables.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import {
  AttributeType,
  BillingMode,
  StreamViewType,
  Table,
  TableEncryption,
} from 'aws-cdk-lib/aws-dynamodb';
import { Effect, PolicyStatement, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import { Key } from 'aws-cdk-lib/aws-kms';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

export interface AgentCoreRegistryConstructProps {
  readonly envName: string;
}

export class AgentCoreRegistryConstruct extends Construct {
  readonly agentTable: Table;
  readonly toolTable: Table;
  readonly kmsKey: Key;

  constructor(scope: Construct, id: string, props: AgentCoreRegistryConstructProps) {
    super(scope, id);

    const stack = Stack.of(this);

    this.kmsKey = new Key(this, 'Key', {
      alias: `alias/agenticai/registry-${props.envName}`,
      description: `AgentCore Registry CMK (${props.envName}).`,
      enableKeyRotation: true,
      pendingWindow: Duration.days(30),
      removalPolicy: RemovalPolicy.RETAIN,
    });
    this.kmsKey.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowDynamoDBService',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal('dynamodb.amazonaws.com')],
        actions: ['kms:Encrypt', 'kms:Decrypt', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:DescribeKey'],
        resources: ['*'],
        conditions: {
          StringEquals: { 'aws:SourceAccount': stack.account },
        },
      }),
    );

    const commonTableProps = {
      billingMode: BillingMode.PAY_PER_REQUEST,
      encryption: TableEncryption.CUSTOMER_MANAGED as TableEncryption,
      encryptionKey: this.kmsKey,
      pointInTimeRecovery: true,
      removalPolicy: RemovalPolicy.RETAIN,
      stream: StreamViewType.NEW_AND_OLD_IMAGES,
    };

    this.agentTable = new Table(this, 'AgentTable', {
      tableName: `agenticai-registry-agents-${props.envName}`,
      partitionKey: { name: 'tenantId', type: AttributeType.STRING },
      sortKey: { name: 'agentId', type: AttributeType.STRING },
      ...commonTableProps,
    });
    this.agentTable.addGlobalSecondaryIndex({
      indexName: 'by-kind',
      partitionKey: { name: 'kind', type: AttributeType.STRING },
      sortKey: { name: 'agentId', type: AttributeType.STRING },
    });
    // Z7-E: A2A Agent Card discovery. The card JSON is stored under the
    // `agentCard` attribute; `cardName` is the projected discovery key.
    this.agentTable.addGlobalSecondaryIndex({
      indexName: 'by-card-name',
      partitionKey: { name: 'cardName', type: AttributeType.STRING },
      sortKey: { name: 'tenantId', type: AttributeType.STRING },
    });
    // Z7-K: federated-mesh domain scoping. domainId is the projected key;
    // cross-domain discovery walks this index, scoped by SCP-style condition.
    this.agentTable.addGlobalSecondaryIndex({
      indexName: 'by-domain',
      partitionKey: { name: 'domainId', type: AttributeType.STRING },
      sortKey: { name: 'agentId', type: AttributeType.STRING },
    });

    this.toolTable = new Table(this, 'ToolTable', {
      tableName: `agenticai-registry-tools-${props.envName}`,
      partitionKey: { name: 'tenantId', type: AttributeType.STRING },
      sortKey: { name: 'toolId', type: AttributeType.STRING },
      ...commonTableProps,
    });

    // AWS Backup is a deploy-time opt-in. PITR is already enabled; RETAIN
    // on removal means the tables survive accidental stack destroy. Customers
    // running under formal compliance (FedRAMP, IRAP, ISM) can opt-in to
    // AWS Backup via their backup-plan stack and register these tables.
    for (const table of [this.agentTable, this.toolTable]) {
      NagSuppressions.addResourceSuppressions(
        table,
        [
          {
            id: 'NIST.800.53.R5-DynamoDBInBackupPlan',
            reason:
              'SEC-023: PITR enabled (35-day continuous recovery) + RETAIN removal policy. AWS Backup plan is a customer-specific opt-in; referenced in OPERATIONS.md quarterly review.',
          },
        ],
        true,
      );
    }
  }
}
