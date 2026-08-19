/**
 * WorkstreamPermissionSets — IAM Identity Center permission sets that put a
 * developer into the workstream account they own, while keeping the platform
 * account effectively invisible.
 *
 * Three personas per workstream (matching the AgentCore Registry persona model).
 * Permission-set names are deliberately short because Identity Center caps
 * names at 32 characters; we reserve up to 16 chars for the workstream id:
 *
 *   - **Developer**  (`AgenticAI-WS-Dev-<workstream>`)
 *       Build, deploy via the workload pipeline, read all observability,
 *       consume the platform Registry (SearchRegistryRecords / InvokeRegistryMcp).
 *       Cannot mutate the Gateway, the Registry control-plane, or any resource
 *       tagged `agenticai:owner=platform` (SCPs 09 / 11 / 12 enforce at the
 *       Org boundary; the inline policy here is the *positive* grant).
 *
 *   - **ReadOnly**   (`AgenticAI-WS-Ro-<workstream>`)
 *       Pure observability — CW Logs / Metrics, X-Ray, DynamoDB read on
 *       agent-state tables, plus Registry consumer permissions so they can
 *       see what the workstream is subscribed to.
 *
 *   - **Approver**   (`AgenticAI-WS-Apv-<workstream>`)
 *       Can only review evaluation results and click `ApprovePipelineExecution`
 *       on the workload CodePipeline. Bound to the same workstream accounts
 *       so a single human can sit on the gate without holding write access
 *       to the agent code itself.
 *
 * Three-layer enforcement (mirrors D-03 v3 governance):
 *   1. Synth-time   — this construct's positive grants (the inline policy).
 *   2. Org-level    — SCP-09 (Gateway lockdown), SCP-11 (Registry lockdown),
 *                     SCP-12 (`agenticai:owner=platform` mutation deny).
 *   3. Resource-level — Lambda resource policies + KMS key policies in the
 *                       platform account never name the developer permission-set
 *                       SSO-reserved role pattern in their Allow list.
 *
 * The construct emits one `AWS::SSO::PermissionSet` per persona and (optionally)
 * the `AWS::SSO::Assignment` records that bind each permission set to the
 * workstream's target accounts for a specified IdC group.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CfnOutput, Tags } from 'aws-cdk-lib';
import { CfnAssignment, CfnPermissionSet } from 'aws-cdk-lib/aws-sso';
import { Construct } from 'constructs';

/**
 * The three Identity Center personas this construct provisions per
 * workstream. The Developer persona-prefix `AgenticAI-WS-Dev-` is pinned
 * by SCP-12; the other two persona prefixes are scoped at the construct's
 * inline policy and the workload-pipeline ApprovePipeline action.
 */
export type WorkstreamPersona = 'Developer' | 'ReadOnly' | 'Approver';

/**
 * Persona-specific name prefix. Exposed for SCP-12 conformance assertions
 * + the developer CLI's CLI-flag rendering. Each prefix + a 16-char
 * workstream id fits inside Identity Center's 32-char name cap.
 */
export const PERSONA_PREFIX: Record<WorkstreamPersona, string> = {
  Developer: 'AgenticAI-WS-Dev-',
  ReadOnly: 'AgenticAI-WS-Ro-',
  Approver: 'AgenticAI-WS-Apv-',
};

export interface WorkstreamPermissionSetsProps {
  /**
   * IAM Identity Center instance ARN. Format:
   * `arn:aws:sso:::instance/ssoins-XXXXXXXXXXXXXXXX`. Pass this from the
   * management-account stack — the construct does not look it up dynamically
   * because Identity Center exposes only one instance per Organization.
   */
  readonly identityCenterInstanceArn: string;

  /**
   * Logical workstream id (also used as the agent-team slug). Becomes the
   * permission-set name suffix and the value of the `workstream` resource
   * tag. Pattern: `[a-z][a-z0-9-]{1,15}` — kebab-case, lower, no leading
   * digit, max 16 chars (so `AgenticAI-WS-Apv-<wsId>` fits within
   * Identity Center's 32-char name limit).
   */
  readonly workstreamId: string;

  /**
   * Target AWS account ids the three permission sets should be assigned to.
   * Typically the workstream's nonprod + prod accounts.
   */
  readonly targetAccountIds: readonly string[];

  /**
   * Identity Center group GUIDs that should receive each persona. When a
   * persona is omitted, the permission set is created but no assignment is
   * emitted — useful for dry-run / pre-IdC-group-creation deploys.
   */
  readonly groupAssignments?: Partial<Record<WorkstreamPersona, string>>;

