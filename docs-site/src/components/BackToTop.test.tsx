import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { BackToTop } from './BackToTop';

describe('BackToTop', () => {
  it('renders a visibly labelled link to the main landmark', () => {
    render(<BackToTop />);
    const link = screen.getByRole('link', { name: 'Back to top' });
    expect(link).toHaveAttribute('href', '#main-content');
    expect(link.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
  });
});
