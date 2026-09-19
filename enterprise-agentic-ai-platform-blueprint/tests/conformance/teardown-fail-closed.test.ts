/**
 * Round 1B — teardown fail-closed conformance.
 *
 * `scripts/teardown.sh` previously ran, for every stack:
 *
 *   npx cdk destroy --force "$stack" || echo "(stack not present or already destroyed)"
 *
 * which (a) passed no `stage` context, so the app synthesised an empty assembly
 * and matched no stack, and (b) turned every failure — absent stack, failed
 * destroy, expired credentials, denied API call — into a zero exit.
 *
 * These tests execute the real script with `aws` and `npx` replaced by stubs on
 * PATH. Nothing here calls AWS. Each case asserts an exit code AND whether a
 * destroy was actually attempted, so a generic non-zero exit cannot be mistaken
 * for a successful teardown and a generic zero exit cannot be mistaken for a
 * clean account.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const SCRIPT = path.join(REPO_ROOT, 'scripts', 'teardown.sh');

/** Exit codes documented in the script header. */
const EXIT = {
  ok: 0,
  destroyFailed: 1,
  configError: 2,
  unexpectedAwsError: 3,
  aborted: 4,
} as const;

/** `aws` stub: describe-stacks / list-stacks behaviour driven by env vars. */
const AWS_STUB = `#!/usr/bin/env bash
for a in "$@"; do
  case "$a" in
    list-stacks) echo "\${STUB_LIST_OUTPUT:-}"; exit 0 ;;
    describe-stacks)
      case "\${STUB_DESCRIBE_MODE:-absent}" in
        exists) echo "CREATE_COMPLETE"; exit 0 ;;
        absent) echo "An error occurred (ValidationError): Stack with id x does not exist" >&2; exit 254 ;;
        error) echo "An error occurred (AccessDenied): not authorized" >&2; exit 254 ;;
      esac ;;
  esac
done
exit 0
`;

/** `npx` stub: records every invocation, exits with STUB_DESTROY_EXIT. */
const NPX_STUB = `#!/usr/bin/env bash
printf '%s\\n' "npx $*" >> "\${STUB_LOG:?}"
exit "\${STUB_DESTROY_EXIT:-0}"
`;

let stubDir: string;
let stubBin: string;
let stubLog: string;

beforeAll(() => {
  stubDir = fs.mkdtempSync(path.join(os.tmpdir(), 'teardown-conformance-'));
  stubBin = path.join(stubDir, 'bin');
  stubLog = path.join(stubDir, 'npx.log');
  fs.mkdirSync(stubBin);
  fs.writeFileSync(path.join(stubBin, 'aws'), AWS_STUB, { mode: 0o755 });
  fs.writeFileSync(path.join(stubBin, 'npx'), NPX_STUB, { mode: 0o755 });
});

afterAll(() => {
  fs.rmSync(stubDir, { recursive: true, force: true });
});

interface RunResult {
  readonly status: number;
  readonly stdout: string;
  readonly stderr: string;
  /** Every `npx …` invocation the script made, one per line. */
  readonly npxCalls: string[];
}

/**
 * Environment with every ambient AGENTICAI_* / AWS_* value removed, so a
 * developer's shell cannot make a test pass.
 */
function baseEnv(): Record<string, string> {
  const env: Record<string, string> = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (value === undefined) continue;
    if (key.startsWith('AGENTICAI_') || key.startsWith('AWS_')) continue;
    env[key] = value;
  }
  return env;
}

function runTeardown(
  args: readonly string[],
  options: { readonly env?: Record<string, string>; readonly input?: string } = {},
): RunResult {
  fs.writeFileSync(stubLog, '');
  const result = spawnSync('bash', [SCRIPT, ...args], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    input: options.input ?? '',
    env: {
      ...baseEnv(),
      PATH: `${stubBin}${path.delimiter}${process.env.PATH ?? ''}`,
      STUB_LOG: stubLog,
      ...(options.env ?? {}),
    },
  });
  const npxCalls = fs
    .readFileSync(stubLog, 'utf8')
    .split('\n')
    .filter((line) => line.trim().length > 0);
  return {
    status: result.status ?? -1,
    stdout: result.stdout ?? '',
    stderr: result.stderr ?? '',
    npxCalls,
  };
}

