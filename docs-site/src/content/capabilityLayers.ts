/**
 * The Home page capability stack: the ten shared capabilities grouped into five
 * layers, and the posture of each project for each capability, reduced to a dot
 * fill so the stack can be read at a glance. Pure data over `data.ts` and `matrix.ts`.
 */
import { projects, type JourneyStage, type ProjectId } from './data';
import { capabilityMatrix, POSTURE_LABELS, type Posture } from './matrix';

export interface CapabilityLayer {
  id: string;
  /** Layer name rendered as the panel heading. */
  name: string;
  /** `Capability.id` values from data.ts, in display order. */
  capabilityIds: string[];
}

/** Every capability id appears exactly once (asserted by the unit test). */
export const CAPABILITY_LAYERS: CapabilityLayer[] = [
  { id: 'inference', name: 'Inference', capabilityIds: ['llm-gateway'] },
  { id: 'tools', name: 'Tools and catalogue', capabilityIds: ['tool-gateway', 'registry'] },
  { id: 'runtime', name: 'Runtime', capabilityIds: ['runtime', 'memory'] },
  { id: 'trust', name: 'Trust', capabilityIds: ['identity', 'policy'] },
  { id: 'operate', name: 'Operate', capabilityIds: ['delivery', 'observability', 'cost'] },
];

/** How a posture dot is drawn: filled, outlined in the stage colour, or a neutral border only. */
export type DotFill = 'solid' | 'outline' | 'none';

export interface PostureDot {
  projectId: ProjectId;
  stage: JourneyStage;
  posture: Posture;
  fill: DotFill;
}

export function fillFor(posture: Posture): DotFill {
  switch (posture) {
    case 'enforced':
      return 'solid';
    case 'advisory':
    case 'illustrative':
      return 'outline';
    default:
      return 'none';
  }
}

function rowFor(capabilityId: string) {
  const row = capabilityMatrix.find((entry) => entry.capabilityId === capabilityId);
  if (!row) throw new Error(`No capability matrix row for "${capabilityId}"`);
  return row;
}

/** One dot per project, in `projects` order, from the capability matrix. */
export function postureDots(capabilityId: string): PostureDot[] {
  const row = rowFor(capabilityId);
  return projects.map((project) => {
    const { posture } = row.cells[project.id];
    return { projectId: project.id, stage: project.stage, posture, fill: fillFor(posture) };
  });
}

/** Text alternative for the dots: "Workshop: Illustrative; Self-Service: Not applicable; ...". */
export function postureSentence(capabilityId: string): string {
  const row = rowFor(capabilityId);
  return projects.map((project) => `${project.shortName}: ${POSTURE_LABELS[row.cells[project.id].posture].label}`).join('; ');
}
