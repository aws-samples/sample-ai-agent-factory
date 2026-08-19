/**
 * McpProbeConstruct — periodic MCP `tools/list` probe against an AgentCore
 * Gateway URL. Closes BLUEPRINT_GAP_ANALYSIS (2).md Partial-3.
 *
 * Emits:
 *   - Lambda (Node 20 inline) that calls `tools/list` with the locked
 *     `MCP-Protocol-Version: 2025-06-18` header. When the monitored gateway
 *     enforces CUSTOM_JWT, supply `cognitoTokenUrl`/`cognitoClientId`/
 *     `cognitoClientSecretArn` so the probe presents a client-credentials
 *     bearer token (otherwise it runs unauthenticated — AWS_IAM gateway only,
 *     and would sit permanently breaching against a CUSTOM_JWT gateway).
 *   - EventBridge schedule (default 5 min cadence).
 *   - CloudWatch metric `AgenticAI/MCP/MCPProbeSuccess` per gateway.
 *   - Alarm wired to the eval-gates SNS failures topic when the metric
 *     drops to 0 for 3 consecutive periods.
 *
 * The probe asserts the qualified name pattern before publishing success.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import {
  Alarm,
  ComparisonOperator,
  Metric,
  TreatMissingData,
} from 'aws-cdk-lib/aws-cloudwatch';
import { SnsAction } from 'aws-cdk-lib/aws-cloudwatch-actions';
import { Rule, Schedule } from 'aws-cdk-lib/aws-events';
import { LambdaFunction } from 'aws-cdk-lib/aws-events-targets';
import { Effect, PolicyStatement } from 'aws-cdk-lib/aws-iam';
import { Code, Function, Runtime } from 'aws-cdk-lib/aws-lambda';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import { ITopic } from 'aws-cdk-lib/aws-sns';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

import { MCP_PROTOCOL_HEADER, MCP_PROTOCOL_VERSION } from './mcp';

export interface McpProbeConstructProps {
  readonly envName: string;
  readonly tenantId: string;
  readonly agentId: string;
  readonly gatewayUrl: string;
  readonly failuresTopic: ITopic;
  readonly cadence?: Duration;
  /**
   * Exact AgentCore Gateway ARN this probe monitors. When supplied, the
   * probe's `bedrock-agentcore:InvokeGateway` grant is scoped to this single
   * ARN. When omitted, the grant is scoped to the gateway id parsed from
   * `gatewayUrl` if derivable, otherwise to `gateway/*` in THIS account and
   * region (never account-wide `*`). SEC (Holmes CSR).
   */
  readonly gatewayArn?: string;
  /**
   * Optional Cognito OAuth2 token endpoint
   * (`https://<domain>/oauth2/token`). When the monitored gateway enforces
   * CUSTOM_JWT, the probe MUST present a bearer token — supply this plus
   * `cognitoClientId` and `cognitoClientSecretArn` so the probe fetches a
   * client-credentials access token before calling the gateway. When omitted,
   * the probe runs unauthenticated (correct only for an AWS_IAM gateway).
   *
   * Without this, a probe against a CUSTOM_JWT gateway always fails auth and
   * the success alarm sits permanently breaching (live-verified 2026-07-02).
   */
  readonly cognitoTokenUrl?: string;
  /** Cognito app client id used for the client-credentials grant. */
  readonly cognitoClientId?: string;
  /**
   * Secrets Manager ARN holding the Cognito app client secret. The probe
   * reads it at runtime (never embedded in the template). Required with
   * `cognitoTokenUrl`.
   */
  readonly cognitoClientSecretArn?: string;
  /** OAuth2 scope for the client-credentials grant (e.g. `mcp/invoke`). */
  readonly cognitoScope?: string;
}

/**
 * Parse the AgentCore Gateway id from a gateway URL of the shape
 * `https://<gatewayId>.gateway.bedrock-agentcore.<region>.amazonaws.com/...`.
 * Returns undefined when the host does not match (e.g. a custom CNAME), in
 * which case the caller falls back to a `gateway/*` account+region scope.
 */
function parseGatewayId(gatewayUrl: string): string | undefined {
  try {
    const host = new URL(gatewayUrl).hostname;
    const m = /^([a-zA-Z0-9-]+)\.gateway\.bedrock-agentcore\./.exec(host);
    return m?.[1];
  } catch {
    return undefined;
  }
}

export const MCP_METRIC_NAMESPACE = 'AgenticAI/MCP';

export class McpProbeConstruct extends Construct {
  readonly probe: Function;
  readonly schedule: Rule;
  readonly logGroup: LogGroup;
  readonly successAlarm: Alarm;

