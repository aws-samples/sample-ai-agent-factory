/**
 * Mechanical dash normalisation for text that becomes site copy (titles, TOC labels,
 * link lists): a spaced em or en dash becomes ": ", any remaining em or en dash becomes
 * a hyphen. Heading ids are never derived from the result, so anchors stay stable.
 */
export function normaliseDashes(text: string): string {
  return text.replace(/\s+[–—]\s+/g, ': ').replace(/[–—]/g, '-');
}
