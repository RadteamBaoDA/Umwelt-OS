import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import type { ChatMessage } from './api';
import { buildRenderEntries } from './chat-versions';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ confirmedPreferences: { locale: 'en-us', timezone: 'UTC' } }) }));
vi.mock('@/modules/news/api', () => ({ fetchTopics: vi.fn().mockRejectedValue(new Error('offline')) }));

import { ChatTranscript } from './chat-transcript';

Element.prototype.scrollTo = vi.fn();
vi.stubGlobal('matchMedia', (q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }));

/** Minimal persisted message. */
function msg(id: string, role: 'user' | 'assistant', content: string, revisionOf: string | null = null): ChatMessage {
  return {
    id, conversation_id: 'c1', role, content, client_request_id: null, model_identity: null, citations: [],
    response_id: `r-${id}`, revision_of_message_id: revisionOf, created_at: '2026-11-10T10:00:00Z',
  };
}

// u1/a1 -> edit u2 (revises u1) answered by a2 (revises u1, as the worker stores it) -> regenerate: u3 (copy of u2, revises a2) + a3 (revises a2).
const edited = [
  msg('u1', 'user', 'first'), msg('a1', 'assistant', 'answer one'),
  msg('u2', 'user', 'first, edited', 'u1'), msg('a2', 'assistant', 'answer two', 'u1'),
  msg('u3', 'user', 'first, edited', 'a2'), msg('a3', 'assistant', 'answer three', 'a2'),
];

describe('buildRenderEntries', () => {
  it('passes unrevised turns through untouched', () => {
    const plain = [msg('u1', 'user', 'q'), msg('a1', 'assistant', 'a')];
    expect(buildRenderEntries(plain, {}).map((e) => [e.msg.id, e.latest, e.nav])).toEqual([['u1', true, undefined], ['a1', true, undefined]]);
  });

  it('chains edit and regenerate revisions and shows the newest version by default', () => {
    const entries = buildRenderEntries(edited, {});
    expect(entries.map((e) => e.msg.id)).toEqual(['u3', 'a3']);
    // Prompts differ between versions, so the control lives on the user message.
    expect(entries[0].nav).toMatchObject({ index: 2, count: 3, versionIds: ['u1', 'u2', 'u3'] });
    expect(entries[1].nav).toBeUndefined();
    expect(entries.every((e) => e.latest)).toBe(true);
  });

  it('shows the chosen older version with its own answer and marks it not latest', () => {
    const entries = buildRenderEntries(edited, { u1: 'u2' });
    expect(entries.map((e) => [e.msg.id, e.latest])).toEqual([['u2', false], ['a2', false]]);
    expect(entries[0].nav?.index).toBe(1);
  });

  it('hosts the control on the answer when only regenerated (same prompt)', () => {
    const regen = [msg('u1', 'user', 'q'), msg('a1', 'assistant', 'a'), msg('u2', 'user', 'q', 'a1'), msg('a2', 'assistant', 'b', 'a1')];
    const entries = buildRenderEntries(regen, {});
    expect(entries.map((e) => e.msg.id)).toEqual(['u2', 'a2']);
    expect(entries[0].nav).toBeUndefined();
    expect(entries[1].nav).toMatchObject({ index: 1, count: 2 });
  });

  it('keeps the revised thread chronological and ignores a missing parent', () => {
    const thread = [
      msg('u1', 'user', 'one'), msg('a1', 'assistant', 'A1'),
      msg('u2', 'user', 'two'), msg('a2', 'assistant', 'A2'),
      msg('u3', 'user', 'one v2', 'u1'), msg('a3', 'assistant', 'A3', 'u1'),
      msg('u4', 'user', 'orphan', 'gone'), msg('a4', 'assistant', 'A4', 'gone'),
    ];
    expect(buildRenderEntries(thread, {}).map((e) => e.msg.id)).toEqual(['u2', 'a2', 'u3', 'a3', 'u4', 'a4']);
  });
});

/** Renders the transcript with English messages. */
function renderTranscript(props: Partial<React.ComponentProps<typeof ChatTranscript>>) {
  return render(
    <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
      <ChatTranscript conversationId="c1" messages={[]} mode="full" {...props} />
    </NextIntlClientProvider>,
  );
}

describe('version navigation in the transcript', () => {
  it('shows n/m, navigates to an older version and hides edit/regenerate there', async () => {
    renderTranscript({ messages: edited, onEditMessage: vi.fn(), onRegenerateMessage: vi.fn() });
    expect(screen.getByText('3/3')).toBeTruthy();
    expect(screen.getByText('answer three')).toBeTruthy();
    expect(screen.queryByText('answer two')).toBeNull();
    expect(screen.getByRole('button', { name: messages['en-us'].chat.versionNext }).hasAttribute('disabled')).toBe(true);
    expect(screen.getByRole('button', { name: messages['en-us'].chat.editPrompt })).toBeTruthy();
    expect(screen.getByRole('button', { name: messages['en-us'].chat.regenerateAnswer })).toBeTruthy();

    await userEvent.click(screen.getByRole('button', { name: messages['en-us'].chat.versionPrev }));
    expect(screen.getByText('2/3')).toBeTruthy();
    expect(screen.getByText('answer two')).toBeTruthy();
    expect(screen.queryByText('answer three')).toBeNull();
    expect(screen.queryByRole('button', { name: messages['en-us'].chat.editPrompt })).toBeNull();
    expect(screen.queryByRole('button', { name: messages['en-us'].chat.regenerateAnswer })).toBeNull();

    await userEvent.click(screen.getByRole('button', { name: messages['en-us'].chat.versionPrev }));
    expect(screen.getByText('answer one')).toBeTruthy();
    expect(screen.getByRole('button', { name: messages['en-us'].chat.versionPrev }).hasAttribute('disabled')).toBe(true);
    await userEvent.click(screen.getByRole('button', { name: messages['en-us'].chat.versionNext }));
    await userEvent.click(screen.getByRole('button', { name: messages['en-us'].chat.versionNext }));
    expect(screen.getByText('3/3')).toBeTruthy();
  });

  it('shows no control for an unrevised conversation', () => {
    renderTranscript({ messages: [msg('u1', 'user', 'q'), msg('a1', 'assistant', 'a')] });
    expect(screen.queryByTestId('version-nav')).toBeNull();
  });
});
