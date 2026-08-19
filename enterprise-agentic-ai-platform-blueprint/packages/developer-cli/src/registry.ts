/**
 * registry — formatter helpers for the `agenticai registry search` /
 * `agenticai registry get` commands.
 *
 * The actual SearchRegistryRecords / GetRegistryRecord SDK calls happen in
 * the CLI handler (so unit tests don't drag in @aws-sdk dependencies). This
 * module is the pure-function rendering layer + the typed result shape.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

/**
 * Subset of `RegistryRecordSummary` we render. Mirrors the AWS API shape
 * — feed the SDK response straight in.
 */
export interface RegistrySearchResult {
  readonly recordId: string;
  readonly name: string;
  readonly description: string;
  readonly status: string;
  readonly resourceType: string;
  readonly ownerTeam?: string;
  readonly costCentre?: string;
}

/**
 * Render a paginated list of search results as a fixed-width table the CLI
 * prints to stdout. Returns the string — caller decides whether to print
 * (so unit tests can snapshot).
 */
export function formatSearchResults(results: readonly RegistrySearchResult[]): string {
  if (results.length === 0) {
    return 'No matching records.';
  }
  const headers = ['Record Id', 'Name', 'Status', 'Type', 'Owner', 'Cost Centre', 'Description'];
  const rows = results.map((r) => [
    r.recordId,
    r.name,
    r.status,
    r.resourceType,
    r.ownerTeam ?? '-',
    r.costCentre ?? '-',
    truncate(r.description, 48),
  ]);
  const widths = headers.map((h, i) =>
    Math.max(h.length, ...rows.map((row) => row[i].length)),
  );
  const fmt = (cells: readonly string[]): string =>
    cells.map((c, i) => c.padEnd(widths[i])).join('  ');
  return [fmt(headers), widths.map((w) => '-'.repeat(w)).join('  '), ...rows.map(fmt)].join(
    '\n',
  );
}

/**
 * Filter a list of records to only the ones whose status is `APPROVED` —
 * subscribers should never look at rejected/deprecated/pending records.
 */
export function filterApproved(
  results: readonly RegistrySearchResult[],
): readonly RegistrySearchResult[] {
  return results.filter((r) => r.status === 'APPROVED');
}

function truncate(s: string, max: number): string {
  if (s.length <= max) return s;
  return s.slice(0, max - 1) + '…';
}
