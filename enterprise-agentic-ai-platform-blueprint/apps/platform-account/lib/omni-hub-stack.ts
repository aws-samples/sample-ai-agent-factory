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
 * Keys. By default the stack creates the space key and the centralized-logs
 * key itself, which needs operator credentials: the Audit account's scoped
 * CloudFormation execution policy (`render-cfn-execution-policy.py audit`)
 * never creates, tags or changes a key. To deploy through that role, operators
 * deploy OmniHubKeysStack (`omni-hub-keys` stage) once and pass its two key
 * ARNs as `existingKeyArns`; the stack then imports the keys and only uses
 * them (adopt mode needs only the centralized-logs key).
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
import { CfnOutput, RemovalPolicy, Stack, Tags, type StackProps } from "aws-cdk-lib";
import { ManagedPolicy } from "aws-cdk-lib/aws-iam";
import { Key, type IKey } from "aws-cdk-lib/aws-kms";
import { Construct } from "constructs";

import {
  OmniCentralizationRuleConstruct,
  OmniCentralizedLogsKeyConstruct,
  OmniGoldenGateRunnerRoleConstruct,
  OmniHubSpaceConstruct,
} from "@agenticai/omni";

const SPACE_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const KMS_KEY_ARN_PATTERN = /^arn:aws[a-z-]*:kms:[a-z0-9-]+:\d{12}:key\/[0-9a-f-]{36}$/;

/** Operator-provisioned hub keys (OmniHubKeysStack outputs). */
export interface OmniHubExistingKeyArns {
  /** Space and Dataset key. Required when the stack creates the space; refused next to `existingSpaceId`. */
  readonly space?: string;
  /** Destination key of the centralization rule. Always required. */
  readonly centralizedLogs: string;
}

function importHubKeys(
  scope: Construct,
  arns: OmniHubExistingKeyArns | undefined,
  adoptMode: boolean,
): { space?: IKey; centralizedLogs: IKey } | undefined {
  if (arns === undefined) {
    return undefined;
  }
  if (typeof arns.centralizedLogs !== "string") {
    throw new Error("OmniHubStack: existingKeyArns.centralizedLogs is required (OmniHubKeysStack output OmniCentralizedLogsKeyArn).");
  }
  for (const [name, value] of Object.entries(arns)) {
    if (value !== undefined && (typeof value !== "string" || !KMS_KEY_ARN_PATTERN.test(value))) {
      throw new Error(
        `OmniHubStack existingKeyArns.${name} '${String(value)}' must be a KMS key ARN (arn:aws:kms:<region>:<account>:key/<uuid>).`,
      );
    }
  }
  if (!adoptMode && arns.space === undefined) {
    throw new Error(
      "OmniHubStack: existingKeyArns needs both space and centralizedLogs when the stack creates the space (OmniHubKeysStack outputs).",
    );
  }
  if (adoptMode && arns.space !== undefined) {
    throw new Error(
      "OmniHubStack: existingKeyArns.space cannot be combined with existingSpaceId; the adopted space's key is managed where the space was created.",
    );
  }
  return {
    space: arns.space === undefined ? undefined : Key.fromKeyArn(scope, "SpaceKey", arns.space),
    centralizedLogs: Key.fromKeyArn(scope, "CentralizedLogsKey", arns.centralizedLogs),
  };
}

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
  /**
   * Operator-provisioned keys (OmniHubKeysStack outputs). When set, the stack
   * creates no KMS key and only uses these; required when the stack deploys
   * through the Audit account's scoped CloudFormation execution policy, which
   * never creates or changes a key. Default mode needs both ARNs, adopt mode
   * only `centralizedLogs`.
   */
  readonly existingKeyArns?: OmniHubExistingKeyArns;
  /**
   * Golden-dataset promotion gate (`scripts/golden_dataset_gate.py`, docs
   * section 3.1): the workload pipeline evaluation role ARN(s) allowed to
   * assume the hub's `AgenticAIGoldenGateRunner-<region>` role, which reads
   * the dataset version and the candidate's centralized copies and runs the
   * batch evaluation. Omit to create no runner role.
   */
  readonly goldenGate?: {
    readonly runnerPrincipalArns: readonly string[];
    /** Cell account ids whose centralized copies the runner may query. Default: all. */
    readonly sourceAccountIds?: readonly string[];
    readonly resultLogGroupPrefix?: string;
    readonly metricsNamespace?: string;
  };
  /**
   * Name of the customer-managed policy every hub role carries as permissions
   * boundary (the operator role keeps the AWS-managed Omni boundary). Render it
   * with `render-cfn-execution-policy.py hub-role-boundary` for this Region and
   * create it once per hub Region in this account
   * (`AgenticAI-OmniHubRoleBoundary-<region>`: the document pins Regional
   * resources, so Regions cannot share one policy); required when the stack
   * deploys through the Audit account's scoped CloudFormation execution policy.
   */
  readonly roleBoundaryPolicyName?: string;
  readonly tags?: Record<string, string>;
}

