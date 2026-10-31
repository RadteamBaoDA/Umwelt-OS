import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ confirmedPreferences: { locale: 'en-us', timezone: 'UTC' } }) }));

import { ChatTranscript } from './chat-transcript';

vi.stubGlobal('matchMedia', (q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }));

/** Renders the transcript with English messages. */
function renderTranscript(props: Partial<React.ComponentProps<typeof ChatTranscript>>) {
  return render(
    <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
      <ChatTranscript messages={[]} mode="full" {...props} />
    </NextIntlClientProvider>,
  );
}

describe('ChatTranscript empty state', () => {
  it('shows one sentence and three plain prompt buttons that send their prompt', async () => {
    const onSendPrompt = vi.fn();
    renderTranscript({ onSendPrompt });
    expect(screen.getByText(messages['en-us'].chat.emptyStateDescription)).toBeTruthy();
    const group = screen.getByTestId('starter-suggestions');
    const buttons = group.querySelectorAll('button');
    expect(buttons).toHaveLength(3);
    await userEvent.click(buttons[0]);
    expect(onSendPrompt).toHaveBeenCalledTimes(1);
  });
});
