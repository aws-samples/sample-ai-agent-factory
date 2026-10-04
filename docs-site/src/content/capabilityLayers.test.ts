import { describe, expect, it } from 'vitest';
import { CAPABILITY_LAYERS, fillFor, postureDots, postureSentence } from './capabilityLayers';
import { capabilities, projects } from './data';
import { capabilityMatrix, POSTURE_LABELS } from './matrix';

describe('capability layers', () => {
  it('lists every capability id exactly once across the layers', () => {
    const ids = CAPABILITY_LAYERS.flatMap((layer) => layer.capabilityIds);
    expect(ids.length).toBe(new Set(ids).size);
    expect([...ids].sort()).toEqual(capabilities.map((capability) => capability.id).sort());
    expect(CAPABILITY_LAYERS).toHaveLength(5);
    expect(new Set(CAPABILITY_LAYERS.map((layer) => layer.id)).size).toBe(CAPABILITY_LAYERS.length);
  });

  it('maps postures to dot fills', () => {
    expect(fillFor('enforced')).toBe('solid');
    expect(fillFor('advisory')).toBe('outline');
    expect(fillFor('illustrative')).toBe('outline');
    expect(fillFor('not-applicable')).toBe('none');
    expect(fillFor('outside-envelope')).toBe('none');
  });

  it('returns four dots per capability, in project order, with fills matching the matrix', () => {
    for (const capability of capabilities) {
      const dots = postureDots(capability.id);
      const row = capabilityMatrix.find((entry) => entry.capabilityId === capability.id);
      expect(row).toBeDefined();
      expect(dots).toHaveLength(4);
      expect(dots.map((dot) => dot.projectId)).toEqual(projects.map((project) => project.id));
      dots.forEach((dot, index) => {
        const posture = row!.cells[projects[index].id].posture;
        expect(dot.stage).toBe(projects[index].stage);
        expect(dot.posture).toBe(posture);
        expect(dot.fill).toBe(fillFor(posture));
      });
    }
  });

  it('builds the hidden sentence from project short names and posture labels', () => {
    expect(postureSentence('llm-gateway')).toBe(
      'Workshop: Illustrative; Self-Service: Not applicable; MCP Gateway: Not applicable; Blueprint: Enforced',
    );
    for (const capability of capabilities) {
      const sentence = postureSentence(capability.id);
      const parts = sentence.split('; ');
      expect(parts).toHaveLength(4);
      parts.forEach((part, index) => {
        const [name, label] = part.split(': ');
        expect(name).toBe(projects[index].shortName);
        expect(Object.values(POSTURE_LABELS).map((entry) => entry.label)).toContain(label);
      });
    }
  });

  it('throws for an unknown capability id', () => {
    expect(() => postureDots('nope')).toThrow(/nope/);
  });
});
