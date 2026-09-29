import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ReadingProgress } from './ReadingProgress';

describe('ReadingProgress', () => {
  it('renders a decorative bar hidden from assistive technology', () => {
    const { container } = render(<ReadingProgress />);
    const bar = container.querySelector('[data-reading-progress]');
    expect(bar).not.toBeNull();
    expect(bar).toHaveAttribute('aria-hidden', 'true');
    expect(bar?.textContent).toBe('');
  });
});
