/**
 * Phase 22 conformance — pipeline-owned GA Agent Registry blue-green producer.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import {
  PLATFORM_TOOL_CATALOGUE,
  type ToolSpec,
} from '@agenticai/platform-tool-catalogue';

import { RegistryStack } from '../../apps/platform-account/lib/registry-stack';
import { buildGaToolGovernanceDocument } from '../../packages/agent-registry/src';

const PLATFORM_ACCOUNT_ID = '222222222222';
const WORKLOAD_ACCOUNT_IDS = ['333333333333', '444444444444'];
const REQUIRED_TAGS = {
  'application-id': 'platform-registry',
  'agent-id': 'shared',
  'tenant-id': 'shared',
  'cost-centre': 'platform',
  environment: 'nonprod',
};

type SynthTags = Array<{ Key: string; Value: string }> | Record<string, string>;

function tagsToRecord(tags: SynthTags = []): Record<string, string> {
  if (Array.isArray(tags)) {
    return Object.fromEntries(tags.map(({ Key, Value }) => [Key, Value]));
  }
  return tags;
}

function partitionArn(suffix: string): Record<string, unknown> {
  return {
    'Fn::Join': ['', ['arn:', { Ref: 'AWS::Partition' }, suffix]],
  };
}

function synth(envName: 'nonprod' | 'prod' = 'nonprod'): Template {
  const app = new App();
  const stack = new RegistryStack(app, `Registry-${envName}`, {
    env: { account: PLATFORM_ACCOUNT_ID, region: 'us-west-2' },
    envName,
    workloadAccountIds: WORKLOAD_ACCOUNT_IDS,
    applicationId: 'platform-registry',
    agentId: 'shared',
    tenantId: 'shared',
    costCentre: 'platform',
  });
  return Template.fromStack(stack);
}

function singleResource(template: Template, type: string): Record<string, any> {
  const resources = template.findResources(type);
  expect(Object.keys(resources)).toHaveLength(1);
  return Object.values(resources)[0] as Record<string, any>;
}

describe('Phase 22 — GA Registry producer remains additive', () => {
  it('preserves the existing DynamoDB rollback-path logical IDs and table names', () => {
    const template = synth();
    const tables = template.findResources('AWS::DynamoDB::Table');

    expect(Object.keys(tables).sort()).toEqual([
      'RegistryAgentTable7EE2A0ED',
      'RegistryToolTable849A77D3',
    ]);
    expect(
      Object.values(tables).map((table: any) => table.Properties.TableName).sort(),
    ).toEqual([
      'agenticai-registry-agents-nonprod',
      'agenticai-registry-tools-nonprod',
    ]);
  });

  it('adds one native IAM-authorized Registry with RetainExceptOnCreate semantics', () => {
    const registry = singleResource(synth(), 'AWS::AgentRegistry::Registry');

    expect(registry.Properties).toMatchObject({
      Name: 'agenticai-platform-nonprod-v1',
      AuthorizerType: 'AWS_IAM',
      ApprovalConfiguration: { AutoApprovalRules: ['APPROVE_ALL'] },
      Tags: expect.any(Array),
    });
    expect(tagsToRecord(registry.Properties.Tags)).toEqual(REQUIRED_TAGS);
    expect(registry.DeletionPolicy).toBe('RetainExceptOnCreate');
    expect(registry.UpdateReplacePolicy).toBe('Retain');
  });

  it('creates one tagged CUSTOM governance record per catalogue tool', () => {
    const records = synth().findResources('AWS::AgentRegistry::RegistryRecord');

    expect(Object.keys(records)).toHaveLength(Object.keys(PLATFORM_TOOL_CATALOGUE).length);
    for (const record of Object.values(records) as Array<Record<string, any>>) {
      expect(record.Properties.RecordType).toBe('CUSTOM');
      expect(record.Properties.RecordVersion).toBe('1.0.0');
      expect(record.Properties.RegistryId).toEqual(
        expect.objectContaining({ 'Fn::GetAtt': expect.any(Array) }),
      );
      expect(tagsToRecord(record.Properties.Tags)).toEqual(REQUIRED_TAGS);
      expect(record.DeletionPolicy).toBe('RetainExceptOnCreate');
      expect(record.UpdateReplacePolicy).toBe('Retain');

      const governance = JSON.parse(record.Properties.Descriptors.Custom.Data);
      const source = PLATFORM_TOOL_CATALOGUE[governance.toolId];
      expect(source).toBeDefined();
      expect(governance).toMatchObject({
        schemaVersion: 'agenticai.tool-governance/1.0',
        catalogueVersion: '1',
        toolId: source.toolId,
        desiredApprovalStatus: source.approvalStatus,
        target: {
          type: source.toolType ?? 'lambda',
          arn: source.targetArn.replace('${PLATFORM_ACCOUNT_ID}', PLATFORM_ACCOUNT_ID),
        },
        authorization: {
          defaultDecision: 'DENY',
          cedarPolicy: source.cedarPolicy,
          allowedSubjects: [],
          allowedGroups: source.allowedGroups ?? [],
          combination: source.allowedGroups ? 'GROUP_ONLY' : 'AUTHENTICATED',
        },
        ownership: {
          ownerTeam: source.ownerTeam,
          costCentre: source.costCentre,
        },
      });
    }
  });

  it('does not emit the deprecated preview Registry custom resources', () => {
    const rendered = JSON.stringify(synth().toJSON());

    expect(rendered).not.toContain('Custom::BedrockAgentCoreRegistry');
    expect(rendered).not.toContain('Custom::BedrockAgentCoreRegistryRecord');
  });
});

describe('Phase 22 — Registry reader trust and permissions', () => {
  it('trusts only configured Workstream validator roles with exact ExternalId', () => {
    const role = singleResource(synth(), 'AWS::IAM::Role');
    const statement = role.Properties.AssumeRolePolicyDocument.Statement[0];

    expect(role.Properties.RoleName).toBe('AgenticAI-RegistryReader-nonprod');
    expect(statement).toEqual({
      Sid: 'AllowWorkstreamRegistryValidators',
      Effect: 'Allow',
      Principal: {
        AWS: WORKLOAD_ACCOUNT_IDS.map((accountId) =>
          partitionArn(`:iam::${accountId}:root`),
        ),
      },
      Action: 'sts:AssumeRole',
      Condition: {
        StringEquals: {
          'sts:ExternalId': `agenticai-registry-v1-nonprod-${PLATFORM_ACCOUNT_ID}`,
        },
        StringLike: {
          'aws:PrincipalArn': WORKLOAD_ACCOUNT_IDS.map((accountId) =>
            partitionArn(
              `:iam::${accountId}:role/AgenticAI-D03-*-RegistryValidator`,
            ),
          ),
          'sts:RoleSessionName': 'registry-*',
        },
      },
    });
    expect(tagsToRecord(role.Properties.Tags)).toEqual(REQUIRED_TAGS);
  });

  it('pins every GA read action to its required registry resource type', () => {
    const policy = singleResource(synth(), 'AWS::IAM::ManagedPolicy');
    const statements = Object.fromEntries(
      policy.Properties.PolicyDocument.Statement.map((statement: any) => [
        statement.Sid,
        statement,
      ]),
    );
    const registryArn = {
      'Fn::GetAtt': ['GaRegistry07EC8B10', 'RegistryArn'],
    };
    const recordArn = {
      'Fn::Join': ['', [registryArn, '/record/*']],
    };

    expect(Object.keys(statements).sort()).toEqual([
      'DiscoverApprovedRegistryRecords',
      'ReadRegistryMetadata',
      'ReadRegistryRecords',
    ]);
    expect(statements.ReadRegistryMetadata).toEqual({
      Sid: 'ReadRegistryMetadata',
      Effect: 'Allow',
      Action: [
        'agent-registry:GetRegistry',
        'agent-registry:ListRegistryRecords',
      ],
      Resource: registryArn,
    });
    expect(statements.ReadRegistryRecords).toEqual({
      Sid: 'ReadRegistryRecords',
      Effect: 'Allow',
      Action: [
        'agent-registry:GetRegistryRecord',
        'agent-registry:GetDiscoverableRegistryRecord',
      ],
      Resource: recordArn,
    });
    expect(statements.DiscoverApprovedRegistryRecords).toEqual({
      Sid: 'DiscoverApprovedRegistryRecords',
      Effect: 'Allow',
      Action: [
        'agent-registry:ListDiscoverableRegistryRecords',
        'agent-registry:SearchDiscoverableRegistryRecords',
      ],
      Resource: registryArn,
    });

    const actions = policy.Properties.PolicyDocument.Statement.flatMap(
      (statement: any) => statement.Action,
    ).sort();
    expect(actions).toEqual(
      [
        'agent-registry:GetDiscoverableRegistryRecord',
        'agent-registry:GetRegistry',
        'agent-registry:GetRegistryRecord',
        'agent-registry:ListDiscoverableRegistryRecords',
        'agent-registry:ListRegistryRecords',
        'agent-registry:SearchDiscoverableRegistryRecords',
      ].sort(),
    );
    const renderedResources = JSON.stringify(
      policy.Properties.PolicyDocument.Statement.map(
        (statement: any) => statement.Resource,
      ),
    );
    expect(renderedResources.match(/\*/g)).toHaveLength(1);
    expect(JSON.stringify(policy)).not.toContain('bedrock-agentcore:');
    expect(JSON.stringify(policy)).not.toContain('BatchGetDiscoverableRegistryRecord');
  });

  it('records a policy-level SEC-030 suppression for the sole record-id wildcard', () => {
    const policy = singleResource(synth(), 'AWS::IAM::ManagedPolicy');

    expect(policy.Metadata.cdk_nag.rules_to_suppress).toEqual([
      {
        id: 'AwsSolutions-IAM5',
        reason: expect.stringMatching(/^SEC-030:.*exact action\/resource pair\.$/),
      },
    ]);
  });
});

