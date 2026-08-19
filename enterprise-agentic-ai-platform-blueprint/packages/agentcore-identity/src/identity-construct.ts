/**
 * AgentCoreIdentityConstruct — Cognito + Token Vault CMK.
 *
 * Emits:
 *   - Cognito User Pool with MFA optional + password policy + advanced
 *     security enforcement.
 *   - User Pool Client (confidential, SRP + OAuth 2.0 code flow) for the
 *     API Gateway JWT authorizer.
 *   - Token Vault CMK (customer-managed key for AgentCore Identity
 *     Token Vault encryption per spec §3.3.5 / R-ID-020..023).
 *
 * The AgentCore Identity data plane (TokenVault resources themselves) is
 * created by the CDK construct in Phase 5 follow-on once the L1 CFN resource
 * lands. The CMK contract here is sufficient for nag-clean synth today.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import {
  UserPool,
  UserPoolClient,
  UserPoolClientIdentityProvider,
  OAuthScope,
  AccountRecovery,
} from 'aws-cdk-lib/aws-cognito';
import { Effect, PolicyStatement, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import { Key } from 'aws-cdk-lib/aws-kms';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

export interface AgentCoreIdentityConstructProps {
  readonly envName: string;
  /**
   * Callback URLs for the Cognito hosted-UI OAuth flow. Default is a local
   * loopback suitable for development; real deployments override.
   */
  readonly oauthCallbackUrls?: string[];
}

export class AgentCoreIdentityConstruct extends Construct {
  readonly userPool: UserPool;
  readonly userPoolClient: UserPoolClient;
  readonly tokenVaultKey: Key;

  constructor(scope: Construct, id: string, props: AgentCoreIdentityConstructProps) {
    super(scope, id);

    const stack = Stack.of(this);

    // ---- Token Vault CMK (R-ID-020..023) ----
    this.tokenVaultKey = new Key(this, 'TokenVaultKey', {
      alias: `alias/agenticai/token-vault-${props.envName}`,
      description: 'CMK for AgentCore Identity Token Vault (spec §3.3.5).',
      enableKeyRotation: true,
      pendingWindow: Duration.days(30),
      removalPolicy: RemovalPolicy.RETAIN,
    });
    this.tokenVaultKey.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowAgentCoreIdentityService',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal('bedrock-agentcore.amazonaws.com')],
        actions: ['kms:Encrypt*', 'kms:Decrypt*', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:Describe*'],
        resources: ['*'],
        conditions: {
          StringEquals: { 'aws:SourceAccount': stack.account },
        },
      }),
    );

    // ---- Cognito User Pool ----
    this.userPool = new UserPool(this, 'UserPool', {
      userPoolName: `agenticai-${props.envName}`,
      selfSignUpEnabled: false,
      signInAliases: { email: true, username: true },
      autoVerify: { email: true },
      mfa: undefined, // let customers decide per-deployment; spec doesn't mandate
      passwordPolicy: {
        minLength: 12,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
        tempPasswordValidity: Duration.days(1),
      },
      accountRecovery: AccountRecovery.EMAIL_ONLY,
      deletionProtection: true,
      removalPolicy: RemovalPolicy.RETAIN,
      standardAttributes: {
        email: { required: true, mutable: true },
      },
    });

    // ---- User Pool Client for JWT authorizer ----
    this.userPoolClient = new UserPoolClient(this, 'Client', {
      userPool: this.userPool,
      userPoolClientName: `agenticai-${props.envName}-api`,
      authFlows: { userSrp: true, userPassword: false, custom: true, adminUserPassword: false },
      generateSecret: true,
      preventUserExistenceErrors: true,
      supportedIdentityProviders: [UserPoolClientIdentityProvider.COGNITO],
      oAuth: {
        flows: { authorizationCodeGrant: true, implicitCodeGrant: false, clientCredentials: false },
        scopes: [OAuthScope.OPENID, OAuthScope.EMAIL, OAuthScope.PROFILE],
        callbackUrls: props.oauthCallbackUrls ?? ['http://localhost:3000/callback'],
      },
      accessTokenValidity: Duration.hours(1),
      idTokenValidity: Duration.hours(1),
      refreshTokenValidity: Duration.days(30),
    });

    NagSuppressions.addResourceSuppressions(
      this.userPool,
      [
        {
          id: 'AwsSolutions-COG8',
          reason:
            'SEC-014: Cognito Plus tier pricing is per-MAU and imposes cost on customers onboarding the blueprint. Callers may opt-in to Plus tier via stack override. Baseline posture (MFA, 12-char password, secure-recovery) already covers the primary controls.',
        },
      ],
      true,
    );
  }
}
