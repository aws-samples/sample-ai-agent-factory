import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { DeployResult } from './DeployResult';

describe('DeployResult MCP guidance', () => {
  it('does not show a prompt-shaped HTTP invocation command for MCP', () => {
    render(
      <DeployResult
        message="Deployed"
        runtimeId="mcp-runtime"
        runtimeProtocol="MCP"
        endpoint="arn:aws:bedrock-agentcore:eu-west-1:111111111111:runtime/mcp-runtime/runtime-endpoint/DEFAULT"
        onRedeploy={() => {}}
        onDelete={() => {}}
        isDeleting={false}
      />,
    );

    expect(screen.getByText('Standalone MCP runtime')).toBeVisible();
    expect(screen.getByText(/Use the MCP Tools tab/)).toBeVisible();
    expect(
      screen.queryByLabelText('AWS CLI invocation command'),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/"prompt": "Hello"/)).not.toBeInTheDocument();
  });
});
