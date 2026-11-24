/**
 * Phase 25 conformance — CloudWatch Omni hub model and evaluation options
 * (ADR-15 to ADR-19).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App, RemovalPolicy } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";

import { OmniDomainStack } from "../../apps/management-account/lib/omni-domain-stack";
import { OmniHubKeysStack } from "../../apps/platform-account/lib/omni-hub-keys-stack";
import { OmniHubStack } from "../../apps/platform-account/lib/omni-hub-stack";
import { D03WorkstreamRuntimeMemoryStack } from "../../apps/workload-account/lib/d03-workstream-runtime-memory-stack";
import { WorkstreamObservabilityStack } from "../../pipelines/workstream-observability-stack";

const MANAGEMENT = "111111111111";
const HUB = "222222222222";
const CELL = "444444444444";
const REGION = "eu-west-1";
const TAGS = {
  "application-id": "platform-observability",
  "agent-id": "shared",
  "tenant-id": "shared",
  "cost-centre": "platform",
  environment: "shared",
};
// Operator-provisioned keys (OmniHubKeysStack outputs) for the import-mode tests.
const SPACE_KEY_ARN = `arn:aws:kms:${REGION}:${HUB}:key/11111111-2222-4333-8444-555555555555`;
const LOGS_KEY_ARN = `arn:aws:kms:${REGION}:${HUB}:key/66666666-7777-4888-9999-000000000000`;

function domainTemplate(): Template {
  const app = new App();
  return Template.fromStack(
    new OmniDomainStack(app, "Domain", {
      env: { account: MANAGEMENT, region: REGION },
      domainName: "agenticai-fleet",
      identityCenterInstanceArn: "arn:aws:sso:::instance/ssoins-1234567890abcdef",
      tags: TAGS,
    }),
  );
}

function hubTemplate(withCentralization = true): Template {
  const app = new App();
  return Template.fromStack(
    new OmniHubStack(app, "Hub", {
      env: { account: HUB, region: REGION },
      domainId: "d-1a2b3c4d5e",
      keyRemovalPolicy: RemovalPolicy.DESTROY,
      centralization: withCentralization
        ? {
            organizationId: "o-abc123def4",
            sourceScope: "OrganizationalUnitId IN ('ou-aaaa-11111111','ou-aaaa-22222222')",
            sourceRegions: ["eu-west-1", "eu-west-3"],
          }
        : undefined,
      tags: TAGS,
    }),
  );
}

function observabilityTemplate(enableTransactionSearch?: boolean): Template {
  const app = new App();
  return Template.fromStack(
    new WorkstreamObservabilityStack(app, "Obs", {
      env: { account: CELL, region: REGION },
      sinkArn: `arn:aws:oam:${REGION}:${HUB}:sink/abc`,
      enableTransactionSearch,
      resourceTags: {
        applicationId: "demo",
        agentId: "primary",
        tenantId: "demo",
        costCentre: "engineering",
        environment: "nonprod",
      },
    }),
  );
}

function runtimeTemplate(telemetry?: {
  keepPromptContentInSpans?: boolean;
  redactSpanAttributes?: readonly string[];
}): Template {
  const app = new App();
  return Template.fromStack(
    new D03WorkstreamRuntimeMemoryStack(app, "RuntimeMemory", {
      env: { account: CELL, region: REGION },
      envName: "nonprod",
      applicationId: "demo",
      agentId: "primary",
      tenantId: "demo",
      costCentre: "engineering",
      runtimeExecutionRoleArnOverride: `arn:aws:iam::${CELL}:role/AgenticAI-D03-nonprod-demo-primary-runtime`,
      telemetry,
    }),
  );
}

describe("Phase 25 — Omni organization domain (management account)", () => {
  it("emits one organization domain bound to Identity Center with a customer-provided access role", () => {
    const t = domainTemplate();
    t.resourceCountIs("AWS::CloudWatch::DomainForOrganization", 1);
    t.hasResourceProperties("AWS::CloudWatch::DomainForOrganization", {
      Name: "agenticai-fleet",
      IdentityProviders: ["IDC"],
      Tags: Match.arrayWith([{ Key: "cost-centre", Value: "platform" }]),
    });
    t.hasResourceProperties("AWS::IAM::Role", {
      RoleName: "CloudWatchOmniDomainAccessRole",
    });
    const outputs = Object.keys(t.findOutputs("*"));
    expect(outputs.some((k) => k.startsWith("OmniDomainOmniDomainId"))).toBe(true);
    expect(outputs.some((k) => k.startsWith("OmniDomainOmniDomainUrl"))).toBe(true);
  });
});

describe("Phase 25 — Omni hub space (Audit account)", () => {
  it("emits the space, roles, keys, Transaction Search and the centralization rule in one stack", () => {
    const t = hubTemplate();
    t.resourceCountIs("AWS::CloudWatch::Space", 1);
    t.hasResourceProperties("AWS::CloudWatch::Space", {
      Name: `agenticai-fleet-${REGION}`,
      DomainId: "d-1a2b3c4d5e",
      EncryptionConfiguration: { EncryptionStrategy: "CUSTOMER_MANAGED", KmsKeyArn: Match.anyValue() },
    });
    t.resourceCountIs("AWS::XRay::TransactionSearchConfig", 1);
    // The console-created X-Ray resource policy, declared explicitly (live 403 without it).
    t.resourceCountIs("AWS::Logs::ResourcePolicy", 1);
    t.resourceCountIs("AWS::KMS::Key", 2);
    t.resourceCountIs("AWS::ObservabilityAdmin::OrganizationCentralizationRule", 1);
    const roles = Object.values(t.findResources("AWS::IAM::Role")).map(
      (r) => (r.Properties as any).RoleName as string | undefined,
    );
    expect(roles).toContain("CloudWatchOmniOperatorRole");
    expect(roles).toContain(`CloudWatchOmniDatasetIntegrationRole-${REGION}`);
    expect(roles).toContain(`AgentCoreEvaluationRole-omni-${REGION}`);
  });

  it("tags every hub key agenticai:omni-hub=true so the Audit execution policy can pin key administration to the hub's keys", () => {
    const keys = Object.values(hubTemplate().findResources("AWS::KMS::Key"));
    expect(keys).toHaveLength(2);
    for (const key of keys) {
      expect((key.Properties as any).Tags).toEqual(
        expect.arrayContaining([{ Key: "agenticai:omni-hub", Value: "true" }]),
      );
    }
  });

  it("puts the configured permissions boundary on every hub role but the operator, which keeps the Omni one", () => {
    const app = new App();
    const t = Template.fromStack(
      new OmniHubStack(app, "BoundedHub", {
        env: { account: HUB, region: REGION },
        domainId: "d-1a2b3c4d5e",
        keyRemovalPolicy: RemovalPolicy.DESTROY,
        centralization: {
          organizationId: "o-abc123def4",
          sourceScope: `AccountId IN ('${CELL}')`,
          sourceRegions: [REGION],
        },
        goldenGate: { runnerPrincipalArns: [`arn:aws:iam::${CELL}:role/AgenticAI-WorkloadPipeline-Evaluation`] },
        roleBoundaryPolicyName: "AgenticAI-OmniHubRoleBoundary-eu-west-1",
        tags: TAGS,
      }),
    );
    const roles = Object.values(t.findResources("AWS::IAM::Role")) as any[];
    expect(roles.length).toBeGreaterThanOrEqual(5);
    for (const role of roles) {
      const boundary = JSON.stringify(role.Properties.PermissionsBoundary ?? null);
      if (role.Properties.RoleName === "CloudWatchOmniOperatorRole") {
        expect(boundary).toContain("CloudWatchOmniSpaceAccessPolicy");
      } else {
        expect(boundary).toContain("AgenticAI-OmniHubRoleBoundary-eu-west-1");
      }
    }
    // Without the name only the operator role carries a boundary (unchanged default).
    const plain = Object.values(hubTemplate().findResources("AWS::IAM::Role")) as any[];
    expect(
      plain.filter((r) => r.Properties.PermissionsBoundary !== undefined).map((r) => r.Properties.RoleName),
    ).toEqual(["CloudWatchOmniOperatorRole"]);
  });

  it("points the centralization rule at this account and encrypts every destination group under the hub key", () => {
    const t = hubTemplate();
    const rule = Object.values(
      t.findResources("AWS::ObservabilityAdmin::OrganizationCentralizationRule"),
    )[0].Properties as any;
    expect(rule.Rule.Destination.Account).toBe(HUB);
    expect(rule.Rule.Destination.Region).toBe(REGION);
    expect(rule.Rule.Source.Regions).toEqual(["eu-west-1", "eu-west-3"]);
    expect(rule.Rule.Source.SourceContextGraphConfiguration).toBeUndefined();
    const enc = rule.Rule.Destination.DestinationLogsConfiguration.LogsEncryptionConfiguration;
    expect(enc.EncryptionStrategy).toBe("CUSTOMER_MANAGED");
    expect(enc.EncryptionScope).toBe("NEW_DESTINATION_LOG_GROUPS");
    // The key ARN is a reference to the hub's centralized-logs key, not a literal.
    expect(JSON.stringify(enc.KmsKeyArn)).toContain("Fn::GetAtt");
    // Only blueprint log groups are selected; never all.
    const selection: string = rule.Rule.Source.SourceLogsConfiguration.LogGroupSelectionCriteria;
    expect(selection).toContain("/agenticai/%");
    expect(selection).toContain("aws/spans");
    expect(selection).not.toContain("LogGroupName = '*'");
  });

  it("deploys the space alone when no centralization scope is given", () => {
    const t = hubTemplate(false);
    t.resourceCountIs("AWS::CloudWatch::Space", 1);
    t.resourceCountIs("AWS::ObservabilityAdmin::OrganizationCentralizationRule", 0);
  });

  it("adopts an existing space: only the centralized-logs key and the rule are deployed", () => {
    const app = new App();
    const stack = new OmniHubStack(app, "AdoptHub", {
      env: { account: HUB, region: REGION },
      domainId: "d-1a2b3c4d5e",
      existingSpaceId: "2e98b6d1-0ebb-4359-a608-12a1147dc089",
      keyRemovalPolicy: RemovalPolicy.DESTROY,
      centralization: {
        organizationId: "o-abc123def4",
        sourceScope: "AccountId IN ('333333333333','444444444444')",
        sourceRegions: ["eu-west-1"],
      },
      tags: TAGS,
    });
    const t = Template.fromStack(stack);
    expect(stack.hub).toBeUndefined();
    expect(stack.spaceId).toBe("2e98b6d1-0ebb-4359-a608-12a1147dc089");
    t.resourceCountIs("AWS::CloudWatch::Space", 0);
    t.resourceCountIs("AWS::XRay::TransactionSearchConfig", 0);
    t.resourceCountIs("AWS::KMS::Key", 1);
    t.hasResourceProperties("AWS::KMS::Key", {
      Tags: Match.arrayWith([{ Key: "cost-centre", Value: "platform" }, { Key: "LogsManaged", Value: "true" }]),
    });
    t.resourceCountIs("AWS::ObservabilityAdmin::OrganizationCentralizationRule", 1);
    // Only the tag-propagation role: no operator, Dataset or evaluation role is created around an adopted space.
    const roles = Object.values(t.findResources("AWS::IAM::Role"));
    expect(roles).toHaveLength(1);
    const rule = Object.values(
      t.findResources("AWS::ObservabilityAdmin::OrganizationCentralizationRule"),
    )[0].Properties as any;
    expect(rule.Rule.Destination.Account).toBe(HUB);
    expect(JSON.stringify(rule.Rule.Destination.DestinationLogsConfiguration.LogsEncryptionConfiguration.KmsKeyArn)).toContain(
      "Fn::GetAtt",
    );
    t.hasOutput("OmniSpaceId", { Value: "2e98b6d1-0ebb-4359-a608-12a1147dc089" });
  });

  it("refuses space-level settings next to an adopted space, and a malformed space id", () => {
    const base = {
      env: { account: HUB, region: REGION },
      domainId: "d-1a2b3c4d5e",
      existingSpaceId: "2e98b6d1-0ebb-4359-a608-12a1147dc089",
    };
    expect(() => new OmniHubStack(new App(), "A", { ...base, spaceName: "other" })).toThrow(/spaceName cannot be combined/);
    expect(() => new OmniHubStack(new App(), "B", { ...base, allowModelInference: true })).toThrow(/allowModelInference/);
    expect(() => new OmniHubStack(new App(), "C", { ...base, existingSpaceId: "not-a-uuid" })).toThrow(/must be a space id/);
  });

  it("creates the golden-gate runner role only when pipeline principals are named, in both modes, scoped to the named cells", () => {
    const pipelineRole = "arn:aws:iam::111111111111:role/AgenticAI-acme-EvaluationGateRole";
    const template = Template.fromStack(
      new OmniHubStack(new App(), "HubGate", {
        env: { account: HUB, region: REGION },
        domainId: "d-1a2b3c4d5e",
        keyRemovalPolicy: RemovalPolicy.DESTROY,
        goldenGate: { runnerPrincipalArns: [pipelineRole], sourceAccountIds: ["444444444444"] },
        tags: TAGS,
      }),
    );
    template.hasResourceProperties("AWS::IAM::Role", {
      RoleName: `AgenticAIGoldenGateRunner-${REGION}`,
      AssumeRolePolicyDocument: {
        Statement: [Match.objectLike({ Condition: { ArnEquals: { "aws:PrincipalArn": [pipelineRole] } } })],
      },
    });
    const text = JSON.stringify(template.toJSON());
    expect(text).toContain("log-group:/centralized/444444444444/*");
    expect(text).toContain("OmniGoldenGateRunnerRoleArn");
    // Adopt mode: the runner still exists, keyed to the stack's own centralized-logs key.
    const adoptTemplate = Template.fromStack(
      new OmniHubStack(new App(), "AdoptHubGate", {
        env: { account: HUB, region: REGION },
        domainId: "d-1a2b3c4d5e",
        existingSpaceId: "2e98b6d1-0ebb-4359-a608-12a1147dc089",
        goldenGate: { runnerPrincipalArns: [pipelineRole] },
      }),
    );
    adoptTemplate.resourceCountIs("AWS::IAM::Role", 1);
    expect(JSON.stringify(adoptTemplate.toJSON())).toContain("log-group:/centralized/*");
    // Default: no runner role.
    expect(JSON.stringify(hubTemplate().toJSON())).not.toContain("AgenticAIGoldenGateRunner");
  });

  it("omni-hub-keys: operators deploy both hub keys ahead of the hub, tagged for the audit execution policy and retained by default", () => {
    const t = Template.fromStack(
      new OmniHubKeysStack(new App(), "Keys", { env: { account: HUB, region: REGION }, tags: TAGS }),
    );
    t.resourceCountIs("AWS::KMS::Key", 2);
    t.resourceCountIs("AWS::KMS::Alias", 2);
    t.hasResourceProperties("AWS::KMS::Alias", { AliasName: `alias/agenticai/omni-space-${REGION}` });
    t.hasResourceProperties("AWS::KMS::Alias", { AliasName: `alias/agenticai/omni-centralized-logs-${REGION}` });
    t.resourceCountIs("AWS::IAM::Role", 0);
    const keys = Object.values(t.findResources("AWS::KMS::Key")) as any[];
    const tagged = (key: any, name: string): boolean =>
      (key.Properties.Tags as any[]).some((tag) => tag.Key === name && tag.Value === "true");
    for (const key of keys) {
      expect(key.Properties.EnableKeyRotation).toBe(true);
      expect(key.Properties.Tags).toEqual(
        expect.arrayContaining([{ Key: "agenticai:omni-hub", Value: "true" }, { Key: "cost-centre", Value: "platform" }]),
      );
      expect(key.DeletionPolicy).toBe("Retain");
      expect(key.UpdateReplacePolicy).toBe("Retain");
    }
    expect(keys.filter((key) => tagged(key, "cw-omni"))).toHaveLength(1);
    expect(keys.filter((key) => tagged(key, "LogsManaged"))).toHaveLength(1);
    // The space key policy keeps the documented service statements but names no
    // hub role (none exists yet); the root statement delegates the rest to IAM.
    const spaceKeyPolicy = JSON.stringify(keys.find((key) => tagged(key, "cw-omni")).Properties.KeyPolicy);
    expect(spaceKeyPolicy).toContain("AllowCloudWatchSpaceEncryptionService");
    expect(spaceKeyPolicy).toContain("EnableRootKeyAdministration");
    expect(spaceKeyPolicy).not.toContain("AllowOperatorRoleDecrypt");
    expect(spaceKeyPolicy).not.toContain("AllowCallerDatasetForwarding");
    const outputs = Object.keys(t.findOutputs("*"));
    expect(outputs).toContain("OmniSpaceKeyArn");
    expect(outputs).toContain("OmniCentralizedLogsKeyArn");
  });

  it("imports operator-provisioned keys: creates none, points the space and the rule at the given ARNs, moves the Dataset key grant onto the role", () => {
    const t = Template.fromStack(
      new OmniHubStack(new App(), "ImportHub", {
        env: { account: HUB, region: REGION },
        domainId: "d-1a2b3c4d5e",
        centralization: { organizationId: "o-abc123def4", sourceScope: `AccountId IN ('${CELL}')`, sourceRegions: [REGION] },
        existingKeyArns: { space: SPACE_KEY_ARN, centralizedLogs: LOGS_KEY_ARN },
        tags: TAGS,
      }),
    );
    t.resourceCountIs("AWS::KMS::Key", 0);
    t.resourceCountIs("AWS::KMS::Alias", 0);
    t.resourceCountIs("AWS::CloudWatch::Space", 1);
    t.hasResourceProperties("AWS::CloudWatch::Space", {
      EncryptionConfiguration: { EncryptionStrategy: "CUSTOMER_MANAGED", KmsKeyArn: SPACE_KEY_ARN },
    });
    const rule = Object.values(
      t.findResources("AWS::ObservabilityAdmin::OrganizationCentralizationRule"),
    )[0].Properties as any;
    expect(rule.Rule.Destination.DestinationLogsConfiguration.LogsEncryptionConfiguration.KmsKeyArn).toBe(LOGS_KEY_ARN);
    const roles = Object.values(t.findResources("AWS::IAM::Role")).map((r) => r.Properties as any);
    const dataset = roles.find((r) => r.RoleName === `CloudWatchOmniDatasetIntegrationRole-${REGION}`);
    const grant = (dataset.Policies[0].PolicyDocument.Statement as any[]).find(
      (s) => s.Sid === "UseSpaceKeyForDatasetForwarding",
    );
    expect(grant.Action).toEqual(["kms:GenerateDataKey", "kms:Encrypt", "kms:Decrypt"]);
    expect(grant.Resource).toBe(SPACE_KEY_ARN);
    expect(Object.keys(grant.Condition)).toEqual(["StringEquals"]);
    expect(grant.Condition.StringEquals["kms:ViaService"]).toBe(`cloudwatch.${REGION}.amazonaws.com`);
    // The dataset ARN is partition-aware (Fn::Join on AWS::Partition), so compare its rendered text.
    expect(JSON.stringify(grant.Condition.StringEquals["kms:EncryptionContext:aws:cloudwatch:arn"])).toContain(
      `:cloudwatch:${REGION}:${HUB}:dataset/default`,
    );
    // The operator role relies on its AWS-managed policy (cw-omni tag), no inline policy.
    const operator = roles.find((r) => r.RoleName === "CloudWatchOmniOperatorRole");
    expect(operator.Policies).toBeUndefined();
    expect(Object.values(t.findOutputs("*")).some((o) => o.Value === LOGS_KEY_ARN)).toBe(true);
    // Default mode keeps the grant in the key policy, not on the role.
    const plainDataset = (Object.values(hubTemplate().findResources("AWS::IAM::Role")) as any[])
      .map((r) => r.Properties)
      .find((r) => r.RoleName === `CloudWatchOmniDatasetIntegrationRole-${REGION}`);
    expect(JSON.stringify(plainDataset.Policies)).not.toContain("UseSpaceKeyForDatasetForwarding");
  });

  it("adopt mode imports only the centralized-logs key, and refuses incomplete or malformed key ARNs", () => {
    const adopt = Template.fromStack(
      new OmniHubStack(new App(), "AdoptImportHub", {
        env: { account: HUB, region: REGION },
        domainId: "d-1a2b3c4d5e",
        existingSpaceId: "2e98b6d1-0ebb-4359-a608-12a1147dc089",
        centralization: { organizationId: "o-abc123def4", sourceScope: `AccountId IN ('${CELL}')`, sourceRegions: [REGION] },
        existingKeyArns: { centralizedLogs: LOGS_KEY_ARN },
      }),
    );
    adopt.resourceCountIs("AWS::KMS::Key", 0);
    adopt.resourceCountIs("AWS::IAM::Role", 1);
    const rule = Object.values(
      adopt.findResources("AWS::ObservabilityAdmin::OrganizationCentralizationRule"),
    )[0].Properties as any;
    expect(rule.Rule.Destination.DestinationLogsConfiguration.LogsEncryptionConfiguration.KmsKeyArn).toBe(LOGS_KEY_ARN);

    const base = { env: { account: HUB, region: REGION }, domainId: "d-1a2b3c4d5e" };
    expect(
      () => new OmniHubStack(new App(), "A", { ...base, existingKeyArns: { centralizedLogs: LOGS_KEY_ARN } }),
    ).toThrow(/needs both space and centralizedLogs/);
    expect(
      () =>
        new OmniHubStack(new App(), "B", {
          ...base,
          existingSpaceId: "2e98b6d1-0ebb-4359-a608-12a1147dc089",
          existingKeyArns: { space: SPACE_KEY_ARN, centralizedLogs: LOGS_KEY_ARN },
        }),
    ).toThrow(/cannot be combined with existingSpaceId/);
    expect(
      () => new OmniHubStack(new App(), "C", { ...base, existingKeyArns: { space: "alias/agenticai/omni-space", centralizedLogs: LOGS_KEY_ARN } }),
    ).toThrow(/must be a KMS key ARN/);
  });

  it("keeps judge inference off by default (ADR-16)", () => {
    const t = hubTemplate();
    const roles = Object.values(t.findResources("AWS::IAM::Role")).map((r) => r.Properties as any);
    const operator = roles.find((r) => r.RoleName === "CloudWatchOmniOperatorRole");
    expect(JSON.stringify(operator.ManagedPolicyArns)).not.toContain("CloudWatchOmniModelInferencePolicy");
    const policies = JSON.stringify(roles.map((r) => r.Policies ?? []));
    expect(policies).not.toContain("bedrock:InvokeModel");
  });
});

describe("Phase 25 — Workstream cell: Transaction Search stays opt-in (ADR-13)", () => {
  it("emits no Transaction Search config by default", () => {
    observabilityTemplate().resourceCountIs("AWS::XRay::TransactionSearchConfig", 0);
    observabilityTemplate().resourceCountIs("AWS::Logs::ResourcePolicy", 0);
    observabilityTemplate().resourceCountIs("AWS::Oam::Link", 1);
  });

  it("enables Transaction Search declaratively when the cell opts in", () => {
    const t = observabilityTemplate(true);
    t.resourceCountIs("AWS::XRay::TransactionSearchConfig", 1);
    t.hasResourceProperties("AWS::XRay::TransactionSearchConfig", { IndexingPercentage: 1 });
    t.resourceCountIs("AWS::Logs::ResourcePolicy", 1);
    t.resourceCountIs("AWS::Oam::Link", 1);
  });
});

describe("Phase 25 — Runtime span content contract (ADR-18)", () => {
  function runtimeEnv(t: Template): Record<string, unknown> {
    const runtime = Object.values(t.findResources("AWS::BedrockAgentCore::Runtime"))[0];
    return (runtime.Properties as any).EnvironmentVariables as Record<string, unknown>;
  }

  it("sets neither content nor redaction variables by default", () => {
    const env = runtimeEnv(runtimeTemplate());
    expect(env.AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT).toBeUndefined();
    expect(env.AWS_REDACT_SPAN_ATTRIBUTES).toBeUndefined();
    // Managed OTLP defaults are never duplicated by the blueprint.
    expect(env.OTEL_TRACES_EXPORTER).toBeUndefined();
    expect(env.AGENT_OBSERVABILITY_ENABLED).toBeUndefined();
  });

  it("keeps prompt content and redacts declared attributes when the cell opts in", () => {
    const env = runtimeEnv(
      runtimeTemplate({
        keepPromptContentInSpans: true,
        redactSpanAttributes: ["gen_ai.input.messages", "llm.input_messages.*"],
      }),
    );
    expect(env.AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT).toBe("true");
    expect(env.AWS_REDACT_SPAN_ATTRIBUTES).toBe("gen_ai.input.messages,llm.input_messages.*");
  });

  it("rejects a malformed redaction pattern", () => {
    expect(() => runtimeTemplate({ redactSpanAttributes: ["bad attr;rm"] })).toThrow(/not a valid span attribute/);
  });
});
