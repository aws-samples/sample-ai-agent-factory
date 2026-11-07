/**
 * OmniCentralizationRuleConstruct
 *
 * One AWS::ObservabilityAdmin::OrganizationCentralizationRule that replicates
 * the blueprint's logs, metrics and (where Transaction Search is on) traces
 * from the Platform and Workstream accounts into the account and Region that
 * host the CloudWatch Omni fleet space. Created by the Organizations
 * management account or a CloudWatch delegated administrator (ADR-15).
 *
 * Defaults encode the blueprint's governance:
 *   - log-group selection limited to the blueprint prefixes (never "all");
 *   - destination names keep source account and Region recoverable;
 *   - every destination log group the rule creates is encrypted under the
 *     destination account's CMK (EncryptionScope NEW_DESTINATION_LOG_GROUPS);
 *   - the Context Graph is centralized so the application map spans accounts;
 *   - metrics are centralized (the service currently supports only `*`);
 *   - a backup Region is opt-in because it is a billable second copy.
 *
 * The rule is declared with CfnResource against the documented CloudFormation
 * schema (capitalised property names) so the construct can carry properties
 * the pinned aws-cdk-lib L1 (CfnOrganizationCentralizationRule) does not yet
 * model, and because CDK's override merge drops empty structures.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CfnOutput, CfnResource, Stack, Token } from 'aws-cdk-lib';
import {
  ArnPrincipal,
  Effect,
  PolicyDocument,
  PolicyStatement,
  Role,
} from 'aws-cdk-lib/aws-iam';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

import { assertAccountId } from './naming';

/** Log-group name patterns the blueprint emits and the hub must see. */
export const BLUEPRINT_CENTRALIZED_LOG_GROUP_PATTERNS: readonly string[] = [
  '/agenticai/%',
  '/aws/bedrock-agentcore/%',
  '/aws/vendedlogs/bedrock-agentcore/%',
  'aws/spans',
];

export const DEFAULT_DESTINATION_LOG_GROUP_PATTERN =
  '/centralized/${source.accountId}/${source.region}${source.logGroup}';

export interface OmniCentralizationRuleConstructProps {
  /** Rule name, `^[0-9A-Za-z-]+$`, at most 100 characters. Replacement on change. */
  readonly ruleName: string;
  /**
   * Source scope expression, for example
   * `OrganizationalUnitId IN ('ou-aaaa-11111111','ou-aaaa-22222222')`
   * or `AccountId IN ('111111111111','222222222222')`.
   */
  readonly sourceScope: string;
  /** Source Regions to collect from. */
  readonly sourceRegions: readonly string[];
  /** Destination (hub) account id. */
  readonly destinationAccountId: string;
  /** Destination (hub) Region, the Region of the Omni space. */
  readonly destinationRegion: string;
  /**
   * Destination CMK ARN (in the hub account and Region, tagged
   * `LogsManaged=true`) under which every new destination log group is
   * encrypted. Strongly recommended; omit only for a throwaway sandbox.
   */
  readonly destinationLogsKmsKeyArn?: string;
  /** Log-group selection patterns. Default: the blueprint prefixes. */
  readonly logGroupPatterns?: readonly string[];
  /** Destination log-group name pattern. Default keeps account and Region. */
  readonly destinationLogGroupPattern?: string;
  /**
   * Centralize the Context Graph (cross-account application map). The
   * Observability Admin API accepts `SourceContextGraphConfiguration`, but the
   * published CloudFormation schema for OrganizationCentralizationRule does not
   * yet, so this defaults to FALSE and is applied post-deploy by
   * `scripts/omni/omni_space_bootstrap.py centralization enable-context-graph`.
   * Set true only once the CloudFormation schema lists the property.
   */
  readonly centralizeContextGraph?: boolean;
  /**
   * How propagated source tags meet existing destination tags. Default
   * `UPDATE_SYNC` (add and update the five allocation tags, never remove
   * hub-local tags). Allowed: IN_SYNC | ADD_ONLY | UPDATE_SYNC.
   */
  readonly tagConflictResolutionStrategy?: 'IN_SYNC' | 'ADD_ONLY' | 'UPDATE_SYNC';
  /** Centralize metrics. Default true. */
  readonly centralizeMetrics?: boolean;
  /** Optional backup Region (billable second copy). */
  readonly backupRegion?: string;
  /** Backup Region CMK ARN for logs; required when `backupRegion` and `destinationLogsKmsKeyArn` are set. */
  readonly backupLogsKmsKeyArn?: string;
  /**
   * Propagate the five allocation tags from source to destination log groups.
   * When this stack deploys INTO the destination (hub) account as delegated
   * administrator, pass `organizationId` and the role is created here (its
   * trust uses the Organization id as ExternalId). When the rule deploys from
   * the management account instead, pass `tagPropagationRoleArn` of a role
   * already created in the hub account.
   */
  readonly organizationId?: string;
  /** Existing tag-propagation role ARN in the destination account. */
  readonly tagPropagationRoleArn?: string;
  readonly tags?: Record<string, string>;
}

