import type { ReactNode } from 'react';
import { type FeatureFlagName, type FeatureFlagsState, isFeatureEnabled } from '../hooks/useFeatureFlags';

export interface FeatureGateProps {
  /** The flags state from useFeatureFlags(); off while loading or after an error. */
  state: FeatureFlagsState;
  flag: FeatureFlagName;
  children: ReactNode;
  /** Rendered while the flag is off (default: nothing). */
  fallback?: ReactNode;
}

/**
 * Renders `children` only while `flag` is on. The backend is the control (a flagged-off route
 * answers 404); this keeps the UI from offering a surface that would only fail.
 */
export function FeatureGate({ state, flag, children, fallback = null }: FeatureGateProps) {
  return <>{isFeatureEnabled(state, flag) ? children : fallback}</>;
}
