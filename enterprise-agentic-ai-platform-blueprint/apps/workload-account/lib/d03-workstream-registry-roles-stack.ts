/*
 * Stable IAM prerequisites for the pipeline-owned Workstream GA Gateway.
 * These roles deploy in a dedicated pipeline stage before any Gateway target,
 * so Lambda resource policies can resolve the exact Gateway role principal ID.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  CfnOutput,
  DefaultStackSynthesizer,
  Stack,
  StackProps,
  Tags,
} from "aws-cdk-lib";
import {
  Effect,
  ManagedPolicy,
  PolicyDocument,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from "aws-cdk-lib/aws-iam";
import { NagSuppressions } from "cdk-nag";
import { Construct } from "constructs";

import type { GaRegistryConsumerContext } from "@agenticai/agent-registry";

export interface D03WorkstreamRegistryRolesStackProps extends StackProps {
  readonly envName: "nonprod" | "prod";
  readonly tenantId: string;
  readonly agentId: string;
  readonly applicationId: string;
  readonly costCentre: string;
  readonly registryContext: GaRegistryConsumerContext;
  /** Emit the prior-stage Runtime execution role only for the opt-in slice. */
  readonly enablePipelineRuntimeMemory?: boolean;
  /**
   * Opt-in generated-agent grants added to the Runtime execution role. Required
   * only when the RuntimeMemory stack runs the `"generated-agent"` variant.
   * When set, the role also gains: `bedrock-agentcore:InvokeGateway` on the
   * workstream tool Gateway ARN family; `GetWorkloadAccessToken` +
   * `GetResourceOauth2Token` on the `<prefix>_*` workload-identity family, the
   * exact CognitoOauth2 credential provider, and their two parent containers;
   * and `secretsmanager:GetSecretValue` on exactly that provider's
   * service-managed client secret (live-proven 2026-09-23); and
   * `bedrock-agentcore:CreateEvent` + `GetEvent` on exactly this workstream's
   * deterministic Memory name family. The Runtime never reads the Platform
   * M2M secret -- only the RuntimeMemory custom resource does.
   */
  readonly generatedAgentGrants?: GeneratedAgentRuntimeGrants;
}

/** Inputs for the opt-in generated-agent grants on the Runtime role. */
export interface GeneratedAgentRuntimeGrants {
  /**
   * Platform M2M secret ARN published by the inference Gateway (Stage A).
   * Retained as a pipeline-contract input; NOT granted to the Runtime role
   * (the RuntimeMemory custom resource is the only reader).
   */
  readonly m2mSecretArn: string;
  /** Deterministic CognitoOauth2 credential-provider name (this account). */
  readonly credentialProviderName: string;
  /** Deterministic workload-identity name PREFIX; RuntimeMemory mints `<prefix>_<12 hex>`. */
  readonly workloadIdentityName: string;
}

export class D03WorkstreamRegistryRolesStack extends Stack {
  readonly gatewayServiceRole: Role;
  readonly registryValidatorRole: Role;
  readonly gatewayAdminRole: Role;
  readonly runtimeExecutionRole?: Role;

