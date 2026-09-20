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
    .join(" ");
}

/**
 * Render context whose values are produced by earlier commands in the same
 * CodeBuild v0.2 shell. Only validated variable names are accepted; values are
 * expanded inside double quotes and are never re-evaluated as shell syntax.
 */
function renderEnvironmentContextArgs(
  context: Readonly<Record<string, string>>,
): string {
  return Object.keys(context)
    .sort()
    .map((key) => {
      if (!/^[A-Za-z0-9/_-]+$/.test(key)) {
        throw new Error(`Invalid CDK context key '${key}'.`);
      }
      const variable = context[key];
      if (!/^[A-Z_][A-Z0-9_]*$/.test(variable)) {
        throw new Error(
          `Invalid environment variable '${variable}' for context '${key}'.`,
        );
      }
      return `--context ${key}="\${${variable}}"`;
    })
    .join(" ");
}

export interface StageAwareSynthOptions {
  /** `stage` context value handed to `bin/agentic-ai-platform.ts`. */
  readonly stage: string;
  /** Additional `agenticai/*` context the stage requires. */
  readonly context: Readonly<Record<string, string>>;
  /** Commands that resolve non-secret context values before `cdk synth`. */
  readonly preSynthCommands?: readonly string[];
  /** CDK context key -> validated shell variable produced by preSynthCommands. */
  readonly contextFromEnvironment?: Readonly<Record<string, string>>;
  /** Cloud Assembly artifact ID of the pipeline's own stack. */
  readonly expectedStackArtifactId: string;
  /**
   * Globs relative to the blueprint package that must each resolve to a nested
   * stage assembly containing at least one CloudFormation stack.
   */
  readonly expectedStageAssemblyGlobs: readonly string[];
}

const BLUEPRINT_SOURCE_DIRECTORY = "enterprise-agentic-ai-platform-blueprint";

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

/** Render one complete shell command; CodeBuild validates each list item alone. */
function validateStageAssembliesCommand(globs: readonly string[]): string {
  return [
    `for asm in ${globs.join(" ")}; do`,
    'if [ ! -f "$asm/manifest.json" ]; then echo "ERROR: cdk synth produced no stage assembly at $asm"; exit 1; fi;',
    'if ! grep -q "aws:cloudformation:stack" "$asm/manifest.json"; then echo "ERROR: stage assembly $asm declares no stacks"; exit 1; fi;',
    "done",
  ].join(" ");
}

/**
 * ShellStep collects `cdk.out` from the CodeBuild source root. Move a nested
 * package's validated assembly there without overwriting another output.
 */
function publishCloudAssemblyFromCheckoutRootCommand(): string {
  return (
    'if [ -n "${CODEBUILD_SRC_DIR:-}" ] && [ "$PWD" != "$CODEBUILD_SRC_DIR" ]; then ' +
    'if [ -e "$CODEBUILD_SRC_DIR/cdk.out" ]; then echo "ERROR: checkout-root cdk.out already exists"; exit 1; fi; ' +
    'mv cdk.out "$CODEBUILD_SRC_DIR/cdk.out"; fi'
  );
}

/**
 * Build a stage-aware synth command sequence that rejects unknown source
 * layouts and empty assemblies.
 */
export function stageAwareSynthCommands(
  options: StageAwareSynthOptions,
): string[] {
  const contextArgs = renderContextArgs(options.context);
  const environmentContextArgs = renderEnvironmentContextArgs(
    options.contextFromEnvironment ?? {},
  );
  const allContextArgs = [contextArgs, environmentContextArgs]
    .filter((value) => value.length > 0)
    .join(" ");
  return [
    "set -eu",
    enterBlueprintSourceDirectory(),
    "npm ci",
    "npm run build",
    "npm test",
    ...(options.preSynthCommands ?? []),
    `npx cdk synth --context stage=${shellQuote(options.stage)} ${allContextArgs} --strict`,
    "test -f cdk.out/manifest.json",
    `test -f cdk.out/${shellQuote(options.expectedStackArtifactId)}.template.json`,
    'grep -q "aws:cloudformation:stack" cdk.out/manifest.json',
    validateStageAssembliesCommand(options.expectedStageAssemblyGlobs),
    publishCloudAssemblyFromCheckoutRootCommand(),
  ];
}
