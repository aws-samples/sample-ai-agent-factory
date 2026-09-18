/**
 * Phase 7 conformance — CDK Pipelines (platform + workload) with evaluation
 * gate and manual approval.
 *
 * Spec: R-DEVX-002 mandatory stage sequence (§1.3.5 L210-212).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { App } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';

import { PlatformPipelineStack } from '../../pipelines/platform-pipeline-stack';
import { stageAwareSynthCommands } from '../../pipelines/synth-commands';
import { WorkloadPipelineStack } from '../../pipelines/workload-pipeline-stack';

const GITHUB_CONNECTION =
  'arn:aws:codestar-connections:us-west-2:111111111111:connection/abc-123';

function synthPlatform() {
  const app = new App();
  const stack = new PlatformPipelineStack(app, 'PP', {
    env: { account: '111111111111', region: 'us-west-2' },
    githubRepo: 'aws-samples/sample-ai-agent-factory',
    githubConnectionArn: GITHUB_CONNECTION,
    organizationId: 'o-example123',
    logArchive: { env: { account: '333333333333', region: 'us-west-2' }, envName: 'nonprod' },
    audit: { env: { account: '666666666666', region: 'us-west-2' }, envName: 'nonprod' },
    platformNonprod: { env: { account: '111111111111', region: 'us-west-2' }, envName: 'nonprod' },
    platformProd: { env: { account: '222222222222', region: 'us-west-2' }, envName: 'prod' },
    workloadAccountIds: ['444444444444', '555555555555'],
    pipelineRoleArn: 'arn:aws:iam::111111111111:role/AgenticAI-PlatformPipelineRole',
    applicationId: 'platform-inference',
    tenantId: 'shared',
    agentId: 'shared',
    costCentre: 'platform',
    inferenceModelRateLimits: [
      {
        qualifiedModelId: 'openai.gpt-oss-120b',
        requestsPerMinute: 10,
        tokensPerMinute: 10_000,
      },
    ],
  });
  return Template.fromStack(stack);
}

function synthWorkload() {
  const app = new App();
  const stack = new WorkloadPipelineStack(app, 'WP', {
    env: { account: '111111111111', region: 'us-west-2' },
    githubRepo: 'aws-samples/sample-ai-agent-factory',
    githubConnectionArn: GITHUB_CONNECTION,
    tenantId: 'demo',
    agentId: 'primary',
    costCentre: 'engineering',
    workloadNonprodEnv: { account: '444444444444', region: 'us-west-2' },
    workloadProdEnv: { account: '555555555555', region: 'us-west-2' },
    workloadNonprodAvailabilityZones: ['us-west-2a', 'us-west-2b', 'us-west-2c'],
    workloadProdAvailabilityZones: ['us-west-2a', 'us-west-2b', 'us-west-2c'],
  });
  return Template.fromStack(stack);
}

function expectCleanupSafeArtifactStore(
  template: Template,
  expectedTags: Record<string, string>,
): void {
  template.resourceCountIs('AWS::S3::Bucket', 1);
  template.hasResource('AWS::S3::Bucket', {
    DeletionPolicy: 'Delete',
    UpdateReplacePolicy: 'Delete',
    Properties: Match.objectLike({
      BucketEncryption: Match.anyValue(),
      LifecycleConfiguration: {
        Rules: Match.arrayWith([
          Match.objectLike({
            AbortIncompleteMultipartUpload: { DaysAfterInitiation: 7 },
            ExpirationInDays: 30,
            Status: 'Enabled',
          }),
        ]),
      },
      OwnershipControls: {
        Rules: [{ ObjectOwnership: 'BucketOwnerEnforced' }],
      },
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    }),
  });
  template.resourceCountIs('AWS::KMS::Key', 1);
  template.hasResource('AWS::KMS::Key', {
    DeletionPolicy: 'Delete',
    UpdateReplacePolicy: 'Delete',
    Properties: Match.objectLike({
      EnableKeyRotation: true,
      PendingWindowInDays: 7,
    }),
  });

  for (const resourceType of ['AWS::S3::Bucket', 'AWS::KMS::Key']) {
    const resources = Object.values(template.findResources(resourceType)) as any[];
    const tagMap = Object.fromEntries(
      resources[0].Properties.Tags.map((tag: { Key: string; Value: string }) => [
        tag.Key,
        tag.Value,
      ]),
    );
    expect(tagMap).toMatchObject(expectedTags);
  }

  template.resourceCountIs('Custom::S3AutoDeleteObjects', 1);
}

describe('Phase 7 — Platform pipeline', () => {
  it('emits a single CodePipeline', () => {
    const t = synthPlatform();
    t.resourceCountIs('AWS::CodePipeline::Pipeline', 1);
  });

  it('pipeline name is stable', () => {
    const t = synthPlatform();
    t.hasResourceProperties('AWS::CodePipeline::Pipeline', {
      Name: 'agenticai-platform-pipeline',
    });
  });

  it('configures cross-account keys (required for multi-account stages)', () => {
    const t = synthPlatform();
    // CodePipelines emits encrypted artifact store with KMS key ARN when crossAccountKeys=true.
    const pipelines = t.findResources('AWS::CodePipeline::Pipeline');
    const pipeline = Object.values(pipelines)[0] as any;
    const stores = pipeline.Properties.ArtifactStores ?? [
      pipeline.Properties.ArtifactStore,
    ];
    const usesKms = stores.some(
      (s: any) => s?.ArtifactStore?.EncryptionKey?.Type === 'KMS' || s?.EncryptionKey?.Type === 'KMS',
    );
    expect(usesKms).toBe(true);
  });
});

describe('Phase 7 — pipeline artifact stores', () => {
  it('encrypts, tags, expires and removes Platform and Workload artifacts', () => {
    expectCleanupSafeArtifactStore(synthPlatform(), {
      'application-id': 'platform-inference',
      'agent-id': 'shared',
      'tenant-id': 'shared',
      'cost-centre': 'platform',
      environment: 'pipeline',
    });
    expectCleanupSafeArtifactStore(synthWorkload(), {
      'application-id': 'demo',
      'agent-id': 'primary',
      'tenant-id': 'demo',
      'cost-centre': 'engineering',
      environment: 'pipeline',
    });
  });
});

describe('Phase 7 — cross-account bootstrap role contract', () => {
  it('references both deploy and CloudFormation execution roles in target accounts', () => {
    const platform = JSON.stringify(synthPlatform().toJSON());
    expect(platform).toContain(
      'cdk-hnb659fds-deploy-role-333333333333-us-west-2',
    );
    expect(platform).toContain(
      'cdk-hnb659fds-cfn-exec-role-333333333333-us-west-2',
    );

    const workload = JSON.stringify(synthWorkload().toJSON());
    expect(workload).toContain(
      'cdk-hnb659fds-deploy-role-444444444444-us-west-2',
    );
    expect(workload).toContain(
      'cdk-hnb659fds-cfn-exec-role-444444444444-us-west-2',
    );

    const bootstrapSource = readFileSync(
      resolve(__dirname, '../../pipelines/bootstrap/bootstrap-cross-account.sh'),
      'utf8',
    );
    expect(bootstrapSource).toContain(
      'iam:PassedToService=codepipeline.amazonaws.com',
    );
    expect(bootstrapSource).toContain(
      'iam:PassedToService=cloudformation.amazonaws.com',
    );
  });
});

describe('Phase 7 — Workload pipeline has mandatory stages + eval gate', () => {
  it('emits an evaluation-gate CodeBuild with the 5 SLO threshold env vars', () => {
    const t = synthWorkload();
    const projects = t.findResources('AWS::CodeBuild::Project');
    const joined = JSON.stringify(projects);
    expect(joined).toContain('EVAL_REGRESSION_PASS_MIN_PCT');
    expect(joined).toContain('EVAL_GUARDRAIL_VIOLATION_MAX_PCT');
    expect(joined).toContain('EVAL_QUALITY_MIN_PCT');
    expect(joined).toContain('EVAL_TOOL_SUCCESS_MIN_PCT');
    expect(joined).toContain('EVAL_FIRST_TOKEN_P99_MAX_MS');
  });

  it('pipeline name embeds tenant + agent', () => {
    const t = synthWorkload();
    t.hasResourceProperties('AWS::CodePipeline::Pipeline', {
      Name: 'agenticai-workload-demo-primary',
    });
  });
});

/**
 * Round 1B — false-green regressions (tasks/todo.md §Round 1 B).
 *
 * Each expectation below fails against the pre-Round-1B implementation:
 *   - synth ran a bare `npx cdk synth`, which the app answers with an EMPTY
 *     cloud assembly and exit 0;
 *   - evaluation, approval, canary and soak sat in one `pre` array with no
 *     declared dependencies, so CDK Pipelines gave them the same RunOrder and
 *     ran them concurrently;
 *   - the canary deploy called `aws lambda update-alias … || true` against a
 *     function this blueprint never creates, and the soak read a missing alarm
 *     as `OK`.
 */
