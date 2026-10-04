/**
 * Unit tests for AgentBuilderInspectRole (sanctioned control-plane surface #2).
 *
 * Proves the role is strictly read-only and exactly scoped:
 *   - grants only describe/list/get actions (adversarial "can read" twin);
 *   - contains no create/update/delete/put/tag/invoke/approve action
 *     (adversarial "cannot mutate" twin);
 *   - scopes to the exact registry ARN + record/* (no wildcard resource).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App, Stack } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { AccountRootPrincipal, ArnPrincipal } from 'aws-cdk-lib/aws-iam';

import {
  AgentBuilderInspectRole,
  AGENT_BUILDER_INSPECT_ACTIONS,
  AGENT_BUILDER_INSPECT_FORBIDDEN_FRAGMENTS,
} from './index';

const REGISTRY_ARN =
  'arn:aws:agent-registry:us-west-2:333333333333:registry/agenticai-nonprod';

type InspectProps = ConstructorParameters<typeof AgentBuilderInspectRole>[2];

function synth(props?: Partial<InspectProps>) {
  const app = new App();
  const stack = new Stack(app, 'TestStack', {
    env: { account: '333333333333', region: 'us-west-2' },
  });
  makeRole(stack, {
    registryArn: REGISTRY_ARN,
    trustedPrincipal: new AccountRootPrincipal(),
    roleName: 'AgenticAI-AgentBuilderInspect-nonprod',
    ...props,
  });
  return Template.fromStack(stack);
}

function makeRole(stack: Stack, props: InspectProps) {
  return new AgentBuilderInspectRole(stack, 'Inspect', props);
}

describe('AgentBuilderInspectRole', () => {
  it('creates a role with the exact configured name', () => {
    synth().hasResourceProperties('AWS::IAM::Role', {
      RoleName: 'AgenticAI-AgentBuilderInspect-nonprod',
    });
  });

  it('grants only the read-only registry actions, scoped to the exact ARN + record/*', () => {
    const template = synth();
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: [
          {
            Sid: 'AgentBuilderInspectRegistryRead',
            Effect: 'Allow',
            Action: [...AGENT_BUILDER_INSPECT_ACTIONS],
            Resource: [REGISTRY_ARN, `${REGISTRY_ARN}/record/*`],
          },
        ],
      },
    });
  });

  it('emits no mutating action anywhere in the rendered policy (cannot-mutate twin)', () => {
    const template = synth();
    const policies = template.findResources('AWS::IAM::Policy');
    const rendered = JSON.stringify(policies);
    for (const fragment of AGENT_BUILDER_INSPECT_FORBIDDEN_FRAGMENTS) {
      // The role's own actions must not contain any mutating verb. Guard against
      // a substring false-positive by only checking action strings.
      for (const [, res] of Object.entries(policies)) {
        const doc = (res as { Properties: { PolicyDocument: { Statement: Array<{ Action: string[] | string }> } } })
          .Properties.PolicyDocument.Statement;
        for (const stmt of doc) {
          const actions = Array.isArray(stmt.Action) ? stmt.Action : [stmt.Action];
          for (const a of actions) {
            expect(a).not.toContain(fragment);
          }
        }
      }
    }
    expect(rendered).not.toContain('*:*');
  });

  it('optionally grants exact runtime read ARNs when provided', () => {
    const runtimeArn =
      'arn:aws:bedrock-agentcore:us-west-2:333333333333:runtime/agenticai-d03-nonprod-demo-primary';
    const template = synth({ runtimeArns: [runtimeArn] });
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          {
            Sid: 'AgentBuilderInspectRuntimeRead',
            Effect: 'Allow',
            Action: ['bedrock-agentcore:GetAgentRuntime', 'bedrock-agentcore:ListAgentRuntimes'],
            Resource: runtimeArn,
          },
        ]),
      },
    });
  });

  it('rejects a wildcard registry ARN', () => {
    const app = new App();
    const stack = new Stack(app, 'Bad');
    expect(() =>
      makeRole(stack, {
        registryArn: 'arn:aws:agent-registry:us-west-2:333333333333:registry/*',
        trustedPrincipal: new ArnPrincipal('arn:aws:iam::333333333333:role/Builder'),
        roleName: 'X',
      }),
    ).toThrow(/exact ARN/);
  });

  it('rejects a wildcard runtime ARN', () => {
    const app = new App();
    const stack = new Stack(app, 'Bad2');
    expect(() =>
      makeRole(stack, {
        registryArn: REGISTRY_ARN,
        trustedPrincipal: new ArnPrincipal('arn:aws:iam::333333333333:role/Builder'),
        roleName: 'X',
        runtimeArns: ['arn:aws:bedrock-agentcore:us-west-2:333333333333:runtime/*'],
      }),
    ).toThrow(/exact ARNs/);
  });
});
