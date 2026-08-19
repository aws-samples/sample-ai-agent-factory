/**
 * SCP-12 — Developer Permission Set / Platform-Tag Mutation Deny.
 *
 * Under v0.5.0, developers log into their workstream account via Identity
 * Center permission sets (`AgenticAI-WS-Dev-<workstream>`). They get full
 * read access to observability, the right to deploy via the workload
 * pipeline, and consumer-only access to the platform-account Registry. They
 * MUST NOT be able to mutate any resource that the platform team owns —
 * even if a stale ARN survives an Org reorg.
 *
 * The deny condition fires when ALL three are true:
 *   1. Resource is tagged `agenticai:owner=platform`
 *   2. Principal ARN matches the developer permission-set role pattern
 *      (`arn:aws:iam::*:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_AgenticAI-WS-Dev-*`)
 *   3. Action is a generic write (TagResource, UntagResource, Delete*, Put*,
 *      Update*) — narrowed via `NotAction` is structurally simpler so we
 *      use a lifecycle prefix list.
 *
 * Like SCP-10, the deny is scoped on principal so platform automation,
 * CDK Pipelines, and AWS service principals are unaffected.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { toScpDefinition, type ScpDefinition } from './index';

export interface Scp12Options {
  /**
   * The Identity Center permission-set name prefix that identifies developer
   * permission sets. The SCP matches the SSO-reserved role pattern derived
   * from this prefix.
   *
   * Default: `AgenticAI-WS-Dev-`. Identity Center mints role names of the
   * form `AWSReservedSSO_<permissionSetName>_<16hex>` under
   * `arn:aws:iam::*:role/aws-reserved/sso.amazonaws.com/`. The prefix is
   * deliberately short because Identity Center caps PermissionSet names at
   * 32 characters; `AgenticAI-WS-Dev-` (16 chars) leaves room for a
   * workstream id of up to 16 chars.
   */
  readonly developerPermissionSetPrefix?: string;
}

export function scp12DeveloperPlatformTagDeny(opts: Scp12Options = {}): ScpDefinition {
  const prefix = opts.developerPermissionSetPrefix ?? 'AgenticAI-WS-Dev-';
  const ssoRoleArn = `arn:aws:iam::*:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_${prefix}*`;
  const ssoSessionArn = `arn:aws:sts::*:assumed-role/AWSReservedSSO_${prefix}*/*`;

  const body = {
    Version: '2012-10-17',
    Statement: [
      {
        Sid: 'DenyDeveloperMutationOfPlatformOwnedResources',
        Effect: 'Deny',
        // Lifecycle write actions — narrow enough to avoid catching reads
        // and broad enough that a developer cannot get cute with rare
        // mutator verbs (Modify*, Detach*, Disable*, etc.).
        Action: [
          'bedrock-agentcore:Update*',
          'bedrock-agentcore:Delete*',
          'bedrock-agentcore:Create*',
          'bedrock-agentcore:Put*',
          'bedrock-agentcore:TagResource',
          'bedrock-agentcore:UntagResource',
          'iam:Update*',
          'iam:Delete*',
          'iam:Put*',
          'iam:AttachRolePolicy',
          'iam:DetachRolePolicy',
          'iam:TagRole',
          'iam:UntagRole',
          'lambda:Update*',
          'lambda:Delete*',
          'lambda:Put*',
          'lambda:TagResource',
          'lambda:UntagResource',
          'kms:ScheduleKeyDeletion',
          'kms:DisableKey',
          'kms:Update*',
          'kms:Put*',
          'organizations:Update*',
          'organizations:Delete*',
          'organizations:AttachPolicy',
          'organizations:DetachPolicy',
        ],
        Resource: '*',
        Condition: {
          // Fire only when the resource is owned by the platform team.
          StringEquals: {
            'aws:ResourceTag/agenticai:owner': 'platform',
          },
          // Narrow to the developer permission-set assumed-role principals.
          ArnLike: {
            'aws:PrincipalArn': [ssoRoleArn, ssoSessionArn],
          },
          BoolIfExists: {
            'aws:PrincipalIsAWSService': 'false',
          },
        },
      },
    ],
  };

  return toScpDefinition(
    'scp-12',
    'AgenticAI-SCP-12-DeveloperPlatformTagDeny',
    'Deny developer Identity Center permission sets from mutating any resource tagged agenticai:owner=platform (D-03 v3 / v0.5.0).',
    body,
  );
}
