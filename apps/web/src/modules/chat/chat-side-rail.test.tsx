import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it } from 'vitest';
import { messages } from '@/core/messages';
import { ChatSourcesSheet } from './chat-side-rail';

describe('ChatSourcesSheet', () => {
  it('opens the evidence tabs from a labelled mobile trigger', async () => {
    render(
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <ChatSourcesSheet citations={[]} context={null} />
      </NextIntlClientProvider>,
    );
    await userEvent.click(screen.getByRole('button', { name: 'Open sources and evidence' }));
    expect(await screen.findByText('No sources cited in this conversation yet.')).toBeTruthy();
  });
});