/** Non-comment, non-blank lines of the script. */
function scriptCode(): string[] {
  return fs
    .readFileSync(SCRIPT, 'utf8')
    .split('\n')
    .filter((line) => !/^\s*#/.test(line) && line.trim().length > 0);
}

const NETWORK_STACK = 'AgenticAI-Workload-NetworkStack';
const APP_STACK = 'AgenticAI-Workload-AppStack';
const WORKLOAD_CONTEXT = { AGENTICAI_WORKLOAD_ACCOUNT_ID: '111111111111' };

describe('Round 1B — teardown stage mapping', () => {
  it('maps every planned stack to its owning bin/ stage and destroys nothing in --dry-run', () => {
    const run = runTeardown(['--dry-run']);
    expect(run.status).toBe(EXIT.ok);
    expect(run.npxCalls).toEqual([]);

    const expectedPairs: ReadonlyArray<readonly [string, string]> = [
      ['AgenticAI-WorkloadPipelineStack', 'pipeline'],
      ['AgenticAI-PlatformPipelineStack', 'pipeline'],
      ['AgenticAI-GapClosureStack', 'gap-closure'],
      ['AgenticAI-D03-WorkstreamGateway-demo-primary', 'd03-workstream-gateway'],
      ['AgenticAI-D03-WorkloadAgentStack', 'd03-workload'],
      ['AgenticAI-D03-PlatformCoreStack', 'd03-platform'],
      [APP_STACK, 'workload'],
      [NETWORK_STACK, 'workload'],
      ['AgenticAI-Platform-InferenceGatewayStack', 'platform'],
      ['AgenticAI-Platform-RegistryStack', 'platform'],
      ['AgenticAI-Platform-GuardrailStack', 'platform'],
      ['AgenticAI-Platform-AuditStack', 'platform'],
      ['AgenticAI-Platform-LogArchiveStack', 'platform'],
      ['AgenticAI-Management-OrgStack', 'management'],
    ];
    for (const [stack, stage] of expectedPairs) {
      const row = run.stdout.split('\n').find((line) => line.startsWith(`${stack} `));
      if (row === undefined) {
        throw new Error(`teardown plan has no row for ${stack}:\n${run.stdout}`);
      }
      expect(row).toContain(stage);
    }
  });

  it('passes the owning stage (and stage-gating context) to cdk destroy', () => {
    const run = runTeardown(['--stack', APP_STACK], {
      env: { ...WORKLOAD_CONTEXT, STUB_DESCRIBE_MODE: 'exists', STUB_DESTROY_EXIT: '0' },
      input: 'y\n',
    });
    expect(run.status).toBe(EXIT.ok);
    expect(run.npxCalls).toHaveLength(1);
    expect(run.npxCalls[0]).toContain('cdk destroy --force');
    expect(run.npxCalls[0]).toContain('--context stage=workload');
    // WorkloadAppStack is only declared when this flag is set; without it the
    // destroy would silently match no stack.
    expect(run.npxCalls[0]).toContain('--context agenticai/deployWorkloadApp=true');
    expect(run.npxCalls[0]).toContain(APP_STACK);
  });

  it('requires and forwards the Platform inference Gateway model-rate context', () => {
    const stack = 'AgenticAI-Platform-InferenceGatewayStack';
    const required = {
      AGENTICAI_ORGANIZATION_ID: 'o-example123',
      AGENTICAI_PLATFORM_ACCOUNT_ID: '111111111111',
      AGENTICAI_PIPELINE_ROLE_ARN:
        'arn:aws:iam::111111111111:role/AgenticAI-PlatformPipelineRole',
    };
    const missing = runTeardown(['--stack', stack], {
      env: { ...required, STUB_DESCRIBE_MODE: 'exists' },
      input: 'y\n',
    });
    expect(missing.status).toBe(EXIT.configError);
    expect(missing.stderr).toContain('AGENTICAI_INFERENCE_MODEL_RATE_LIMITS');
    expect(missing.npxCalls).toEqual([]);

    const configured = runTeardown(['--stack', stack], {
      env: {
        ...required,
        AGENTICAI_INFERENCE_MODEL_RATE_LIMITS:
          '[{"qualifiedModelId":"openai.gpt-oss-120b","requestsPerMinute":10,"tokensPerMinute":10000}]',
        STUB_DESCRIBE_MODE: 'exists',
      },
      input: 'y\n',
    });
    expect(configured.status).toBe(EXIT.ok);
    expect(configured.npxCalls).toHaveLength(1);
    expect(configured.npxCalls[0]).toContain('--context stage=platform');
    expect(configured.npxCalls[0]).toContain(
      '--context agenticai/inferenceModelRateLimits=',
    );
  });

  it('never issues a stage-less cdk destroy', () => {
    const run = runTeardown(['--stack', NETWORK_STACK], {
      env: { ...WORKLOAD_CONTEXT, STUB_DESCRIBE_MODE: 'exists' },
      input: 'y\n',
    });
    expect(run.npxCalls.length).toBeGreaterThan(0);
    for (const call of run.npxCalls) {
      expect(call).toMatch(/--context stage=[a-z0-9-]+/);
    }
  });

  it('discovers per-workstream gateway stacks and destroys them with their own tenant/agent', () => {
    const run = runTeardown(['--stack', 'AgenticAI-D03-WorkstreamGateway-acme-billing'], {
      env: {
        STUB_DESCRIBE_MODE: 'exists',
        STUB_LIST_OUTPUT:
          'AgenticAI-Platform-AuditStack\tAgenticAI-D03-WorkstreamGateway-acme-billing',
        AGENTICAI_D03_PLATFORM_ACCOUNT_ID: '222222222222',
        AGENTICAI_D03_ALLOWED_TOOL_IDS: '["tool-one"]',
      },
      input: 'y\n',
    });
    expect(run.status).toBe(EXIT.ok);
    expect(run.npxCalls).toHaveLength(1);
    expect(run.npxCalls[0]).toContain('--context stage=d03-workstream-gateway');
    expect(run.npxCalls[0]).toContain('--context agenticai/tenantId=acme');
    expect(run.npxCalls[0]).toContain('--context agenticai/agentId=billing');
  });
});

describe('Round 1B — teardown refuses to call failure success', () => {
  it('reports an absent stack as ABSENT and attempts no destroy', () => {
    const run = runTeardown(['--stack', NETWORK_STACK], {
      env: { ...WORKLOAD_CONTEXT, STUB_DESCRIBE_MODE: 'absent' },
      input: 'y\n',
    });
    expect(run.status).toBe(EXIT.ok);
    expect(run.stdout).toContain('ABSENT');
    expect(run.npxCalls).toEqual([]);
  });

  it('exits non-zero when a present stack fails to destroy', () => {
    const run = runTeardown(['--stack', NETWORK_STACK], {
      env: { ...WORKLOAD_CONTEXT, STUB_DESCRIBE_MODE: 'exists', STUB_DESTROY_EXIT: '1' },
      input: 'y\n',
    });
    expect(run.status).toBe(EXIT.destroyFailed);
    expect(run.npxCalls).toHaveLength(1);
    expect(run.stderr).toContain('cdk destroy failed');
    expect(run.stdout).toContain('FAILED');
    // The distinction that the old `|| echo` destroyed: a failed destroy is
    // never reported as an absent stack.
    expect(run.stdout).not.toContain('ABSENT');
  });

  it('distinguishes an unexpected describe-stacks failure from an absent stack', () => {
    const run = runTeardown(['--stack', NETWORK_STACK], {
      env: { ...WORKLOAD_CONTEXT, STUB_DESCRIBE_MODE: 'error' },
      input: 'y\n',
    });
    expect(run.status).toBe(EXIT.unexpectedAwsError);
    expect(run.status).not.toBe(EXIT.ok);
    expect(run.stderr).toContain('describe-stacks failed');
    expect(run.stdout).not.toContain('ABSENT');
    expect(run.npxCalls).toEqual([]);
  });

  it('aborts before any destroy when required stage context is missing', () => {
    const run = runTeardown(['--stack', NETWORK_STACK], {
      env: { STUB_DESCRIBE_MODE: 'exists' },
      input: 'y\n',
    });
    expect(run.status).toBe(EXIT.configError);
    expect(run.stderr).toContain('AGENTICAI_WORKLOAD_ACCOUNT_ID');
    expect(run.npxCalls).toEqual([]);
  });

  it('still requires confirmation and destroys nothing when it is declined', () => {
    const run = runTeardown(['--stack', NETWORK_STACK], {
      env: { ...WORKLOAD_CONTEXT, STUB_DESCRIBE_MODE: 'exists' },
      input: 'n\n',
    });
    expect(run.status).toBe(EXIT.aborted);
    expect(run.stdout).toContain('Aborted.');
    expect(run.npxCalls).toEqual([]);
  });

  it('rejects an unknown flag instead of silently destroying the default list', () => {
    const run = runTeardown(['--destroy-everything'], { input: 'y\n' });
    expect(run.status).toBe(EXIT.configError);
    expect(run.npxCalls).toEqual([]);
  });

  it('contains no blanket error-swallowing operators', () => {
    const code = scriptCode().join('\n');
    expect(code).not.toMatch(/\|\|\s*echo/);
    expect(code).not.toMatch(/\|\|\s*true/);
    expect(code).not.toMatch(/\|\|\s*:/);
  });

  it('runs under set -e and set -u', () => {
    expect(scriptCode()[0]).toMatch(/^set -euo?/);
  });
});
