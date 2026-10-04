import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { EvaluationConfigurationModal } from './EvaluationConfigurationModal';

describe('EvaluationConfigurationModal accessibility', () => {
  it('renders evaluator identifiers with the readable secondary text token', () => {
    render(
      <EvaluationConfigurationModal
        isOpen
        onClose={vi.fn()}
        onSave={vi.fn()}
      />,
    );

    expect(screen.getByText('Builtin.GoalSuccessRate')).toHaveClass(
      'text-gray-500',
    );
  });
});
