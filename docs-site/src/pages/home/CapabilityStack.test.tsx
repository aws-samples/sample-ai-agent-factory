import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { CAPABILITY_LAYERS, postureSentence } from '../../content/capabilityLayers';
import { capabilities } from '../../content/data';
import { CapabilityStack } from './CapabilityStack';

function renderStack() {
  return render(
    <MemoryRouter>
      <CapabilityStack />
    </MemoryRouter>,
  );
}

describe('<CapabilityStack>', () => {
  it('renders five layer panels with an h3 each and every capability once', () => {
    renderStack();
    const stack = document.querySelector('[data-capability-stack]');
    expect(stack).not.toBeNull();
    expect(stack?.tagName).toBe('OL');
    expect(screen.getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent)).toEqual(
      CAPABILITY_LAYERS.map((layer) => layer.name),
    );
    for (const capability of capabilities) {
      expect(screen.getByRole('link', { name: new RegExp(`^${capability.name}\\b`) })).toBeInTheDocument();
    }
  });

  it('draws 40 decorative posture dots and one hidden sentence per capability', () => {
    renderStack();
    const stack = document.querySelector('[data-capability-stack]') as HTMLElement;
    const dots = stack.querySelectorAll('[data-fill]');
    expect(dots).toHaveLength(40);
    dots.forEach((dot) => {
      expect(dot).toHaveAttribute('aria-hidden', 'true');
      expect(dot.getAttribute('data-stage')).toMatch(/^(learn|build|govern|scale)$/);
      expect(dot.getAttribute('data-fill')).toMatch(/^(solid|outline|none)$/);
    });
    const sentences = stack.querySelectorAll('[data-posture-text]');
    expect(sentences).toHaveLength(10);
    sentences.forEach((sentence) => {
      expect(sentence).toHaveClass('visually-hidden');
    });
    // Capabilities with the same postures share a sentence, so compare the full list in order.
    expect(Array.from(sentences).map((sentence) => sentence.textContent)).toEqual(
      CAPABILITY_LAYERS.flatMap((layer) => layer.capabilityIds).map((id) => postureSentence(id)),
    );
  });

  it('links every capability to its matrix row on the contracts page', () => {
    renderStack();
    const stack = document.querySelector('[data-capability-stack]') as HTMLElement;
    const links = stack.querySelectorAll('a[href*="/concepts/capability-contracts/#matrix-"]');
    expect(links).toHaveLength(10);
    const targets = Array.from(links).map((link) => link.getAttribute('href')?.split('#matrix-')[1]);
    expect([...targets].sort()).toEqual(capabilities.map((capability) => capability.id).sort());
  });

  it('shows a legend with the three fills and the four stage keys, outside the counted stack', () => {
    renderStack();
    const legend = screen.getByRole('list', { name: 'Dot legend' });
    expect(legend).toHaveTextContent('Enforced');
    expect(legend).toHaveTextContent('Advisory or illustrative');
    expect(legend).toHaveTextContent('Not part of the project');
    const order = screen.getByRole('list', { name: 'Dot order' });
    expect(order.querySelectorAll('[data-stage]')).toHaveLength(4);
    expect(order).toHaveTextContent('1. Learn');
    expect(order).toHaveTextContent('Blueprint');
  });
});
