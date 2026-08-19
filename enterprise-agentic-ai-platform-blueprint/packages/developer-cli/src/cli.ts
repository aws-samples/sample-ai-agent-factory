#!/usr/bin/env node
/**
 * agenticai — workstream developer CLI binary.
 *
 * Subcommands (Phase N — v0.5.0):
 *   agenticai init <tenantId> <agentId> [--workstream <id>] [--registry <id>] [--kind <task|chatbot|...>]
 *   agenticai registry search [--query <text>] [--registry <id>]
 *   agenticai registry subscribe <recordId>
 *   agenticai registry unsubscribe <recordId>
 *   agenticai registry list
 *   agenticai dev eval [--input <runResults.jsonl>]
 *   agenticai context validate
 *   agenticai submit [--prev-eval <path>]
 *
 * The binary is a thin command dispatcher over the pure helpers in this
 * package. AWS SDK calls (registry search/get) live inline here — testing
 * is via the helpers, not via mocking SDK clients.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { argv, exit, stderr, stdout } from 'node:process';

import {
  appendSubscription,
  formatEvalReport,
  formatSearchResults,
  listSubscriptions,
  readAgenticContext,
  removeSubscription,
  renderPullRequestBody,
  runLocalEval,
  scaffoldAgentRepo,
  validateForSynth,
  type AgenticAiContext,
  type EvalRunRow,
  type RegistrySearchResult,
  type ScaffoldKind,
} from './index';

interface CliArgs {
  readonly positional: readonly string[];
  readonly flags: ReadonlyMap<string, string>;
}

function parseArgs(raw: readonly string[]): CliArgs {
  const positional: string[] = [];
  const flags = new Map<string, string>();
  for (let i = 0; i < raw.length; i++) {
    const tok = raw[i];
    if (tok.startsWith('--')) {
      const name = tok.slice(2);
      const next = raw[i + 1];
      if (next === undefined || next.startsWith('--')) {
        flags.set(name, 'true');
      } else {
        flags.set(name, next);
        i++;
      }
    } else {
      positional.push(tok);
    }
  }
  return { positional, flags };
}

function loadCdkContext(repoRoot: string): AgenticAiContext {
  const path = join(repoRoot, 'cdk.context.json');
  if (!existsSync(path)) {
    throw new Error(`cdk.context.json not found at ${path}. Run \`agenticai init\` first.`);
  }
  const parsed: unknown = JSON.parse(readFileSync(path, 'utf-8'));
  return readAgenticContext(parsed);
}

function saveCdkContext(repoRoot: string, ctx: AgenticAiContext): void {
  const path = join(repoRoot, 'cdk.context.json');
  writeFileSync(path, JSON.stringify(ctx, null, 2) + '\n');
}

function writeFiles(rootDir: string, files: ReadonlyMap<string, string>): void {
  for (const [rel, contents] of files) {
    const full = join(rootDir, rel);
    mkdirSync(dirname(full), { recursive: true });
    writeFileSync(full, contents);
  }
}

async function cmdInit(args: CliArgs): Promise<number> {
  const [tenantId, agentId] = args.positional;
  if (!tenantId || !agentId) {
    stderr.write('agenticai init: usage `agenticai init <tenantId> <agentId>`\n');
    return 2;
  }
  const workstreamId = args.flags.get('workstream') ?? tenantId;
  const registryId = args.flags.get('registry');
  if (!registryId) {
    stderr.write(
      'agenticai init: --registry <id> is required (the platform AgentCore Registry id).\n',
    );
    return 2;
  }
  const kind = (args.flags.get('kind') ?? 'task') as ScaffoldKind;
  const targetDir = resolve(args.flags.get('out') ?? `${tenantId}-${agentId}`);

  const files = scaffoldAgentRepo({
    tenantId,
    agentId,
    workstreamId,
    platformRegistryId: registryId,
    platformAccountId: args.flags.get('platform-account'),
    kind,
  });
  writeFiles(targetDir, files);
  stdout.write(`Scaffolded ${tenantId}/${agentId} at ${targetDir}\n`);
  return 0;
}

async function cmdRegistrySubscribe(args: CliArgs): Promise<number> {
  const [, recordId] = args.positional;
  if (!recordId) {
    stderr.write('agenticai registry subscribe: usage `agenticai registry subscribe <recordId>`\n');
    return 2;
  }
  const repo = resolve(args.flags.get('cwd') ?? '.');
  const ctx = loadCdkContext(repo);
  const next = appendSubscription(ctx, recordId);
  saveCdkContext(repo, next);
  stdout.write(`Subscribed to ${recordId}. Subscriptions now: ${listSubscriptions(next).join(', ')}\n`);
  return 0;
}

async function cmdRegistryUnsubscribe(args: CliArgs): Promise<number> {
  const [, recordId] = args.positional;
  if (!recordId) {
    stderr.write('agenticai registry unsubscribe: usage `agenticai registry unsubscribe <recordId>`\n');
    return 2;
  }
  const repo = resolve(args.flags.get('cwd') ?? '.');
  const ctx = loadCdkContext(repo);
  const next = removeSubscription(ctx, recordId);
  saveCdkContext(repo, next);
  stdout.write(`Unsubscribed from ${recordId}.\n`);
  return 0;
}

async function cmdRegistryList(args: CliArgs): Promise<number> {
  const repo = resolve(args.flags.get('cwd') ?? '.');
  const ctx = loadCdkContext(repo);
  const subs = listSubscriptions(ctx);
  stdout.write(subs.length === 0 ? '(no subscriptions)\n' : subs.join('\n') + '\n');
  return 0;
}

async function cmdRegistrySearch(args: CliArgs): Promise<number> {
  // The actual SDK call is intentionally not bundled into this CLI in the
  // OSS blueprint — operators wire it up against their own bedrock-agentcore
  // SDK version. We accept a JSON results file via --results so the renderer
  // is exercised in CI without requiring AWS creds.
  const resultsPath = args.flags.get('results');
  if (!resultsPath) {
    stderr.write(
      'agenticai registry search: --results <path> is required in this build (live SDK wiring is operator-supplied; see README).\n',
    );
    return 2;
  }
  const results = JSON.parse(readFileSync(resolve(resultsPath), 'utf-8')) as RegistrySearchResult[];
  stdout.write(formatSearchResults(results) + '\n');
  return 0;
}

async function cmdDevEval(args: CliArgs): Promise<number> {
  const inputPath = args.flags.get('input');
  if (!inputPath) {
    stderr.write('agenticai dev eval: --input <runResults.jsonl> is required\n');
    return 2;
  }
  const rows: EvalRunRow[] = readFileSync(resolve(inputPath), 'utf-8')
    .split('\n')
    .filter((l) => l.trim().length > 0)
    .map((l) => JSON.parse(l) as EvalRunRow);
  const report = runLocalEval(rows);
  stdout.write(formatEvalReport(report) + '\n');
  return report.overallPassed ? 0 : 1;
}

async function cmdContextValidate(args: CliArgs): Promise<number> {
  const repo = resolve(args.flags.get('cwd') ?? '.');
  const ctx = loadCdkContext(repo);
  const errors = validateForSynth(ctx);
  if (errors.length > 0) {
    stderr.write(errors.map((e) => `[INVALID] ${e}`).join('\n') + '\n');
    return 1;
  }
  stdout.write('[OK] cdk.context.json is valid for Registry-mode synth\n');
  return 0;
}

async function cmdSubmit(args: CliArgs): Promise<number> {
  const repo = resolve(args.flags.get('cwd') ?? '.');
  const ctx = loadCdkContext(repo);
  const errors = validateForSynth(ctx);
  if (errors.length > 0) {
    stderr.write(
      'agenticai submit: cdk.context.json is incomplete:\n' +
        errors.map((e) => `  - ${e}`).join('\n') +
        '\n',
    );
    return 2;
  }
  const evalInputPath = args.flags.get('eval-input');
  if (!evalInputPath) {
    stderr.write('agenticai submit: --eval-input <runResults.jsonl> is required\n');
    return 2;
  }
  const evalRows: EvalRunRow[] = readFileSync(resolve(evalInputPath), 'utf-8')
    .split('\n')
    .filter((l) => l.trim().length > 0)
    .map((l) => JSON.parse(l) as EvalRunRow);
  const evalReport = runLocalEval(evalRows);
  const prev = args.flags.get('prev-eval');
  const previousEvalReport = prev
    ? (JSON.parse(readFileSync(resolve(prev), 'utf-8')) as ReturnType<typeof runLocalEval>)
    : undefined;
  const tenantId = ctx['agenticai/tenantId'] as string;
  const agentId = ctx['agenticai/agentId'] as string;
  const workstreamId = (ctx['agenticai/workstreamId'] as string | undefined) ?? tenantId;
  const subscribedRecordIds = listSubscriptions(ctx);
  const registryId = ctx['agenticai/d03RegistryId'] as string;
  const body = renderPullRequestBody({
    tenantId,
    agentId,
    workstreamId,
    subscribedRecordIds,
    evalReport,
    previousEvalReport,
    registryId,
  });
  const outPath = args.flags.get('out') ?? 'PR_BODY.md';
  writeFileSync(resolve(outPath), body);
  stdout.write(`Wrote PR body to ${outPath} (eval ${evalReport.overallPassed ? 'PASS' : 'FAIL'})\n`);
  return evalReport.overallPassed ? 0 : 1;
}

function printHelp(): void {
  stdout.write(
    [
      'agenticai — workstream developer CLI',
      '',
      'Usage:',
      '  agenticai init <tenantId> <agentId> --registry <id> [--workstream <id>] [--kind task|chatbot|langgraph|crewai|multi-agent]',
      '  agenticai registry search --results <path>',
      '  agenticai registry subscribe <recordId>',
      '  agenticai registry unsubscribe <recordId>',
      '  agenticai registry list',
      '  agenticai dev eval --input <runResults.jsonl>',
      '  agenticai context validate',
      '  agenticai submit --eval-input <runResults.jsonl> [--prev-eval <path>] [--out <PR_BODY.md>]',
      '',
    ].join('\n'),
  );
}

async function main(): Promise<number> {
  const raw = argv.slice(2);
  if (raw.length === 0 || raw[0] === '-h' || raw[0] === '--help') {
    printHelp();
    return 0;
  }
  const cmd = raw[0];
  const args = parseArgs(raw.slice(1));
  if (cmd === 'init') return cmdInit(args);
  if (cmd === 'registry') {
    const sub = args.positional[0];
    if (sub === 'search') return cmdRegistrySearch(args);
    if (sub === 'subscribe') return cmdRegistrySubscribe(args);
    if (sub === 'unsubscribe') return cmdRegistryUnsubscribe(args);
    if (sub === 'list') return cmdRegistryList(args);
    stderr.write(`agenticai registry: unknown sub-command '${sub ?? ''}'.\n`);
    return 2;
  }
  if (cmd === 'dev') {
    const sub = args.positional[0];
    if (sub === 'eval') return cmdDevEval(args);
    stderr.write(`agenticai dev: unknown sub-command '${sub ?? ''}'.\n`);
    return 2;
  }
  if (cmd === 'context') {
    const sub = args.positional[0];
    if (sub === 'validate') return cmdContextValidate(args);
    stderr.write(`agenticai context: unknown sub-command '${sub ?? ''}'.\n`);
    return 2;
  }
  if (cmd === 'submit') return cmdSubmit(args);
  stderr.write(`agenticai: unknown command '${cmd}'. Run \`agenticai --help\`.\n`);
  return 2;
}

if (require.main === module) {
  main().then((code) => exit(code), (err) => {
    stderr.write(`agenticai: ${err instanceof Error ? err.message : String(err)}\n`);
    exit(1);
  });
}

export { parseArgs, main };
