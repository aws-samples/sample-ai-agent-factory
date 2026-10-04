import { useState } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import {
  ResourceNamingFields,
  type ResourceNamingState,
} from './ResourceNamingFields';

function latest(mock: ReturnType<typeof vi.fn>): ResourceNamingState {
  return mock.mock.calls.at(-1)?.[0] as ResourceNamingState;
}

function FieldsHarness({ onChange }: { onChange: (state: ResourceNamingState) => void }) {
  const [value, setValue] = useState<ResourceNamingState>({
    prefix: '',
    rawResourceNames: '',
    profile: null,
    error: null,
  });
  return (
    <ResourceNamingFields
      value={value}
      onChange={(next) => {
        setValue(next);
        onChange(next);
      }}
    />
  );
}

describe('ResourceNamingFields', () => {
  const onChange = vi.fn();

  beforeEach(() => {
    onChange.mockReset();
  });

  it('preserves the legacy naming contract while the optional prefix is blank', async () => {
    render(<FieldsHarness onChange={onChange} />);
    expect(screen.getByLabelText('Naming prefix')).toHaveValue('');
    expect(onChange).not.toHaveBeenCalled();
  });

  it('emits the simple prefix profile used by regulated customers', async () => {
    render(<FieldsHarness onChange={onChange} />);

    fireEvent.change(screen.getByLabelText('Naming prefix'), {
      target: { value: 'ecb' },
    });

    await waitFor(() => {
      expect(latest(onChange)).toEqual({
        prefix: 'ecb',
        rawResourceNames: '',
        profile: { prefix: 'ecb' },
        error: null,
      });
    });
  });

  it('rejects an invalid prefix instead of normalising it silently', async () => {
    render(<FieldsHarness onChange={onChange} />);

    fireEvent.change(screen.getByLabelText('Naming prefix'), {
      target: { value: 'ECB-prod' },
    });

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /start with a lowercase letter/i,
    );
    expect(latest(onChange).profile).toBeNull();
  });

  it('emits validated per-family templates', async () => {
    render(<FieldsHarness onChange={onChange} />);
    fireEvent.change(screen.getByLabelText('Naming prefix'), {
      target: { value: 'ecb' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Advanced resource templates' }),
    );
    fireEvent.change(screen.getByLabelText('Per-family templates (JSON)'), {
      target: {
        value: JSON.stringify({
          gateway: '{prefix}-{deployment}-gw',
          runtimeRole: '{prefix}-{deployment}-{suffix}-agent-role',
        }),
      },
    });

    await waitFor(() => {
      expect(latest(onChange)).toEqual({
        prefix: 'ecb',
        rawResourceNames: JSON.stringify({
          gateway: '{prefix}-{deployment}-gw',
          runtimeRole: '{prefix}-{deployment}-{suffix}-agent-role',
        }),
        profile: {
          prefix: 'ecb',
          resourceNames: {
            gateway: '{prefix}-{deployment}-gw',
            runtimeRole: '{prefix}-{deployment}-{suffix}-agent-role',
          },
        },
        error: null,
      });
    });
  });

  it('drops the previous valid profile as soon as advanced JSON becomes invalid', async () => {
    render(<FieldsHarness onChange={onChange} />);
    fireEvent.change(screen.getByLabelText('Naming prefix'), {
      target: { value: 'ecb' },
    });
    await waitFor(() => expect(latest(onChange).profile).toEqual({ prefix: 'ecb' }));

    fireEvent.click(
      screen.getByRole('button', { name: 'Advanced resource templates' }),
    );
    fireEvent.change(screen.getByLabelText('Per-family templates (JSON)'), {
      target: { value: '{"gateway":' },
    });

    expect(await screen.findByRole('alert')).toHaveTextContent(/valid JSON/i);
    expect(latest(onChange).profile).toBeNull();
  });

  it.each([
    ['unknown family', { notAResource: '{prefix}-{deployment}' }, /Unknown resource family/i],
    ['collision-prone fixed name', { gateway: '{prefix}-gw' }, /must include \{deployment\}/i],
    ['role without stack suffix', { runtimeRole: '{prefix}-{deployment}-role' }, /must include \{suffix\}/i],
  ])('rejects %s before the download request', async (_label, resourceNames, message) => {
    render(<FieldsHarness onChange={onChange} />);
    fireEvent.change(screen.getByLabelText('Naming prefix'), {
      target: { value: 'ecb' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Advanced resource templates' }),
    );
    fireEvent.change(screen.getByLabelText('Per-family templates (JSON)'), {
      target: { value: JSON.stringify(resourceNames) },
    });

    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    expect(latest(onChange).profile).toBeNull();
  });

  it('keeps the templates field reachable by its exact label once it holds saved templates', () => {
    // A wrapping <label> names a control after its content: with saved templates the
    // accessible name became the label text followed by the JSON, and an exact-name
    // lookup (Playwright, a screen reader's dialog) found nothing. Live, 2026-09-28.
    const saved = JSON.stringify(
      { gateway: '{prefix}-{deployment}-gw', runtime: '{prefix}_{deployment}_agent' },
      null,
      2,
    );
    render(
      <ResourceNamingFields
        value={{
          prefix: 'ecb',
          rawResourceNames: saved,
          profile: {
            prefix: 'ecb',
            resourceNames: {
              gateway: '{prefix}-{deployment}-gw',
              runtime: '{prefix}_{deployment}_agent',
            },
          },
          error: null,
        }}
        onChange={vi.fn()}
      />,
    );

    const templates = screen.getByLabelText('Per-family templates (JSON)');
    expect(templates.tagName).toBe('TEXTAREA');
    expect(templates).toHaveValue(saved);
    // Testing Library strips embedded controls' text before matching a label, so it cannot
    // see the browser's name; assert the structure that determines it: the textarea is not
    // inside any label, and its label reaches it through htmlFor.
    expect(templates.closest('label')).toBeNull();
    const label = document.querySelector('label[for="cfn-resource-name-templates"]');
    expect(label).not.toBeNull();
    expect(label?.textContent?.trim()).toBe('Per-family templates (JSON)');
    expect((label as HTMLLabelElement).control).toBe(templates);
    expect(screen.getByRole('button', { name: 'Hide advanced resource templates' })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    expect(screen.getByLabelText('Naming prefix')).toHaveValue('ecb');
  });

  it('rejects placeholders the selected resource family cannot resolve', async () => {
    render(<FieldsHarness onChange={onChange} />);
    fireEvent.change(screen.getByLabelText('Naming prefix'), {
      target: { value: 'ecb' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Advanced resource templates' }),
    );
    fireEvent.change(screen.getByLabelText('Per-family templates (JSON)'), {
      target: {
        value: JSON.stringify({
          gateway: '{prefix}-{deployment}-{component}',
        }),
      },
    });

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /unsupported placeholder \{component\}/i,
    );
    expect(latest(onChange).profile).toBeNull();
  });
});