let workloadTemplate: Template | undefined;
let platformTemplate: Template | undefined;

function workload(): Template {
  workloadTemplate = workloadTemplate ?? synthWorkload();
  return workloadTemplate;
}

function platform(): Template {
  platformTemplate = platformTemplate ?? synthPlatform();
  return platformTemplate;
}

/**
 * All CodeBuild projects as one string. Assertions must avoid double quotes:
 * buildspecs are embedded as JSON strings, so `"` appears escaped.
 */
function codeBuildBlob(t: Template): string {
  return JSON.stringify(t.findResources('AWS::CodeBuild::Project'));
}

function countOccurrences(haystack: string, needle: string): number {
  return haystack.split(needle).length - 1;
}

function prodStageActions(t: Template): Array<{ Name: string; RunOrder: number }> {
  const pipelines = t.findResources('AWS::CodePipeline::Pipeline');
  const pipeline = Object.values(pipelines)[0] as any;
  const stage = (pipeline.Properties.Stages as any[]).find((s) => s.Name === 'Prod');
  if (!stage) {
    throw new Error(
      `No 'Prod' stage; got: ${(pipeline.Properties.Stages as any[]).map((s) => s.Name).join(', ')}`,
    );
  }
  return stage.Actions as Array<{ Name: string; RunOrder: number }>;
}