  constructor(
    scope: Construct,
    id: string,
    props: D03WorkstreamRegistryRolesStackProps,
  ) {
    super(scope, id, props);
    if (props.registryContext.environment !== props.envName) {
      throw new Error(
        "D03WorkstreamRegistryRolesStack: Registry environment does not match.",
      );
    }
    const base = `${props.envName}-${props.tenantId}-${props.agentId}`;
    const names = {
      gateway: `AgenticAI-D03-${base}-gw-svc`,
      validator: `AgenticAI-D03-${base}-RegistryValidator`,
      admin: `AgenticAI-D03-${props.envName}-GatewayAdmin`,
      runtime: `AgenticAI-D03-${base}-runtime`,
    };
    for (const [kind, name] of Object.entries(names)) {
      if (name.length > 64) {
        throw new Error(
          `D03WorkstreamRegistryRolesStack: ${kind} role name exceeds 64 characters.`,
        );
      }
    }
    const targetArns = props.registryContext.records.map(
      (record) => record.document.target.arn,
    );
    if (new Set(targetArns).size !== targetArns.length) {
      throw new Error(
        "D03WorkstreamRegistryRolesStack: Registry target ARNs must be unique.",
      );
    }

    this.gatewayServiceRole = new Role(this, "GatewayServiceRole", {
      roleName: names.gateway,
      assumedBy: new ServicePrincipal("bedrock-agentcore.amazonaws.com"),
      description:
        "Stable AgentCore Gateway service role scoped to exact Registry-resolved tool aliases.",
      inlinePolicies: {
        InvokeSubscribedTools: new PolicyDocument({
          statements: [
            new PolicyStatement({
              sid: "InvokeSubscribedTools",
              actions: ["lambda:InvokeFunction"],
              resources: targetArns,
            }),
          ],
        }),
      },
    });

    this.registryValidatorRole = new Role(this, "RegistryValidatorRole", {
      roleName: names.validator,
      assumedBy: new ServicePrincipal("lambda.amazonaws.com"),
      description:
        "Stable deploy-time role that assumes the Platform RegistryReaderRole.",
      inlinePolicies: {
        AssumeRegistryReader: new PolicyDocument({
          statements: [
            new PolicyStatement({
              actions: ["sts:AssumeRole"],
              resources: [props.registryContext.readerRoleArn],
            }),
          ],
        }),
      },
      managedPolicies: [
        ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AWSLambdaBasicExecutionRole",
        ),
      ],
    });

