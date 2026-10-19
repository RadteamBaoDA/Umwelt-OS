import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { ChatTranscript } from '../../chat-transcript';

type Handler = (prompt: string) => void | Promise<unknown>;

// Expose which prompt handler each widget kind receives from the transcript.
const received: Record<string, Handler | undefined> = {};
vi.mock('../components/tool-rendering', () => ({
  ToolRendering: ({ payload, onSendPrompt }: { payload: { kind: string }; onSendPrompt?: Handler }) => {
    received[payload.kind] = onSendPrompt;
    return <div data-testid={`w-${payload.kind}`} />;
  },
}));
vi.mock('@/core/query-provider', () => ({
  useDisplayPreferences: () => ({ locale: 'en-US', timezone: 'UTC' }),
}));

const form = { kind: 'form', part_id: 'f', status: 'streaming', title: 'F', form: { fields: [] } };
const table = { kind: 'table', part_id: 't', status: 'streaming', title: 'T', table: { columns: [], rows: [] } };

describe('ChatTranscript widget prompt handlers', () => {
  beforeAll(() => {
    Element.prototype.scrollTo = (() => undefined) as unknown as typeof Element.prototype.scrollTo;
    window.matchMedia = ((query: string) => ({
      matches: false, media: query, addEventListener: () => undefined, removeEventListener: () => undefined,
      addListener: () => undefined, removeListener: () => undefined, onchange: null, dispatchEvent: () => false,
    })) as unknown as typeof window.matchMedia;
  });

  it('gives form widgets the rejecting handler and other widgets the plain one', async () => {
    const plain = vi.fn();
    const rejecting = vi.fn().mockRejectedValue(new Error('busy'));
    render(
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <ChatTranscript
          conversationId="c1" messages={[]} isStreaming streamingUi={[form, table]}
          onSendPrompt={plain} onWidgetSendPrompt={rejecting}
        />
      </NextIntlClientProvider>,
    );
    expect(screen.getByTestId('w-form')).toBeTruthy();
    expect(received.table).toBe(plain);
    expect(received.form).toBe(rejecting);
    await expect(received.form?.('hi')).rejects.toThrow('busy');
  });
});
