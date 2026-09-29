import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Section } from './Section';
import { SectionHeading } from './SectionHeading';

describe('SectionHeading', () => {
  it('renders eyebrow, heading level, lead and alignment', () => {
    render(<SectionHeading id="pick" eyebrow="Projects" title="Pick a project" lead="Four samples." align="center" />);
    const heading = screen.getByRole('heading', { level: 2, name: 'Pick a project' });
    expect(heading).toHaveAttribute('id', 'pick');
    expect(screen.getByText('Projects')).toBeInTheDocument();
    expect(screen.getByText('Four samples.')).toBeInTheDocument();
    expect(heading.closest('[data-section-heading]')).toHaveAttribute('data-align', 'center');
  });

  it('renders an h3 with a badge inside the heading', () => {
    render(<SectionHeading level={3} title="Workshop" badge={<span>1. Learn</span>} />);
    expect(screen.getByRole('heading', { level: 3 })).toHaveTextContent('1. LearnWorkshop');
  });
});

describe('Section', () => {
  it('labels the region with its heading', () => {
    render(
      <Section id="quickstart" title="Quickstart" flush>
        <p>Steps</p>
      </Section>,
    );
    const region = screen.getByRole('region', { name: 'Quickstart' });
    expect(region).toHaveAttribute('id', 'quickstart');
    expect(region).toHaveAttribute('data-flush', '');
    expect(screen.getByRole('heading', { level: 2 })).toHaveAttribute('id', 'quickstart-heading');
  });
});
