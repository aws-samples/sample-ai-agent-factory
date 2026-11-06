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
    t.resourceCountIs("AWS::KMS::Key", 2);
    t.resourceCountIs("AWS::ObservabilityAdmin::OrganizationCentralizationRule", 1);
    const roles = Object.values(t.findResources("AWS::IAM::Role")).map(
      (r) => (r.Properties as any).RoleName as string | undefined,
    );
    expect(roles).toContain("CloudWatchOmniOperatorRole");
    expect(roles).toContain(`CloudWatchOmniDatasetIntegrationRole-${REGION}`);
    expect(roles).toContain(`AgentCoreEvaluationRole-omni-${REGION}`);
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
    observabilityTemplate().resourceCountIs("AWS::Oam::Link", 1);
  });

  it("enables Transaction Search declaratively when the cell opts in", () => {
    const t = observabilityTemplate(true);
    t.resourceCountIs("AWS::XRay::TransactionSearchConfig", 1);
    t.hasResourceProperties("AWS::XRay::TransactionSearchConfig", { IndexingPercentage: 1 });
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
