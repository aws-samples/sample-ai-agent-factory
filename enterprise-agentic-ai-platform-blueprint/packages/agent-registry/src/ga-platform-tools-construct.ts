/*
 * Pipeline-owned Platform demo tools referenced by GA Registry governance
 * records. Functions and permissions are environment-isolated so nonprod and
 * prod can share one test account without fixed-name collisions.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { createHash } from "node:crypto";

import { Duration, RemovalPolicy, Stack, Tags } from "aws-cdk-lib";
import {
  ArnPrincipal,
  PolicyStatement,
  ServicePrincipal,
} from "aws-cdk-lib/aws-iam";
import { Key } from "aws-cdk-lib/aws-kms";
import {
  Alias,
  Code,
  Function as LambdaFunction,
  Runtime,
} from "aws-cdk-lib/aws-lambda";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";
import { NagSuppressions } from "cdk-nag";
import { Construct } from "constructs";

import { PLATFORM_TOOL_CATALOGUE } from "@agenticai/platform-tool-catalogue";

export interface GaPlatformToolsConstructProps {
  readonly envName: "nonprod" | "prod";
  readonly workloadAccountIds: readonly string[];
  readonly applicationId: string;
  readonly tenantId: string;
  readonly agentId: string;
  readonly costCentre: string;
  /** Grant aliases only after the Workload role-prerequisite stage succeeds. */
  readonly grantGatewayInvokePermissions?: boolean;
  /** Exact existing Gateway service-role ARNs produced by the Workload pipeline. */
  readonly gatewayServiceRoleArns?: readonly string[];
  /** Workload account that must own this environment's Gateway role. */
  readonly gatewayWorkloadAccountId?: string;
}

const TOOL_HANDLER = `
exports.handler = async (event, context) => {
  const toolId = process.env.AGENTICAI_TOOL_ID;
  if (toolId === 'tool-echo') {
    return {
      message: event && typeof event.message === 'string' ? event.message : '',
      toolId,
    };
  }
  if (toolId === 'tool-ping') {
    return {
      pong: true,
      timestamp: new Date().toISOString(),
      requestId: context && context.awsRequestId ? context.awsRequestId : '',
      toolId,
    };
  }
  throw new Error('Unsupported platform tool id');
};
`;

export class GaPlatformToolsConstruct extends Construct {
  readonly aliasArns: Readonly<Record<string, string>>;
  readonly aliases: Readonly<Record<string, Alias>>;