describe('Phase 22 — versioned late-binding contract', () => {
  it('publishes Registry, reader, ExternalId, and every record ID through SSM', () => {
    const parameters = synth().findResources('AWS::SSM::Parameter');
    const names = Object.values(parameters)
      .map((parameter: any) => parameter.Properties.Name)
      .sort();
    const expectedRecordNames = Object.keys(PLATFORM_TOOL_CATALOGUE).map(
      (toolId) => `/agenticai/registry/v1/nonprod/records/${toolId}/id`,
    );

    expect(names).toEqual(
      [
        '/agenticai/registry/v1/nonprod/arn',
        '/agenticai/registry/v1/nonprod/id',
        '/agenticai/registry/v1/nonprod/reader-external-id',
        '/agenticai/registry/v1/nonprod/reader-role-arn',
        ...expectedRecordNames,
      ].sort(),
    );
    for (const parameter of Object.values(parameters) as Array<Record<string, any>>) {
      expect(tagsToRecord(parameter.Properties.Tags)).toEqual(REQUIRED_TAGS);
      expect(parameter.DeletionPolicy).toBe('RetainExceptOnCreate');
      expect(parameter.UpdateReplacePolicy).toBe('Retain');
    }
  });

  it('keeps nonprod and prod names distinct in a shared Platform account', () => {
    const nonprod = synth('nonprod');
    const prod = synth('prod');
    const nonprodRegistry = singleResource(nonprod, 'AWS::AgentRegistry::Registry');
    const prodRegistry = singleResource(prod, 'AWS::AgentRegistry::Registry');
    const nonprodRole = singleResource(nonprod, 'AWS::IAM::Role');
    const prodRole = singleResource(prod, 'AWS::IAM::Role');
    const nonprodParameters = Object.values(
      nonprod.findResources('AWS::SSM::Parameter'),
    ).map((parameter: any) => parameter.Properties.Name);
    const prodParameters = Object.values(
      prod.findResources('AWS::SSM::Parameter'),
    ).map((parameter: any) => parameter.Properties.Name);

    expect(nonprodRegistry.Properties.Name).not.toBe(prodRegistry.Properties.Name);
    expect(nonprodRole.Properties.RoleName).not.toBe(prodRole.Properties.RoleName);
    expect(new Set([...nonprodParameters, ...prodParameters]).size).toBe(
      nonprodParameters.length + prodParameters.length,
    );
  });
});

describe('buildGaToolGovernanceDocument', () => {
  it('fails on invalid tools and resolves platform-owned target ARNs', () => {
    const source = PLATFORM_TOOL_CATALOGUE['tool-echo'];
    const governance = buildGaToolGovernanceDocument(source, PLATFORM_ACCOUNT_ID);

    expect(governance.target.arn).toContain(`:${PLATFORM_ACCOUNT_ID}:`);
    expect(governance.target.arn).not.toContain('${PLATFORM_ACCOUNT_ID}');
    expect(() =>
      buildGaToolGovernanceDocument(
        { ...source, approvalStatus: 'invalid' } as unknown as ToolSpec,
        PLATFORM_ACCOUNT_ID,
      ),
    ).toThrow(/approvalStatus/);
  });
});
