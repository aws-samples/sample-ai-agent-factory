/**
 * @agenticai/tenant-quota-guard
 *
 * Per-tenant rate limiting + token-budget enforcement, layered above
 * Bedrock service quotas. Bedrock-level quotas are account-scoped; in a
 * multi-tenant deployment a single tenant can starve others. This package
 * provides:
 *
 *   - `TenantQuotaTableConstruct` — DDB table tracking
 *     `(tenantId, windowKey)` → token-bucket state. CMK + PITR.
 *   - `consumeTokens()` pure-fn — atomic conditional-write helper used by
 *     the runtime path; returns granted/denied + remaining tokens.
 *   - `monthlyTokenBudget` enum + per-tenant override.
 *
 * Bonus shippable (Z7-L). Closes BLUEPRINT_GAP_ANALYSIS implicit gap
 * "noisy neighbour" (multiple security findings around shared quotas).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Duration, RemovalPolicy } from 'aws-cdk-lib';
import {
  AttributeType,
  BillingMode,
  Table,
  TableEncryption,
} from 'aws-cdk-lib/aws-dynamodb';
import { IKey, Key } from 'aws-cdk-lib/aws-kms';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

export interface TenantQuotaTableConstructProps {
  readonly envName: string;
  readonly kmsKey?: IKey;
}

export class TenantQuotaTableConstruct extends Construct {
  readonly table: Table;
  readonly kmsKey: IKey;

  constructor(scope: Construct, id: string, props: TenantQuotaTableConstructProps) {
    super(scope, id);
    this.kmsKey =
      props.kmsKey ??
      new Key(this, 'Key', {
        alias: `alias/agenticai/tenant-quota-${props.envName}`,
        description: `Per-tenant quota state CMK (${props.envName}).`,
        enableKeyRotation: true,
        pendingWindow: Duration.days(30),
        removalPolicy: RemovalPolicy.RETAIN,
      });

    this.table = new Table(this, 'Table', {
      tableName: `agenticai-tenant-quota-${props.envName}`,
      partitionKey: { name: 'tenantId', type: AttributeType.STRING },
      sortKey: { name: 'windowKey', type: AttributeType.STRING }, // YYYY-MM-DD-HH or YYYY-MM
      billingMode: BillingMode.PAY_PER_REQUEST,
      encryption: TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: this.kmsKey,
      pointInTimeRecovery: true,
      removalPolicy: RemovalPolicy.RETAIN,
      timeToLiveAttribute: 'ttl',
    });
    NagSuppressions.addResourceSuppressions(
      this.table,
      [{ id: 'NIST.800.53.R5-DynamoDBInBackupPlan', reason: 'SEC-023: PITR is enabled.' }],
      true,
    );
  }
}

// ---- Pure-fn token-bucket logic, callable from a Lambda handler ----

export interface QuotaState {
  readonly tokensConsumed: number;
  readonly maxTokens: number;
  readonly windowKey: string;
}

export interface ConsumeRequest {
  readonly current: QuotaState;
  readonly requestedTokens: number;
}

export interface ConsumeResult {
  readonly granted: boolean;
  readonly remaining: number;
  readonly newState: QuotaState;
}

export function consumeTokens(req: ConsumeRequest): ConsumeResult {
  const tentative = req.current.tokensConsumed + req.requestedTokens;
  if (tentative > req.current.maxTokens) {
    return {
      granted: false,
      remaining: Math.max(0, req.current.maxTokens - req.current.tokensConsumed),
      newState: req.current,
    };
  }
  return {
    granted: true,
    remaining: req.current.maxTokens - tentative,
    newState: { ...req.current, tokensConsumed: tentative },
  };
}

export function windowKey(date: Date, granularity: 'hourly' | 'daily' | 'monthly'): string {
  const iso = date.toISOString();
  if (granularity === 'monthly') return iso.slice(0, 7);
  if (granularity === 'daily') return iso.slice(0, 10);
  return iso.slice(0, 13).replace('T', '-');
}
