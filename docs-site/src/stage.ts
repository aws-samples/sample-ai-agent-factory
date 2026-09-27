import type { JourneyStage } from './content/data';

/** Default visible label for each stage; pages may override it (for example "1. Learn"). */
export const STAGE_LABELS: Record<JourneyStage, string> = {
  learn: 'Learn',
  build: 'Build',
  govern: 'Govern',
  scale: 'Scale',
};