function runOrderOf(t: Template, needle: string): number {
  const actions = prodStageActions(t);
  const action = actions.find((a) => String(a.Name).includes(needle));
  if (!action) {
    throw new Error(
      `No Prod action matching '${needle}'; got: ${actions.map((a) => a.Name).join(', ')}`,
    );
  }
  return action.RunOrder;
}

describe('Round 1B — workload pipeline synth cannot produce an empty assembly', () => {
  assertSynthIsStageAware(workload);
});

describe('Round 1B — platform pipeline synth cannot produce an empty assembly', () => {
  assertSynthIsStageAware(platform);
});

/**
 * Shared synth-step expectations. Declared as a function so both pipelines get
 * the identical checks without relying on `describe.each` tuple inference.
 */
function assertSynthIsStageAware(template: () => Template): void {
  it('names the stage explicitly on every cdk synth invocation', () => {
    const blob = codeBuildBlob(template());
    const staged = countOccurrences(blob, 'npx cdk synth --context stage=');
    const total = countOccurrences(blob, 'npx cdk synth');
    expect(staged).toBeGreaterThan(0);
    // A bare `npx cdk synth` anywhere means the app's `undefined` stage branch
    // can still emit an empty assembly and exit 0.
    expect(staged).toBe(total);
  });

  it('asserts the assembly, its own template and each stage assembly are non-empty', () => {
    const blob = codeBuildBlob(template());
    expect(blob).toContain('test -f cdk.out/manifest.json');
    expect(blob).toContain('aws:cloudformation:stack');
    expect(blob).toContain('cdk.out/assembly-*Nonprod');
    expect(blob).toContain('cdk.out/assembly-*Prod');
    expect(blob).toContain('.template.json');
  });

  it('forwards the context the stage needs so a missing key fails loudly', () => {
    const blob = codeBuildBlob(template());
    expect(blob).toContain('--context agenticai/githubRepo=');
    expect(blob).toContain('--context agenticai/githubConnectionArn=');
  });

  it('swallows no command failure in any build step', () => {
    const blob = codeBuildBlob(template());
    expect(blob).not.toContain('|| true');
    expect(blob).not.toContain('|| echo');
    expect(blob).not.toContain('2>/dev/null ||');
  });
}

