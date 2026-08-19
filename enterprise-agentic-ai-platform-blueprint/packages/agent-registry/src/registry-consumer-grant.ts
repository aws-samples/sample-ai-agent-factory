/**
 * RegistryConsumerGrant — attach the data-plane permissions a workstream
 * runtime role (or developer permission set) needs to discover + invoke the
 * platform-account Registry's MCP search endpoint.
 *
 * Permissions granted (consumer persona only — never publisher/admin):
 *   - bedrock-agentcore:SearchRegistryRecords
 *   - bedrock-agentcore:InvokeRegistryMcp        (data-plane MCP /search)
 *   - bedrock-agentcore:GetRegistryRecord        (single-record fetch for synth)
 *   - bedrock-agentcore:GetRegistry              (registry metadata read)
 *   - bedrock-agentcore:ListRegistries           (Registry-id discovery)
 *   - bedrock-agentcore:ListRegistryRecords      (paged list for dashboards)
 *
 * Resources are scoped to the parent registry's ARN (and its
 * `record/*` sub-ARNs). No `*` shortcuts — the consumer cannot read
 * other registries that may exist in the same account.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Effect, IRole, PolicyStatement } from 'aws-cdk-lib/aws-iam';

export interface RegistryConsumerGrantOptions {
  /**
   * Registry ARN — the resource to scope grants to. Pass
   * `PlatformRegistryConstruct.registryArn` directly.
   */
  readonly registryArn: string;
  /** Optional Sid prefix; useful when a role aggregates multiple registries. */
  readonly sidPrefix?: string;
}

/**
 * Idempotent helper: attach two inline policy statements to `role` granting
 * data-plane registry consumption against the supplied registry ARN.
 */
export function grantRegistryConsumer(
  role: IRole,
  opts: RegistryConsumerGrantOptions,
): void {
  const sidPrefix = opts.sidPrefix ?? 'AgenticAIRegistryConsumer';
  const registryArn = opts.registryArn;
  const recordArn = `${registryArn}/record/*`;

  role.addToPrincipalPolicy(
    new PolicyStatement({
      sid: `${sidPrefix}DataPlane`,
      effect: Effect.ALLOW,
      actions: [
        'bedrock-agentcore:SearchRegistryRecords',
        'bedrock-agentcore:InvokeRegistryMcp',
      ],
      resources: [registryArn],
    }),
  );

  role.addToPrincipalPolicy(
    new PolicyStatement({
      sid: `${sidPrefix}ControlPlaneRead`,
      effect: Effect.ALLOW,
      actions: [
        'bedrock-agentcore:GetRegistry',
        'bedrock-agentcore:ListRegistries',
        'bedrock-agentcore:ListRegistryRecords',
        'bedrock-agentcore:GetRegistryRecord',
      ],
      resources: [registryArn, recordArn],
    }),
  );
}
