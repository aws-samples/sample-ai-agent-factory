import { SectionNav, type SectionNavItem } from '../../components/SectionNav';
import { PATHS } from '../../paths';

const CONCEPT_PAGES: SectionNavItem[] = [
  { to: PATHS.conceptsAgentFactory, label: 'The Agent Factory' },
  { to: PATHS.conceptsCapabilityContracts, label: 'Capability contracts' },
  { to: PATHS.conceptsArchitecture, label: 'Architecture' },
  { to: PATHS.conceptsGlossary, label: 'Glossary' },
];

/** Chip navigation between the pages of the Concepts section; SectionNav marks the current page. */
export function ConceptsNav() {
  return <SectionNav label="Concepts section" items={CONCEPT_PAGES} />;
}
