/**
 * OamSourceLinkConstruct — emitted in the workload/platform account;
 * shares CloudWatch metrics, logs, and traces with the audit-account OAM sink
 * created by packages/landing-zone/AuditConstruct.
 *
 * Spec reference: R-OBS-002 (derived from §5.1 + `_research/R11…`).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CfnLink } from 'aws-cdk-lib/aws-oam';
import { Stack } from 'aws-cdk-lib';
import { Construct } from 'constructs';

export interface OamSourceLinkConstructProps {
  /**
   * Audit-account OAM sink ARN. Output of `AuditConstruct` (Phase 2).
   * Typically pulled from CloudFormation cross-account imports or SSM.
   */
  readonly sinkArn: string;

  /**
   * Which telemetry categories to share. Default all three (Metrics, Logs, Traces).
   */
  readonly resourceTypes?: readonly ('AWS::CloudWatch::Metric' | 'AWS::Logs::LogGroup' | 'AWS::XRay::Trace')[];

  /**
   * Optional label policy — a JSON policy that selectively filters metrics/logs
   * that cross the link. Defaults to no filter (everything flows).
   */
  readonly labelPolicy?: Record<string, unknown>;
}

export class OamSourceLinkConstruct extends Construct {
  readonly link: CfnLink;

  constructor(scope: Construct, id: string, props: OamSourceLinkConstructProps) {
    super(scope, id);

    const stack = Stack.of(this);
    const resourceTypes = props.resourceTypes ?? [
      'AWS::CloudWatch::Metric',
      'AWS::Logs::LogGroup',
      'AWS::XRay::Trace',
    ];

    this.link = new CfnLink(this, 'Link', {
      labelTemplate: `${stack.account}-${stack.region}`,
      resourceTypes: [...resourceTypes],
      sinkIdentifier: props.sinkArn,
      ...(props.labelPolicy ? { linkConfiguration: {} } : {}),
    });
  }
}
