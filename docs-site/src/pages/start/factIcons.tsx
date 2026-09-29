import type { ReactNode } from 'react';
import { Activity, Clock, Coins, Layers, MapPin, Package, Timer, Trash2, Wrench, type LucideIcon } from 'lucide-react';
import type { ProjectFacts } from '../../content/facts';

/** Decorative icon for each fact key (always rendered aria-hidden beside the label). */
export const FACT_ICONS: Partial<Record<keyof ProjectFacts, LucideIcon>> = {
  regions: MapPin,
  firstDeploy: Timer,
  cost: Coins,
  accountTopology: Layers,
  iac: Wrench,
  status: Activity,
  teardown: Trash2,
  handsOnTime: Clock,
  deploys: Package,
};

/** The icon element for a fact key, or undefined when the key has none. */
export function factIcon(key: keyof ProjectFacts, size = 14): ReactNode {
  const Icon = FACT_ICONS[key];
  return Icon ? <Icon size={size} aria-hidden="true" /> : undefined;
}
