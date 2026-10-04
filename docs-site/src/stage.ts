import { Building2, GraduationCap, Hammer, ShieldCheck, type LucideIcon } from 'lucide-react';
import type { JourneyStage } from './content/data';

/** Default visible label for each stage; pages may override it (for example "1. Learn"). */
export const STAGE_LABELS: Record<JourneyStage, string> = {
  learn: 'Learn',
  build: 'Build',
  govern: 'Govern',
  scale: 'Scale',
};

/** One icon per stage, used beside stage labels (always rendered aria-hidden next to text). */
export const STAGE_ICONS: Record<JourneyStage, LucideIcon> = {
  learn: GraduationCap,
  build: Hammer,
  govern: ShieldCheck,
  scale: Building2,
};
