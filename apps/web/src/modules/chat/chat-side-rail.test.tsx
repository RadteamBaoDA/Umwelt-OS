import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { ChatSideRail, ChatSourcesSheet } from './chat-side-rail';
import { focusCitations } from './chat-citation-focus';
import type { Citation } from './api';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ confirmedPreferences: { locale: 'en-us', timezone: 'UTC' } }) }));

describe('ChatSourcesSheet', () => {
  it('opens the evidence tabs from a labelled mobile trigger', async () => {
    const web = { sourceType: 'web', url: 'https://example.com/a', title: 'Example', quote: 'An excerpt' } as unknown as Citation;
    render(
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <ChatSourcesSheet citations={[web]} context={null} />
      </NextIntlClientProvider>,
    );
    await userEvent.click(screen.getByRole('button', { name: 'Open sources and evidence' }));
    expect(await screen.findByText('An excerpt')).toBeTruthy();
  });

  it('renders no trigger when there is nothing to show', () => {
    render(
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <ChatSourcesSheet citations={[]} context={null} />
      </NextIntlClientProvider>,
    );
    expect(screen.queryByRole('button', { name: 'Open sources and evidence' })).toBeNull();
  });
});

describe('ChatSideRail', () => {
  const wrap = (node: React.ReactNode) => (
    <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>{node}</NextIntlClientProvider>
  );

  it('renders nothing when there is no rail data', () => {
    const { container } = render(wrap(<ChatSideRail citations={[]} context={null} />));
    expect(container.querySelector('#chat-evidence-rail')).toBeNull();
  });

  it('lists cited sources as rows when citations exist', () => {
    const web = { sourceType: 'web', url: 'https://example.com/a', title: 'Example', quote: 'An excerpt' } as unknown as Citation;
    render(wrap(<ChatSideRail citations={[web]} context={null} />));
    expect(screen.getAllByTestId('rail-source')).toHaveLength(1);
    expect(screen.getByText('An excerpt')).toBeTruthy();
  });
});

describe('citation focus', () => {
  const web = { sourceType: 'web', url: 'https://example.com/a', title: 'Example', quote: 'An excerpt' } as unknown as Citation;
  const other = { sourceType: 'web', url: 'https://example.com/b', title: 'Other', quote: 'Second excerpt' } as unknown as Citation;
  const wrap = (node: React.ReactNode) => (
    <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>{node}</NextIntlClientProvider>
  );

  it('highlights the requested source in the rail and re-opens a collapsed rail', async () => {
    render(wrap(<ChatSideRail citations={[web, other]} context={null} />));
    await userEvent.click(screen.getByRole('button', { name: /^Collapse sources/ }));
    expect(screen.queryAllByTestId('rail-source')).toHaveLength(0);
    act(() => focusCitations([other]));
    const cards = screen.getAllByTestId('rail-source');
    expect(cards).toHaveLength(2);
    expect(cards[1].getAttribute('aria-current')).toBe('true');
    expect(cards[0].getAttribute('aria-current')).toBeNull();
  });

  it('opens the mobile sheet on the requested source', async () => {
    render(wrap(<ChatSourcesSheet citations={[web, other]} context={null} />));
    act(() => focusCitations([web]));
    const cards = await screen.findAllByTestId('rail-source');
    expect(cards[0].getAttribute('aria-current')).toBe('true');
  });
});
