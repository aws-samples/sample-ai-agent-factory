import {
  Activity,
  BookMarked,
  Brain,
  Coins,
  Cpu,
  KeyRound,
  Plug,
  Rocket,
  Route,
  Scale,
  type LucideIcon,
} from 'lucide-react';

/**
 * One icon per shared capability (ids from `capabilities` in content/data.ts).
 * Icons are decorative: render them `aria-hidden` beside the capability name.
 */
export const CAPABILITY_ICONS: Record<string, LucideIcon> = {
  'llm-gateway': Route,
  'tool-gateway': Plug,
  runtime: Cpu,
  memory: Brain,
  identity: KeyRound,
  registry: BookMarked,
  policy: Scale,
  delivery: Rocket,
  observability: Activity,
  cost: Coins,
};

/** Icon for a capability id; falls back to the registry icon for an unknown id. */
export function capabilityIcon(id: string): LucideIcon {
  return CAPABILITY_ICONS[id] ?? BookMarked;
}
