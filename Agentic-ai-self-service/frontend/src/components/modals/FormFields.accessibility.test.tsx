import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SliderField, Toggle } from './FormFields';

describe('Toggle accessibility', () => {
  it('exposes its visible label and description to assistive technology', () => {
    const onChange = vi.fn();
    render(
      <Toggle
        id="memory-toggle"
        label="Enable AgentCore Memory"
        description="Persist conversation context across turns."
        checked={false}
        onChange={onChange}
      />,
    );

    const toggle = screen.getByRole('switch', { name: 'Enable AgentCore Memory' });
    expect(toggle).toHaveAccessibleDescription(
      'Persist conversation context across turns.',
    );

    fireEvent.click(screen.getByText('Enable AgentCore Memory'));
    expect(onChange).toHaveBeenCalledWith(true);
  });
});

describe('SliderField accessibility', () => {
  it('names and describes the control and keeps boundary labels readable', () => {
    render(
      <SliderField
        id="sampling-rate"
        label="Sampling rate"
        helpText="Percent of invocations to evaluate."
        value={50}
        min={1}
        max={100}
        step={1}
        onChange={vi.fn()}
      />,
    );

    expect(
      screen.getByRole('slider', { name: 'Sampling rate' }),
    ).toBeInTheDocument();
    expect(screen.getByText('1').parentElement).toHaveClass('text-gray-500');
    expect(screen.getByText('100').parentElement).toHaveClass('text-gray-500');
  });
});
