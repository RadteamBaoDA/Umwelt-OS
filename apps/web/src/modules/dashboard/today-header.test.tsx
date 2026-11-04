import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';

const mocks = vi.hoisted(() => ({ fetchTopics: vi.fn(), fetchStories: vi.fn() }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/modules/news/api', () => ({ fetchTopics: mocks.fetchTopics }));
vi.mock('@/modules/news/story-api', () => ({ fetchStories: mocks.fetchStories }));

import { consumeLastVisit, greetingKey, LAST_VISIT_KEY, TodayHeader } from './today-header';

/** Renders the header with English messages and a fresh query cache. */
function renderHeader(name?: string) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <TodayHeader name={name} sourceIds={['s1']} />
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
}

describe('TodayHeader', () => {
  beforeEach(() => { window.localStorage.clear(); window.sessionStorage.clear(); vi.useFakeTimers({ toFake: ['Date'] }); });
  afterEach(() => vi.useRealTimers());

  it('maps hours to greetings', () => {
    expect([greetingKey(8), greetingKey(14), greetingKey(20), greetingKey(2)]).toEqual(['greetingMorning', 'greetingAfternoon', 'greetingEvening', 'greetingEvening']);
  });
  it('greets by time of day with the name, or without it', () => {
    vi.setSystemTime(new Date('2026-11-03T14:00:00Z'));
    const { unmount } = renderHeader('Bao');
    expect(screen.getByText(/Good afternoon, Bao/)).toBeTruthy();
    unmount();
    renderHeader();
    expect(screen.getByText(/Good afternoon/).textContent).not.toContain('Bao');
  });
  it('stores the visit and returns the previous one', () => {
    window.localStorage.setItem(LAST_VISIT_KEY, '2026-11-03T08:00:00.000Z');
    expect(consumeLastVisit(Date.parse('2026-11-03T10:00:00Z'))).toBe(Date.parse('2026-11-03T08:00:00Z'));
    expect(window.localStorage.getItem(LAST_VISIT_KEY)).toBe('2026-11-03T10:00:00.000Z');
  });
  it('pins the baseline for the browser session so repeated calls agree', () => {
    window.localStorage.setItem(LAST_VISIT_KEY, '2026-11-03T08:00:00.000Z');
    const first = consumeLastVisit(Date.parse('2026-11-03T10:00:00Z'));
    expect(consumeLastVisit(Date.parse('2026-11-03T12:00:00Z'))).toBe(first);
    window.sessionStorage.clear();
    window.localStorage.clear();
    expect(consumeLastVisit(Date.parse('2026-11-03T10:00:00Z'))).toBeNull();
    expect(consumeLastVisit(Date.parse('2026-11-03T12:00:00Z'))).toBeNull();
  });
  it('shows per-topic counts of stories newer than the last visit', async () => {
    window.localStorage.setItem(LAST_VISIT_KEY, '2026-11-03T10:00:00.000Z');
    vi.setSystemTime(new Date('2026-11-03T14:00:00Z'));
    mocks.fetchTopics.mockResolvedValue({ items: [{ id: 't1', name: 'AI' }] });
    mocks.fetchStories.mockResolvedValue({ items: [{ observed_at: '2026-11-03T11:00:00Z' }, { observed_at: '2026-11-03T09:00:00Z' }] });
    renderHeader();
    expect(await screen.findByText('Since you last looked: AI 1')).toBeTruthy();
  });
});
