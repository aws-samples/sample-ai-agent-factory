import { SectionNav, type SectionNavItem } from '../../components/SectionNav';
import { PATHS } from '../../paths';

const REFERENCE_PAGES: SectionNavItem[] = [
  { to: PATHS.referenceSecurity, label: 'Security' },
  { to: PATHS.referenceSupportEnvelope, label: 'Support envelope' },
];

/** Chip navigation between the pages of the Reference section; SectionNav marks the current page. */
export function ReferenceNav() {
  return <SectionNav label="Reference section" items={REFERENCE_PAGES} />;
}