export class OmniHubStack extends Stack {
  /** The space and its roles and keys. Undefined in adopt mode. */
  readonly hub?: OmniHubSpaceConstruct;
  /** Space id: a CloudFormation attribute in default mode, the adopted id otherwise. */
  readonly spaceId: string;
  /** Destination CMK for every log group the centralization rule creates (created here or imported). */
  readonly centralizedLogsKey: IKey;
  readonly centralization?: OmniCentralizationRuleConstruct;
  /** Hub role the workload pipeline assumes for the golden-dataset gate. Undefined when `goldenGate` is omitted. */
  readonly goldenGateRunner?: OmniGoldenGateRunnerRoleConstruct;

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
      existingKeyArns,
      goldenGate,
      roleBoundaryPolicyName,
      tags,
      ...stackProps
    } = props;
    super(scope, id, stackProps);

    // Ownership marker for the Audit account's scoped CloudFormation execution
    // policy (render-cfn-execution-policy.py audit) and the hub-role boundary:
    // both let a key be used only when it carries this tag. OmniHubKeysStack
    // puts it on the keys it creates; the hub stack puts it on everything it
    // creates, so a hub deployed with operator credentials carries it too.
    Tags.of(this).add("agenticai:omni-hub", "true");
    const importedKeys = importHubKeys(this, existingKeyArns, existingSpaceId !== undefined);
    // The same policy creates or widens a hub role only when it carries the
    // boundary (the operator role keeps the AWS-managed Omni one).
    const roleBoundary = roleBoundaryPolicyName
      ? ManagedPolicy.fromManagedPolicyName(this, "HubRoleBoundary", roleBoundaryPolicyName)
      : undefined;

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
      this.centralizedLogsKey =
        importedKeys?.centralizedLogs ??
        new OmniCentralizedLogsKeyConstruct(this, "CentralizedLogs", {
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
        roleBoundary,
        existingSpaceKey: importedKeys?.space,
        existingCentralizedLogsKey: importedKeys?.centralizedLogs,
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
          tagPropagationRoleBoundary: roleBoundary,
          tags,
        },
      );
      this.centralization.node.addDependency(this.hub ?? this.centralizedLogsKey);
    }

    if (goldenGate) {
      this.goldenGateRunner = new OmniGoldenGateRunnerRoleConstruct(this, "GoldenGateRunner", {
        trustedPrincipalArns: goldenGate.runnerPrincipalArns,
        centralizedLogsKey: this.centralizedLogsKey,
        sourceAccountIds: goldenGate.sourceAccountIds,
        resultLogGroupPrefix: goldenGate.resultLogGroupPrefix,
        metricsNamespace: goldenGate.metricsNamespace,
        permissionsBoundary: roleBoundary,
        tags,
      });
    }
  }
}
