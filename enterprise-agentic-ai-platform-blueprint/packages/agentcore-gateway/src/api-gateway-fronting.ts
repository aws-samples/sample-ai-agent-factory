/**
 * ApiGatewayFronting — the spec §3.2 primary auth boundary.
 *
 * HTTP API v2 + Cognito JWT authorizer + WAF Web ACL.
 * The API Gateway delegates to AgentCore Gateway via VPC Link to a private
 * ALB (AgentCoreGatewayConstruct owns the ALB).
 *
 * Four spec statements make this the ONLY spec-aligned posture:
 *   §3.2.1 L3119-3121  "API gateway remains the primary API control point"
 *   §3.2.2 L3204       "API gateway is the control point. AgentCore Gateway
 *                       is the observability and configuration point."
 *   §3.2.5 L3387-3393  "API gateway remains the hard security control point"
 *   §3.2.7 L3523-3525  "Not the primary control point for authorisation
 *                       (that's the API gateway)"
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import {
  CfnApi,
  CfnAuthorizer,
  CfnStage,
  CfnVpcLink,
  CfnRoute,
  CfnIntegration,
} from 'aws-cdk-lib/aws-apigatewayv2';
import { ISecurityGroup, IVpc, Peer, Port, SecurityGroup, SubnetType } from 'aws-cdk-lib/aws-ec2';
import { CfnWebACL, CfnWebACLAssociation, CfnLoggingConfiguration } from 'aws-cdk-lib/aws-wafv2';
import { IUserPool } from 'aws-cdk-lib/aws-cognito';
import { Key } from 'aws-cdk-lib/aws-kms';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import { Effect, PolicyStatement, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export interface ApiGatewayFrontingProps {
  /** Workload VPC (for the VPC Link). */
  readonly vpc: IVpc;

  /**
   * Cognito User Pool backing the JWT authorizer. Callers pass the pool
   * created by the identity stack (Phase 3/5).
   */
  readonly userPool: IUserPool;

  /**
   * Client (audience) ID registered in the User Pool for this API.
   */
  readonly userPoolClientId: string;

  /**
   * Stage name. Default 'v1'.
   */
  readonly stageName?: string;

  /**
   * Target ALB listener ARN — the AgentCore Gateway private integration.
   * Typically supplied once AgentCoreGatewayConstruct has provisioned it.
   */
  readonly targetAlbListenerArn: string;

  /**
   * SG that the ALB uses. API Gateway VPC Link's ENI SG is allowed to
   * reach this SG on 443.
   */
  readonly targetAlbSecurityGroup: ISecurityGroup;

  /**
   * Rate-limit per 5-minute window per requester IP. Default 5000.
   * Turn this down for public-facing APIs.
   */
  readonly rateLimitPer5Min?: number;
}

export class ApiGatewayFronting extends Construct {
  readonly api: CfnApi;
  readonly stage: CfnStage;
  readonly authorizer: CfnAuthorizer;
  readonly vpcLink: CfnVpcLink;
  readonly webAcl: CfnWebACL;
  readonly vpcLinkSg: SecurityGroup;