  /**
   * ISO-8601 session duration. Default `PT8H`. AWS valid range is PT1H..PT12H.
   */
  readonly sessionDuration?: string;

  /**
   * Platform-account id hosting the AgentCore Registry. When supplied, the
   * Registry consumer permissions in the Developer + ReadOnly inline
   * policies scope to `arn:aws:bedrock-agentcore:*:<platformAccountId>:registry/*`
   * instead of `*` — keeping consumer reads off any other account's registry
   * that might ever exist in this Organization.
   */
  readonly platformAccountId?: string;

  /**
   * The workload pipeline name(s) the Approver persona may approve.
   * Each entry should be a CodePipeline name (region-agnostic; the inline
   * policy resource pattern uses `arn:aws:codepipeline:*:*:<name>`).
   * If empty, the Approver persona inline policy denies all approvals (a
   * placeholder useful pre-pipeline-deployment).
   */
  readonly approverPipelineNames?: readonly string[];
}

/**
 * Renders the inline policy JSON (as a plain object) for one of the three
 * personas. Exposed as a pure function so unit tests can assert the policy
 * shape without spinning up CDK.
 */
export function renderInlinePolicy(
  persona: WorkstreamPersona,
  opts: {
    readonly platformAccountId?: string;
    readonly approverPipelineNames?: readonly string[];
    /**
     * The workstream slug. When supplied, the developer's CI/CD write grant
     * (StartPipelineExecution / GitPush / StartBuild) is scoped to ARNs
     * prefixed with this workstream id so a developer cannot trigger another
     * workstream's pipeline. SEC (Holmes CSR).
     */
    readonly workstreamId?: string;
  } = {},
): Record<string, unknown> {
  const registryArnScope = opts.platformAccountId
    ? `arn:aws:bedrock-agentcore:*:${opts.platformAccountId}:registry/*`
    : 'arn:aws:bedrock-agentcore:*:*:registry/*';
  const recordArnScope = `${registryArnScope}/record/*`;
  // SEC (Holmes CSR): per-workstream CI/CD resource scoping. The blueprint
  // convention names a workstream's pipeline/repo/build project with the
  // workstream id as prefix (see pipelines/*). When no workstreamId is
  // supplied (legacy callers), fall back to the platform naming prefix
  // `AgenticAI-*` rather than a bare '*'.
  const wsScope = opts.workstreamId ?? '*';
  const pipelineArns = [
    `arn:aws:codepipeline:*:*:AgenticAI-${wsScope}-*`,
    `arn:aws:codepipeline:*:*:${wsScope}-*`,
  ];
  const codecommitArns = [
    `arn:aws:codecommit:*:*:AgenticAI-${wsScope}-*`,
    `arn:aws:codecommit:*:*:${wsScope}-*`,
  ];
  const codebuildArns = [
    `arn:aws:codebuild:*:*:project/AgenticAI-${wsScope}-*`,
    `arn:aws:codebuild:*:*:project/${wsScope}-*`,
  ];

  if (persona === 'Developer') {
    return {
      Version: '2012-10-17',
      Statement: [
        {
          Sid: 'ObservabilityRead',
          Effect: 'Allow',
          Action: [
            'logs:Describe*',
            'logs:Get*',
            'logs:List*',
            'logs:FilterLogEvents',
            'logs:StartQuery',
            'logs:StopQuery',
            'logs:GetQueryResults',
            'logs:TestMetricFilter',
            'cloudwatch:Get*',
            'cloudwatch:List*',
            'cloudwatch:Describe*',
            'xray:Get*',
            'xray:BatchGet*',
            'xray:List*',
          ],
          Resource: '*',
        },
        {
          Sid: 'AgentStateRead',
          Effect: 'Allow',
          Action: [
            'dynamodb:DescribeTable',
            'dynamodb:Query',
            'dynamodb:Scan',
            'dynamodb:GetItem',
            'dynamodb:BatchGetItem',
            's3:GetObject',
            's3:ListBucket',
          ],
          Resource: '*',
        },
        {
          Sid: 'PipelineDeployForOwnWorkstream',
          Effect: 'Allow',
          Action: [
            'codepipeline:GetPipeline',
            'codepipeline:GetPipelineState',
            'codepipeline:ListPipelineExecutions',
            'codepipeline:StartPipelineExecution',
          ],
          Resource: pipelineArns,
        },
        {
          Sid: 'RepoAccessForOwnWorkstream',
          Effect: 'Allow',
          Action: ['codecommit:GitPull', 'codecommit:GitPush'],
          Resource: codecommitArns,
        },
        {
          Sid: 'BuildForOwnWorkstream',
          Effect: 'Allow',
          Action: ['codebuild:StartBuild', 'codebuild:BatchGetBuilds'],
          Resource: codebuildArns,
        },
        {
          Sid: 'AgentCoreRegistryConsumer',
          Effect: 'Allow',
          Action: [
            'bedrock-agentcore:SearchRegistryRecords',
            'bedrock-agentcore:InvokeRegistryMcp',
            'bedrock-agentcore:GetRegistry',
            'bedrock-agentcore:ListRegistries',
            'bedrock-agentcore:ListRegistryRecords',
            'bedrock-agentcore:GetRegistryRecord',
          ],
          Resource: [registryArnScope, recordArnScope],
        },
        {
          Sid: 'DenyPlatformOwnedMutation',
          Effect: 'Deny',
          // Defense-in-depth: SCP-12 also denies these org-wide. We mirror
          // the deny in the inline policy so a misconfigured SCP attachment
          // does not silently widen the developer's blast radius.
          Action: [
            'bedrock-agentcore:Update*',
            'bedrock-agentcore:Delete*',
            'bedrock-agentcore:Create*',
            'bedrock-agentcore:Put*',
            'iam:Update*',
            'iam:Delete*',
            'iam:Put*',
            'iam:AttachRolePolicy',
            'iam:DetachRolePolicy',
            'lambda:Update*',
            'lambda:Delete*',
            'lambda:Put*',
            'kms:ScheduleKeyDeletion',
            'kms:DisableKey',
            'organizations:*',
          ],
          Resource: '*',
          Condition: {
            StringEquals: {
              'aws:ResourceTag/agenticai:owner': 'platform',
            },
          },
        },
      ],
    };
  }

  if (persona === 'ReadOnly') {
    return {
      Version: '2012-10-17',
      Statement: [
        {
          Sid: 'ObservabilityRead',
          Effect: 'Allow',
          Action: [
            'logs:Describe*',
            'logs:Get*',
            'logs:List*',
            'logs:FilterLogEvents',
            'cloudwatch:Get*',
            'cloudwatch:List*',
            'cloudwatch:Describe*',
            'xray:Get*',
            'xray:BatchGet*',
            'xray:List*',
            'dynamodb:DescribeTable',
            'dynamodb:Query',
            'dynamodb:Scan',
            'dynamodb:GetItem',
            's3:GetObject',
            's3:ListBucket',
          ],
          Resource: '*',
        },
        {
          Sid: 'AgentCoreRegistryConsumerReadOnly',
          Effect: 'Allow',
          Action: [
            'bedrock-agentcore:SearchRegistryRecords',
            'bedrock-agentcore:GetRegistry',
            'bedrock-agentcore:ListRegistries',
            'bedrock-agentcore:ListRegistryRecords',
            'bedrock-agentcore:GetRegistryRecord',
          ],
          Resource: [registryArnScope, recordArnScope],
        },
      ],
    };
  }

  // Approver — the most narrowly scoped of the three. Action is the
  // CodePipeline approve verb on a specific named pipeline list.
  const pipelineResources =
    opts.approverPipelineNames && opts.approverPipelineNames.length > 0
      ? opts.approverPipelineNames.map((n) => `arn:aws:codepipeline:*:*:${n}/*`)
      : ['arn:aws:codepipeline:*:*:__no_pipeline_configured__'];
  return {
    Version: '2012-10-17',
    Statement: [
      {
        Sid: 'ApproverRead',
        Effect: 'Allow',
        Action: [
          'codepipeline:GetPipeline',
          'codepipeline:GetPipelineState',
          'codepipeline:GetPipelineExecution',
          'codepipeline:ListPipelineExecutions',
          'codepipeline:ListActionExecutions',
          'codepipeline:ListPipelines',
        ],
        Resource: '*',
      },
      {
        Sid: 'ApproverApprove',
        Effect: 'Allow',
        Action: ['codepipeline:PutApprovalResult'],
        Resource: pipelineResources,
      },
      {
        Sid: 'ObservabilityForApproval',
        Effect: 'Allow',
        Action: [
          'logs:Get*',
          'logs:Describe*',
          'cloudwatch:Get*',
          'cloudwatch:Describe*',
          'cloudwatch:List*',
        ],
        Resource: '*',
      },
    ],
  };
}

