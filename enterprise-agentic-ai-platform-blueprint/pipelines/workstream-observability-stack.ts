/**
 * One CloudWatch OAM source link for a distinct Workstream account and Region.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Stack, type StackProps } from "aws-cdk-lib";
import { OamSourceLinkConstruct } from "@agenticai/observability";
import { Construct } from "constructs";

import {
  applyPipelineResourceTags,
  type PipelineResourceTags,
} from "./pipeline-artifacts";

export interface WorkstreamObservabilityStackProps extends StackProps {
  readonly sinkArn: string;
  readonly resourceTags: PipelineResourceTags;
}

export class WorkstreamObservabilityStack extends Stack {
  constructor(
    scope: Construct,
    id: string,
    props: WorkstreamObservabilityStackProps,
  ) {
    const { sinkArn, resourceTags, ...stackProps } = props;
    super(scope, id, stackProps);

    applyPipelineResourceTags(this, resourceTags);
    new OamSourceLinkConstruct(this, "OamSourceLink", { sinkArn });
  }
}
