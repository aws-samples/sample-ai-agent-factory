/**
 * AuditConstruct
 *
 * Deployed into the Audit account. Stands up a CloudWatch cross-account
 * observability (OAM) sink so workload and platform accounts can ship
 * metrics + logs + traces into a single observability plane (R-OBS-002).
 *
 * Spec §5 observability is body-missing from the source PDF, so the details
 * are derived from the AWS Well-Architected GenAI Lens, NIST 800-53 Rev 5,
 * and the AWS OAM documentation.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Stack } from "aws-cdk-lib";
import { CfnSink } from "aws-cdk-lib/aws-oam";
import { Construct } from "constructs";

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

    const trustedAccountIds = [...new Set(props.trustedAccountIds ?? [])];
    if (!props.organizationId && trustedAccountIds.length === 0) {
      throw new Error(
        "AuditConstruct requires either organizationId or trustedAccountIds to scope the OAM sink policy.",
      );
    }
    for (const accountId of trustedAccountIds) {
      if (!/^\d{12}$/.test(accountId)) {
        throw new Error(
          `AuditConstruct trusted account id '${accountId}' must contain exactly 12 digits.`,
        );
      }
    }

    const actions = ["oam:CreateLink", "oam:UpdateLink"];
    const statements: Record<string, unknown>[] = [];
    if (props.organizationId) {
      // The wildcard principal is constrained by PrincipalOrgID. Never emit it
      // without this condition.
      statements.push({
        Sid: "AllowOamOrganizationLinks",
        Effect: "Allow",
        Principal: { AWS: "*" },
        Action: actions,
        Resource: "*",
        Condition: {
          "ForAnyValue:StringEquals": {
            "aws:PrincipalOrgID": props.organizationId,
          },
        },
      });
    }
    if (trustedAccountIds.length > 0) {
      // Standalone validation accounts are an independent allow path. Do not
      // attach the Organization condition: these principals may deliberately
      // sit outside the configured Organization.
      statements.push({
        Sid: "AllowOamExplicitAccountLinks",
        Effect: "Allow",
        Principal: {
          AWS: trustedAccountIds.map(
            (accountId) => `arn:aws:iam::${accountId}:root`,
          ),
        },
        Action: actions,
        Resource: "*",
      });
    }

    const sinkPolicy: Record<string, unknown> = {
      Version: "2012-10-17",
      Statement: statements,
    };

    this.oamSink = new CfnSink(this, "AgenticAiOamSink", {
      name: `agenticai-audit-oam-sink-${Stack.of(this).region}`,
      policy: sinkPolicy,
    });
  }
}
