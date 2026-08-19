/**
 * Tests for A2A Agent Card schema validation.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { serializeAgentCard, validateAgentCard, type AgentCard } from './agent-card-schema';

const goodCard: AgentCard = {
  schemaVersion: '0.2',
  name: 'demo-chatbot',
  description: 'Reference chatbot exposed over A2A.',
  provider: 'AWS Solutions',
  version: '0.4.0',
  endpoints: [{ url: 'https://gateway.example.com/a2a', transport: 'streamable-http' }],
  skills: [
    { id: 'tool-echo', name: 'Echo', description: 'Echo a message back.' },
    { id: 'tool-ping', name: 'Ping', description: 'Ping for pong + timestamp.' },
  ],
  auth: { type: 'sigv4' },
};

describe('AgentCard validation', () => {
  it('accepts a fully-formed card', () => {
    expect(() => validateAgentCard(goodCard)).not.toThrow();
  });

  it('rejects HTTP (non-TLS) endpoints', () => {
    expect(() =>
      validateAgentCard({
        ...goodCard,
        endpoints: [{ url: 'http://insecure', transport: 'streamable-http' }],
      }),
    ).toThrow(/HTTPS/);
  });

  it('rejects non-semver versions', () => {
    expect(() => validateAgentCard({ ...goodCard, version: 'latest' })).toThrow(/semver/);
  });

  it('requires at least one skill', () => {
    expect(() => validateAgentCard({ ...goodCard, skills: [] })).toThrow();
  });

  it('rejects non-kebab skill ids', () => {
    expect(() =>
      validateAgentCard({
        ...goodCard,
        skills: [{ id: 'Tool_Echo', name: 'Echo', description: 'X' }],
      }),
    ).toThrow(/kebab-case/);
  });

  it('schemaVersion is locked to 0.2', () => {
    expect(() =>
      validateAgentCard({ ...(goodCard as unknown as AgentCard), schemaVersion: '0.3' as '0.2' }),
    ).toThrow();
  });

  it('serialised cards are skill-id-sorted', () => {
    const reversed: AgentCard = {
      ...goodCard,
      skills: [...goodCard.skills].reverse(),
    };
    const a = serializeAgentCard(goodCard);
    const b = serializeAgentCard(reversed);
    expect(a).toEqual(b);
  });
});
