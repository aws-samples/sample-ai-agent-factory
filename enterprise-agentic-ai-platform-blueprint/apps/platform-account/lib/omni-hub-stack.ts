/**
 * OmniHubStack — deployed into the Audit/Observability account, one per
 * CloudWatch Omni hosting Region (ADR-15).
 *
 * Default mode creates the fleet space with its roles and keys, enables
 * Transaction Search in the hub, and (when `centralization` is supplied and
 * this account is the management account or the CloudWatch delegated
 * administrator) the organization centralization rule that replicates the
 * Platform and Workstream telemetry into this account. The existing OAM sink
 * (`AuditStack`) is untouched: Omni reads the Dataset, the CloudWatch console
 * GenAI Observability view keeps reading OAM links.
 *
 * Adopt mode (`existingSpaceId`) is for an account that already hosts an Omni
 * space created outside the blueprint, for example an account-level domain
 * created in the console before the organization domain (Omni allows one
 * domain per organization, so the organization domain cannot be added next to
 * it). The stack then owns only what the hub model needs around that space:
 * the centralized-logs key and the organization centralization rule. The
 * space, its roles, its key and Transaction Search stay where they were
 * created.
 *
 * Post-deploy, `scripts/omni/omni_space_bootstrap.py` applies the two
 * operations without CloudFormation support: the Dataset integration and the
 * access grants. Pre-deploy, when trusted access for
 * `observabilityadmin.amazonaws.com` was enabled through the API rather than
 * the console, `centralization ensure-service-linked-roles` creates the two
 * service-linked roles the console would have created; the tag-propagation
 * role's trust policy names one of them and IAM refuses a principal that does
 * not exist yet.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CfnOutput, RemovalPolicy, Stack, type StackProps } from "aws-cdk-lib";
import type { Key } from "aws-cdk-lib/aws-kms";
import { Construct } from "constructs";

import {
  OmniCentralizationRuleConstruct,
  OmniCentralizedLogsKeyConstruct,
  OmniHubSpaceConstruct,
} from "@agenticai/omni";

const SPACE_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export interface OmniHubStackProps extends StackProps {
  /** Domain id (`d-...`): the organization domain from OmniDomainStack, or the domain of an adopted space. */
  readonly domainId: string;
  /**
   * Adopt a space that already exists in this account and Region instead of
   * creating one. The stack then deploys only the centralized-logs key and
   * the centralization rule; every space-level prop below must stay unset.
   */
  readonly existingSpaceId?: string;
  /** Space name. Default `agenticai-fleet-<region>`. */
  readonly spaceName?: string;
  /** ADR-16: allow prompt playground and custom judge inference. Default false. */
  readonly allowModelInference?: boolean;
  /** Exact judge model/inference-profile ARNs (requires allowModelInference). */
  readonly judgeModelArns?: readonly string[];
  /** Enable Context Graph resource discovery through AWS Config. Default false. */
  readonly enableAwsIntegrationDiscovery?: boolean;
  /**
   * Organization centralization rule. Omit to deploy the space only (for
   * example when the rule is created from the management account instead).
   */
  readonly centralization?: {
    readonly organizationId: string;
    /** `OrganizationalUnitId IN (...)`, `AccountId IN (...)` or `OrganizationId = ...`. */
    readonly sourceScope: string;
    readonly sourceRegions: readonly string[];
    readonly backupRegion?: string;
  };
  /** Transaction Search indexing percentage in the hub (0-100). Default 1. */
  readonly transactionSearchIndexingPercentage?: number;
  /**
   * Manage Transaction Search from this stack. Set false when the hub account
   * already has Transaction Search enabled outside the blueprint (the setting
   * is account-wide and must have exactly one owner). Default true; ignored in
   * adopt mode, where the adopted space's account already owns the setting.
   */
  readonly enableTransactionSearch?: boolean;
  /** Keep keys when the stack is deleted. Default RETAIN; use DESTROY for throwaway validation. */
  readonly keyRemovalPolicy?: RemovalPolicy;
  readonly tags?: Record<string, string>;
}

export class OmniHubStack extends Stack {
  /** The space and its roles and keys. Undefined in adopt mode. */
  readonly hub?: OmniHubSpaceConstruct;
  /** Space id: a CloudFormation attribute in default mode, the adopted id otherwise. */
  readonly spaceId: string;
  /** Destination CMK for every log group the centralization rule creates. */
  readonly centralizedLogsKey: Key;
  readonly centralization?: OmniCentralizationRuleConstruct;

  constructor(scope: Construct, id: string, props: OmniHubStackProps) {
    const {
      domainId,
      existingSpaceId,
      spaceName,
      allowModelInference,
      judgeModelArns,
      enableAwsIntegrationDiscovery,
      centralization,
      transactionSearchIndexingPercentage,
      enableTransactionSearch,
      keyRemovalPolicy,
      tags,
      ...stackProps
    } = props;
    super(scope, id, stackProps);

    if (existingSpaceId !== undefined) {
      if (!SPACE_ID_PATTERN.test(existingSpaceId)) {
        throw new Error(`OmniHubStack existingSpaceId '${existingSpaceId}' must be a space id (UUID).`);
      }
      const spaceLevelProps = {
        spaceName,
        allowModelInference,
        judgeModelArns,
        enableAwsIntegrationDiscovery,
        transactionSearchIndexingPercentage,
      };
      const conflicting = Object.entries(spaceLevelProps)
        .filter(([, value]) => value !== undefined && value !== false)
        .map(([name]) => name);
      if (conflicting.length > 0) {
        throw new Error(
          `OmniHubStack: ${conflicting.join(", ")} cannot be combined with existingSpaceId; the adopted space is managed where it was created.`,
        );
      }
      this.spaceId = existingSpaceId;
      this.centralizedLogsKey = new OmniCentralizedLogsKeyConstruct(this, "CentralizedLogs", {
        removalPolicy: keyRemovalPolicy,
        tags,
      }).key;
      new CfnOutput(this, "OmniSpaceId", {
        value: existingSpaceId,
        description: "Adopted space (created outside this stack).",
      });
      new CfnOutput(this, "OmniDomainId", { value: domainId });
      new CfnOutput(this, "OmniCentralizedLogsKeyArn", {
        value: this.centralizedLogsKey.keyArn,
        description: "Destination CMK for the organization centralization rule (DestinationLogsConfiguration).",
      });
    } else {
      this.hub = new OmniHubSpaceConstruct(this, "FleetSpace", {
        spaceName: spaceName ?? `agenticai-fleet-${this.region}`,
        domainId,
        allowModelInference,
        judgeModelArns,
        enableAwsIntegrationDiscovery,
        transactionSearchIndexingPercentage,
        enableTransactionSearch,
        keyRemovalPolicy,
        tags,
      });
      this.spaceId = this.hub.spaceId;
      this.centralizedLogsKey = this.hub.centralizedLogsKey;
    }

    if (centralization) {
      this.centralization = new OmniCentralizationRuleConstruct(
        this,
        "Centralization",
        {
          ruleName: `agenticai-fleet-${this.region}`,
          organizationId: centralization.organizationId,
          sourceScope: centralization.sourceScope,
          sourceRegions: centralization.sourceRegions,
          destinationAccountId: this.account,
          destinationRegion: this.region,
          destinationLogsKmsKeyArn: this.centralizedLogsKey.keyArn,
          backupRegion: centralization.backupRegion,
          tags,
        },
      );
      this.centralization.node.addDependency(this.hub ?? this.centralizedLogsKey);
    }
  }
}
