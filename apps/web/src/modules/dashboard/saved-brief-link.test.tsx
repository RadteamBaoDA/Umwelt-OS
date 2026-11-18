import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { getCatalog } from '@locales';

const api = vi.hoisted(() => ({ getBriefSlotOwner: vi.fn() }));
const automations = vi.hoisted(() => ({ getRuleConversationId: vi.fn() }));
vi.mock('@/modules/automations', () => automations);
vi.mock('./daily-api', () => ({ dailyKeys: { slotOwner: ['brief-slot-owner'] }, ...api }));
vi.mock('./brief-text', () => ({ BriefText: () => null }));
vi.mock('./brief-sharing', () => ({ BriefShareButton: () => null, SharedBriefs: () => null }));
vi.mock('./date-selector', () => ({ DateSelector: () => null }));
vi.mock('./selected-day', () => ({ useSelectedDay: () => ({ date: '2026-10-09', timezone: 'UTC' }) }));
vi.mock('@/core/app-shell/chat-controller', () => ({ useChatController: () => ({}) }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({}) }));
vi.mock('@/core/session', () => ({ useSession: () => ({}) }));
vi.mock('@/core/api', () => ({ ApiError: class extends Error {} }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));

import { SavedBriefBody } from './daily-brief';

const brief = {
  id: 'b1',
  brief_date: '2026-10-09',
  timezone: 'UTC',
  revision: 1,
  status: 'current' as const,
  content: 'x',
  citations: [],
  model_alias: 'm',
  generated_at: '2026-10-09T00:00:00Z',
};
const view = () => (
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <NextIntlClientProvider locale="en-US" messages={{ daily: getCatalog('en-us', 'daily') }}>
      <SavedBriefBody brief={brief} />
    </NextIntlClientProvider>
  </QueryClientProvider>
);

describe('SavedBriefBody full-brief link', () => {
  beforeEach(() => vi.clearAllMocks());

  it('links a story citation to the News story and leaves other kinds as text', async () => {
    api.getBriefSlotOwner.mockResolvedValue({ logical_job: 'daily_brief', schedule_owner: 'internal_brief', automation_id: null });
    const cited = {
      ...brief,
      citations: [
        { ref: 1, kind: 'stories', id: 's1', title: 'Story one', source_ids: [] },
        { ref: 2, kind: 'weather', id: 'w1', title: 'Rain', source_ids: [] },
      ],
    };
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <NextIntlClientProvider locale="en-US" messages={{ daily: getCatalog('en-us', 'daily') }}>
          <SavedBriefBody brief={cited} />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    expect(await screen.findByRole('link', { name: 'Story one' })).toHaveAttribute('href', '/news?story=s1');
    expect(screen.queryByRole('link', { name: 'Rain' })).toBeNull();
  });

  it('is shown while an automation owns the brief slot', async () => {
    api.getBriefSlotOwner.mockResolvedValue({ logical_job: 'daily_brief', schedule_owner: 'automation', automation_id: 'a1' });
    automations.getRuleConversationId.mockResolvedValue('c1');
    render(view());
    const link = await screen.findByRole('link', { name: 'Open full brief' });
    await vi.waitFor(() => expect(link.getAttribute('href')).toBe('/chat/c1'));
  });

  it('falls back to Scheduled tasks while the task has no thread yet', async () => {
    api.getBriefSlotOwner.mockResolvedValue({ logical_job: 'daily_brief', schedule_owner: 'automation', automation_id: 'a1' });
    automations.getRuleConversationId.mockResolvedValue(null);
    render(view());
    const link = await screen.findByRole('link', { name: 'Open full brief' });
    await vi.waitFor(() => expect(automations.getRuleConversationId).toHaveBeenCalledWith('a1'));
    expect(link.getAttribute('href')).toBe('/settings/scheduled-tasks');
  });

  it('is hidden when the internal cron owns the slot (no Daily brief task)', async () => {
    api.getBriefSlotOwner.mockResolvedValue({ logical_job: 'daily_brief', schedule_owner: 'internal_brief', automation_id: null });
    render(view());
    await vi.waitFor(() => expect(api.getBriefSlotOwner).toHaveBeenCalled());
    expect(screen.queryByRole('link', { name: 'Open full brief' })).toBeNull();
  });
});
