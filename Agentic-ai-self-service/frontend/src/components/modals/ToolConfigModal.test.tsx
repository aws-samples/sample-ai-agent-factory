/**
 * ToolConfigModal — regression test for the "custom tool Configure does nothing"
 * bug. Before the fix, no modal rendered for custom/built-in tool nodes. These
 * tests prove the modal renders, edits the surfaced fields, and preserves every
 * hidden field (toolId, isCustom, lambdaCode, inputSchema) on save.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ToolConfigModal } from './ToolConfigModal';
import type { ToolConfiguration } from '../../types/components';

const customTool: ToolConfiguration = {
  name: 'CreateJiraTicket',
  toolId: 'create_jira_ticket',
  description: 'Creates a Jira ticket from a confirmed issue.',
  enabled: true,
  isCustom: true,
  displayName: 'Jira Ticket Creator',
  lambdaCode: 'def handler(event, context):\n    return {"ok": True}',
  inputSchema: { type: 'object', properties: { summary: { type: 'string' } }, required: ['summary'] },
};

describe('ToolConfigModal', () => {
  it('renders an editable modal for a custom tool (the bug: nothing rendered)', () => {
    render(
      <ToolConfigModal isOpen onClose={() => {}} onSave={() => {}} initialConfig={customTool} />,
    );
    // Modal is present with the tool's name in the title field.
    expect(screen.getByTestId('configuration-modal')).toBeTruthy();
    const nameField = screen.getByTestId('field-displayName') as HTMLInputElement;
    expect(nameField.value).toBe('Jira Ticket Creator');
    // Custom tools expose an Implementation tab with the generated code/schema.
    expect(screen.getByTestId('tab-implementation')).toBeTruthy();
  });

  it('preserves hidden fields (toolId/isCustom/lambdaCode/inputSchema) on save', () => {
    const onSave = vi.fn();
    render(
      <ToolConfigModal isOpen onClose={() => {}} onSave={onSave} initialConfig={customTool} />,
    );

    fireEvent.change(screen.getByTestId('field-description'), {
      target: { value: 'Updated description.' },
    });
    fireEvent.click(screen.getByTestId('modal-save-button'));

    expect(onSave).toHaveBeenCalledTimes(1);
    const saved = onSave.mock.calls[0][0] as ToolConfiguration;
    expect(saved.description).toBe('Updated description.');
    // Hidden / non-surfaced fields survive untouched.
    expect(saved.toolId).toBe('create_jira_ticket');
    expect(saved.isCustom).toBe(true);
    expect(saved.lambdaCode).toBe(customTool.lambdaCode);
    expect(saved.inputSchema).toEqual(customTool.inputSchema);
    // name and displayName are kept in sync.
    expect(saved.name).toBe('Jira Ticket Creator');
    expect(saved.displayName).toBe('Jira Ticket Creator');
  });

  it('blocks save when the tool name is empty', () => {
    const onSave = vi.fn();
    render(
      <ToolConfigModal
        isOpen
        onClose={() => {}}
        onSave={onSave}
        initialConfig={{ ...customTool, name: '', displayName: '' }}
      />,
    );
    const saveBtn = screen.getByTestId('modal-save-button') as HTMLButtonElement;
    expect(saveBtn.disabled).toBe(true);
    fireEvent.click(saveBtn);
    expect(onSave).not.toHaveBeenCalled();
  });

  it('hides the Implementation tab for built-in tools (no generated code)', () => {
    render(
      <ToolConfigModal
        isOpen
        onClose={() => {}}
        onSave={() => {}}
        initialConfig={{
          name: 'Web Search',
          toolId: 'duckduckgo_search',
          description: 'Search the web.',
          enabled: true,
          isCustom: false,
        }}
      />,
    );
    expect(screen.queryByTestId('tab-implementation')).toBeNull();
  });
});
