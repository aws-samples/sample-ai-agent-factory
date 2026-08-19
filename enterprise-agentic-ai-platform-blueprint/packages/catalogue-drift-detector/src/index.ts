export {
  CATALOGUE_DRIFT_NAMESPACE,
  CatalogueDriftDetectorConstruct,
  type CatalogueDriftDetectorConstructProps,
} from './drift-detector-construct';

/**
 * @agenticai/catalogue-drift-detector
 *
 * The platform-tool-catalogue is a TS SSOT. The live AgentCore Gateway has
 * targets created by the platform pipeline. Drift between the two can occur
 * if (a) someone edits a Gateway target out-of-band via the AWS console,
 * (b) a workstream operator deletes a tool Lambda alias, or (c) the SSOT
 * is updated but a deploy hasn't run yet.
 *
 * This pure-fn detector compares two sets and produces a structured drift
 * report. Callers wire it into a scheduled Lambda that emits a CloudWatch
 * metric `AgenticAI/Catalogue/DriftCount`.
 *
 * Bonus shippable (Z7-L).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export interface DriftInput {
  /** Tool ids declared in the SSOT for this workstream. */
  readonly catalogueIds: readonly string[];
  /** Tool ids actually configured on the live Gateway (qualified or short id). */
  readonly liveTargetIds: readonly string[];
}

export interface DriftReport {
  readonly missingFromGateway: readonly string[]; // in catalogue, not on gateway
  readonly missingFromCatalogue: readonly string[]; // on gateway, not in catalogue
  readonly inSync: boolean;
}

export function computeDrift(input: DriftInput): DriftReport {
  const cat = new Set(input.catalogueIds);
  const live = new Set(input.liveTargetIds);
  const missingFromGateway = [...cat].filter((id) => !live.has(id)).sort();
  const missingFromCatalogue = [...live].filter((id) => !cat.has(id)).sort();
  return {
    missingFromGateway,
    missingFromCatalogue,
    inSync: missingFromGateway.length === 0 && missingFromCatalogue.length === 0,
  };
}

/**
 * Build the CW metric data the scheduled Lambda emits per detection cycle.
 */
export function buildDriftMetricData(
  report: DriftReport,
  dimensions: { TenantId: string; AgentId: string; Env: string },
): Array<{ MetricName: string; Dimensions: Array<{ Name: string; Value: string }>; Value: number; Unit: string }> {
  const dims = Object.entries(dimensions).map(([Name, Value]) => ({ Name, Value }));
  return [
    { MetricName: 'DriftCount', Dimensions: dims, Value: report.missingFromGateway.length + report.missingFromCatalogue.length, Unit: 'Count' },
    { MetricName: 'MissingFromGateway', Dimensions: dims, Value: report.missingFromGateway.length, Unit: 'Count' },
    { MetricName: 'MissingFromCatalogue', Dimensions: dims, Value: report.missingFromCatalogue.length, Unit: 'Count' },
    { MetricName: 'InSync', Dimensions: dims, Value: report.inSync ? 1 : 0, Unit: 'Count' },
  ];
}
