/*
 * Shared fail-closed CDK synth command generation for platform and workload
 * pipelines.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

/**
 * POSIX single-quote a value so ARNs, JSON arrays, and shell metacharacters
 * survive the CodeBuild shell unchanged.
 */
function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

/** Render a deterministic (key-sorted) `--context key=value` argument list. */
function renderContextArgs(context: Readonly<Record<string, string>>): string {
  return Object.keys(context)
    .sort()
    .map((key) => `--context ${key}=${shellQuote(context[key])}`)
    .join(' ');
}

export interface StageAwareSynthOptions {
  /** `stage` context value handed to `bin/agentic-ai-platform.ts`. */
  readonly stage: string;
  /** Additional `agenticai/*` context the stage requires. */
  readonly context: Readonly<Record<string, string>>;
  /** Cloud Assembly artifact ID of the pipeline's own stack. */
  readonly expectedStackArtifactId: string;
  /**
   * Globs relative to the repository root that must each resolve to a nested
   * stage assembly containing at least one CloudFormation stack.
   */
  readonly expectedStageAssemblyGlobs: readonly string[];
}

const BLUEPRINT_SOURCE_DIRECTORY = 'enterprise-agentic-ai-platform-blueprint';

/**
 * Enter the blueprint package after CodeConnections checks out the repository.
 * The aws-samples repository is multi-project, while standalone copies may put
 * this package at the checkout root. Reject every other layout rather than
 * letting npm run against an unrelated package.json.
 */
function enterBlueprintSourceDirectory(): string {
  const nestedPackage = `${BLUEPRINT_SOURCE_DIRECTORY}/package.json`;
  return (
    `if [ -f package.json ]; then :; ` +
    `elif [ -f ${shellQuote(nestedPackage)} ]; then cd ${shellQuote(BLUEPRINT_SOURCE_DIRECTORY)}; ` +
    'else echo "ERROR: blueprint package.json not found at checkout root or expected subdirectory"; exit 1; fi'
  );
}

/**
 * Build a stage-aware synth command sequence that rejects unknown source
 * layouts and empty assemblies.
 */
export function stageAwareSynthCommands(options: StageAwareSynthOptions): string[] {
  const contextArgs = renderContextArgs(options.context);
  return [
    'set -eu',
    enterBlueprintSourceDirectory(),
    'npm ci',
    'npm run build',
    'npm test',
    `npx cdk synth --context stage=${shellQuote(options.stage)} ${contextArgs} --strict`,
    'test -f cdk.out/manifest.json',
    `test -f cdk.out/${shellQuote(options.expectedStackArtifactId)}.template.json`,
    'grep -q "aws:cloudformation:stack" cdk.out/manifest.json',
    `for asm in ${options.expectedStageAssemblyGlobs.join(' ')}; do`,
    '  if [ ! -f "$asm/manifest.json" ]; then echo "ERROR: cdk synth produced no stage assembly at $asm"; exit 1; fi',
    '  if ! grep -q "aws:cloudformation:stack" "$asm/manifest.json"; then echo "ERROR: stage assembly $asm declares no stacks"; exit 1; fi',
    'done',
  ];
}
