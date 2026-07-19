/**
 * ChatInterface regression tests.
 *
 * Guards the bug where a fresh chat session showed the empty-state placeholder
 * but NO message input, making the first message impossible to send: the input
 * must always be present, and the empty-state placeholder must render inside
 * the message area (not replace the whole component).
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ChatInterface } from './ChatInterface';

const baseProps = {
  chatMessages: [],
  testInput: '',
  isTesting: false,
  onTestInputChange: vi.fn(),
  onSendMessage: vi.fn(),
  onKeyDown: vi.fn(),
};

describe('ChatInterface', () => {
  it('renders the message input even with zero messages', () => {
    render(<ChatInterface {...baseProps} />);
    expect(screen.getByPlaceholderText('Type a message...')).toBeInTheDocument();
  });

  it('shows the empty-state placeholder alongside the input when there are no messages', () => {
    render(<ChatInterface {...baseProps} />);
    expect(screen.getByText('Chat with your Agent')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Type a message...')).toBeInTheDocument();
  });

  it('typing calls onTestInputChange and the send button fires onSendMessage', () => {
    const onChange = vi.fn();
    const onSend = vi.fn();
    const { rerender } = render(
      <ChatInterface {...baseProps} onTestInputChange={onChange} onSendMessage={onSend} />,
    );
    fireEvent.change(screen.getByPlaceholderText('Type a message...'), { target: { value: 'hi' } });
    expect(onChange).toHaveBeenCalledWith('hi');
    // With non-empty input the send button is enabled and fires.
    rerender(<ChatInterface {...baseProps} testInput="hi" onTestInputChange={onChange} onSendMessage={onSend} />);
    fireEvent.click(screen.getByRole('button'));
    expect(onSend).toHaveBeenCalled();
  });

  it('hides the empty-state once messages exist', () => {
    render(
      <ChatInterface
        {...baseProps}
        chatMessages={[{ id: '1', role: 'user', content: 'hello', timestamp: new Date() }]}
      />,
    );
    expect(screen.queryByText('Chat with your Agent')).not.toBeInTheDocument();
    expect(screen.getByText('hello')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Type a message...')).toBeInTheDocument();
  });
});
