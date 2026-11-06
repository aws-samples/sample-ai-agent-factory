/**
 * @agenticai/omni
 *
 * Amazon CloudWatch Omni, hub model (ADR-15 to ADR-19):
 *   - OmniOrganizationDomainConstruct   -- management account, once;
 *   - OmniCentralizationRuleConstruct   -- management account or delegated administrator;
 *   - OmniHubSpaceConstruct             -- Audit/Observability account, one per Region;
 *   - OmniDashboardConstruct            -- fleet dashboards exported from the web UI.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
export {
  OMNI_HOSTING_REGIONS,
  assertAccountId,
  assertOmniDomainName,
  assertOmniHostingRegion,
  assertOmniSpaceName,
  omniDomainUrl,
} from './naming';
export {
  OmniOrganizationDomainConstruct,
  type OmniOrganizationDomainConstructProps,
} from './omni-organization-domain-construct';
export {
  BLUEPRINT_CENTRALIZED_LOG_GROUP_PATTERNS,
  DEFAULT_DESTINATION_LOG_GROUP_PATTERN,
  OmniCentralizationRuleConstruct,
  type OmniCentralizationRuleConstructProps,
} from './omni-centralization-rule-construct';
export { OmniHubSpaceConstruct, type OmniHubSpaceConstructProps } from './omni-hub-space-construct';
export {
  OmniCentralizedLogsKeyConstruct,
  type OmniCentralizedLogsKeyConstructProps,
} from './omni-centralized-logs-key-construct';
export { OmniDashboardConstruct, type OmniDashboardConstructProps } from './omni-dashboard-construct';
export {
  OmniGoldenGateRunnerRoleConstruct,
  type OmniGoldenGateRunnerRoleConstructProps,
} from './omni-golden-gate-runner-role-construct';
export * from './transaction-search-construct';
