/**
 * OmniHubStack — deployed into the Audit/Observability account, one per
 * CloudWatch Omni hosting Region (ADR-15).
 *
 * Creates the fleet space with its roles and keys, enables Transaction Search
 * in the hub, and (when `sourceScope` is supplied and this account is the
 * CloudWatch delegated administrator) the organization centralization rule
 * that replicates the Platform and Workstream telemetry into this account.
 * The existing OAM sink (`AuditStack`) is untouched: Omni reads the Dataset,
 * the CloudWatch console GenAI Observability view keeps reading OAM links.
 *
 * Post-deploy, `scripts/omni/omni_space_bootstrap.py` applies the two
 * operations without CloudFormation support: the Dataset integration and the
 * access grants.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { RemovalPolicy, Stack, type StackProps } from "aws-cdk-lib";
import { Construct } from "constructs";

import {
  OmniCentralizationRuleConstruct,
  OmniHubSpaceConstruct,
} from "@agenticai/omni";

export interface OmniHubStackProps extends StackProps {
  /** Organization domain id (`d-...`) from OmniDomainStack. */
  readonly domainId: string;
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
  /** Keep keys when the stack is deleted. Default RETAIN; use DESTROY for throwaway validation. */
  readonly keyRemovalPolicy?: RemovalPolicy;
  readonly tags?: Record<string, string>;
}

export class OmniHubStack extends Stack {
  readonly hub: OmniHubSpaceConstruct;
  readonly centralization?: OmniCentralizationRuleConstruct;

  constructor(scope: Construct, id: string, props: OmniHubStackProps) {
    const {
      domainId,
      spaceName,
      allowModelInference,
      judgeModelArns,
      enableAwsIntegrationDiscovery,
      centralization,
      transactionSearchIndexingPercentage,
      keyRemovalPolicy,
      tags,
      ...stackProps
    } = props;
    super(scope, id, stackProps);

    this.hub = new OmniHubSpaceConstruct(this, "FleetSpace", {
      spaceName: spaceName ?? `agenticai-fleet-${this.region}`,
      domainId,
      allowModelInference,
      judgeModelArns,
      enableAwsIntegrationDiscovery,
      transactionSearchIndexingPercentage,
      keyRemovalPolicy,
      tags,
    });

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
          destinationLogsKmsKeyArn: this.hub.centralizedLogsKey.keyArn,
          backupRegion: centralization.backupRegion,
          tags,
        },
      );
      this.centralization.node.addDependency(this.hub);
    }
  }
}
