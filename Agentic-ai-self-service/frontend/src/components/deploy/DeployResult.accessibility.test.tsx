import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { DeployResult } from './DeployResult';

describe('DeployResult invocation command', () => {
  it('exposes a keyboard-focusable named region with a runnable AgentCore command', () => {
    render(
      <DeployResult
        message="Deployed successfully"
        runtimeId="runtime-audit"
        endpoint="arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/runtime-audit/runtime-endpoint/DEFAULT"
        onRedeploy={vi.fn()}
        onDelete={vi.fn()}
        isDeleting={false}
      />,
    );

    const command = screen.getByRole('region', {
      name: 'AWS CLI invocation command',
    });
    expect(command).toHaveAttribute('tabindex', '0');
    expect(command).toHaveTextContent('aws bedrock-agentcore invoke-agent-runtime');
    expect(command).toHaveTextContent('--region eu-west-1');
    expect(command).toHaveTextContent(
      '--agent-runtime-arn arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/runtime-audit',
    );
    expect(command).not.toHaveTextContent('bedrock-agent-runtime invoke-agent');
    expect(command).not.toHaveTextContent('TSTALIASID');
  });
});
