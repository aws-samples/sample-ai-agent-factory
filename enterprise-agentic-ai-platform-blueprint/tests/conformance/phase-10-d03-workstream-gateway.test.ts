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

  it("retains the service-minted Gateway ID for update and rollback", () => {
    const { template } = synth();
    const resource = Object.values(
      template.findResources("Custom::BedrockAgentCoreGateway"),
    )[0] as any;
    const rendered = JSON.stringify(resource);
    expect(rendered).toContain(
      '\\"physicalResourceId\\":{\\"responsePath\\":\\"gatewayId\\"}',
    );
    expect(rendered.match(/PHYSICAL:RESOURCEID:/g)).toHaveLength(2);
    expect(rendered).toContain(
      '\\"ignoreErrorCodesMatching\\":\\"ResourceNotFoundException\\"',
    );
    expect(rendered).not.toContain("ValidationException");
    expect(rendered).not.toContain("AgenticAI-D03-Gateway-");
  });

  it("waits for target deletion convergence before deleting the Gateway", () => {
    const { template } = synth({ allowedToolIds: ["tool-echo", "tool-ping"] });
    const resources = template.toJSON().Resources as Record<string, any>;
    const [gatewayId] = Object.entries(resources).find(
      ([, resource]) => resource.Type === "Custom::BedrockAgentCoreGateway",
    )!;
    const [barrierId, barrier] = Object.entries(resources).find(
      ([, resource]) =>
        resource.Type === "AWS::CloudFormation::CustomResource" &&
        resource.Properties?.GatewayIdentifier,
    )!;
    expect(barrier.DependsOn).toContain(gatewayId);
    const targets = Object.values(resources).filter(
      (resource) => resource.Type === "Custom::BedrockAgentCoreGatewayTarget",
    ) as any[];
    expect(targets).toHaveLength(2);
    for (const target of targets) {
      expect(target.DependsOn).toContain(barrierId);
    }
    const waiter = Object.values(resources).find(
      (resource: any) =>
        resource.Type === "AWS::Lambda::Function" &&
        resource.Properties?.Description ===
          "Waits until AgentCore reports no targets before Gateway deletion.",
    ) as any;
    expect(waiter.Properties.Code.ZipFile).toContain(
      "/gateways/' + encodeURIComponent(gatewayIdentifier) + '/targets/",
    );
    expect(waiter.Properties.Code.ZipFile).toContain(
      "IsComplete: items.length === 0 && !parsed.nextToken",
    );
  });

  it("polls target inventory until the Gateway is safe to delete", async () => {
    const { template } = synth({ allowedToolIds: ["tool-echo"] });
    const waiter = Object.values(
      template.findResources("AWS::Lambda::Function"),
    ).find(
      (resource: any) =>
        resource.Properties?.Description ===
        "Waits until AgentCore reports no targets before Gateway deletion.",
    ) as any;
    const responses = [
      JSON.stringify({ items: [{ targetId: "TARGET1234" }] }),
      JSON.stringify({ items: [] }),
    ];
    const fakeHttps = {
      request: (_options: any, callback: (response: any) => void) => {
        const request = new EventEmitter() as any;
        request.end = () => {
          const response = new EventEmitter() as any;
          response.statusCode = 200;
          callback(response);
          response.emit("data", Buffer.from(responses.shift()!, "utf8"));
          response.emit("end");
        };
        return request;
      },
    };
    const exported: Record<string, any> = {};
    runInNewContext(waiter.Properties.Code.ZipFile, {
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
        },
      },
      Buffer,
      Date,
      JSON,
      Promise,
      encodeURIComponent,
    });
    const event = {
      RequestType: "Delete",
      ResourceProperties: { GatewayIdentifier: "gateway-123" },
    };
    await expect(exported.isComplete(event)).resolves.toEqual({
      IsComplete: false,
    });
    await expect(exported.isComplete(event)).resolves.toEqual({
      IsComplete: true,
    });
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
      readonly policyEngineMode?: "OFF" | "LOG_ONLY" | "ENFORCE";
      readonly policyEngineIamRoleArns?: readonly string[];
      readonly cognitoDiscoveryUrl?: string;
      readonly cognitoAudience?: readonly string[];
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
        policyEngineMode: opts.policyEngineMode,
        policyEngineIamRoleArns: opts.policyEngineIamRoleArns,
        cognitoDiscoveryUrl: opts.cognitoDiscoveryUrl,
        cognitoAudience: opts.cognitoAudience,
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
    expect(requests[1].host).toBe("agent-registry-control.us-west-2.api.aws");
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

  it("preserves the R2 template when PolicyEngine mode is OFF", () => {
    const implicit = synthRegistry().template.toJSON();
    const explicit = synthRegistry({
      policyEngineMode: "OFF",
    }).template.toJSON();
    expect(explicit).toEqual(implicit);
    const template = Template.fromJSON(explicit);
    template.resourceCountIs("AWS::BedrockAgentCore::PolicyEngine", 0);
    template.resourceCountIs("AWS::BedrockAgentCore::Policy", 0);
    template.resourceCountIs("Custom::AgenticAIPolicyEngineAssociation", 0);
    template.resourceCountIs("Custom::AgenticAIPolicyEngineMode", 0);
    expect(JSON.stringify(explicit)).not.toContain(
      "bedrock-agentcore:AuthorizeAction",
    );
  });

  it("changes only the mode mutation, mode-ready check, and output between LOG_ONLY and ENFORCE", () => {
    const iamRoleArns = [
      `arn:aws:iam::${WORKLOAD_ACCOUNT_ID}:role/AgenticAI-D03-acme-primary-runtime`,
    ];
    const logOnly = synthRegistry({
      policyEngineMode: "LOG_ONLY",
      policyEngineIamRoleArns: iamRoleArns,
    }).template.toJSON();
    const enforce = synthRegistry({
      policyEngineMode: "ENFORCE",
      policyEngineIamRoleArns: iamRoleArns,
    }).template.toJSON();
    const changedResources = Object.keys(logOnly.Resources).filter(
      (logicalId) =>
        JSON.stringify(logOnly.Resources[logicalId]) !==
        JSON.stringify(enforce.Resources[logicalId]),
    );
    expect(
      changedResources.map((id) => logOnly.Resources[id].Type).sort(),
    ).toEqual(
      [
        "AWS::CloudFormation::CustomResource",
        "Custom::AgenticAIPolicyEngineMode",
      ].sort(),
    );
    const changedOutputs = Object.keys(logOnly.Outputs).filter(
      (logicalId) =>
        JSON.stringify(logOnly.Outputs[logicalId]) !==
        JSON.stringify(enforce.Outputs[logicalId]),
    );
    expect(changedOutputs).toEqual(["PolicyEngineMode"]);
  });

  it("emits strict IAM PolicyEngine resources and exact service-role access in LOG_ONLY", () => {
    const callerRole = `arn:aws:iam::${WORKLOAD_ACCOUNT_ID}:role/AgenticAI-D03-acme-primary-runtime`;
    const { template } = synthRegistry({
      policyEngineMode: "LOG_ONLY",
      policyEngineIamRoleArns: [callerRole],
    });
    template.resourceCountIs("AWS::BedrockAgentCore::PolicyEngine", 1);
    template.resourceCountIs("AWS::BedrockAgentCore::Policy", 2);
    template.resourceCountIs("AWS::KMS::Key", 1);
    const engine = Object.values(
      template.findResources("AWS::BedrockAgentCore::PolicyEngine"),
    )[0] as any;
    expect(engine.Properties.EncryptionKeyArn).toEqual({
      "Fn::GetAtt": [expect.any(String), "Arn"],
    });
    const key = Object.values(
      template.findResources("AWS::KMS::Key"),
    )[0] as any;
    const keyPolicy = key.Properties.KeyPolicy.Statement;
    expect(JSON.stringify(keyPolicy)).toContain("kms:CreateGrant");
    expect(JSON.stringify(keyPolicy)).toContain(
      "aws:bedrock-agentcore-policy:policy-engine-arn",
    );
    expect(JSON.stringify(keyPolicy)).toContain("kms:ViaService");
    expect(JSON.stringify(keyPolicy)).toContain("aws:SourceAccount");
    expect(JSON.stringify(keyPolicy)).not.toContain(
      '"Service":"bedrock-agentcore.amazonaws.com"',
    );
    const resources = template.toJSON().Resources as Record<string, any>;
    const propagation = Object.values(resources).find(
      (resource) =>
        resource.Type === "AWS::CloudFormation::CustomResource" &&
        resource.Properties?.WaitMs,
    ) as any;
    expect(propagation.Properties.WaitMs).toBe(360000);
    template.resourceCountIs("Custom::AgenticAIPolicyEngineAssociation", 1);
    template.resourceCountIs("Custom::AgenticAIPolicyEngineMode", 1);

    const policies = Object.values(
      template.findResources("AWS::BedrockAgentCore::Policy"),
    ) as any[];
    for (const policy of policies) {
      expect(policy.Properties.ValidationMode).toBe("FAIL_ON_ANY_FINDINGS");
      expect(policy.Properties.EnforcementMode).toBe("ACTIVE");
      const statement = JSON.stringify(
        policy.Properties.Definition.Cedar.Statement,
      );
      const expectedPrincipal = JSON.stringify(
        `AgentCore::IamEntity::"arn:aws:sts::${WORKLOAD_ACCOUNT_ID}:assumed-role/AgenticAI-D03-acme-primary-runtime"`,
      ).slice(1, -1);
      expect(statement).toContain(expectedPrincipal);
      expect(statement).toContain("AgentCore::Gateway");
      expect(statement).not.toContain("forbid(");
      expect(statement).not.toContain("gateway/*");
    }
    expect(JSON.stringify(policies)).toContain("target-tool-echo___tool-echo");
    expect(JSON.stringify(policies)).toContain("target-tool-ping___tool-ping");

    const iamPolicies = Object.values(
      template.findResources("AWS::IAM::Policy"),
    ) as any[];
    const access = iamPolicies.find(
      (policy) =>
        policy.Properties.PolicyName === "AgenticAI-nonprod-PolicyEngineAccess",
    );
    expect(access).toBeDefined();
    const accessStatements = access.Properties.PolicyDocument.Statement;
    expect(accessStatements).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          Action: "bedrock-agentcore:GetPolicyEngine",
          Resource: expect.anything(),
        }),
        expect.objectContaining({
          Action: [
            "bedrock-agentcore:AuthorizeAction",
            "bedrock-agentcore:PartiallyAuthorizeActions",
          ],
          Resource: expect.any(Array),
        }),
      ]),
    );
    expect(JSON.stringify(accessStatements)).not.toContain('"Resource":"*"');

    template.hasOutput("PolicyEngineMode", { Value: "LOG_ONLY" });
    template.hasOutput("PolicyEnginePolicyCount", { Value: "2" });
  });

  it("orders mode rollback and detach checks before policy, Gateway, and engine deletion", () => {
    const { template } = synthRegistry({
      policyEngineMode: "ENFORCE",
      policyEngineIamRoleArns: [
        `arn:aws:iam::${WORKLOAD_ACCOUNT_ID}:role/AgenticAI-D03-acme-primary-runtime`,
      ],
    });
    const resources = template.toJSON().Resources as Record<string, any>;
    const [modeId, mode] = Object.entries(resources).find(
      ([, resource]) => resource.Type === "Custom::AgenticAIPolicyEngineMode",
    )!;
    const [modeRollbackId] = Object.entries(resources).find(
      ([, resource]) =>
        resource.Type === "AWS::CloudFormation::CustomResource" &&
        resource.Properties?.ExpectedMode === "LOG_ONLY" &&
        resource.Properties?.CheckOn === "DELETE",
    )!;
    expect(mode.DependsOn).toContain(modeRollbackId);
    expect(JSON.stringify(mode)).toContain('\\"mode\\":\\"ENFORCE\\"');
    expect(JSON.stringify(mode)).toContain('\\"mode\\":\\"LOG_ONLY\\"');

    const [detachId, detach] = Object.entries(resources).find(
      ([, resource]) =>
        resource.Type === "AWS::CloudFormation::CustomResource" &&
        resource.Properties?.ExpectedMode === "DETACHED",
    )!;
    const [associationId, association] = Object.entries(resources).find(
      ([, resource]) =>
        resource.Type === "Custom::AgenticAIPolicyEngineAssociation",
    )!;
    expect(association.DependsOn).toContain(detachId);
    expect(association.Properties.ManageAssociation).toBe(true);
    expect(association.Properties.GatewayUpdateParameters).toBeDefined();

    const policyIds = Object.entries(resources)
      .filter(
        ([, resource]) => resource.Type === "AWS::BedrockAgentCore::Policy",
      )
      .map(([id]) => id);
    const [associationReadyId] = Object.entries(resources).find(
      ([, resource]) =>
        resource.Type === "AWS::CloudFormation::CustomResource" &&
        resource.Properties?.ExpectedMode === "LOG_ONLY" &&
        resource.Properties?.CheckOn === "CREATE_UPDATE",
    )!;
    for (const policyId of policyIds) {
      expect(resources[policyId].DependsOn).toContain(associationReadyId);
    }
    expect(resources[modeRollbackId].DependsOn).toEqual(
      expect.arrayContaining(policyIds),
    );

    const [modeReadyId] = Object.entries(resources).find(
      ([, resource]) =>
        resource.Type === "AWS::CloudFormation::CustomResource" &&
        resource.Properties?.ExpectedMode === "ENFORCE",
    )!;
    expect(resources[modeReadyId].DependsOn).toContain(modeId);
    const targetBarrier = Object.values(resources).find(
      (resource) =>
        resource.Type === "AWS::CloudFormation::CustomResource" &&
        resource.Properties?.GatewayIdentifier &&
        !resource.Properties?.ExpectedMode,
    ) as any;
    expect(targetBarrier.DependsOn).toContain(modeReadyId);

    const stateWaiter = Object.values(resources).find(
      (resource) =>
        resource.Type === "AWS::Lambda::Function" &&
        resource.Properties?.Description ===
          "Waits for Gateway PolicyEngine association and mode convergence.",
    ) as any;
    expect(stateWaiter.Properties.Code.ZipFile).toContain(
      "desiredMode === 'DETACHED'",
    );
    expect(stateWaiter.Properties.Code.ZipFile).toContain(
      "isRetryablePolicyEnginePropagation",
    );
    expect(stateWaiter.Properties.Code.ZipFile).toContain(
      "event.RequestType === 'Delete'",
    );
    expect(detach).toBeDefined();
    expect(associationId).toBeDefined();
  });

  it("polls PolicyEngine mode and detach convergence through the synthesized waiter", async () => {
    const { template } = synthRegistry({
      policyEngineMode: "LOG_ONLY",
      policyEngineIamRoleArns: [
        `arn:aws:iam::${WORKLOAD_ACCOUNT_ID}:role/AgenticAI-D03-acme-primary-runtime`,
      ],
    });
    const waiter = Object.values(
      template.findResources("AWS::Lambda::Function"),
    ).find(
      (resource: any) =>
        resource.Properties?.Description ===
        "Waits for Gateway PolicyEngine association and mode convergence.",
    ) as any;
    const engineArn =
      "arn:aws:bedrock-agentcore:us-west-2:333333333333:policy-engine/AgenticAI_nonprod_acme_primary_pe-abcdefghij";
    const responses = [
      {
        status: "UPDATING",
        policyEngineConfiguration: { arn: engineArn, mode: "LOG_ONLY" },
      },
      {
        status: "READY",
        policyEngineConfiguration: { arn: engineArn, mode: "LOG_ONLY" },
      },
      {
        status: "READY",
        policyEngineConfiguration: { arn: engineArn, mode: "LOG_ONLY" },
      },
      { status: "READY" },
    ];
    const fakeHttps = {
      request: (_options: any, callback: (response: any) => void) => {
        const request = new EventEmitter() as any;
        request.end = () => {
          const response = new EventEmitter() as any;
          response.statusCode = 200;
          callback(response);
          response.emit(
            "data",
            Buffer.from(JSON.stringify(responses.shift()), "utf8"),
          );
          response.emit("end");
        };
        return request;
      },
    };
    const exported: Record<string, any> = {};
    runInNewContext(waiter.Properties.Code.ZipFile, {
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
        },
      },
      Buffer,
      Date,
      JSON,
      Promise,
      encodeURIComponent,
    });
    const createEvent = {
      RequestType: "Create",
      ResourceProperties: {
        GatewayIdentifier: "gateway-123",
        PolicyEngineArn: engineArn,
        ExpectedMode: "LOG_ONLY",
        CheckOn: "CREATE_UPDATE",
      },
    };
    await expect(exported.isComplete(createEvent)).resolves.toEqual({
      IsComplete: false,
    });
    await expect(exported.isComplete(createEvent)).resolves.toEqual({
      IsComplete: true,
    });
    const deleteEvent = {
      RequestType: "Delete",
      ResourceProperties: {
        GatewayIdentifier: "gateway-123",
        PolicyEngineArn: engineArn,
        ExpectedMode: "DETACHED",
        CheckOn: "DELETE",
      },
    };
    await expect(exported.isComplete(deleteEvent)).resolves.toEqual({
      IsComplete: false,
    });
    await expect(exported.isComplete(deleteEvent)).resolves.toEqual({
      IsComplete: true,
    });
  });

  it("retries only the live-proven association propagation denial", async () => {
    const { template } = synthRegistry({
      policyEngineMode: "LOG_ONLY",
      policyEngineIamRoleArns: [
        `arn:aws:iam::${WORKLOAD_ACCOUNT_ID}:role/AgenticAI-D03-acme-primary-runtime`,
      ],
    });
    const waiter = Object.values(
      template.findResources("AWS::Lambda::Function"),
    ).find(
      (resource: any) =>
        resource.Properties?.Description ===
        "Waits for Gateway PolicyEngine association and mode convergence.",
    ) as any;
    const engineArn =
      "arn:aws:bedrock-agentcore:us-west-2:333333333333:policy-engine/AgenticAI_nonprod_acme_primary_pe-abcdefghij";
    const gatewayUpdateParameters = {
      gatewayIdentifier: "gateway-123",
      name: "agenticai-d03-nonprod-acme-primary-gw",
      roleArn:
        "arn:aws:iam::333333333333:role/AgenticAI-D03-nonprod-acme-primary-gw-svc",
      protocolType: "MCP",
      protocolConfiguration: {
        mcp: { supportedVersions: ["2025-06-18"], searchType: "SEMANTIC" },
      },
      authorizerType: "AWS_IAM",
    };
    const responses = [
      { status: 200, body: JSON.stringify({ status: "READY" }) },
      {
        status: 400,
        body: JSON.stringify({
          message:
            "Access denied while calling GetPolicyEngine on Policy Engine with Gateway role",
        }),
      },
      { status: 200, body: JSON.stringify({ status: "READY" }) },
      { status: 202, body: "{}" },
      {
        status: 200,
        body: JSON.stringify({
          status: "READY",
          policyEngineConfiguration: { arn: engineArn, mode: "LOG_ONLY" },
        }),
      },
      {
        status: 200,
        body: JSON.stringify({
          status: "READY",
          policyEngineConfiguration: { arn: engineArn, mode: "LOG_ONLY" },
        }),
      },
      { status: 202, body: "{}" },
      { status: 200, body: JSON.stringify({ status: "READY" }) },
      { status: 200, body: JSON.stringify({ status: "READY" }) },
      { status: 400, body: JSON.stringify({ message: "invalid protocol" }) },
    ];
    const requests: Array<{ options: any; body: string }> = [];
    const fakeHttps = {
      request: (options: any, callback: (response: any) => void) => {
        let body = "";
        const request = new EventEmitter() as any;
        request.write = (chunk: unknown) => {
          body += Buffer.isBuffer(chunk)
            ? chunk.toString("utf8")
            : String(chunk);
        };
        request.end = () => {
          const next = responses.shift();
          if (!next) throw new Error("unexpected HTTPS request");
          requests.push({ options, body });
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
    runInNewContext(waiter.Properties.Code.ZipFile, {
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
        },
      },
      Buffer,
      Date,
      JSON,
      Promise,
      encodeURIComponent,
    });
    const createEvent = {
      RequestType: "Create",
      LogicalResourceId: "PolicyEngineAssociation",
      ResourceProperties: {
        GatewayIdentifier: "gateway-123",
        PolicyEngineArn: engineArn,
        ExpectedMode: "LOG_ONLY",
        CheckOn: "CREATE_UPDATE",
        ManageAssociation: true,
        PhysicalResourceId: "policy-engine-association-nonprod-acme-primary",
        GatewayUpdateParameters: gatewayUpdateParameters,
      },
    };
    await expect(exported.onEvent(createEvent)).resolves.toEqual({
      PhysicalResourceId: "policy-engine-association-nonprod-acme-primary",
    });
    await expect(exported.isComplete(createEvent)).resolves.toEqual({
      IsComplete: false,
    });
    await expect(exported.isComplete(createEvent)).resolves.toEqual({
      IsComplete: false,
    });
    await expect(exported.isComplete(createEvent)).resolves.toEqual({
      IsComplete: true,
    });

    const deleteEvent = { ...createEvent, RequestType: "Delete" };
    await expect(exported.isComplete(deleteEvent)).resolves.toEqual({
      IsComplete: false,
    });
    await expect(exported.isComplete(deleteEvent)).resolves.toEqual({
      IsComplete: true,
    });
    await expect(exported.isComplete(createEvent)).rejects.toThrow(
      /UpdateGateway HTTP 400.*invalid protocol/,
    );

    const puts = requests.filter(({ options }) => options.method === "PUT");
    expect(puts).toHaveLength(4);
    expect(puts[0].options.path).toBe("/gateways/gateway-123/");
    expect(puts[0].options.headers["x-amz-content-sha256"]).toHaveLength(64);
    expect(puts[0].options.headers["x-amz-date"]).toMatch(/^\d{8}T\d{6}Z$/);
    const associationPayload = JSON.parse(puts[1].body);
    expect(associationPayload).not.toHaveProperty("gatewayIdentifier");
    expect(associationPayload).toEqual({
      name: gatewayUpdateParameters.name,
      roleArn: gatewayUpdateParameters.roleArn,
      protocolType: gatewayUpdateParameters.protocolType,
      protocolConfiguration: gatewayUpdateParameters.protocolConfiguration,
      authorizerType: gatewayUpdateParameters.authorizerType,
      policyEngineConfiguration: { arn: engineArn, mode: "LOG_ONLY" },
    });
    expect(JSON.parse(puts[2].body)).not.toHaveProperty(
      "policyEngineConfiguration",
    );
  });
  it("renders the live-proven quoted group candidate for CUSTOM_JWT", () => {
    const context = gaRegistryContext(["tool-echo"]);
    (context.records[0].document.authorization as any).allowedGroups = [
      "retail-developers",
    ];
    (context.records[0].document.authorization as any).combination =
      "GROUP_ONLY";
    const { template } = synthRegistry({
      context,
      policyEngineMode: "LOG_ONLY",
      cognitoDiscoveryUrl:
        "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_example/.well-known/openid-configuration",
      cognitoAudience: ["aud-example"],
    });
    const policy = Object.values(
      template.findResources("AWS::BedrockAgentCore::Policy"),
    )[0] as any;
    const statement = JSON.stringify(
      policy.Properties.Definition.Cedar.Statement,
    );
    expect(statement).toContain("principal is AgentCore::OAuthUser");
    expect(statement).toContain("cognito:groups");
    expect(statement).toContain("retail-developers");
    expect(statement).toContain("like");
  });

  it("fails closed on incomplete or mismatched PolicyEngine configuration", () => {
    expect(() => synthRegistry({ policyEngineMode: "LOG_ONLY" })).toThrow(
      /at least one exact IAM role ARN/,
    );
    expect(() =>
      synthRegistry({
        policyEngineMode: "LOG_ONLY",
        policyEngineIamRoleArns: [
          "arn:aws:iam::999999999999:role/ForeignRuntime",
        ],
      }),
    ).toThrow(/must belong to the Workstream account/);
    expect(() =>
      synthRegistry({
        policyEngineMode: "OFF",
        policyEngineIamRoleArns: [
          `arn:aws:iam::${WORKLOAD_ACCOUNT_ID}:role/RuntimeRole`,
        ],
      }),
    ).toThrow(/require LOG_ONLY or ENFORCE/);
    expect(() =>
      synthRegistry({
        policyEngineMode: "LOG_ONLY",
        cognitoDiscoveryUrl:
          "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_example/.well-known/openid-configuration",
        policyEngineIamRoleArns: [
          `arn:aws:iam::${WORKLOAD_ACCOUNT_ID}:role/RuntimeRole`,
        ],
      }),
    ).toThrow(/must not carry IAM role principals/);
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
