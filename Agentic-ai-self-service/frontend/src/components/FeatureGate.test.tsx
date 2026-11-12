import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { FeatureGate } from './FeatureGate';
import { parseFeatureFlags, type FeatureFlagsState } from '../hooks/useFeatureFlags';

const ready = (on: boolean): FeatureFlagsState => ({
  status: 'ready',
  flags: parseFeatureFlags({ features: { platformFlagsCanary: on }, source: 'ssm' })!,
});

describe('FeatureGate', () => {
  it('renders the children only while the flag is on', () => {
    render(
      <FeatureGate state={ready(true)} flag="platformFlagsCanary">
        <p>canary surface</p>
      </FeatureGate>,
    );
    expect(screen.getByText('canary surface')).toBeInTheDocument();
  });

  it('renders the fallback (nothing by default) while off, loading or errored', () => {
    for (const state of [ready(false), { status: 'loading' } as const, { status: 'error', message: 'x' } as const]) {
      const { container, unmount } = render(
        <FeatureGate state={state} flag="platformFlagsCanary">
          <p>canary surface</p>
        </FeatureGate>,
      );
      expect(container).toBeEmptyDOMElement();
      unmount();
    }
    render(
      <FeatureGate state={ready(false)} flag="platformFlagsCanary" fallback={<p>coming soon</p>}>
        <p>canary surface</p>
      </FeatureGate>,
    );
    expect(screen.getByText('coming soon')).toBeInTheDocument();
    expect(screen.queryByText('canary surface')).toBeNull();
  });
});
