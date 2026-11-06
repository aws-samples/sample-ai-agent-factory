/**
 * Unit tests for @agenticai/omni (synth-time contracts).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App, Stack } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';

import {
  BLUEPRINT_CENTRALIZED_LOG_GROUP_PATTERNS,
  OmniCentralizationRuleConstruct,
  OmniDashboardConstruct,
  OmniHubSpaceConstruct,
  OmniOrganizationDomainConstruct,
  assertOmniDomainName,
  assertOmniHostingRegion,
  TransactionSearchConstruct,
} from './index';

const HUB = { account: '222222222222', region: 'eu-west-1' };
const MGMT = { account: '111111111111', region: 'eu-west-1' };
const IDC = 'arn:aws:sso:::instance/ssoins-1234567890abcdef';

function hubStack(): Stack {
  return new Stack(new App(), 'Hub', { env: HUB });
}

describe('naming rules', () => {
  it('accepts a valid domain name and refuses reserved or malformed ones', () => {
    expect(() => assertOmniDomainName('agenticai-fleet')).not.toThrow();
    expect(() => assertOmniDomainName('aws-fleet')).toThrow(/reserved prefix/);
    expect(() => assertOmniDomainName('Fleet')).toThrow(/invalid/);
    expect(() => assertOmniDomainName('fleet--x')).toThrow(/invalid/);
    expect(() => assertOmniDomainName('eu-west-1')).toThrow(/Region/);
  });

  it('fails closed on Regions that cannot host a space (ADR-19)', () => {
    expect(() => assertOmniHostingRegion('eu-west-1')).not.toThrow();
    expect(() => assertOmniHostingRegion('eu-central-1')).not.toThrow();
    expect(() => assertOmniHostingRegion('eu-west-2')).not.toThrow();
    expect(() => assertOmniHostingRegion('eu-west-3')).toThrow(/cannot host a space in eu-west-3/);
  });
});

describe('OmniOrganizationDomainConstruct', () => {
  it('emits an organization domain bound to Identity Center with a customer-provided access role', () => {
    const stack = new Stack(new App(), 'Mgmt', { env: MGMT });
    new OmniOrganizationDomainConstruct(stack, 'Domain', {
      domainName: 'agenticai-fleet',
      identityCenterInstanceArn: IDC,
      tags: { environment: 'shared' },
    });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::CloudWatch::DomainForOrganization', 1);
    t.hasResourceProperties('AWS::CloudWatch::DomainForOrganization', {
      Name: 'agenticai-fleet',
      IdentityProviders: ['IDC'],
      IdentityProviderConfiguration: {
        IdentityCenterConfiguration: { IdentityCenterInstanceArn: IDC },
      },
      Tags: [{ Key: 'environment', Value: 'shared' }],
    });
    t.hasResourceProperties('AWS::IAM::Role', {
      RoleName: 'CloudWatchOmniDomainAccessRole',
      AssumeRolePolicyDocument: {
        Statement: [
          Match.objectLike({
            Principal: { Service: 'cloudwatch.amazonaws.com' },
            Condition: Match.objectLike({ StringEquals: { 'aws:SourceAccount': '111111111111' } }),
          }),
        ],
      },
    });
  });

  it('requires an Identity Center instance when IDC is the provider', () => {
    const stack = new Stack(new App(), 'Mgmt', { env: MGMT });
    expect(
      () => new OmniOrganizationDomainConstruct(stack, 'Domain', { domainName: 'agenticai-fleet' }),
    ).toThrow(/identityCenterInstanceArn is required/);
  });
});

describe('OmniCentralizationRuleConstruct', () => {
  const base = {
    ruleName: 'agenticai-fleet-eu-west-1',
    sourceScope: "OrganizationalUnitId IN ('ou-aaaa-11111111','ou-aaaa-22222222')",
    sourceRegions: ['eu-west-1', 'eu-west-3'],
    destinationAccountId: HUB.account,
    destinationRegion: 'eu-west-1',
    destinationLogsKmsKeyArn: 'arn:aws:kms:eu-west-1:222222222222:key/11111111-2222-3333-4444-555555555555',
  };

  it('centralizes only blueprint log groups and all metrics, encrypting every destination group', () => {
    const stack = hubStack();
    new OmniCentralizationRuleConstruct(stack, 'Rule', { ...base, organizationId: 'o-abc123def4' });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::ObservabilityAdmin::OrganizationCentralizationRule', 1);
    const rule = Object.values(t.findResources('AWS::ObservabilityAdmin::OrganizationCentralizationRule'))[0]
      .Properties as any;
    expect(rule.RuleName).toBe('agenticai-fleet-eu-west-1');
    expect(rule.Rule.Source.Regions).toEqual(['eu-west-1', 'eu-west-3']);
    // Context Graph stays off in CloudFormation until the published schema lists the property.
    expect(rule.Rule.Source.SourceContextGraphConfiguration).toBeUndefined();
    expect(rule.Rule.Source.SourceMetricsConfiguration).toEqual({ MetricsSelectionCriteria: '*' });
    const selection: string = rule.Rule.Source.SourceLogsConfiguration.LogGroupSelectionCriteria;
    for (const pattern of BLUEPRINT_CENTRALIZED_LOG_GROUP_PATTERNS) {
      expect(selection).toContain(pattern);
    }
    expect(selection).not.toMatch(/LogGroupName = '\*'/);
    expect(rule.Rule.Destination.Account).toBe(HUB.account);
    expect(rule.Rule.Destination.DestinationLogsConfiguration.LogsEncryptionConfiguration).toEqual({
      EncryptionStrategy: 'CUSTOMER_MANAGED',
      EncryptionScope: 'NEW_DESTINATION_LOG_GROUPS',
      EncryptionConflictResolutionStrategy: 'SKIP',
      KmsKeyArn: base.destinationLogsKmsKeyArn,
    });
    expect(rule.Rule.Destination.DestinationLogsConfiguration.LogGroupNameConfiguration.LogGroupNamePattern).toBe(
      '/centralized/${source.accountId}/${source.region}${source.logGroup}',
    );
    // Tag propagation role trusts the destination-account centralization SLR with the Organization id as ExternalId.
    t.hasResourceProperties('AWS::IAM::Role', {
      AssumeRolePolicyDocument: {
        Statement: [
          Match.objectLike({
            Condition: { StringEquals: { 'sts:ExternalId': 'o-abc123def4' } },
            Principal: { AWS: Match.anyValue() },
          }),
        ],
      },
    });
    const roleJson = JSON.stringify(t.findResources('AWS::IAM::Role'));
    expect(roleJson).toContain('AWSServiceRoleForObservabilityAdmin_LogsCentralization');
    expect(roleJson).toContain(`:iam::${HUB.account}:role/aws-service-role/logs-centralization.observabilityadmin.amazonaws.com/`);
    expect(rule.Rule.Destination.DestinationLogsConfiguration.TagPropagationConfiguration).toEqual({
      DestinationRoleArn: expect.anything(),
      TagConflictResolutionStrategy: 'UPDATE_SYNC',
    });
  });

  it('emits the Context Graph opt-in only when explicitly requested', () => {
    const stack = hubStack();
    new OmniCentralizationRuleConstruct(stack, 'Rule', { ...base, centralizeContextGraph: true });
    const rule = Object.values(
      Template.fromStack(stack).findResources('AWS::ObservabilityAdmin::OrganizationCentralizationRule'),
    )[0].Properties as any;
    expect(rule.Rule.Source.SourceContextGraphConfiguration).toEqual({});
  });

  it('refuses an implicit all-log-groups selection and a tag-propagation role in the wrong account', () => {
    expect(() => new OmniCentralizationRuleConstruct(hubStack(), 'R', { ...base, logGroupPatterns: [] })).toThrow(
      /never centralize "all log groups"/,
    );
    const mgmt = new Stack(new App(), 'Mgmt', { env: MGMT });
    expect(
      () => new OmniCentralizationRuleConstruct(mgmt, 'R', { ...base, organizationId: 'o-abc123def4' }),
    ).toThrow(/must live in the destination account/);
  });

  it('requires a backup key when a backup Region is combined with a destination CMK', () => {
    expect(
      () => new OmniCentralizationRuleConstruct(hubStack(), 'R', { ...base, backupRegion: 'eu-central-1' }),
    ).toThrow(/backupLogsKmsKeyArn is required/);
  });
});

describe('OmniHubSpaceConstruct', () => {
  function synth(overrides: Partial<ConstructorParameters<typeof OmniHubSpaceConstruct>[2]> = {}) {
    const stack = hubStack();
    const hub = new OmniHubSpaceConstruct(stack, 'Hub', {
      spaceName: 'agenticai-fleet-eu-west-1',
      domainId: 'd-1a2b3c4d5e',
      tags: { 'cost-centre': 'platform' },
      ...overrides,
    });
    return { stack, hub, t: Template.fromStack(stack) };
  }

  it('emits a CMK-encrypted space bound to the operator and evaluation roles, with Transaction Search on', () => {
    const { t } = synth();
    t.resourceCountIs('AWS::CloudWatch::Space', 1);
    t.hasResourceProperties('AWS::CloudWatch::Space', {
      Name: 'agenticai-fleet-eu-west-1',
      DomainId: 'd-1a2b3c4d5e',
      EncryptionConfiguration: Match.objectLike({ EncryptionStrategy: 'CUSTOMER_MANAGED' }),
      Tags: Match.arrayWith([{ Key: 'cost-centre', Value: 'platform' }]),
    });
    t.resourceCountIs('AWS::XRay::TransactionSearchConfig', 1);
    t.hasResourceProperties('AWS::XRay::TransactionSearchConfig', { IndexingPercentage: 1 });
  });

  it('keeps judge inference OFF by default and applies the space access policy as the boundary (ADR-16)', () => {
    const { t } = synth();
    const roles = t.findResources('AWS::IAM::Role');
    const operator = Object.values(roles).find((r) => (r.Properties as any).RoleName === 'CloudWatchOmniOperatorRole');
    expect(operator).toBeDefined();
    const managed = JSON.stringify((operator!.Properties as any).ManagedPolicyArns);
    expect(managed).toContain('CloudWatchOmniSpaceAccessPolicy');
    expect(managed).not.toContain('CloudWatchOmniModelInferencePolicy');
    expect(JSON.stringify((operator!.Properties as any).PermissionsBoundary)).toContain('CloudWatchOmniSpaceAccessPolicy');
    // The evaluation role has no Bedrock invoke statement by default.
    const evaluation = Object.values(roles).find((r) =>
      String((r.Properties as any).RoleName).startsWith('AgentCoreEvaluationRole-'),
    );
    expect(evaluation).toBeDefined();
    expect(JSON.stringify(evaluation!.Properties)).not.toContain('bedrock:InvokeModel');
  });

  it('refuses judge model ARNs unless model inference is explicitly allowed', () => {
    expect(() =>
      synth({ judgeModelArns: ['arn:aws:bedrock:eu-west-1::foundation-model/anthropic.claude-haiku-4-5'] }),
    ).toThrow(/requires allowModelInference=true/);
  });

  it('grants judge inference only on the exact allow-listed models when ADR-16 option 2 is chosen', () => {
    const judge = 'arn:aws:bedrock:eu-west-1::foundation-model/anthropic.claude-haiku-4-5';
    const { t } = synth({ allowModelInference: true, judgeModelArns: [judge] });
    const roles = t.findResources('AWS::IAM::Role');
    const operator = Object.values(roles).find((r) => (r.Properties as any).RoleName === 'CloudWatchOmniOperatorRole');
    expect(JSON.stringify((operator!.Properties as any).ManagedPolicyArns)).toContain('CloudWatchOmniModelInferencePolicy');
    const evaluation = Object.values(roles).find((r) =>
      String((r.Properties as any).RoleName).startsWith('AgentCoreEvaluationRole-'),
    );
    const policy = JSON.stringify((evaluation!.Properties as any).Policies);
    expect(policy).toContain('bedrock:InvokeModel');
    expect(policy).toContain(judge);
    expect(policy).not.toContain('foundation-model/*');
  });

  it('encrypts the space with the documented key policy and tags both keys for the services that read them', () => {
    const { t } = synth();
    const keys = t.findResources('AWS::KMS::Key');
    expect(Object.keys(keys)).toHaveLength(2);
    const serialized = Object.values(keys).map((k) => JSON.stringify(k.Properties));
    const spaceKey = serialized.find((s) => s.includes('AllowCloudWatchSpaceEncryptionService'));
    expect(spaceKey).toBeDefined();
    for (const sid of [
      'AllowCloudWatchDatasetEncryption',
      'AllowOmniIntelligenceBranchKey',
      'AllowCallerDatasetForwarding',
      'AllowOperatorRoleDecrypt',
    ]) {
      expect(spaceKey).toContain(sid);
    }
    expect(spaceKey).toContain('"Key":"cw-omni"');
    const logsKey = serialized.find((s) => s.includes('AllowCloudWatchLogsUse'));
    expect(logsKey).toContain('"Key":"LogsManaged"');
  });

  it('gives the operator role the documented trust: SourceAccount only, with session-tag, source-identity and context actions', () => {
    const { t } = synth();
    const operator = Object.values(t.findResources('AWS::IAM::Role')).find(
      (r) => (r.Properties as any).RoleName === 'CloudWatchOmniOperatorRole',
    )!.Properties as any;
    const statements = operator.AssumeRolePolicyDocument.Statement as any[];
    const actions = statements.flatMap((st) => (Array.isArray(st.Action) ? st.Action : [st.Action]));
    expect(actions).toEqual(expect.arrayContaining(['sts:AssumeRole', 'sts:TagSession', 'sts:SetSourceIdentity', 'sts:SetContext']));
    for (const st of statements) {
      expect(st.Principal).toEqual({ Service: 'cloudwatch.amazonaws.com' });
      expect(st.Condition).toEqual({ StringEquals: { 'aws:SourceAccount': '222222222222' } });
    }
    // A space/* SourceArn condition breaks GetSpaceCredentialsForOrganization (observed live).
    expect(JSON.stringify(operator.AssumeRolePolicyDocument)).not.toContain('space/*');
  });

  it('scopes the Dataset integration role to the hub log-group prefixes', () => {
    const { t } = synth();
    t.hasResourceProperties('AWS::IAM::Role', {
      RoleName: 'CloudWatchOmniDatasetIntegrationRole-eu-west-1',
      AssumeRolePolicyDocument: {
        Statement: [
          Match.objectLike({
            Principal: { Service: 'logs.amazonaws.com' },
            Condition: Match.objectLike({
              StringEquals: { 'aws:SourceAccount': '222222222222' },
              ArnLike: { 'aws:SourceArn': Match.anyValue() },
            }),
          }),
        ],
      },
    });
    expect(JSON.stringify(t.toJSON())).toContain('dataset-integration/default');
    const roles = t.findResources('AWS::IAM::Role');
    const integration = Object.values(roles).find(
      (r) => (r.Properties as any).RoleName === 'CloudWatchOmniDatasetIntegrationRole-eu-west-1',
    );
    const policy = JSON.stringify((integration!.Properties as any).Policies);
    expect(policy).toContain('log-group:/centralized/*');
    expect(policy).toContain('logs:IntegrateWithDataset');
    expect(policy).not.toContain(':log-group:*"');
  });

  it('fails closed on a Region without Omni hosting', () => {
    const stack = new Stack(new App(), 'Paris', { env: { account: HUB.account, region: 'eu-west-3' } });
    expect(
      () => new OmniHubSpaceConstruct(stack, 'Hub', { spaceName: 'agenticai-fleet-paris', domainId: 'd-1a2b3c4d5e' }),
    ).toThrow(/cannot host a space in eu-west-3/);
  });
});

describe('OmniDashboardConstruct', () => {
  it('emits an OmniDashboard with a serialised body and refuses an empty one', () => {
    const stack = hubStack();
    new OmniDashboardConstruct(stack, 'Dash', {
      name: 'agenticai-fleet',
      spaceId: 'a1b2c3d4-5e6f-4a3b-8c9d-0e1f2a3b4c5d',
      body: { widgets: [] },
    });
    Template.fromStack(stack).hasResourceProperties('AWS::CloudWatch::OmniDashboard', {
      Name: 'agenticai-fleet',
      Body: '{"widgets":[]}',
    });
    expect(
      () => new OmniDashboardConstruct(hubStack(), 'D', { name: 'x', spaceId: 's', body: {} }),
    ).toThrow(/refuses an empty dashboard body/);
  });
});


describe('TransactionSearchConstruct', () => {
  it('creates the X-Ray resource policy before the Transaction Search config (live 403 without it)', () => {
    const stack = new Stack(new App(), 'Ts', { env: { account: '222222222222', region: 'eu-west-1' } });
    new TransactionSearchConstruct(stack, 'TransactionSearch', { indexingPercentage: 5 });
    const t = Template.fromStack(stack);
    t.resourceCountIs('AWS::Logs::ResourcePolicy', 1);
    t.resourceCountIs('AWS::XRay::TransactionSearchConfig', 1);
    const policy = Object.values(t.findResources('AWS::Logs::ResourcePolicy'))[0].Properties as any;
    expect(policy.PolicyName).toBe('AgenticAI-TransactionSearch-Ts');
    // The partition is a deploy-time token, so the document renders as Fn::Join; resolve it.
    const parts = policy.PolicyDocument['Fn::Join'][1] as unknown[];
    const doc = JSON.parse(parts.map((p) => (typeof p === 'string' ? p : 'aws')).join(''));
    expect(doc.Statement[0].Principal).toEqual({ Service: 'xray.amazonaws.com' });
    expect(doc.Statement[0].Action).toBe('logs:PutLogEvents');
    expect(doc.Statement[0].Resource).toEqual([
      'arn:aws:logs:eu-west-1:222222222222:log-group:aws/spans:*',
      'arn:aws:logs:eu-west-1:222222222222:log-group:/aws/application-signals/data:*',
    ]);
    expect(doc.Statement[0].Condition.StringEquals['aws:SourceAccount']).toBe('222222222222');
    const config = Object.values(t.findResources('AWS::XRay::TransactionSearchConfig'))[0] as any;
    expect(config.Properties.IndexingPercentage).toBe(5);
    expect(config.DependsOn).toEqual(expect.arrayContaining([expect.stringMatching(/XRayLogsPolicy/)]));
  });

  it('refuses an out-of-range indexing percentage', () => {
    const stack = new Stack(new App(), 'Ts2', { env: { account: '222222222222', region: 'eu-west-1' } });
    expect(() => new TransactionSearchConstruct(stack, 'Bad', { indexingPercentage: 101 })).toThrow(/between 0 and 100/);
  });
});