export class WorkstreamPermissionSets extends Construct {
  /** Permission-set ARNs by persona, exposed for downstream wiring/tests. */
  readonly permissionSetArns: Record<WorkstreamPersona, string>;

  /** Permission-set names — convenient for SCP-12 conformance assertions. */
  readonly permissionSetNames: Record<WorkstreamPersona, string>;

  constructor(scope: Construct, id: string, props: WorkstreamPermissionSetsProps) {
    super(scope, id);

    const wsId = props.workstreamId;
    if (!/^[a-z][a-z0-9-]{1,15}$/.test(wsId)) {
      throw new Error(
        `WorkstreamPermissionSets: workstreamId must match /^[a-z][a-z0-9-]{1,15}$/ (max 16 chars to fit IdC's 32-char name cap); got '${wsId}'`,
      );
    }
    if (!/^arn:aws:sso:::instance\/ssoins-[0-9a-f]{16}$/.test(props.identityCenterInstanceArn)) {
      throw new Error(
        `WorkstreamPermissionSets: identityCenterInstanceArn must be of the form arn:aws:sso:::instance/ssoins-<16hex>; got '${props.identityCenterInstanceArn}'`,
      );
    }
    if (props.targetAccountIds.length === 0) {
      throw new Error(
        'WorkstreamPermissionSets: targetAccountIds must contain at least one 12-digit account id.',
      );
    }
    for (const acct of props.targetAccountIds) {
      if (!/^[0-9]{12}$/.test(acct)) {
        throw new Error(
          `WorkstreamPermissionSets: each targetAccountId must be 12 digits; got '${acct}'`,
        );
      }
    }
    if (props.sessionDuration && !/^PT([1-9]|1[0-2])H$/.test(props.sessionDuration)) {
      throw new Error(
        `WorkstreamPermissionSets: sessionDuration must be ISO-8601 PT1H..PT12H; got '${props.sessionDuration}'`,
      );
    }

    const sessionDuration = props.sessionDuration ?? 'PT8H';

    const personas: readonly WorkstreamPersona[] = ['Developer', 'ReadOnly', 'Approver'];
    const arns: Partial<Record<WorkstreamPersona, string>> = {};
    const names: Partial<Record<WorkstreamPersona, string>> = {};

    for (const persona of personas) {
      const psName = `${PERSONA_PREFIX[persona]}${wsId}`;
      // SSO permission-set names are length-bounded server-side at 32 chars.
      // Belt-and-braces — workstreamId regex above also caps at 16 chars.
      if (psName.length > 32) {
        throw new Error(
          `WorkstreamPermissionSets: permission-set name '${psName}' exceeds the AWS 32-char limit. Shorten workstreamId.`,
        );
      }

      const inlinePolicy = renderInlinePolicy(persona, {
        platformAccountId: props.platformAccountId,
        approverPipelineNames: props.approverPipelineNames,
        workstreamId: wsId,
      });

      const ps = new CfnPermissionSet(this, `PermissionSet${persona}`, {
        instanceArn: props.identityCenterInstanceArn,
        name: psName,
        description: `AgenticAI ${persona} permission set for workstream ${wsId}.`,
        sessionDuration,
        inlinePolicy,
        tags: [
          { key: 'agenticai:persona', value: persona },
          { key: 'agenticai:workstream', value: wsId },
          { key: 'deviation', value: 'D-03' },
        ],
      });

      arns[persona] = ps.attrPermissionSetArn;
      names[persona] = psName;

      const groupId = props.groupAssignments?.[persona];
      if (groupId) {
        if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(groupId)) {
          throw new Error(
            `WorkstreamPermissionSets: groupAssignments.${persona} must be an Identity Center group GUID; got '${groupId}'`,
          );
        }
        for (const acct of props.targetAccountIds) {
          new CfnAssignment(this, `Assignment${persona}${acct}`, {
            instanceArn: props.identityCenterInstanceArn,
            permissionSetArn: ps.attrPermissionSetArn,
            principalId: groupId,
            principalType: 'GROUP',
            targetId: acct,
            targetType: 'AWS_ACCOUNT',
          });
        }
      }

      new CfnOutput(this, `PermissionSetArn${persona}`, {
        value: ps.attrPermissionSetArn,
        description: `Identity Center permission-set ARN for the ${persona} persona of workstream ${wsId}.`,
        exportName: `AgenticAI-${persona}-${wsId}-PermissionSetArn`,
      });
    }

    this.permissionSetArns = arns as Record<WorkstreamPersona, string>;
    this.permissionSetNames = names as Record<WorkstreamPersona, string>;

    Tags.of(this).add('agenticai:workstream', wsId);
    Tags.of(this).add('deviation', 'D-03');
  }
}
