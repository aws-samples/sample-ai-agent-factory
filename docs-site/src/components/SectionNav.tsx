import { ChipNav } from './ChipNav';

export interface SectionNavItem {
  label: string;
  /** Canonical site path with a trailing slash. */
  to: string;
}

export interface SectionNavProps {
  /** Accessible name of the nav landmark, for example "Concepts section". Defaults to "In this section". */
  label?: string;
  items: SectionNavItem[];
  className?: string;
}

/**
 * "In this section" chip rail linking the pages of one hub section. A thin wrapper
 * over `ChipNav`, which marks the current page with aria-current.
 */
export function SectionNav({ label = 'In this section', items, className }: SectionNavProps) {
  return <ChipNav label={label} lead="In this section:" items={items} className={className} />;
}
