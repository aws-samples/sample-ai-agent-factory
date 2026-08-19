/**
 * AuditConstruct
 *
 * Deployed into the Audit account. Stands up a CloudWatch cross-account
 * observability (OAM) sink so workload and platform accounts can ship
 * metrics + logs + traces into a single observability plane (R-OBS-002).
 *
 * Spec §5 observability is body-missing from the source PDF, so the details
 * are derived per `_research/R11-wa-genai-nist-nag.md` + AWS OAM docs.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Stack } from 'aws-cdk-lib';
import { CfnSink } from 'aws-cdk-lib/aws-oam';
import { Construct } from 'constructs';

export interface AuditConstructProps {
  /**
   * AWS Organization id so the OAM sink policy trusts all Organization members.
   * Alternatively use `trustedAccountIds` to specify individual accounts.
   */
  readonly organizationId?: string;

  /**
   * Explicit workload/platform account ids allowed to push observability
   * data into the sink. If omitted, uses `organizationId`-wide trust.
   */
  readonly trustedAccountIds?: readonly string[];
}

export class AuditConstruct extends Construct {
  readonly oamSink: CfnSink;

  constructor(scope: Construct, id: string, props: AuditConstructProps) {
    super(scope, id);

    if (!props.organizationId && !props.trustedAccountIds) {
      throw new Error(
        'AuditConstruct requires either organizationId or trustedAccountIds to scope the OAM sink policy.',
      );
    }

    // Build sink-policy principal set.
    // SEC (Holmes CSR): the `{ AWS: '*' }` branch is used ONLY together with
    // the `aws:PrincipalOrgID` Condition below — it is the standard,
    // AWS-recommended pattern for organization-wide OAM sink sharing. The
    // wildcard principal is NOT open: only accounts belonging to the specified
    // AWS Organization can link to this sink. Never copy `Principal: '*'`
    // without an equivalent org/account Condition. When an explicit
    // `trustedAccountIds` list is supplied we scope to those account roots
    // instead and drop the wildcard entirely.
    const policyPrincipal: Record<string, unknown> = props.trustedAccountIds
      ? {
          AWS: props.trustedAccountIds.map(
            (acct) => `arn:aws:iam::${acct}:root`,
          ),
        }
      : { AWS: '*' };

    const policyCondition: Record<string, unknown> | undefined = props.organizationId
      ? {
          'ForAnyValue:StringEquals': {
            'aws:PrincipalOrgID': props.organizationId,
          },
        }
      : undefined;

    const sinkPolicy: Record<string, unknown> = {
      Version: '2012-10-17',
      Statement: [
        {
          Sid: 'AllowOamCrossAccountPut',
          Effect: 'Allow',
          Principal: policyPrincipal,
          Action: [
            'oam:CreateLink',
            'oam:UpdateLink',
          ],
          Resource: '*',
          ...(policyCondition ? { Condition: policyCondition } : {}),
        },
      ],
    };

    this.oamSink = new CfnSink(this, 'AgenticAiOamSink', {
      name: `agenticai-audit-oam-sink-${Stack.of(this).region}`,
      policy: sinkPolicy,
    });
  }
}