  constructor(scope: Construct, id: string, props: ApiGatewayFrontingProps) {
    super(scope, id);

    const stack = Stack.of(this);
    const stageName = props.stageName ?? 'v1';

    // ---- HTTP API v2 ----
    this.api = new CfnApi(this, 'Api', {
      name: `agenticai-gateway-front-${stack.region}`,
      protocolType: 'HTTP',
      description:
        'Primary auth boundary for the agentic platform (spec §3.2). Fronts AgentCore Gateway via VPC Link.',
      disableExecuteApiEndpoint: false,
    });

    // ---- JWT authorizer (Cognito) ----
    this.authorizer = new CfnAuthorizer(this, 'JwtAuthorizer', {
      apiId: this.api.ref,
      name: 'CognitoJwtAuthorizer',
      authorizerType: 'JWT',
      identitySource: ['$request.header.Authorization'],
      jwtConfiguration: {
        issuer: `https://cognito-idp.${stack.region}.amazonaws.com/${props.userPool.userPoolId}`,
        audience: [props.userPoolClientId],
      },
    });

    // ---- VPC Link (private integration to target ALB) ----
    this.vpcLinkSg = new SecurityGroup(this, 'VpcLinkSg', {
      vpc: props.vpc,
      description: 'API Gateway VPC Link ENI SG; egress 443 to target ALB SG.',
      allowAllOutbound: false,
    });
    this.vpcLinkSg.addEgressRule(props.targetAlbSecurityGroup, Port.tcp(443), 'TLS to AgentCore Gateway ALB');
    props.targetAlbSecurityGroup.addIngressRule(this.vpcLinkSg, Port.tcp(443), 'From API GW VPC Link');

    this.vpcLink = new CfnVpcLink(this, 'VpcLink', {
      name: `agenticai-gateway-vpclink-${stack.region}`,
      subnetIds: props.vpc.selectSubnets({ subnetType: SubnetType.PRIVATE_ISOLATED }).subnetIds,
      securityGroupIds: [this.vpcLinkSg.securityGroupId],
    });

    // ---- Default integration to AgentCore Gateway ALB ----
    const integration = new CfnIntegration(this, 'AlbIntegration', {
      apiId: this.api.ref,
      integrationType: 'HTTP_PROXY',
      integrationMethod: 'ANY',
      integrationUri: props.targetAlbListenerArn,
      connectionType: 'VPC_LINK',
      connectionId: this.vpcLink.ref,
      payloadFormatVersion: '1.0',
      timeoutInMillis: 29000,
    });

    // Catch-all route, JWT-authorized.
    new CfnRoute(this, 'DefaultRoute', {
      apiId: this.api.ref,
      routeKey: 'ANY /{proxy+}',
      authorizationType: 'JWT',
      authorizerId: this.authorizer.ref,
      target: `integrations/${integration.ref}`,
    });

    // ---- Stage access log group (CMK-encrypted) ----
    const stageLogKey = new Key(this, 'StageLogKey', {
      alias: `alias/agenticai/apigw-fronting-${stack.region}`,
      description: 'CMK for API Gateway fronting access logs.',
      enableKeyRotation: true,
      pendingWindow: Duration.days(7),
      removalPolicy: RemovalPolicy.DESTROY,
    });
    stageLogKey.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowCWL',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal(`logs.${stack.region}.amazonaws.com`)],
        actions: ['kms:Encrypt*', 'kms:Decrypt*', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:Describe*'],
        resources: ['*'],
      }),
    );
    const stageLogGroup = new LogGroup(this, 'StageAccessLogs', {
      logGroupName: `/agenticai/apigw-fronting/${stageName}`,
      retention: RetentionDays.THREE_MONTHS,
      encryptionKey: stageLogKey,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // ---- Stage with access logging ----
    this.stage = new CfnStage(this, 'Stage', {
      apiId: this.api.ref,
      stageName,
      autoDeploy: true,
      defaultRouteSettings: {
        throttlingBurstLimit: 200,
        throttlingRateLimit: 100,
      },
      accessLogSettings: {
        destinationArn: stageLogGroup.logGroupArn,
        format: JSON.stringify({
          requestId: '$context.requestId',
          ip: '$context.identity.sourceIp',
          requestTime: '$context.requestTime',
          httpMethod: '$context.httpMethod',
          routeKey: '$context.routeKey',
          status: '$context.status',
          protocol: '$context.protocol',
          responseLength: '$context.responseLength',
          authorizer: '$context.authorizer.error',
        }),
      },
    });

    // ---- WAF Web ACL attached to the API stage ----
    // Using the `REGIONAL` scope because HTTP API v2 stage is a regional resource.
    this.webAcl = new CfnWebACL(this, 'WebAcl', {
      name: `agenticai-gateway-waf-${stack.region}`,
      defaultAction: { allow: {} },
      scope: 'REGIONAL',
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: 'AgenticAIGatewayWaf',
        sampledRequestsEnabled: true,
      },
      rules: [
        {
          name: 'AWSManagedRulesCommonRuleSet',
          priority: 0,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: 'AWS',
              name: 'AWSManagedRulesCommonRuleSet',
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: 'AWSManagedRulesCommonRuleSet',
            sampledRequestsEnabled: true,
          },
        },
        {
          name: 'AWSManagedRulesKnownBadInputsRuleSet',
          priority: 1,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: 'AWS',
              name: 'AWSManagedRulesKnownBadInputsRuleSet',
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: 'AWSManagedRulesKnownBadInputsRuleSet',
            sampledRequestsEnabled: true,
          },
        },
        {
          name: 'RateLimit',
          priority: 10,
          action: { block: {} },
          statement: {
            rateBasedStatement: {
              limit: props.rateLimitPer5Min ?? 5000,
              aggregateKeyType: 'IP',
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: 'RateLimit',
            sampledRequestsEnabled: true,
          },
        },
      ],
    });

    new CfnWebACLAssociation(this, 'WebAclAssociation', {
      resourceArn: `arn:aws:apigateway:${stack.region}::/apis/${this.api.ref}/stages/${stageName}`,
      webAclArn: this.webAcl.attrArn,
    });

    // ---- WAF logging to a dedicated CWL log group ----
    // The log group name MUST be prefixed `aws-waf-logs-` per AWS requirement.
    const wafLogGroup = new LogGroup(this, 'WafLogs', {
      logGroupName: `aws-waf-logs-agenticai-gateway-${stack.region}`,
      retention: RetentionDays.THREE_MONTHS,
      encryptionKey: stageLogKey,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    new CfnLoggingConfiguration(this, 'WafLogging', {
      logDestinationConfigs: [wafLogGroup.logGroupArn],
      resourceArn: this.webAcl.attrArn,
      redactedFields: [
        { singleHeader: { Name: 'authorization' } },
        { singleHeader: { Name: 'cookie' } },
      ],
    });

    void Duration;
    void Peer;
  }
}
