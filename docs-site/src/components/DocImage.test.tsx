import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { DocImage } from './DocImage';
import { resolveAlt } from './docImageAlt';

describe('DocImage', () => {
  it('keeps a descriptive Markdown alt', () => {
    expect(resolveAlt('/assets/architecture-Ab12Cd34.jpg', 'Deployment architecture diagram')).toBe(
      'Deployment architecture diagram',
    );
  });

  it('replaces a one-word alt for a known image, with or without a bundle hash', () => {
    expect(resolveAlt('/sample-ai-agent-factory/assets/architecture-Ab12Cd34.jpg', 'Architecture')).toMatch(
      /^Architecture of the AgentCore Visual Workflow Platform/,
    );
    expect(resolveAlt('/@fs/repo/Agentic-ai-self-service/docs/architecture.jpg?import', 'Architecture')).toMatch(
      /Step Functions/,
    );
  });

  it('leaves unknown images alone and is lazy by default', () => {
    render(<DocImage src="/x/unknown.png" alt="Diagram" />);
    const img = screen.getByRole('img', { name: 'Diagram' });
    expect(img).toHaveAttribute('loading', 'lazy');
    expect(img).not.toHaveAttribute('fetchpriority');
  });

  it('forwards eager loading and fetch priority as lowercase attributes', () => {
    render(<DocImage src="/x/hero.png" alt="Hero" loading="eager" fetchPriority="high" width={1200} height={700} />);
    const img = screen.getByRole('img', { name: 'Hero' });
    expect(img).toHaveAttribute('loading', 'eager');
    expect(img).toHaveAttribute('fetchpriority', 'high');
    expect(img).toHaveAttribute('width', '1200');
  });
});
