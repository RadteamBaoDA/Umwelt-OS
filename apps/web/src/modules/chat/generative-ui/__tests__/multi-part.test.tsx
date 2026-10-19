import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import type { ChatMessage } from '@/modules/chat/api';
import { ChatTranscript } from '../../chat-transcript';
import { messageUiParts, upsertUiPart, uiPartKey } from '../../generative-ui-parts';

// The renderer itself is covered elsewhere; here it only has to show which part it received.
vi.mock('../components/tool-rendering', () => ({
  ToolRendering: ({ payload }: { payload: { part_id?: string; title?: string; status?: string } }) => (
    <div data-testid={`part-${payload.part_id}`}>{payload.title}:{payload.status}</div>
  ),
}));
vi.mock('@/core/query-provider', () => ({
  useDisplayPreferences: () => ({ locale: 'en-US', timezone: 'UTC' }),
}));

const table = { kind: 'table', part_id: 'a', status: 'completed', title: 'Sales', table: { columns: [], rows: [] } };
const plan = { kind: 'plan', part_id: 'b', status: 'completed', title: 'Roadmap', plan: { steps: [] } };

function assistant(extra: Partial<ChatMessage>): ChatMessage {
  return {
    id: 'm1', conversation_id: 'c1', role: 'assistant', content: 'Here you go.', client_request_id: null,
    model_identity: 'm', citations: [], response_id: null, revision_of_message_id: null,
    created_at: '2026-01-01T00:00:00Z', ...extra,
  };
}

function view(props: Partial<React.ComponentProps<typeof ChatTranscript>>) {
  return render(
    <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
      <ChatTranscript conversationId="c1" messages={[]} {...props} />
    </NextIntlClientProvider>,
  );
}

describe('multiple generative UI parts', () => {
  beforeAll(() => {
    // jsdom has no matchMedia/scrollTo; the reveal/scroll hooks use them.
    Element.prototype.scrollTo = (() => undefined) as unknown as typeof Element.prototype.scrollTo;
    window.matchMedia = ((query: string) => ({
      matches: false, media: query, addEventListener: () => undefined, removeEventListener: () => undefined,
      addListener: () => undefined, removeListener: () => undefined, onchange: null, dispatchEvent: () => false,
    })) as unknown as typeof window.matchMedia;
  });

  it('renders every persisted part of a message in order', () => {
    view({ messages: [assistant({ generative_ui: table, generative_ui_parts: [table, plan] })] });
    const widgets = screen.getAllByTestId('generative-ui-widget');
    expect(widgets.map((w) => w.textContent)).toEqual(['Sales:completed', 'Roadmap:completed']);
  });

  it('falls back to the single legacy generative_ui field', () => {
    view({ messages: [assistant({ generative_ui: table })] });
    expect(screen.getAllByTestId('generative-ui-widget')).toHaveLength(1);
  });

  it('renders streaming parts side by side', () => {
    view({ isStreaming: true, streamingUi: [{ ...table, status: 'streaming' }, { ...plan, status: 'streaming' }] });
    expect(screen.getAllByTestId('streaming-generative-ui-widget').map((w) => w.textContent))
      .toEqual(['Sales:streaming', 'Roadmap:streaming']);
  });

  it('upserts streamed snapshots by part_id and keeps arrival order', () => {
    let parts = upsertUiPart([], { part_id: 'a', n: 1 });
    parts = upsertUiPart(parts, { part_id: 'b', n: 1 });
    parts = upsertUiPart(parts, { part_id: 'a', n: 2 });
    expect(parts).toEqual([{ part_id: 'a', n: 2 }, { part_id: 'b', n: 1 }]);
    expect(upsertUiPart([{ n: 1 }], { n: 2 })).toEqual([{ n: 2 }]); // legacy payloads without part_id
    expect(messageUiParts({})).toEqual([]);
    expect(uiPartKey({}, 3)).toBe('part-3');
  });
});
