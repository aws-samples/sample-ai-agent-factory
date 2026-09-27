import type { ReactNode } from 'react';
import type { ProjectId } from '../../../content/data';
import { BlueprintFigures, BlueprintHero, BlueprintSections } from './BlueprintSections';
import { McpGatewayFigures, McpGatewayHero, McpGatewaySections } from './McpGatewaySections';
import { SelfServiceExtraDocs, SelfServiceFigures, SelfServiceHero, SelfServiceSections } from './SelfServiceSections';
import { WorkshopFigures, WorkshopHero, WorkshopSections } from './WorkshopSections';

/** The project-specific parts of the landing template. */
export interface ProjectExtras {
  /** Figure slot of the page header. */
  hero: ReactNode;
  /** Sections rendered between Quickstart and Architecture. */
  sections: ReactNode;
  /** Body of the "Architecture and figures" section. */
  figures: ReactNode;
  /** Extra list items for "Documentation on this site". */
  extraDocs?: ReactNode;
}

export function getProjectExtras(id: ProjectId): ProjectExtras {
  switch (id) {
    case 'workshop':
      return { hero: <WorkshopHero />, sections: <WorkshopSections />, figures: <WorkshopFigures /> };
    case 'self-service':
      return {
        hero: <SelfServiceHero />,
        sections: <SelfServiceSections />,
        figures: <SelfServiceFigures />,
        extraDocs: <SelfServiceExtraDocs />,
      };
    case 'mcp-gateway':
      return { hero: <McpGatewayHero />, sections: <McpGatewaySections />, figures: <McpGatewayFigures /> };
    case 'blueprint':
      return { hero: <BlueprintHero />, sections: <BlueprintSections />, figures: <BlueprintFigures /> };
  }
}