describe('Round 1B — workload promotion order is declared, not implied', () => {
  it('orders evaluation -> approval -> canary deploy -> canary soak', () => {
    const t = workload();
    expect(runOrderOf(t, 'EvaluationGate')).toBeLessThan(runOrderOf(t, 'ProdApproval'));
    expect(runOrderOf(t, 'ProdApproval')).toBeLessThan(runOrderOf(t, 'CanaryDeploy'));
    expect(runOrderOf(t, 'CanaryDeploy')).toBeLessThan(runOrderOf(t, 'CanarySoak'));
  });

  it('places the manual approval before any canary action', () => {
    const t = workload();
    const approval = runOrderOf(t, 'ProdApproval');
    for (const action of prodStageActions(t)) {
      if (String(action.Name).includes('Canary')) {
        expect(action.RunOrder).toBeGreaterThan(approval);
      }
    }
  });

  it('deploys Prod only after the soak', () => {
    const t = workload();
    const soak = runOrderOf(t, 'CanarySoak');
    const deployActions = prodStageActions(t).filter((a) => {
      const name = String(a.Name);
      // 'CanaryDeploy' is a gate, not the Prod deployment.
      if (name.includes('Canary')) return false;
      return name.includes('Deploy') || name.includes('Prepare');
    });
    expect(deployActions.length).toBeGreaterThan(0);
    for (const action of deployActions) {
      expect(action.RunOrder).toBeGreaterThan(soak);
    }
  });
});

describe('Round 1B — workload canary and evaluation fail closed', () => {
  it('replaces the fake Lambda alias canary with an explicit failing placeholder', () => {
    const blob = codeBuildBlob(workload());
    expect(blob).not.toContain('aws lambda update-alias');
    expect(blob).toContain('NOT IMPLEMENTED');
    expect(blob).toContain('exit 1');
  });

  it('runs a real traffic-shift command when one is supplied', () => {
    const app = new App();
    const stack = new WorkloadPipelineStack(app, 'WPCanary', {
      env: { account: '111111111111', region: 'us-west-2' },
      githubRepo: 'aws-samples/sample-ai-agent-factory',
      githubConnectionArn: GITHUB_CONNECTION,
      tenantId: 'demo',
      agentId: 'primary',
      costCentre: 'engineering',
      workloadNonprodEnv: { account: '444444444444', region: 'us-west-2' },
      workloadProdEnv: { account: '555555555555', region: 'us-west-2' },
      workloadNonprodAvailabilityZones: ['us-west-2a', 'us-west-2b', 'us-west-2c'],
      workloadProdAvailabilityZones: ['us-west-2a', 'us-west-2b', 'us-west-2c'],
      canaryDeployCommands: ['scripts/shift-agentcore-canary.sh 5'],
    });
    const blob = codeBuildBlob(Template.fromStack(stack));
    expect(blob).toContain('scripts/shift-agentcore-canary.sh 5');
    expect(blob).not.toContain('NOT IMPLEMENTED');
  });

  it('treats a missing or unusable online-eval alarm as a failed soak', () => {
    const blob = codeBuildBlob(workload());
    // The soak must prove the composite alarm exists before polling it.
    expect(blob).toContain('--alarm-types CompositeAlarm');
    expect(blob).toContain('length(CompositeAlarms)');
    expect(blob).toContain('not found');
    // …and must end on OK, so INSUFFICIENT_DATA cannot pass as health.
    expect(blob).toContain('INSUFFICIENT_DATA');
    expect(blob).toContain('OK required');
  });

  it('fails the evaluation gate when the harness is absent', () => {
    const blob = codeBuildBlob(workload());
    expect(blob).toContain('scripts/evaluation_gate.py is missing');
  });
});


