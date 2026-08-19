/**
 * @agenticai/developer-cli — public exports.
 *
 * Pure-function helpers (scaffold, eval, submit) consumed by the CLI binary
 * (`agenticai` in src/cli.ts) and exposed for unit testing. The CLI binary
 * is intentionally a thin handler over these — testing the helpers covers
 * the meaningful logic.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
export {
  scaffoldAgentRepo,
  type ScaffoldKind,
  type ScaffoldOptions,
} from './init';

export {
  CTX_TENANT_ID,
  CTX_AGENT_ID,
  CTX_SUBSCRIBED_RECORDS,
  CTX_REGISTRY_ID,
  CTX_PLATFORM_REGISTRY_ARN,
  readAgenticContext,
  appendSubscription,
  removeSubscription,
  listSubscriptions,
  validateForSynth,
  type AgenticAiContext,
} from './cdk-context';

export {
  runLocalEval,
  formatEvalReport,
  type EvalRunRow,
  type CategoryResult,
  type EvalReport,
} from './dev-eval';

export {
  formatSearchResults,
  filterApproved,
  type RegistrySearchResult,
} from './registry';

export { renderPullRequestBody, type SubmitContext } from './submit';
