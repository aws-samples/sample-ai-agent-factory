import { execFileSync } from 'child_process';
import path from 'path';
import { buildAgentManifest, type AgentManifestInput } from './agent-manifest';

const FIXED_NOW = () => new Date('2026-09-18T12:00:00.000Z');
const PYTHON_CONTRACT = path.resolve(
  __dirname,
  '../../../scripts/live-agentcore-gateway-spike/manifest_contract.py',
);

function buildWithPython(input: AgentManifestInput) {
  const output = execFileSync('python3', [PYTHON_CONTRACT], {
    input: JSON.stringify({ input, now: FIXED_NOW().toISOString() }),
    encoding: 'utf8',
  });
  return JSON.parse(output);
}

describe('Python Agent Manifest parity', () => {
  const fixtures: AgentManifestInput[] = [
    {
      agentId: 'gateway-spike',
      tenantId: 'platform',
      gitSha: 'abcdef1234567',
      promptHashes: {
        'z-system.md': 'bbbb',
        'a-few-shot.md': 'aaaa',
      },
      toolPermissions: ['tool-zulu', 'tool-alpha'],
      configHash: 'cccc',
      thresholdsHash: 'dddd',
    },
    {
      agentId: 'assistant-é',
      tenantId: 'tenant-東京',
      gitSha: '0123456789abcdef',
      promptHashes: {
        'quote".md': 'hash\\value',
        'emoji-😀.md': 'value-with-ü',
      },
      toolPermissions: ['tool-beta', 'tool-alpha'],
      configHash: 'line\\break',
      thresholdsHash: 'threshold-✓',
    },
  ];

  it.each(fixtures)('matches TypeScript byte semantics for %#', (input) => {
    const expected = buildAgentManifest(input, FIXED_NOW);
    const actual = buildWithPython(input);
    expect(actual).toEqual(expected);
  });
});
