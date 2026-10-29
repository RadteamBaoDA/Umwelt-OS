import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it } from 'vitest';
import { messages } from '@/core/messages';
import { ChatReasoning } from './chat-reasoning';

const wrap = (node: React.ReactNode) => (
  <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>{node}</NextIntlClientProvider>
);
const block = () => screen.getByTestId('chat-reasoning') as HTMLDetailsElement;

describe('ChatReasoning', () => {
  it('is open and labelled Thinking while streaming with no answer, then collapses when the answer starts', () => {
    const { rerender } = render(wrap(<ChatReasoning text="step" streaming answerStarted={false} />));
    expect(block().open).toBe(true);
    expect(block().textContent).toContain('Thinking…');

    rerender(wrap(<ChatReasoning text="step" streaming answerStarted />));
    expect(block().open).toBe(false);
    expect(block().textContent).toContain('Thought process');
  });

  it('mounts collapsed for persisted reasoning and renders it as plain text, never HTML', async () => {
    render(wrap(<ChatReasoning text={'<b>bold</b>\nline two'} />));
    expect(block().open).toBe(false);
    await userEvent.click(screen.getByText('Thought process'));
    expect(block().open).toBe(true);
    expect(block().querySelector('b')).toBeNull();
    expect(block().textContent).toContain('<b>bold</b>');
  });

  it('keeps the reader choice once the summary was clicked', async () => {
    const { rerender } = render(wrap(<ChatReasoning text="step" streaming answerStarted={false} />));
    await userEvent.click(screen.getByText('Thinking…'));
    expect(block().open).toBe(false);
    rerender(wrap(<ChatReasoning text="step" streaming answerStarted />));
    expect(block().open).toBe(false);
  });

  it('renders nothing for empty text', () => {
    render(wrap(<ChatReasoning text="" streaming />));
    expect(screen.queryByTestId('chat-reasoning')).toBeNull();
  });
});