    this.gatewayAdminRole = new Role(this, "GatewayAdminRole", {
      roleName: names.admin,
      assumedBy: new ServicePrincipal("lambda.amazonaws.com"),
      description:
        "Workstream-local provisioning role allowed by SCP-09 to manage AgentCore Gateways.",
      inlinePolicies: {
        ProvisionGateway: new PolicyDocument({
          statements: [
            new PolicyStatement({
              actions: ["bedrock-agentcore:*"],
              resources: ["*"],
            }),
            new PolicyStatement({
              actions: ["iam:PassRole"],
              resources: [this.gatewayServiceRole.roleArn],
              conditions: {
                StringEquals: {
                  "iam:PassedToService": "bedrock-agentcore.amazonaws.com",
                },
              },
            }),
          ],
        }),
      },
      managedPolicies: [
        ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AWSLambdaBasicExecutionRole",
        ),
      ],
    });

    new CfnOutput(this, "GatewayServiceRoleArn", {
      description:
        "Exact existing principal ARN for agenticai/gaGatewayServiceRoleArns.",
      value: this.gatewayServiceRole.roleArn,
    });

    const taggedRoles = [
      this.gatewayServiceRole,
      this.registryValidatorRole,
      this.gatewayAdminRole,
    ];
    if (props.enablePipelineRuntimeMemory) {
      // Created in this prior stage so AgentCore sees a propagated role before
      // the native Runtime stack references it.
      this.runtimeExecutionRole = this.buildRuntimeExecutionRole(
        props,
        names.runtime,
      );
      taggedRoles.push(this.runtimeExecutionRole);
      new CfnOutput(this, "RuntimeExecutionRoleArn", {
        description:
          "Stable AgentCore Runtime execution role ARN consumed by the opt-in Runtime+Memory stack.",
        value: this.runtimeExecutionRole.roleArn,
      });
    }

    for (const role of taggedRoles) {
      Tags.of(role).add("application-id", props.applicationId);
      Tags.of(role).add("agent-id", props.agentId);
      Tags.of(role).add("tenant-id", props.tenantId);
      Tags.of(role).add("cost-centre", props.costCentre);
      Tags.of(role).add("environment", props.envName);
    }
    NagSuppressions.addResourceSuppressions(
      this.registryValidatorRole,
      [
        {
          id: "AwsSolutions-IAM4",
          appliesTo: [
            "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
          ],
          reason:
            "SEC-010: AWSLambdaBasicExecutionRole is the documented logging policy for the validator Lambda.",
        },
      ],
      true,
    );
    NagSuppressions.addResourceSuppressions(
      this.gatewayAdminRole,
      [
        {
          id: "AwsSolutions-IAM4",
          appliesTo: [
            "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
          ],
          reason:
            "SEC-010: AWSLambdaBasicExecutionRole is the documented logging policy for the AgentCore provisioning Lambda.",
        },
        {
          id: "AwsSolutions-IAM5",
          reason:
            "SEC-028: The AgentCore control-plane action family requires Resource:* during resource creation; the role is Workstream-local, Lambda-trusted, pipeline-created, and SCP-09-name-bound.",
        },
      ],
      true,
    );
    NagSuppressions.addResourceSuppressions(
      this.gatewayServiceRole,
      [
        {
          id: "NIST.800.53.R5-IAMNoInlinePolicy",
          reason:
            "SEC-005: The exact Registry-resolved Lambda alias allowlist must remain visible on the stable Gateway service role.",
        },
      ],
      true,
    );
    if (this.runtimeExecutionRole) {
      NagSuppressions.addResourceSuppressions(
        this.runtimeExecutionRole,
        [
          {
            id: "AwsSolutions-IAM5",
            reason:
              "SEC-011: ecr:GetAuthorizationToken is registry-wide and takes no resource ARN; Runtime logs/tracing/metric actions have no resource-level ARN and are scoped by log-group prefix and cloudwatch:namespace instead. The opt-in generated-agent InvokeGateway grant is scoped to this account's bedrock-agentcore gateway/* family (the service-minted Gateway id suffix is not known at synth); Identity token and secret/KMS grants are scoped to exact ARNs.",
          },
          {
            id: "NIST.800.53.R5-IAMNoInlinePolicy",
            reason:
              "SEC-005: Single-purpose Runtime execution role uses inline least-privilege policies.",
          },
        ],
        true,
      );
    }
  }

  /**
   * Stable AgentCore Runtime execution role. Trusted only by the AgentCore
   * service principal, bound to this account and the exact Runtime name family
   * so a Runtime in another account/name cannot assume it. It can pull the
   * inert agent image from the account's default CDK bootstrap container-assets
   * repository, obtain an ECR auth token, write its own Runtime logs/traces and
   * emit bedrock-agentcore metrics — and is explicitly DENIED direct Bedrock
   * model invocation, since the golden-path inference boundary is the Gateway,
   * never a direct model call from the Runtime role.
   */
  private buildRuntimeExecutionRole(
    props: D03WorkstreamRegistryRolesStackProps,
    roleName: string,
  ): Role {
    // AgentCore Runtime/Memory names use the underscore charset; the Runtime is
    // minted as `<name>-<10-char-id>`, so the trust condition matches that ARN
    // family exactly.
    const underscoreBase =
      `${props.envName}_${props.tenantId}_${props.agentId}`.replace(/-/g, "_");
    const runtimeArnLike = `arn:aws:bedrock-agentcore:${this.region}:${this.account}:runtime/AgenticAI_D03_${underscoreBase}_runtime-*`;

    const role = new Role(this, "RuntimeExecutionRole", {
      roleName,
      assumedBy: new ServicePrincipal("bedrock-agentcore.amazonaws.com", {
        conditions: {
          StringEquals: { "aws:SourceAccount": this.account },
          ArnLike: { "aws:SourceArn": runtimeArnLike },
        },
      }),
      description:
        "Stable AgentCore Runtime execution role: ECR pull of the inert agent image, Runtime logs/tracing, bedrock-agentcore metrics; direct Bedrock invoke denied.",
    });

    // ECR pull is scoped to the default CDK bootstrap container-assets repo in
    // this Workstream account/region, where the pipeline publishes the image.
    const qualifier = DefaultStackSynthesizer.DEFAULT_QUALIFIER;
    const containerAssetsRepoArn = `arn:aws:ecr:${this.region}:${this.account}:repository/cdk-${qualifier}-container-assets-${this.account}-${this.region}`;
    role.addToPolicy(
      new PolicyStatement({
        sid: "PullAgentImage",
        effect: Effect.ALLOW,
        actions: [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
        ],
        resources: [containerAssetsRepoArn],
      }),
    );
    // GetAuthorizationToken is registry-wide and does not accept a repo ARN.
    role.addToPolicy(
      new PolicyStatement({
        sid: "EcrAuthToken",
        effect: Effect.ALLOW,
        actions: ["ecr:GetAuthorizationToken"],
        resources: ["*"],
      }),
    );
    // Runtime logs (documented AgentCore log group family).
    role.addToPolicy(
      new PolicyStatement({
        sid: "RuntimeLogs",
        effect: Effect.ALLOW,
        actions: [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams",
        ],
        resources: [
          `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*`,
        ],
      }),
    );
    role.addToPolicy(
      new PolicyStatement({
        sid: "RuntimeLogControl",
        effect: Effect.ALLOW,
        actions: ["logs:DescribeLogGroups", "logs:PutResourcePolicy"],
        resources: ["*"],
      }),
    );
    // Tracing (X-Ray, no resource-level ARN) and AgentCore namespace metrics.
    role.addToPolicy(
      new PolicyStatement({
        sid: "RuntimeTracing",
        effect: Effect.ALLOW,
        actions: [
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords",
          "xray:GetSamplingRules",
          "xray:GetSamplingTargets",
        ],
        resources: ["*"],
      }),
    );
    role.addToPolicy(
      new PolicyStatement({
        sid: "RuntimeMetrics",
        effect: Effect.ALLOW,
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
        conditions: {
          StringEquals: { "cloudwatch:namespace": "bedrock-agentcore" },
        },
      }),
    );
    // Defence-in-depth: the Runtime role must never call Bedrock models
    // directly — inference flows through the Gateway. These are the only two
    // valid Bedrock invoke actions (bedrock:Converse is NOT a real IAM action).
    role.addToPolicy(
      new PolicyStatement({
        sid: "DenyDirectBedrockInvoke",
        effect: Effect.DENY,
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ],
        resources: ["*"],
      }),
    );

    // Opt-in generated-agent grants: MCP tool Gateway invoke (SigV4), the
    // AgentCore Identity data-plane for the inference bearer, and cross-account
    // read of the Platform M2M secret used to seed the credential provider.
    const grants = props.generatedAgentGrants;
    if (grants) {
      const gatewayArnLike = `arn:aws:bedrock-agentcore:${this.region}:${this.account}:gateway/*`;
      role.addToPolicy(
        new PolicyStatement({
          sid: "InvokeToolGateway",
          effect: Effect.ALLOW,
          actions: ["bedrock-agentcore:InvokeGateway"],
          resources: [gatewayArnLike],
        }),
      );
      const providerArn = `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/default/oauth2credentialprovider/${grants.credentialProviderName}`;
      // The RuntimeMemory custom resource mints "<prefix>_<12 hex>" per Create
      // (a fixed name proved unsafe live: TagResource 500s and deleted names
      // tombstone). Scope to this workstream's prefix family, not "*".
      const workloadIdentityArn = `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default/workload-identity/${grants.workloadIdentityName}_*`;
      // Service reference: GetWorkloadAccessToken authorizes on `workload-identity`
      // AND its parent `workload-identity-directory`; GetResourceOauth2Token on
      // `oauth2credentialprovider`, `token-vault`, `workload-identity` and the
      // directory. Live-proven (2026-09-23, first generated-agent invoke): the
      // container was denied GetWorkloadAccessToken on the bare directory ARN
      // with only the named-identity family granted. Both default containers
      // are single fixed resources in this account/region -- never "*".
      const workloadDirectoryArn = `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default`;
      const tokenVaultArn = `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/default`;
      role.addToPolicy(
        new PolicyStatement({
          sid: "AgentCoreIdentityInferenceToken",
          effect: Effect.ALLOW,
          actions: [
            "bedrock-agentcore:GetWorkloadAccessToken",
            "bedrock-agentcore:GetResourceOauth2Token",
          ],
          resources: [
            providerArn,
            workloadIdentityArn,
            workloadDirectoryArn,
            tokenVaultArn,
          ],
        }),
      );
      // Live-proven (2026-09-23, second generated-agent invoke): when the
      // Runtime exchanges its workload token via GetResourceOauth2Token,
      // AgentCore Identity reads the provider's MANAGED client secret AS THE
      // CALLER and fails closed with "not authorized to perform:
      // secretsmanager:GetSecretValue on ...:secret:bedrock-agentcore-identity!
      // default/oauth2/<provider>-<random>". Grant exactly that provider's
      // managed secret (Secrets Manager appends a random suffix, hence the
      // trailing "-*" on an otherwise exact name). The Runtime never reads the
      // Platform M2M secret -- only the RuntimeMemory custom resource does --
      // so no cross-account secret or KMS grant belongs on this role.
      role.addToPolicy(
        new PolicyStatement({
          sid: "ReadManagedInferenceProviderSecret",
          effect: Effect.ALLOW,
          actions: ["secretsmanager:GetSecretValue"],
          resources: [
            `arn:aws:secretsmanager:${this.region}:${this.account}:secret:bedrock-agentcore-identity!default/oauth2/${grants.credentialProviderName}-*`,
          ],
        }),
      );
      // Live-proven gap (2026-09-23, third generated-agent invoke): the agent
      // writes one short-term event per turn and reads that exact event back
      // (CreateEvent + GetEvent by id -- ListEvents ordering is unspecified),
      // but the role carried no Memory data-plane grant at all. The Memory is
      // created later (RuntimeMemory stage) with the deterministic name
      // `AgenticAI_D03_<env>_<tenant>_<agent>_memory` and a service-minted
      // `-<10 chars>` id suffix, so scope to exactly that name family.
      const memoryBase =
        `${props.envName}-${props.tenantId}-${props.agentId}`.replace(
          /-/g,
          "_",
        );
      role.addToPolicy(
        new PolicyStatement({
          sid: "ShortTermMemoryEvents",
          effect: Effect.ALLOW,
          actions: [
            "bedrock-agentcore:CreateEvent",
            "bedrock-agentcore:GetEvent",
          ],
          resources: [
            `arn:aws:bedrock-agentcore:${this.region}:${this.account}:memory/AgenticAI_D03_${memoryBase}_memory-*`,
          ],
        }),
      );
      // Live-proven (2026-09-23, fourth generated-agent invoke): the Memory
      // data plane performs KMS operations AS THE CALLER; CreateEvent failed
      // closed with "Unable to perform KMS operations" once the Memory grant
      // above was in place. KMS requires BOTH the key policy (the RuntimeMemory
      // stage trusts this exact role) and this identity policy. The CMK id is
      // minted later, so scope by the alias the RuntimeMemory stack assigns
      // (`alias/agenticai/d03-runtime-memory-<env>-<tenant>-<agent>`) plus
      // ViaService -- never an unconditioned kms:* on key/*.
      role.addToPolicy(
        new PolicyStatement({
          sid: "MemoryCmkDataPlaneCrypto",
          effect: Effect.ALLOW,
          actions: [
            "kms:Decrypt",
            "kms:DescribeKey",
            "kms:GenerateDataKey",
            "kms:GenerateDataKeyWithoutPlaintext",
            "kms:ReEncryptFrom",
            "kms:ReEncryptTo",
          ],
          resources: [`arn:aws:kms:${this.region}:${this.account}:key/*`],
          conditions: {
            StringEquals: {
              "kms:ViaService": `bedrock-agentcore.${this.region}.amazonaws.com`,
            },
            "ForAnyValue:StringEquals": {
              "kms:ResourceAliases": `alias/agenticai/d03-runtime-memory-${props.envName}-${props.tenantId}-${props.agentId}`,
            },
          },
        }),
      );
    }
    return role;
  }
}