export class OmniCentralizationRuleConstruct extends Construct {
  readonly rule: CfnResource;
  readonly ruleArn: string;
  readonly tagPropagationRole?: Role;

  constructor(scope: Construct, id: string, props: OmniCentralizationRuleConstructProps) {
    super(scope, id);

    if (!/^[0-9A-Za-z-]{1,100}$/.test(props.ruleName)) {
      throw new Error(`OmniCentralizationRuleConstruct ruleName '${props.ruleName}' must match ^[0-9A-Za-z-]{1,100}$.`);
    }
    assertAccountId('destinationAccountId', props.destinationAccountId);
    if (props.sourceRegions.length === 0) {
      throw new Error('OmniCentralizationRuleConstruct requires at least one source Region.');
    }
    if (!/^(OrganizationId|OrganizationalUnitId|AccountId|\*)/.test(props.sourceScope.trim())) {
      throw new Error(
        `OmniCentralizationRuleConstruct sourceScope '${props.sourceScope}' must start with OrganizationId, OrganizationalUnitId, AccountId or *.`,
      );
    }
    if (props.backupRegion && props.destinationLogsKmsKeyArn && !props.backupLogsKmsKeyArn) {
      throw new Error(
        'OmniCentralizationRuleConstruct: backupLogsKmsKeyArn is required when a backup Region is combined with a destination CMK.',
      );
    }
    const patterns = props.logGroupPatterns ?? BLUEPRINT_CENTRALIZED_LOG_GROUP_PATTERNS;
    if (patterns.length === 0) {
      throw new Error('OmniCentralizationRuleConstruct refuses an empty log-group selection; never centralize "all log groups" implicitly.');
    }
    const destinationPattern = props.destinationLogGroupPattern ?? DEFAULT_DESTINATION_LOG_GROUP_PATTERN;
    if (props.organizationId && props.tagPropagationRoleArn) {
      throw new Error('OmniCentralizationRuleConstruct: pass either organizationId (create the role here) or tagPropagationRoleArn, not both.');
    }
    const propagateTags = props.organizationId !== undefined || props.tagPropagationRoleArn !== undefined;
    if (propagateTags) {
      for (const required of ['${source.logGroup}', '${source.accountId}', '${source.region}']) {
        if (!destinationPattern.includes(required)) {
          throw new Error(
            `OmniCentralizationRuleConstruct: tag propagation requires the destination pattern to contain ${required}.`,
          );
        }
      }
    }

    const stack = Stack.of(this);
    let tagPropagationRoleArn = props.tagPropagationRoleArn;

    if (props.organizationId) {
      if (!Token.isUnresolved(stack.account) && stack.account !== props.destinationAccountId) {
        throw new Error(
          `OmniCentralizationRuleConstruct: the tag-propagation role must live in the destination account ${props.destinationAccountId}, but this stack deploys to ${stack.account}. Pass tagPropagationRoleArn instead.`,
        );
      }
      // The centralization service-linked role in the DESTINATION account
      // assumes this role to tag destination log groups.
      this.tagPropagationRole = new Role(this, 'TagPropagationRole', {
        description: 'CloudWatch Logs centralization tag propagation role (destination log groups only).',
        assumedBy: new ArnPrincipal(
          `arn:${stack.partition}:iam::${props.destinationAccountId}:role/aws-service-role/logs-centralization.observabilityadmin.amazonaws.com/AWSServiceRoleForObservabilityAdmin_LogsCentralization`,
        ).withConditions({ StringEquals: { 'sts:ExternalId': props.organizationId } }),
        inlinePolicies: {
          TagDestinationLogGroups: new PolicyDocument({
            statements: [
              new PolicyStatement({
                effect: Effect.ALLOW,
                actions: ['logs:ListTagsForResource', 'logs:TagResource', 'logs:UntagResource'],
                resources: [
                  `arn:${stack.partition}:logs:*:${props.destinationAccountId}:log-group:/centralized/*`,
                ],
              }),
            ],
          }),
        },
      });
      NagSuppressions.addResourceSuppressions(
        this.tagPropagationRole,
        [
          {
            id: 'AwsSolutions-IAM5',
            reason: 'SEC-033: tag actions are scoped to destination log groups under the /centralized/ prefix in the hub account; the Region is a wildcard because the rule can carry a backup Region.',
          },
          {
            id: 'NIST.800.53.R5-IAMNoInlinePolicy',
            reason: 'SEC-033: the tag-propagation policy is specific to this rule\'s destination prefix and is deleted with the role.',
          },
        ],
        true,
      );
      tagPropagationRoleArn = this.tagPropagationRole.roleArn;
    }

    const selection = patterns
      .map((p) => (p.includes('%') ? `LogGroupName LIKE '${p}'` : `LogGroupName = '${p}'`))
      .join(' OR ');

    const sourceLogsConfiguration: Record<string, unknown> = {
      LogGroupSelectionCriteria: selection,
      EncryptedLogGroupStrategy: 'ALLOW',
    };
    const source: Record<string, unknown> = {
      Regions: [...props.sourceRegions],
      Scope: props.sourceScope,
      SourceLogsConfiguration: sourceLogsConfiguration,
    };
    if (props.centralizeMetrics !== false) {
      source.SourceMetricsConfiguration = { MetricsSelectionCriteria: '*' };
    }
    if (props.centralizeContextGraph === true) {
      // Empty structure: its presence opts the rule into Context Graph centralization.
      source.SourceContextGraphConfiguration = {};
    }

    const destinationLogsConfiguration: Record<string, unknown> = {
      LogGroupNameConfiguration: { LogGroupNamePattern: destinationPattern },
      LogsEncryptionConfiguration: props.destinationLogsKmsKeyArn
        ? {
            EncryptionStrategy: 'CUSTOMER_MANAGED',
            EncryptionScope: 'NEW_DESTINATION_LOG_GROUPS',
            EncryptionConflictResolutionStrategy: 'SKIP',
            KmsKeyArn: props.destinationLogsKmsKeyArn,
          }
        : { EncryptionStrategy: 'AWS_OWNED' },
    };
    if (props.backupRegion) {
      destinationLogsConfiguration.BackupConfiguration = {
        Region: props.backupRegion,
        ...(props.backupLogsKmsKeyArn ? { KmsKeyArn: props.backupLogsKmsKeyArn } : {}),
      };
    }
    if (tagPropagationRoleArn) {
      destinationLogsConfiguration.TagPropagationConfiguration = {
        DestinationRoleArn: tagPropagationRoleArn,
        TagConflictResolutionStrategy: props.tagConflictResolutionStrategy ?? 'UPDATE_SYNC',
      };
    }
    const destination: Record<string, unknown> = {
      Account: props.destinationAccountId,
      Region: props.destinationRegion,
      DestinationLogsConfiguration: destinationLogsConfiguration,
    };
    if (props.centralizeMetrics !== false && props.backupRegion) {
      destination.DestinationMetricsConfiguration = { BackupConfiguration: { Region: props.backupRegion } };
    }

    const properties: Record<string, unknown> = {
      RuleName: props.ruleName,
      Rule: { Source: source, Destination: destination },
    };
    if (props.tags) {
      properties.Tags = Object.entries(props.tags).map(([Key, Value]) => ({ Key, Value }));
    }
    this.rule = new CfnResource(this, 'Rule', {
      type: 'AWS::ObservabilityAdmin::OrganizationCentralizationRule',
      properties,
    });
    if (this.tagPropagationRole) {
      this.rule.node.addDependency(this.tagPropagationRole);
    }
    this.ruleArn = this.rule.getAtt('RuleArn').toString();

    new CfnOutput(this, 'CentralizationRuleArn', { value: this.ruleArn });
  }
}
