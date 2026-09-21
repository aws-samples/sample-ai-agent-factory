/*
 * Service-created CloudWatch log groups are not CloudFormation resources.
 * The teardown script must capture exact CodeBuild/Lambda physical ids before
 * stack deletion, then remove only those exact default log groups afterward.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

const REPO_ROOT = path.resolve(__dirname, "..", "..");
const SCRIPT = path.join(REPO_ROOT, "scripts", "teardown.sh");
const STACK = "AgenticAI-Workload-NetworkStack";

const AWS_STUB = `#!/usr/bin/env bash
printf '%s\\n' "aws $*" >> "\${STUB_SEQUENCE_LOG:?}"
case "\${1:-}:\${2:-}" in
  cloudformation:list-stacks)
    exit 0
    ;;
  cloudformation:describe-stacks)
    printf 'CREATE_COMPLETE\\n'
    exit 0
    ;;
  cloudformation:list-stack-resources)
    case "\${STUB_RESOURCE_MODE:-success}" in
      success) printf '%b' "\${STUB_STACK_RESOURCES:-}"; exit 0 ;;
      error)
        printf 'An error occurred (AccessDenied): not authorized\\n' >&2
        exit 254
        ;;
    esac
    ;;
  logs:delete-log-group)
    case "\${STUB_DELETE_LOG_MODE:-success}" in
      success) exit 0 ;;
      absent)
        printf 'An error occurred (ResourceNotFoundException): log group absent\\n' >&2
        exit 254
        ;;
      error)
        printf 'An error occurred (AccessDeniedException): not authorized\\n' >&2
        exit 254
        ;;
    esac
    ;;
esac
exit 0
`;

const NPX_STUB = `#!/usr/bin/env bash
printf '%s\\n' "npx $*" >> "\${STUB_SEQUENCE_LOG:?}"
exit "\${STUB_DESTROY_EXIT:-0}"
`;

interface RunResult {
  readonly status: number;
  readonly stdout: string;
  readonly stderr: string;
  readonly sequence: readonly string[];
}

let stubDir: string;
let stubBin: string;
let sequenceLog: string;

beforeAll(() => {
  const scratchRoot = process.env.KIROCREW_SCRATCH ?? os.tmpdir();
  stubDir = fs.mkdtempSync(path.join(scratchRoot, "teardown-service-logs-"));
  stubBin = path.join(stubDir, "bin");
  sequenceLog = path.join(stubDir, "sequence.log");
  fs.mkdirSync(stubBin);
  fs.writeFileSync(path.join(stubBin, "aws"), AWS_STUB, { mode: 0o755 });
  fs.writeFileSync(path.join(stubBin, "npx"), NPX_STUB, { mode: 0o755 });
});

afterAll(() => {
  fs.rmSync(stubDir, { recursive: true, force: true });
});

function cleanEnv(): Record<string, string> {
  const env: Record<string, string> = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (value === undefined) continue;
    if (
      key.startsWith("AGENTICAI_") ||
      key.startsWith("AWS_") ||
      key.startsWith("STUB_")
    ) {
      continue;
    }
    env[key] = value;
  }
  return env;
}

function runTeardown(extraEnv: Record<string, string> = {}): RunResult {
  fs.writeFileSync(sequenceLog, "");
  const result = spawnSync("bash", [SCRIPT, "--stack", STACK], {
    cwd: REPO_ROOT,
    encoding: "utf8",
    input: "y\n",
    env: {
      ...cleanEnv(),
      PATH: `${stubBin}${path.delimiter}${process.env.PATH ?? ""}`,
      STUB_SEQUENCE_LOG: sequenceLog,
      AGENTICAI_WORKLOAD_ACCOUNT_ID: "111111111111",
      ...extraEnv,
    },
  });
  return {
    status: result.status ?? -1,
    stdout: result.stdout ?? "",
    stderr: result.stderr ?? "",
    sequence: fs
      .readFileSync(sequenceLog, "utf8")
      .split("\n")
      .filter((line) => line.length > 0),
  };
}

const GENERATED_RESOURCES = [
  "AWS::Lambda::Function\tAgenticAI-TestCleanupFunction",
  "AWS::CodeBuild::Project\tAgenticAI-TestCleanupProject",
  "AWS::S3::Bucket\tAgenticAI-IgnoredBucket",
  "",
].join("\n");

function deleteCalls(run: RunResult): readonly string[] {
  return run.sequence.filter((line) =>
    line.startsWith("aws logs delete-log-group"),
  );
}

describe("teardown service-created log cleanup", () => {
  it("captures exact physical ids before destroy and deletes only their log groups afterward", () => {
    const run = runTeardown({ STUB_STACK_RESOURCES: GENERATED_RESOURCES });

    expect(run.status).toBe(0);
    expect(deleteCalls(run)).toEqual([
      "aws logs delete-log-group --log-group-name /aws/lambda/AgenticAI-TestCleanupFunction",
      "aws logs delete-log-group --log-group-name /aws/codebuild/AgenticAI-TestCleanupProject",
    ]);
    expect(run.sequence.join("\n")).not.toContain("AgenticAI-IgnoredBucket");

    const captureIndex = run.sequence.findIndex((line) =>
      line.startsWith("aws cloudformation list-stack-resources"),
    );
    const destroyIndex = run.sequence.findIndex((line) =>
      line.startsWith("npx cdk destroy"),
    );
    const cleanupIndex = run.sequence.findIndex((line) =>
      line.startsWith("aws logs delete-log-group"),
    );
    expect(captureIndex).toBeGreaterThanOrEqual(0);
    expect(destroyIndex).toBeGreaterThan(captureIndex);
    expect(cleanupIndex).toBeGreaterThan(destroyIndex);
  });

  it("does not delete log groups when stack destruction fails", () => {
    const run = runTeardown({
      STUB_STACK_RESOURCES: GENERATED_RESOURCES,
      STUB_DESTROY_EXIT: "1",
    });

    expect(run.status).toBe(1);
    expect(deleteCalls(run)).toEqual([]);
    expect(run.stderr).toContain("cdk destroy failed");
  });

  it("treats an already-absent exact log group as idempotent success", () => {
    const run = runTeardown({
      STUB_STACK_RESOURCES:
        "AWS::Lambda::Function\tAgenticAI-TestCleanupFunction\n",
      STUB_DELETE_LOG_MODE: "absent",
    });

    expect(run.status).toBe(0);
    expect(run.stdout).toContain(
      "ABSENT  service log group /aws/lambda/AgenticAI-TestCleanupFunction",
    );
  });

  it("fails closed when exact log-group deletion is denied", () => {
    const run = runTeardown({
      STUB_STACK_RESOURCES:
        "AWS::Lambda::Function\tAgenticAI-TestCleanupFunction\n",
      STUB_DELETE_LOG_MODE: "error",
    });

    expect(run.status).toBe(1);
    expect(run.stderr).toContain("delete-log-group failed");
    expect(run.stderr).toContain("Teardown is INCOMPLETE");
  });

  it("never destroys a stack when exact resource capture fails", () => {
    const run = runTeardown({ STUB_RESOURCE_MODE: "error" });

    expect(run.status).toBe(1);
    expect(run.stderr).toContain("list-stack-resources failed");
    expect(run.sequence.some((line) => line.startsWith("npx "))).toBe(false);
    expect(deleteCalls(run)).toEqual([]);
  });
});
