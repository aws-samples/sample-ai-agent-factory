/**
 * Phase 10 conformance — D-03 v3 per-workstream AgentCore Gateway stack.
 *
 * Pins the shape of `D03WorkstreamGatewayStack` against the three-layer
 * tool-governance model (see README §3.3 v3):
 *   - Layer 1 (synth-time): unknown `allowedToolIds` fail the CDK synth.
 *   - Layer 3 (runtime): Gateway service role inline policy lists exactly
 *     the resolved N tool ARNs — no wildcards, no extras.
 *
 * Also pins:
 *   - Exactly one `Custom::BedrockAgentCoreGateway` resource per stack.
 *   - Exactly N `Custom::BedrockAgentCoreGatewayTarget` resources.
 *   - Each target's `lambdaArn` matches the catalogue's resolved ARN.
 *   - CfnOutputs surface `GatewayId` / `GatewayArn` / one per tool.
 *   - Stack tags include `tenant-id` and `agent-id`.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { createHash, createHmac } from "node:crypto";
import { EventEmitter } from "node:events";
import { runInNewContext } from "node:vm";

import { App } from "aws-cdk-lib";
import type { GaRegistryConsumerContext } from "@agenticai/agent-registry";
import { Template } from "aws-cdk-lib/assertions";

import {
  PLATFORM_TOOL_CATALOGUE,
  resolveTargetArn,
  type ToolId,
} from "@agenticai/platform-tool-catalogue";

import { D03WorkstreamGatewayStack } from "../../apps/platform-account/lib/d03-workstream-gateway-stack";

const PLATFORM_ACCOUNT_ID = "222222222222";
const WORKLOAD_ACCOUNT_ID = "333333333333";

interface SynthOpts {
  readonly allowedToolIds?: readonly ToolId[];
  readonly tenantId?: string;
  readonly agentId?: string;
  readonly cognitoDiscoveryUrl?: string;
  readonly cognitoAudience?: readonly string[];
}

function synth(opts: SynthOpts = {}): {
  template: Template;
  stack: D03WorkstreamGatewayStack;
} {
  const app = new App();
  const allowedToolIds = opts.allowedToolIds ?? ["tool-echo", "tool-ping"];
  const tenantId = opts.tenantId ?? "acme";
  const agentId = opts.agentId ?? "primary";
  const stack = new D03WorkstreamGatewayStack(
    app,
    `AgenticAI-D03-WorkstreamGateway-${tenantId}-${agentId}`,
    {
      env: { account: WORKLOAD_ACCOUNT_ID, region: "us-east-1" },
      tenantId,
      agentId,
      envName: "nonprod",
      workloadAccountId: WORKLOAD_ACCOUNT_ID,
      platformAccountId: PLATFORM_ACCOUNT_ID,
      applicationId: "demo-app",
      costCentre: "engineering",
      allowedToolIds,
      cognitoDiscoveryUrl: opts.cognitoDiscoveryUrl,
      cognitoAudience: opts.cognitoAudience,
    },
  );
  return { template: Template.fromStack(stack), stack };
}

describe("Phase 10 — D03WorkstreamGatewayStack shape", () => {
  it("emits exactly one Custom::BedrockAgentCoreGateway resource", () => {
    const { template } = synth();
    const gws = template.findResources("Custom::BedrockAgentCoreGateway");
    expect(Object.keys(gws)).toHaveLength(1);
  });

  it("emits exactly N gateway-target resources where N = allowedToolIds.length", () => {
    const ids: ToolId[] = ["tool-echo", "tool-ping"];
    const { template } = synth({ allowedToolIds: ids });
    const targets = template.findResources(
      "Custom::BedrockAgentCoreGatewayTarget",
    );
    expect(Object.keys(targets)).toHaveLength(ids.length);
  });

  it("single-tool subscription emits exactly one target", () => {
    const { template } = synth({ allowedToolIds: ["tool-echo"] });
    const targets = template.findResources(
      "Custom::BedrockAgentCoreGatewayTarget",
    );
    expect(Object.keys(targets)).toHaveLength(1);
  });
});

describe("Phase 10 — layer-3 enforcement (Gateway service role inline policy)", () => {
  it("inline policy lists exactly the N resolved tool ARNs — no wildcards, no extras", () => {
    const ids: ToolId[] = ["tool-echo", "tool-ping"];
    const { template } = synth({ allowedToolIds: ids });
    // The service role has a single inline policy `InvokeSubscribedTools`.
    const roles = template.findResources("AWS::IAM::Role", {
      Properties: {
        AssumeRolePolicyDocument: {
          Statement: [
            {
              Principal: { Service: "bedrock-agentcore.amazonaws.com" },
            },
          ],
        },
      },
    });
    // There is exactly one role trusted by bedrock-agentcore.
    const gwRole = Object.values(roles);
    expect(gwRole).toHaveLength(1);
    const policies = ((gwRole[0] as any).Properties.Policies ?? []) as Array<{
      PolicyName: string;
      PolicyDocument: {
        Statement: Array<{
          Effect: string;
          Action: unknown;
          Resource: unknown;
        }>;
      };
    }>;
    expect(policies).toHaveLength(1);
    const stmts = policies[0].PolicyDocument.Statement;
    expect(stmts).toHaveLength(1);
    const stmt = stmts[0];
    expect(stmt.Effect).toBe("Allow");
    expect(stmt.Action).toBe("lambda:InvokeFunction");
    const resources = Array.isArray(stmt.Resource)
      ? stmt.Resource
      : [stmt.Resource];
    // Exact ARNs — must match what the catalogue resolves.
    const expected = ids.map((id) =>
      resolveTargetArn(PLATFORM_TOOL_CATALOGUE[id], PLATFORM_ACCOUNT_ID),
    );
    expect(resources).toHaveLength(expected.length);
    for (const arn of expected) {
      expect(resources).toContain(arn);
    }
    // No wildcards anywhere in the resource list.
    for (const r of resources) {
      expect(typeof r).toBe("string");
      expect(r as string).not.toContain("*");
    }
  });
});

describe("Phase 10 — each target's lambdaArn matches the catalogue's resolved ARN", () => {
  it("per-tool GatewayTarget carries the expected lambdaArn in its CreateGatewayTarget params", () => {
    const ids: ToolId[] = ["tool-echo", "tool-ping"];
    const { template } = synth({ allowedToolIds: ids });
    const targets = template.findResources(
      "Custom::BedrockAgentCoreGatewayTarget",
    );
    // The custom-resource `Create` property is a JSON-in-JSON payload the
    // CDK serialises via Fn::Join — search the raw JSON.stringify output.
    // Inner quotes are therefore double-escaped (`\\\"`).
    const rendered = JSON.stringify(targets);
    for (const id of ids) {
      const expected = resolveTargetArn(
        PLATFORM_TOOL_CATALOGUE[id],
        PLATFORM_ACCOUNT_ID,
      );
      expect(rendered).toContain(expected);
      // And the tool id must appear as the inlinePayload.name (the inner
      // JSON renders `"name":"<id>"` which, after one level of outer
      // JSON.stringify escaping, becomes `\\\"name\\\":\\\"<id>\\\"`).
      expect(rendered).toContain(`\\"name\\":\\"${id}\\"`);
    }
  });
});

describe("Phase 10 — synth-time SSOT gate (layer 1)", () => {
  it("throws when allowedToolIds contains an id not in the catalogue", () => {
    expect(() => synth({ allowedToolIds: ["not-a-real-tool"] })).toThrow(
      /Unknown tool id\(s\)/,
    );
  });

  it("error message lists the known catalogue keys for the operator", () => {
    try {
      synth({ allowedToolIds: ["bogus-tool-x"] });
      fail("expected throw");
    } catch (err) {
      const msg = (err as Error).message;
      expect(msg).toContain("bogus-tool-x");
      expect(msg).toContain("tool-echo");
    }
  });
});

describe("Phase 10 — tags + outputs surface", () => {
  it("stack tags include tenant-id and agent-id", () => {
    const { stack } = synth({ tenantId: "retail", agentId: "triage" });
    const tags = stack.tags.tagValues();
    expect(tags["application-id"]).toBe("demo-app");
    expect(tags["tenant-id"]).toBe("retail");
    expect(tags["agent-id"]).toBe("triage");
    expect(tags["cost-centre"]).toBe("engineering");
    expect(tags["deviation"]).toBe("D-03");
    expect(tags["environment"]).toBe("nonprod");
  });

  it("CfnOutputs include GatewayId + GatewayArn", () => {
    const { template } = synth();
    const outputs = template.findOutputs("*");
    const names = Object.keys(outputs);
    expect(names).toContain("GatewayId");
    expect(names).toContain("GatewayArn");
    expect(names).toContain("GatewayServiceRoleArn");
    expect(names).toContain("SubscribedToolCount");
    expect(names).toContain("PerTenantCedarPolicy");
  });

  it("emits one ToolTarget-<toolId> output per subscribed tool", () => {
    const ids: ToolId[] = ["tool-echo", "tool-ping"];
    const { template } = synth({ allowedToolIds: ids });
    const outputs = template.findOutputs("*");
    for (const id of ids) {
      const key = `ToolTarget${id.replace(/-/g, "")}`;
      // CDK strips non-alphanumeric from the logical id; just look for any
      // output whose value contains the resolved ARN.
      const expected = resolveTargetArn(
        PLATFORM_TOOL_CATALOGUE[id],
        PLATFORM_ACCOUNT_ID,
      );
      const match = Object.values(outputs).find(
        (o) => JSON.stringify((o as any).Value) === JSON.stringify(expected),
      );
      expect(match).toBeDefined();
      // Sanity: the key is derived from the tool id.
      expect(key.toLowerCase()).toContain(id.replace(/-/g, "").toLowerCase());
    }
  });
});

const GA_REGISTRY_ID = "ABCDEFGHIJKLMNOP";
const GA_REGISTRY_ARN = `arn:aws:agent-registry:us-west-2:${PLATFORM_ACCOUNT_ID}:registry/${GA_REGISTRY_ID}`;
const GA_RECORD_IDS: Record<string, string> = {
  "tool-echo": "ABCDEFGHIJKL",
  "tool-ping": "MNOPQRSTUVWX",
};

function gaRegistryContext(
  toolIds: readonly ToolId[] = ["tool-echo", "tool-ping"],
): GaRegistryConsumerContext {
  return {
    schemaVersion: "agenticai.ga-registry-consumer-context/1.0",
    environment: "nonprod",
    region: "us-west-2",
    platformAccountId: PLATFORM_ACCOUNT_ID,
    sourceRevision: "a".repeat(40),
    registryId: GA_REGISTRY_ID,
    registryArn: GA_REGISTRY_ARN,
    readerRoleArn: `arn:aws:iam::${PLATFORM_ACCOUNT_ID}:role/AgenticAI-RegistryReader-nonprod`,
    readerExternalId: `agenticai-registry-v1-nonprod-${PLATFORM_ACCOUNT_ID}`,
    records: [...toolIds].sort().map((toolId, index) => {
      const source = PLATFORM_TOOL_CATALOGUE[toolId];
      const recordId = GA_RECORD_IDS[toolId];
      return {
        recordId,
        recordArn: `${GA_REGISTRY_ARN}/record/${recordId}`,
        descriptorSha256: String(index + 1).repeat(64),
        document: {
          schemaVersion: "agenticai.tool-governance/1.0",
          catalogueVersion: "2",
          toolId,
          description: source.description,
          desiredApprovalStatus: "approved",
          target: {
            type: source.toolType ?? "lambda",
            arn:
              `arn:aws:lambda:us-west-2:${PLATFORM_ACCOUNT_ID}:function:` +
              `agenticai-platform-nonprod-${toolId}:PROD`,
          },
          mcp: {
            toolName: toolId,
            description: source.description,
            inputSchema: source.inputSchema ?? { type: "object" },
          },
          authorization: {
            defaultDecision: "DENY",
            cedarPolicy: source.cedarPolicy,
            allowedSubjects: [],
            allowedGroups: source.allowedGroups ?? [],
            combination:
              source.allowedGroups && source.allowedGroups.length > 0
                ? "GROUP_ONLY"
                : "AUTHENTICATED",
          },
          ownership: {
            ownerTeam: source.ownerTeam,
            costCentre: source.costCentre,
          },
        },
      };
    }),
  };
}

describe("Phase 10 — R2 GA Registry subscription path", () => {
  function synthRegistry(
    opts: {
      readonly context?: GaRegistryConsumerContext;
      readonly tenantId?: string;
    } = {},
  ): { template: Template; stack: D03WorkstreamGatewayStack } {
    const app = new App();
    const tenantId = opts.tenantId ?? "acme";
    const stack = new D03WorkstreamGatewayStack(
      app,
      `AgenticAI-D03-WorkstreamGateway-${tenantId}-registry`,
      {
        env: { account: WORKLOAD_ACCOUNT_ID, region: "us-west-2" },
        tenantId,
        agentId: "primary",
        envName: "nonprod",
        workloadAccountId: WORKLOAD_ACCOUNT_ID,
        platformAccountId: PLATFORM_ACCOUNT_ID,
        applicationId: "demo-app",
        costCentre: "engineering",
        gaRegistryContext: opts.context ?? gaRegistryContext(),
      },
    );
    return { template: Template.fromStack(stack), stack };
  }

  async function executeValidator(
    expectedDigest?: string,
    expectedTargetArn?: string,
  ) {
    const context = gaRegistryContext(["tool-echo"]);
    const resolved = context.records[0];
    const descriptor = JSON.stringify(resolved.document);
    const digest = createHash("sha256")
      .update(descriptor, "utf8")
      .digest("hex");
    const template = synthRegistry({ context }).template;
    const functions = template.findResources("AWS::Lambda::Function");
    const validator = Object.values(functions).find(
      (resource: any) =>
        typeof resource.Properties?.FunctionName === "string" &&
        resource.Properties.FunctionName.includes("reg-validator"),
    ) as any;
    const code = validator.Properties.Code.ZipFile as string;
    const responses = [
      {
        status: 200,
        body:
          "<AssumeRoleResponse><Credentials><AccessKeyId>test-access</AccessKeyId>" +
          "<SecretAccessKey>test-secret</SecretAccessKey>" +
          "<SessionToken>test-session</SessionToken></Credentials></AssumeRoleResponse>",
      },
      {
        status: 200,
        body: JSON.stringify({
          recordId: resolved.recordId,
          name: resolved.document.toolId,
          status: "APPROVED",
          recordType: "CUSTOM",
          recordVersion: `${resolved.document.catalogueVersion}.0.0`,
          descriptors: { custom: { data: descriptor } },
        }),
      },
    ];
    const requests: any[] = [];
    const fakeHttps = {
      request: (options: any, callback: (response: any) => void) => {
        requests.push(options);
        const request = new EventEmitter() as any;
        request.write = () => undefined;
        request.end = () => {
          const next = responses.shift();
          if (!next) throw new Error("unexpected HTTPS request");
          const response = new EventEmitter() as any;
          response.statusCode = next.status;
          callback(response);
          response.emit("data", Buffer.from(next.body, "utf8"));
          response.emit("end");
        };
        return request;
      },
    };
    const exported: Record<string, any> = {};
    runInNewContext(code, {
      exports: exported,
      module: { exports: exported },
      require: (name: string) => {
        if (name === "https") return fakeHttps;
        if (name === "crypto") return { createHash, createHmac };
        throw new Error(`unexpected require: ${name}`);
      },
      process: {
        env: {
          AWS_REGION: "us-west-2",
          AWS_ACCESS_KEY_ID: "test-access",
          AWS_SECRET_ACCESS_KEY: "test-secret",
          AWS_SESSION_TOKEN: "test-session",
          REGISTRY_READER_ROLE_ARN: context.readerRoleArn,
          REGISTRY_READER_EXTERNAL_ID: context.readerExternalId,
          REGISTRY_READER_SESSION_NAME: "registry-nonprod-validator",
          GATEWAY_AUTHORIZER_MODE: "AWS_IAM",
        },
      },
      URLSearchParams,
      Buffer,
      Date,
    });
    const result = await exported.handler({
      RequestType: "Create",
      ResourceProperties: {
        registryId: context.registryId,
        recordId: resolved.recordId,
        expectedToolId: resolved.document.toolId,
        expectedTargetArn: expectedTargetArn ?? resolved.document.target.arn,
        expectedDescriptorSha256: expectedDigest ?? digest,
        validationRevision: context.sourceRevision,
        tenantId: "acme",
        agentId: "primary",
      },
    });
    return { result, requests, digest };
  }

  it("emits one digest validator and one Gateway target per GA record", () => {
    const { template } = synthRegistry();
    const validators = template.findResources(
      "Custom::AgenticAIRegistryRecordValidator",
    );
    const targets = template.findResources(
      "Custom::BedrockAgentCoreGatewayTarget",
    );
    expect(Object.keys(validators)).toHaveLength(2);
    expect(Object.keys(targets)).toHaveLength(2);
    const renderedValidators = JSON.stringify(validators);
    expect(renderedValidators).toContain("expectedDescriptorSha256");
    expect(renderedValidators).toContain("expectedTargetArn");
    expect(renderedValidators).toContain("validationRevision");
    expect(renderedValidators).toContain("expectedToolId");
    expect(renderedValidators).not.toContain("changeNonce");
  });

  it("creates the exact pipeline-owned validator role with one AssumeRole target", () => {
    const { template } = synthRegistry();
    const roles = template.findResources("AWS::IAM::Role");
    const validatorRole = Object.values(roles).find(
      (role: any) =>
        role.Properties?.RoleName ===
        "AgenticAI-D03-nonprod-acme-primary-RegistryValidator",
    ) as any;
    expect(validatorRole).toBeDefined();
    expect(
      validatorRole.Properties.AssumeRolePolicyDocument.Statement,
    ).toContainEqual(
      expect.objectContaining({
        Principal: { Service: "lambda.amazonaws.com" },
      }),
    );
    expect(validatorRole.Properties.Policies).toEqual([
      {
        PolicyName: "AssumeRegistryReader",
        PolicyDocument: {
          Version: "2012-10-17",
          Statement: [
            expect.objectContaining({
              Action: "sts:AssumeRole",
              Resource: `arn:aws:iam::${PLATFORM_ACCOUNT_ID}:role/AgenticAI-RegistryReader-nonprod`,
            }),
          ],
        },
      },
    ]);
  });

  it("pins the GA endpoint, signing name, schema, and reader session pattern", () => {
    const { template } = synthRegistry();
    const functions = template.findResources("AWS::Lambda::Function");
    const validator = Object.values(functions).find(
      (resource: any) =>
        typeof resource.Properties?.FunctionName === "string" &&
        resource.Properties.FunctionName.includes("reg-validator"),
    ) as any;
    expect(validator).toBeDefined();
    const code = validator.Properties.Code.ZipFile as string;
    expect(code).toContain("const service = 'agent-registry'");
    expect(code).toContain("'agent-registry-control.' + region");
    expect(code).toContain("agenticai.tool-governance/1.0");
    expect(code).toContain("descriptor digest changed after pipeline synth");
    expect(code).not.toContain("bedrock-agentcore-control.");
    expect(
      validator.Properties.Environment.Variables.REGISTRY_READER_SESSION_NAME,
    ).toBe("registry-nonprod-validator");
  });

  it("executes the synthesized validator through STS and GA Registry SigV4", async () => {
    const { result, requests, digest } = await executeValidator();
    expect(result.Data).toMatchObject({
      toolId: "tool-echo",
      descriptorSha256: digest,
      status: "APPROVED",
    });
    expect(requests).toHaveLength(2);
    expect(requests[0].host).toBe("sts.amazonaws.com");
    expect(requests[1].host).toBe(
      "agent-registry-control.us-west-2.amazonaws.com",
    );
    expect(requests[1].headers.Authorization).toContain(
      "/agent-registry/aws4_request",
    );
  });

  it("rejects a descriptor changed after pipeline synth", async () => {
    await expect(executeValidator("0".repeat(64))).rejects.toThrow(
      /descriptor digest changed after pipeline synth/,
    );
  });

  it("rejects a live target ARN different from the wired Gateway target", async () => {
    await expect(
      executeValidator(
        undefined,
        `arn:aws:lambda:us-west-2:${PLATFORM_ACCOUNT_ID}:function:rogue:PROD`,
      ),
    ).rejects.toThrow(/target ARN differs from the Gateway target/);
  });

  it("uses tool IDs and full Registry schemas instead of opaque record IDs", () => {
    const { template } = synthRegistry();
    const targets = template.findResources(
      "Custom::BedrockAgentCoreGatewayTarget",
    );
    const rendered = JSON.stringify(targets);
    const context = gaRegistryContext();
    for (const toolId of ["tool-echo", "tool-ping"] as ToolId[]) {
      const expected = context.records.find(
        (record) => record.document.toolId === toolId,
      )!.document.target.arn;
      expect(rendered).toContain(expected);
      expect(rendered).toContain(`\\"name\\":\\"${toolId}\\"`);
      expect(rendered).not.toContain(GA_RECORD_IDS[toolId]);
    }
  });

  it("is deterministic for an unchanged source revision", () => {
    const left = synthRegistry().template.toJSON();
    const right = synthRegistry().template.toJSON();
    expect(right).toEqual(left);
  });

  it("preserves exact Gateway service-role Lambda resources", () => {
    const { template } = synthRegistry();
    const roles = template.findResources("AWS::IAM::Role");
    const gatewayRole = Object.values(roles).find((role: any) =>
      JSON.stringify(role.Properties.AssumeRolePolicyDocument).includes(
        "bedrock-agentcore.amazonaws.com",
      ),
    ) as any;
    const statement =
      gatewayRole.Properties.Policies[0].PolicyDocument.Statement[0];
    expect(statement.Action).toBe("lambda:InvokeFunction");
    expect(statement.Resource).toEqual(
      gaRegistryContext().records.map((record) => record.document.target.arn),
    );
  });

  it("rejects environment/account drift, conflicts, and empty mode", () => {
    const wrongEnvironment = gaRegistryContext();
    (wrongEnvironment as any).environment = "prod";
    expect(() => synthRegistry({ context: wrongEnvironment })).toThrow(
      /environment/,
    );

    const app = new App();
    expect(
      () =>
        new D03WorkstreamGatewayStack(app, "Conflict", {
          env: { account: WORKLOAD_ACCOUNT_ID, region: "us-east-1" },
          tenantId: "a",
          agentId: "b",
          envName: "nonprod",
          workloadAccountId: WORKLOAD_ACCOUNT_ID,
          platformAccountId: PLATFORM_ACCOUNT_ID,
          applicationId: "demo",
          costCentre: "engineering",
          allowedToolIds: ["tool-echo"],
          gaRegistryContext: gaRegistryContext(["tool-echo"]),
        }),
    ).toThrow(/mutually exclusive/);

    expect(
      () =>
        new D03WorkstreamGatewayStack(new App(), "Empty", {
          env: { account: WORKLOAD_ACCOUNT_ID, region: "us-east-1" },
          tenantId: "a",
          agentId: "b",
          envName: "nonprod",
          workloadAccountId: WORKLOAD_ACCOUNT_ID,
          platformAccountId: PLATFORM_ACCOUNT_ID,
          applicationId: "demo",
          costCentre: "engineering",
        }),
    ).toThrow(/either 'allowedToolIds'.*or 'gaRegistryContext'/);
  });
});

describe("Phase 10 — authorizer mode selection", () => {
  it("falls back to AWS_IAM when no Cognito discoveryUrl is supplied", () => {
    const { template } = synth({ cognitoDiscoveryUrl: undefined });
    const gws = template.findResources("Custom::BedrockAgentCoreGateway");
    // The Create prop is JSON-in-JSON so inner quotes are double-escaped.
    const rendered = JSON.stringify(gws);
    expect(rendered).toContain('\\"authorizerType\\":\\"AWS_IAM\\"');
    expect(rendered).not.toContain("customJWTAuthorizer");
  });

  it("switches to CUSTOM_JWT when cognitoDiscoveryUrl is supplied", () => {
    const { template } = synth({
      cognitoDiscoveryUrl:
        "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc/.well-known/openid-configuration",
      cognitoAudience: ["aud-xyz"],
    });
    const gws = template.findResources("Custom::BedrockAgentCoreGateway");
    const rendered = JSON.stringify(gws);
    expect(rendered).toContain('\\"authorizerType\\":\\"CUSTOM_JWT\\"');
    expect(rendered).toContain("customJWTAuthorizer");
    expect(rendered).toContain('\\"allowedAudience\\":[\\"aud-xyz\\"]');
  });
});
