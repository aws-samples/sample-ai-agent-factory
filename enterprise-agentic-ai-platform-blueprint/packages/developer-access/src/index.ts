/**
 * @agenticai/developer-access — public export surface.
 *
 * Identity Center permission sets + workstream roster table that put
 * developers into their own workstream account with three personas
 * (Developer / ReadOnly / Approver). Phase M of v0.5.0.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
export {
  WorkstreamPermissionSets,
  renderInlinePolicy,
  PERSONA_PREFIX,
  type WorkstreamPersona,
  type WorkstreamPermissionSetsProps,
} from './workstream-permission-sets';

export {
  WorkstreamRosterTable,
  type WorkstreamRosterTableProps,
} from './workstream-roster';