  constructor(
    scope: Construct,
    id: string,
    props: GaPlatformToolsConstructProps,
  ) {
    super(scope, id);
    const stack = Stack.of(this);
    const workloadAccountIds = [...new Set(props.workloadAccountIds)].sort();
    if (
      workloadAccountIds.length === 0 ||
      workloadAccountIds.some((accountId) => !/^\d{12}$/.test(accountId))
    ) {
      throw new Error(
        "GaPlatformToolsConstruct: workloadAccountIds must be non-empty 12-digit IDs.",
      );
    }
    const gatewayServiceRoleArns = [
      ...new Set(props.gatewayServiceRoleArns ?? []),
    ].sort();
    if (
      props.grantGatewayInvokePermissions &&
      gatewayServiceRoleArns.length === 0
    ) {
      throw new Error(
        "GaPlatformToolsConstruct: gatewayServiceRoleArns is required when permissions are enabled.",
      );
    }
    if (
      props.grantGatewayInvokePermissions &&
      !/^\d{12}$/.test(props.gatewayWorkloadAccountId ?? "")
    ) {
      throw new Error(
        "GaPlatformToolsConstruct: gatewayWorkloadAccountId must identify this environment's Workload account.",
      );
    }
    if (
      !props.grantGatewayInvokePermissions &&
      gatewayServiceRoleArns.length > 0
    ) {
      throw new Error(
        "GaPlatformToolsConstruct: role ARNs must not be supplied before the permission phase.",
      );
    }
    const rolePattern = new RegExp(
      "^arn:(?:aws|aws-us-gov|aws-cn):iam::(\\d{12}):role/" +
        "AgenticAI-D03-(nonprod|prod)-[A-Za-z0-9+=,.@_-]+-[A-Za-z0-9+=,.@_-]+-gw-svc$",
    );
    if (
      props.grantGatewayInvokePermissions &&
      gatewayServiceRoleArns.length !== 2
    ) {
      throw new Error(
        "GaPlatformToolsConstruct: exactly one nonprod and one prod Gateway role ARN are required.",
      );
    }
    for (const roleArn of gatewayServiceRoleArns) {
      const match = rolePattern.exec(roleArn);
      if (!match || !workloadAccountIds.includes(match[1])) {
        throw new Error(
          `GaPlatformToolsConstruct: invalid or unconfigured Gateway role ARN '${roleArn}'.`,
        );
      }
      if (
        match[2] === props.envName &&
        match[1] !== props.gatewayWorkloadAccountId
      ) {
        throw new Error(
          `GaPlatformToolsConstruct: ${props.envName} Gateway role must be owned by Workload account ${props.gatewayWorkloadAccountId}.`,
        );
      }
    }
    for (const environment of ["nonprod", "prod"] as const) {
      const roleCount = gatewayServiceRoleArns.filter((roleArn) =>
        roleArn.includes(`/AgenticAI-D03-${environment}-`),
      ).length;
      if (props.grantGatewayInvokePermissions && roleCount !== 1) {
        throw new Error(
          `GaPlatformToolsConstruct: exactly one ${environment} Gateway role ARN is required.`,
        );
      }
    }
    const environmentGatewayRoleArns = gatewayServiceRoleArns.filter(
      (roleArn) => roleArn.includes(`/AgenticAI-D03-${props.envName}-`),
    );
    if (
      props.grantGatewayInvokePermissions &&
      environmentGatewayRoleArns.length === 0
    ) {
      throw new Error(
        `GaPlatformToolsConstruct: no ${props.envName} Gateway role ARN was supplied.`,
      );
    }

    const tags = {
      "application-id": props.applicationId,
      "agent-id": props.agentId,
      "tenant-id": props.tenantId,
      "cost-centre": props.costCentre,
      environment: props.envName,
    };
    const applyTags = (resource: Construct): void => {
      for (const [key, value] of Object.entries(tags)) {
        Tags.of(resource).add(key, value);
      }
    };

    const logKey = new Key(this, "LogKey", {
      alias: `alias/agenticai/platform-tool-logs-${props.envName}`,
      description: `CMK for ${props.envName} Platform tool Lambda logs.`,
      enableKeyRotation: true,
      pendingWindow: Duration.days(7),
      removalPolicy:
        props.envName === "prod" ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
    });
    logKey.addToResourcePolicy(
      new PolicyStatement({
        sid: "AllowCloudWatchLogsEncryption",
        principals: [
          new ServicePrincipal(`logs.${stack.region}.${stack.urlSuffix}`),
        ],
        actions: [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey",
        ],
        resources: ["*"],
        conditions: {
          ArnLike: {
            "kms:EncryptionContext:aws:logs:arn": `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:/aws/lambda/agenticai-platform-${props.envName}-tool-*`,
          },
        },
      }),
    );
    applyTags(logKey);

    const aliasArns: Record<string, string> = {};
    const aliases: Record<string, Alias> = {};
    for (const tool of Object.values(PLATFORM_TOOL_CATALOGUE).sort(
      (left, right) => left.toolId.localeCompare(right.toolId),
    )) {
      if ((tool.toolType ?? "lambda") !== "lambda") {
        throw new Error(
          `GaPlatformToolsConstruct: R2 supports Lambda tools only; got ${tool.toolId}.`,
        );
      }
      const functionName = `agenticai-platform-${props.envName}-${tool.toolId}`;
      if (functionName.length > 64) {
        throw new Error(
          `GaPlatformToolsConstruct: function name for ${tool.toolId} exceeds 64 characters.`,
        );
      }
      const logGroup = new LogGroup(this, `LogGroup-${tool.toolId}`, {
        logGroupName: `/aws/lambda/${functionName}`,
        encryptionKey: logKey,
        retention: RetentionDays.ONE_MONTH,
        removalPolicy:
          props.envName === "prod"
            ? RemovalPolicy.RETAIN
            : RemovalPolicy.DESTROY,
      });
      const fn = new LambdaFunction(this, `Function-${tool.toolId}`, {
        functionName,
        description: tool.description,
        runtime: Runtime.NODEJS_20_X,
        handler: "index.handler",
        code: Code.fromInline(TOOL_HANDLER),
        timeout: Duration.seconds(10),
        memorySize: 128,
        logGroup,
        environment: { AGENTICAI_TOOL_ID: tool.toolId },
      });
      const alias = new Alias(this, `Alias-${tool.toolId}`, {
        aliasName: "PROD",
        version: fn.currentVersion,
      });
      if (props.grantGatewayInvokePermissions) {
        for (const principalArn of environmentGatewayRoleArns) {
          const principalId = createHash("sha256")
            .update(principalArn)
            .digest("hex")
            .slice(0, 12);
          alias.addPermission(
            `Allow-${props.envName}-${tool.toolId}-${principalId}`.slice(
              0,
              100,
            ),
            {
              principal: new ArnPrincipal(principalArn),
              action: "lambda:InvokeFunction",
            },
          );
        }
      }
      applyTags(logGroup);
      applyTags(fn);
      applyTags(alias);
      NagSuppressions.addResourceSuppressions(
        fn,
        [
          {
            id: "AwsSolutions-L1",
            reason:
              "SEC-006: NODEJS_20_X is the latest runtime supported by this pinned CDK version.",
          },
          {
            id: "AwsSolutions-IAM4",
            appliesTo: [
              "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            ],
            reason:
              "SEC-010: AWSLambdaBasicExecutionRole is the documented logging policy for Lambda.",
          },
          {
            id: "NIST.800.53.R5-LambdaConcurrency",
            reason:
              "SEC-007: Platform tools are Gateway-throttled and must not introduce a second lower concurrency ceiling.",
          },
          {
            id: "NIST.800.53.R5-LambdaDLQ",
            reason:
              "SEC-008: Synchronous Gateway invocations return tool failures to the caller; no asynchronous event is dropped.",
          },
          {
            id: "NIST.800.53.R5-LambdaInsideVPC",
            reason:
              "SEC-009: The two deterministic demo tools make no network calls; VPC attachment adds no isolation benefit.",
          },
        ],
        true,
      );
      aliasArns[tool.toolId] =
        `arn:aws:lambda:${stack.region}:${stack.account}:function:${functionName}:PROD`;
      aliases[tool.toolId] = alias;
    }
    this.aliasArns = aliasArns;
    this.aliases = aliases;
  }
}