  constructor(scope: Construct, id: string, props: McpProbeConstructProps) {
    super(scope, id);

    const stack = Stack.of(this);
    const dim = { TenantId: props.tenantId, AgentId: props.agentId, Env: props.envName };

    if (!/^https:\/\//.test(props.gatewayUrl)) {
      throw new Error(`McpProbe gatewayUrl must be HTTPS, got: ${props.gatewayUrl}`);
    }

    this.logGroup = new LogGroup(this, 'Logs', {
      logGroupName: `/agenticai/mcp-probe/${props.envName}/${props.tenantId}/${props.agentId}`,
      retention: RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    this.probe = new Function(this, 'Probe', {
      functionName: `agenticai-mcp-probe-${props.envName}-${props.tenantId}-${props.agentId}`,
      runtime: Runtime.NODEJS_20_X,
      handler: 'index.handler',
      timeout: Duration.minutes(1),
      memorySize: 256,
      logGroup: this.logGroup,
      environment: {
        ENV_NAME: props.envName,
        TENANT_ID: props.tenantId,
        AGENT_ID: props.agentId,
        GATEWAY_URL: props.gatewayUrl,
        REGION: stack.region,
        MCP_PROTOCOL_HEADER,
        MCP_PROTOCOL_VERSION,
        METRIC_NAMESPACE: MCP_METRIC_NAMESPACE,
        // CUSTOM_JWT auth (optional). When set, the probe fetches a
        // client-credentials bearer token before calling the gateway so it
        // can authenticate against a CUSTOM_JWT gateway. Empty => unauth
        // (AWS_IAM gateway only).
        COGNITO_TOKEN_URL: props.cognitoTokenUrl ?? '',
        COGNITO_CLIENT_ID: props.cognitoClientId ?? '',
        COGNITO_CLIENT_SECRET_ARN: props.cognitoClientSecretArn ?? '',
        COGNITO_SCOPE: props.cognitoScope ?? '',
      },
      description: 'MCP-native probe — calls tools/list against the workstream gateway and emits a success metric.',
      code: Code.fromInline(MCP_PROBE_HANDLER),
    });

    // Grant the probe read access to the Cognito client secret when CUSTOM_JWT
    // auth is configured (scoped to the exact secret ARN).
    if (props.cognitoClientSecretArn) {
      this.probe.addToRolePolicy(
        new PolicyStatement({
          sid: 'ReadCognitoClientSecret',
          effect: Effect.ALLOW,
          actions: ['secretsmanager:GetSecretValue'],
          resources: [props.cognitoClientSecretArn],
        }),
      );
    }

    // Allow SigV4 calling the gateway. AgentCore Gateway accepts SigV4 by
    // role-arn; the probe role is the identity we'll allow on the gateway
    // resource policy in the live deploy.
    // SEC (Holmes CSR): scope InvokeGateway to the exact gateway ARN when
    // known, else the gateway id parsed from the URL, else `gateway/*` bounded
    // to THIS account + region — never an account-wide `*`.
    const gatewayId = parseGatewayId(props.gatewayUrl);
    const invokeGatewayResource =
      props.gatewayArn ??
      stack.formatArn({
        service: 'bedrock-agentcore',
        resource: 'gateway',
        resourceName: gatewayId ?? '*',
      });
    this.probe.addToRolePolicy(
      new PolicyStatement({
        sid: 'InvokeMonitoredGateway',
        effect: Effect.ALLOW,
        actions: ['execute-api:Invoke', 'bedrock-agentcore:InvokeGateway'],
        resources: [invokeGatewayResource],
      }),
    );
    this.probe.addToRolePolicy(
      new PolicyStatement({
        sid: 'PutMetricScopedToNamespace',
        effect: Effect.ALLOW,
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'],
        conditions: { StringEquals: { 'cloudwatch:namespace': MCP_METRIC_NAMESPACE } },
      }),
    );

    NagSuppressions.addResourceSuppressions(
      this.probe,
      [
        { id: 'AwsSolutions-IAM5', reason: 'SEC-025: bedrock-agentcore:InvokeGateway is scoped to the monitored gateway ARN (or account+region gateway/* when the id is not derivable at synth). execute-api:Invoke retains a path wildcard because the API id/stage are not known until the gateway is provisioned; action set is read-only.' },
        { id: 'NIST.800.53.R5-LambdaConcurrency', reason: 'SEC-007: cadence-driven; concurrent invocations bounded by EventBridge schedule.' },
        { id: 'NIST.800.53.R5-LambdaDLQ', reason: 'SEC-008: failure path emits the metric + alarm; DLQ would duplicate.' },
        { id: 'NIST.800.53.R5-LambdaInsideVPC', reason: 'SEC-009: probe targets the gateway public endpoint; in-VPC runs require VPCE wiring (deferred).' },
      ],
      true,
    );

    this.schedule = new Rule(this, 'Schedule', {
      ruleName: `agenticai-mcp-probe-${props.envName}-${props.tenantId}-${props.agentId}`,
      schedule: Schedule.rate(props.cadence ?? Duration.minutes(5)),
      description: 'MCP probe cadence.',
    });
    this.schedule.addTarget(new LambdaFunction(this.probe));

    this.successAlarm = new Alarm(this, 'ProbeSuccessAlarm', {
      alarmName: `agenticai-mcp-probe-${props.envName}-${props.tenantId}-${props.agentId}-success`,
      metric: new Metric({
        namespace: MCP_METRIC_NAMESPACE,
        metricName: 'MCPProbeSuccess',
        dimensionsMap: dim,
        statistic: 'Sum',
        period: Duration.minutes(15),
      }),
      comparisonOperator: ComparisonOperator.LESS_THAN_THRESHOLD,
      threshold: 1,
      evaluationPeriods: 3,
      treatMissingData: TreatMissingData.BREACHING,
    });
    this.successAlarm.addAlarmAction(new SnsAction(props.failuresTopic));
  }
}

const MCP_PROBE_HANDLER = `
const { CloudWatchClient, PutMetricDataCommand } = require('@aws-sdk/client-cloudwatch');
const { SecretsManagerClient, GetSecretValueCommand } = require('@aws-sdk/client-secrets-manager');
const cw = new CloudWatchClient({});
const sm = new SecretsManagerClient({});

const env = process.env;
const dim = [
  { Name: 'TenantId', Value: env.TENANT_ID },
  { Name: 'AgentId', Value: env.AGENT_ID },
  { Name: 'Env', Value: env.ENV_NAME },
];
const QUALIFIED = /^target-[A-Za-z0-9-]{1,64}___[a-z0-9-]{3,64}$/;

async function emit(success) {
  await cw.send(new PutMetricDataCommand({
    Namespace: env.METRIC_NAMESPACE,
    MetricData: [{ MetricName: 'MCPProbeSuccess', Dimensions: dim, Value: success ? 1 : 0, Unit: 'Count' }],
  }));
}

// When CUSTOM_JWT auth is configured, obtain a client-credentials bearer
// token so the probe can authenticate against a CUSTOM_JWT gateway. Returns
// undefined when not configured (AWS_IAM gateway path).
async function getBearer() {
  if (!env.COGNITO_TOKEN_URL || !env.COGNITO_CLIENT_ID || !env.COGNITO_CLIENT_SECRET_ARN) return undefined;
  const sec = await sm.send(new GetSecretValueCommand({ SecretId: env.COGNITO_CLIENT_SECRET_ARN }));
  const clientSecret = sec.SecretString;
  const basic = Buffer.from(env.COGNITO_CLIENT_ID + ':' + clientSecret).toString('base64');
  const form = 'grant_type=client_credentials' + (env.COGNITO_SCOPE ? '&scope=' + encodeURIComponent(env.COGNITO_SCOPE) : '');
  const res = await fetch(env.COGNITO_TOKEN_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'Authorization': 'Basic ' + basic },
    body: form,
  });
  if (!res.ok) throw new Error('token endpoint ' + res.status);
  const j = await res.json();
  return j.access_token;
}

exports.handler = async () => {
  try {
    const headers = { 'Content-Type': 'application/json', [env.MCP_PROTOCOL_HEADER]: env.MCP_PROTOCOL_VERSION };
    const bearer = await getBearer();
    if (bearer) headers['Authorization'] = 'Bearer ' + bearer;
    const initRes = await fetch(env.GATEWAY_URL, {
      method: 'POST', headers,
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: env.MCP_PROTOCOL_VERSION } }),
    });
    if (!initRes.ok) { await emit(false); return { ok: false, stage: 'initialize', status: initRes.status }; }
    const listRes = await fetch(env.GATEWAY_URL, {
      method: 'POST', headers,
      body: JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/list' }),
    });
    if (!listRes.ok) { await emit(false); return { ok: false, stage: 'tools/list', status: listRes.status }; }
    const body = await listRes.json();
    const names = (body && body.result && body.result.tools || []).map((t) => t.name);
    const allQualified = names.length > 0 && names.every((n) => QUALIFIED.test(n));
    await emit(allQualified);
    return { ok: allQualified, names };
  } catch (e) {
    await emit(false);
    return { ok: false, error: String(e) };
  }
};
`;
