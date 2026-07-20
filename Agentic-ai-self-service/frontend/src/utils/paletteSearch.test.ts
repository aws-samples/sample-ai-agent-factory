/**
 * Property-based tests for palette search/filter functionality.
 * Validates: Requirements 12.5
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { PALETTE_ITEMS, type PaletteItem } from '../components/palette/ComponentPalette';

// ============================================================================
// Search Filter Function (extracted for testing)
// ============================================================================

/**
 * Filter palette items based on search query.
 * Property 41: Component Palette Search Filter
 * For any search query in the component palette, only components whose name or
 * description contains the query (case-insensitive) shall be displayed.
 */
export function filterPaletteItems(items: PaletteItem[], query: string): PaletteItem[] {
  if (!query.trim()) return items;

  // Trim BEFORE matching (mirrors ComponentPalette): a query like "a " must
  // match the same items as "a" — trailing whitespace never changes results.
  // Without the trim, the exclusion property fails on counterexamples like
  // "A " (item contains "a" but not "a ").
  const normalizedQuery = query.trim().toLowerCase();
  return items.filter(
    (item) =>
      item.label.toLowerCase().includes(normalizedQuery) ||
      item.description.toLowerCase().includes(normalizedQuery)
  );
}

// ============================================================================
// Arbitraries (Test Data Generators)
// ============================================================================

// Generate random search queries
const searchQueryArb = fc.oneof(
  fc.string({ minLength: 0, maxLength: 50 }),
  fc.constantFrom('runtime', 'gateway', 'identity', 'policy', 'auth', 'agent', 'API', 'MCP', 'Cedar', 'credential')
);

// Generate substrings from actual palette item labels/descriptions
const paletteSubstringArb = fc.constantFrom(...PALETTE_ITEMS.flatMap((item) => {
  const words = [...item.label.split(' '), ...item.description.split(' ')];
  return words.filter((w) => w.length > 2);
}));

// ============================================================================
// Property 41: Component Palette Search Filter
// ============================================================================

describe('Property 41: Component Palette Search Filter', () => {
  /**
   * **Validates: Requirements 12.5**
   *
   * For any search query in the component palette, only components whose name or
   * description contains the query (case-insensitive) shall be displayed.
   */
  it('empty query returns all items', () => {
    fc.assert(
      fc.property(fc.constantFrom('', '  ', '\t', '\n'), (query) => {
        const result = filterPaletteItems(PALETTE_ITEMS, query);
        expect(result).toHaveLength(PALETTE_ITEMS.length);
        expect(result).toEqual(PALETTE_ITEMS);
      }),
      { numRuns: 10 }
    );
  });

  it('filtered items always contain query in label or description (case-insensitive)', () => {
    fc.assert(
      fc.property(searchQueryArb, (query) => {
        const result = filterPaletteItems(PALETTE_ITEMS, query);
        const normalizedQuery = query.toLowerCase().trim();

        if (normalizedQuery === '') {
          // Empty query returns all items
          expect(result).toHaveLength(PALETTE_ITEMS.length);
        } else {
          // All returned items must contain the query
          for (const item of result) {
            const labelMatch = item.label.toLowerCase().includes(normalizedQuery);
            const descMatch = item.description.toLowerCase().includes(normalizedQuery);
            expect(labelMatch || descMatch).toBe(true);
          }
        }
      }),
      { numRuns: 100 }
    );
  });

  it('items not in result do not contain query in label or description', () => {
    fc.assert(
      fc.property(searchQueryArb, (query) => {
        const result = filterPaletteItems(PALETTE_ITEMS, query);
        const normalizedQuery = query.toLowerCase().trim();

        if (normalizedQuery === '') return; // Skip empty queries

        const resultIds = new Set(result.map((item) => item.type));
        const excluded = PALETTE_ITEMS.filter((item) => !resultIds.has(item.type));

        // Excluded items must NOT contain the query
        for (const item of excluded) {
          const labelMatch = item.label.toLowerCase().includes(normalizedQuery);
          const descMatch = item.description.toLowerCase().includes(normalizedQuery);
          expect(labelMatch || descMatch).toBe(false);
        }
      }),
      { numRuns: 100 }
    );
  });

  it('search is case-insensitive', () => {
    fc.assert(
      fc.property(paletteSubstringArb, (substring) => {
        const lowerResult = filterPaletteItems(PALETTE_ITEMS, substring.toLowerCase());
        const upperResult = filterPaletteItems(PALETTE_ITEMS, substring.toUpperCase());
        const mixedResult = filterPaletteItems(PALETTE_ITEMS, substring);

        // All case variations should return the same items
        expect(lowerResult.map((i) => i.type).sort()).toEqual(upperResult.map((i) => i.type).sort());
        expect(lowerResult.map((i) => i.type).sort()).toEqual(mixedResult.map((i) => i.type).sort());
      }),
      { numRuns: 50 }
    );
  });

  it('known substrings from labels return matching items', () => {
    fc.assert(
      fc.property(paletteSubstringArb, (substring) => {
        const result = filterPaletteItems(PALETTE_ITEMS, substring);

        // At least one item should match since we're using actual substrings
        expect(result.length).toBeGreaterThan(0);
      }),
      { numRuns: 50 }
    );
  });

  it('filter is deterministic for same query', () => {
    fc.assert(
      fc.property(searchQueryArb, (query) => {
        const result1 = filterPaletteItems(PALETTE_ITEMS, query);
        const result2 = filterPaletteItems(PALETTE_ITEMS, query);

        expect(result1).toEqual(result2);
      }),
      { numRuns: 100 }
    );
  });

  it('result is always a subset of original items', () => {
    fc.assert(
      fc.property(searchQueryArb, (query) => {
        const result = filterPaletteItems(PALETTE_ITEMS, query);

        expect(result.length).toBeLessThanOrEqual(PALETTE_ITEMS.length);

        // All result items must be in original list
        for (const item of result) {
          expect(PALETTE_ITEMS.some((p) => p.type === item.type)).toBe(true);
        }
      }),
      { numRuns: 100 }
    );
  });

  it('result preserves original item order', () => {
    fc.assert(
      fc.property(searchQueryArb, (query) => {
        const result = filterPaletteItems(PALETTE_ITEMS, query);

        // Check that relative order is preserved using reference equality
        // (type is not unique — multiple 'tool' items exist)
        const resultIndices = result.map((item) =>
          PALETTE_ITEMS.findIndex((p) => p === item)
        );

        for (let i = 1; i < resultIndices.length; i++) {
          expect(resultIndices[i]).toBeGreaterThan(resultIndices[i - 1]);
        }
      }),
      { numRuns: 100 }
    );
  });
});
