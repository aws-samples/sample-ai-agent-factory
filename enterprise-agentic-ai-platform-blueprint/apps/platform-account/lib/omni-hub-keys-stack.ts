/**
 * OmniHubKeysStack — the two customer managed keys of a CloudWatch Omni hub,
 * deployed into the Audit/Observability account by operators, ahead of
 * OmniHubStack (`omni-hub-keys` stage, one per hosting Region).
 *
 * The Audit account's scoped CloudFormation execution policy
 * (`render-cfn-execution-policy.py audit`) lets the hub stage use keys that
 * carry `agenticai:omni-hub=true` but never create, tag or change a key, so
 * key custody stays with operators: deploy this stack with operator
 * credentials, then pass its two outputs to the hub stage as
 * `agenticai/omniHubKeyArns={"space":"<OmniSpaceKeyArn>","centralizedLogs":"<OmniCentralizedLogsKeyArn>"}`.
 *
 * The space key carries the documented Omni key policy without the statements
 * that name the operator and Dataset integration roles (they do not exist yet;
 * the hub grants those roles their key use through IAM, which the key policy's
 * root statement delegates to) and the `cw-omni=true` tag the AWS-managed
 * space access policy keys on; the centralized-logs key is the same
 * OmniCentralizedLogsKeyConstruct the hub stack creates in default mode.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CfnOutput, RemovalPolicy, Stack, Tags, type StackProps } from "aws-cdk-lib";
import type { Key } from "aws-cdk-lib/aws-kms";
import { Construct } from "constructs";

import { OmniCentralizedLogsKeyConstruct, createOmniSpaceKey } from "@agenticai/omni";

export interface OmniHubKeysStackProps extends StackProps {
  /** Keep the keys when the stack is deleted. Default RETAIN; use DESTROY only for throwaway validation. */
  readonly keyRemovalPolicy?: RemovalPolicy;
  readonly tags?: Record<string, string>;
}

export class OmniHubKeysStack extends Stack {
  /** Space and Dataset key (`alias/agenticai/omni-space-<region>`). */
  readonly spaceKey: Key;
  /** Destination key of the centralization rule (`alias/agenticai/omni-centralized-logs-<region>`). */
  readonly centralizedLogsKey: Key;

  constructor(scope: Construct, id: string, props: OmniHubKeysStackProps = {}) {
    const { keyRemovalPolicy, tags, ...stackProps } = props;
    super(scope, id, stackProps);

    // Ownership marker the audit execution policy and the hub-role boundary
    // key on: the hub's execution role may use keys that carry it, nothing else.
    Tags.of(this).add("agenticai:omni-hub", "true");

    const removalPolicy = keyRemovalPolicy ?? RemovalPolicy.RETAIN;
    this.spaceKey = createOmniSpaceKey(this, "SpaceKey", { removalPolicy, tags });
    this.centralizedLogsKey = new OmniCentralizedLogsKeyConstruct(this, "CentralizedLogs", {
      removalPolicy,
      tags,
    }).key;

    new CfnOutput(this, "OmniSpaceKeyArn", {
      value: this.spaceKey.keyArn,
      description: "Pass to the omni-hub stage as agenticai/omniHubKeyArns.space.",
    });
    new CfnOutput(this, "OmniCentralizedLogsKeyArn", {
      value: this.centralizedLogsKey.keyArn,
      description: "Pass to the omni-hub stage as agenticai/omniHubKeyArns.centralizedLogs.",
    });
  }
}
