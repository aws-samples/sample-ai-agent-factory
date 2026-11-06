/**
 * One CloudWatch OAM source link for a distinct Workstream account and Region,
 * plus (opt-in, ADR-13/ADR-15) the account-wide Transaction Search setting
 * that CloudWatch Omni and AgentCore Evaluations need before any span arrives
 * or centralizes.
 *
 * Transaction Search stays opt-in: it changes account-wide behaviour and is
 * billable. When enabled here it is declarative, so `cdk destroy` reverts the
 * account to its previous destination instead of leaving the setting behind.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Stack, type StackProps } from "aws-cdk-lib";
import { CfnTransactionSearchConfig } from "aws-cdk-lib/aws-xray";

import { TransactionSearchConstruct } from "@agenticai/omni";
import { OamSourceLinkConstruct } from "@agenticai/observability";
import { Construct } from "constructs";

import {
  applyPipelineResourceTags,
  type PipelineResourceTags,
} from "./pipeline-artifacts";

export interface WorkstreamObservabilityStackProps extends StackProps {
  readonly sinkArn: string;
  readonly resourceTags: PipelineResourceTags;
  /**
   * Enable X-Ray Transaction Search (trace-segment destination CloudWatch
   * Logs) in this account and Region. Required for CloudWatch Omni traces and
   * for trace centralization into the hub. Default false (ADR-13).
   */
  readonly enableTransactionSearch?: boolean;
  /** Indexing percentage for Transaction Search (0-100). Default 1. */
  readonly transactionSearchIndexingPercentage?: number;
}

export class WorkstreamObservabilityStack extends Stack {
  readonly transactionSearch?: CfnTransactionSearchConfig;

  constructor(
    scope: Construct,
    id: string,
    props: WorkstreamObservabilityStackProps,
  ) {
    const {
      sinkArn,
      resourceTags,
      enableTransactionSearch,
      transactionSearchIndexingPercentage,
      ...stackProps
    } = props;
    super(scope, id, stackProps);

    applyPipelineResourceTags(this, resourceTags);
    new OamSourceLinkConstruct(this, "OamSourceLink", { sinkArn });

    if (enableTransactionSearch) {
      const indexing = transactionSearchIndexingPercentage ?? 1;
      if (indexing < 0 || indexing > 100) {
        throw new Error(
          "WorkstreamObservabilityStack: transactionSearchIndexingPercentage must be between 0 and 100.",
        );
      }
      // ADR-13 amended: the resource policy for X-Ray is created first; a bare
      // TransactionSearchConfig is refused (403) in an account that never had it on.
      this.transactionSearch = new TransactionSearchConstruct(this, "TransactionSearch", {
        indexingPercentage: indexing,
      }).config;
    }
  }
}
