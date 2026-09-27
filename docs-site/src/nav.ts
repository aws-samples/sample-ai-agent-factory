import { navigation, type NavItem } from './content/data';

export interface SiteNavChild {
  label: string;
  to: string;
}

export interface SiteNavItem {
  label: string;
  /** Link target (used when there is no dropdown). */
  to: string;
  /** Path prefix that marks the item as current, e.g. "/concepts". */
  match: string;
  /** Dropdown entries; when present the item renders as a menu button. */
  children?: SiteNavChild[];
}

function withTrailingSlash(path: string): string {
  return path === '/' || path.endsWith('/') ? path : `${path}/`;
}

function sectionPrefix(path: string): string {
  const [first] = path.split('/').filter(Boolean);
  return first ? `/${first}` : '/';
}

function toSiteNavItem(item: NavItem): SiteNavItem {
  const to = withTrailingSlash(item.path);
  const base: SiteNavItem = { label: item.label, to, match: sectionPrefix(item.path) };
  if (item.children && item.children.length > 0) {
    base.children = [
      { label: `All ${item.label.toLowerCase()}`, to },
      ...item.children.map((child) => ({ label: child.label, to: withTrailingSlash(child.path) })),
    ];
  }
  return base;
}

/**
 * Top navigation, derived from `navigation` in src/content/data.ts (labels and
 * paths) with the structural extras the Layout needs: a section prefix for
 * the current-page state and an "All ..." entry at the top of each dropdown.
 */
export const siteNav: SiteNavItem[] = navigation.map(toSiteNavItem);