describe('Round 1 integration — CDK app self-synth contract', () => {
  const appSource = readFileSync(
    resolve(__dirname, '../../bin/agentic-ai-platform.ts'),
    'utf8',
  );
  const synthCommandsSource = readFileSync(
    resolve(__dirname, '../../pipelines/synth-commands.ts'),
    'utf8',
  );
  const packageDocument = JSON.parse(
    readFileSync(resolve(__dirname, '../../package.json'), 'utf8'),
  ) as { scripts: Record<string, string> };

  it('uses an explicit, meaningful stage for the default npm synth', () => {
    expect(packageDocument.scripts.synth).toContain('--strict');
    expect(packageDocument.scripts.synth).toContain('--context stage=management');
  });

  it('enters the blueprint package for a multi-project source checkout', () => {
    expect(synthCommandsSource).toContain(
      "const BLUEPRINT_SOURCE_DIRECTORY = 'enterprise-agentic-ai-platform-blueprint'",
    );
    expect(synthCommandsSource).toContain('if [ -f package.json ]; then :;');
    expect(synthCommandsSource).toContain('blueprint package.json not found');
    expect(synthCommandsSource).toContain('exit 1');
  });

  it('emits independently valid POSIX commands and publishes root cdk.out', () => {
    const commands = stageAwareSynthCommands({
      stage: 'pipeline',
      context: {},
      expectedStackArtifactId: 'AgenticAI-PlatformPipelineStack',
      expectedStageAssemblyGlobs: [
        'cdk.out/assembly-*Nonprod',
        'cdk.out/assembly-*Prod',
      ],
    });

    for (const command of commands) {
      expect(() => execFileSync('/bin/sh', ['-n', '-c', command])).not.toThrow();
    }

    const assemblyLoop = commands.find((command) => command.startsWith('for asm in'));
    expect(assemblyLoop).toContain('done');
    expect(commands[commands.length - 1]).toContain('CODEBUILD_SRC_DIR/cdk.out');
    expect(commands[commands.length - 1]).toContain('mv cdk.out');
  });

  it('rejects a missing stage instead of emitting an empty assembly', () => {
    expect(appSource).toMatch(/case undefined:\s*throw new Error\(/);
    expect(appSource).toContain("Missing required CDK context 'stage'");
  });

  it('forwards the complete shared context to both pipeline stacks', () => {
    expect(appSource.match(/synthContext: sharedSynthContext/g)).toHaveLength(2);
    for (const key of [
      'organizationId',
      'platformNonprodAccountId',
      'platformProdAccountId',
      'auditAccountId',
      'logArchiveAccountId',
      'workloadNonprodAccountId',
      'workloadProdAccountId',
      'workloadNonprodAvailabilityZones',
      'workloadProdAvailabilityZones',
      'pipelineRoleArn',
      'workloadAccountIds',
      'applicationId',
      'tenantId',
      'agentId',
      'costCentre',
      'inferenceModelRateLimits',
    ]) {
      expect(appSource).toContain(`'agenticai/${key}'`);
    }
  });
});