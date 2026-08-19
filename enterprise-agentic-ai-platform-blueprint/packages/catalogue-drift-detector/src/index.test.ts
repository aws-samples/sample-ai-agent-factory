/**
 * Tests for the catalogue drift detector.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { buildDriftMetricData, computeDrift } from './index';

describe('computeDrift', () => {
  it('reports inSync when both sides match', () => {
    const r = computeDrift({
      catalogueIds: ['tool-a', 'tool-b'],
      liveTargetIds: ['tool-a', 'tool-b'],
    });
    expect(r.inSync).toBe(true);
    expect(r.missingFromGateway).toEqual([]);
    expect(r.missingFromCatalogue).toEqual([]);
  });

  it('reports a tool removed out-of-band on the Gateway', () => {
    const r = computeDrift({
      catalogueIds: ['tool-a', 'tool-b'],
      liveTargetIds: ['tool-a'],
    });
    expect(r.inSync).toBe(false);
    expect(r.missingFromGateway).toEqual(['tool-b']);
  });

  it('reports a Gateway-side tool not in the catalogue (config drift)', () => {
    const r = computeDrift({
      catalogueIds: ['tool-a'],
      liveTargetIds: ['tool-a', 'tool-rogue'],
    });
    expect(r.inSync).toBe(false);
    expect(r.missingFromCatalogue).toEqual(['tool-rogue']);
  });

  it('handles empty catalogue', () => {
    const r = computeDrift({ catalogueIds: [], liveTargetIds: ['tool-rogue'] });
    expect(r.inSync).toBe(false);
    expect(r.missingFromCatalogue).toEqual(['tool-rogue']);
  });
});

describe('buildDriftMetricData', () => {
  it('emits 4 metrics with the right dimensions', () => {
    const data = buildDriftMetricData(
      { missingFromGateway: ['x'], missingFromCatalogue: [], inSync: false },
      { TenantId: 't', AgentId: 'a', Env: 'prod' },
    );
    expect(data).toHaveLength(4);
    const names = data.map((d) => d.MetricName).sort();
    expect(names).toEqual(['DriftCount', 'InSync', 'MissingFromCatalogue', 'MissingFromGateway']);
    expect(data.find((d) => d.MetricName === 'InSync')!.Value).toBe(0);
  });
});
