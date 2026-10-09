import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { translationsMessages } from '@/core/messages/translations';

const api = vi.hoisted(() => ({ fetchTranslationSettings: vi.fn(), submitTranslationBatch: vi.fn(), readTranslationBatch: vi.fn() }));
vi.mock('@/modules/translations/api', () => api);
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'c' }) }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ selection: { id: 'w1' } }) }));
vi.mock('@/core/api', () => ({ workspaceGeneration: () => 1, ApiError: class extends Error {} }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/modules/knowledge/api', () => ({}));
vi.mock('@/modules/knowledge/document-sharing', () => ({ ShareDialog: () => null, shareErrorKey: () => 'loadFailed' }));
vi.mock('./daily-api', () => ({ dailyKeys: { briefs: (d: string, z: string) => ['b', d, z] }, listBriefRevisions: vi.fn() }));

import { BriefText } from './daily-brief';
import { SharedBriefs } from './brief-sharing';
import { listBriefRevisions } from './daily-api';

const brief = (revision: number, content = 'Original text') => ({
  id: 'b1', brief_date: '2026-10-09', timezone: 'UTC', revision, status: 'current' as const, content,
  citations: [], model_alias: 'm', generated_at: '2026-10-09T00:00:00Z',
});
const view = (b: ReturnType<typeof brief>) => (
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <NextIntlClientProvider locale="en-US" messages={{ translations: translationsMessages['en-us'] }}><BriefText brief={b} /></NextIntlClientProvider>
  </QueryClientProvider>
);

describe('Daily Brief translation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchTranslationSettings.mockResolvedValue({ enabled: true, target_language: 'vi', configuration_revision: 1 });
    api.submitTranslationBatch.mockResolvedValue({
      batch_id: null, items: [{ resource_type: 'daily_brief', resource_id: 'b1', status: 'ready', translation: { content: 'Ban dich' } }],
    });
  });

  it('requests only the selected revision, shows the translation and toggles to the original', async () => {
    render(view(brief(2)));
    expect(screen.getByText('Original text')).toBeTruthy();
    expect(await screen.findByText('Ban dich')).toBeTruthy();
    expect(api.submitTranslationBatch.mock.calls[0][0]).toEqual([{ resource_type: 'daily_brief', resource_id: 'b1', resource_revision: '2' }]);
    await userEvent.click(screen.getByRole('button', { name: 'View original' }));
    expect(screen.getByText('Original text')).toBeTruthy();
    expect(screen.queryByText('Ban dich')).toBeNull();
  });

  it('shows no badge and requests nothing when translation is disabled', async () => {
    api.fetchTranslationSettings.mockResolvedValue({ enabled: false, target_language: 'vi', configuration_revision: 1 });
    render(view(brief(1)));
    await screen.findByText('Original text');
    expect(api.submitTranslationBatch).not.toHaveBeenCalled();
    expect(screen.queryByText('Auto-translated')).toBeNull();
  });

  it('translates the member shared brief and toggles to the original', async () => {
    vi.mocked(listBriefRevisions).mockResolvedValue([brief(2)]);
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <NextIntlClientProvider locale="en-US" messages={{ translations: translationsMessages['en-us'], sharing: { sharedBriefs: 'Shared briefs', loading: '…', noSharedBriefs: '-', briefMeta: 'r{revision} {time}' } }}>
          <SharedBriefs date="2026-10-09" timezone="UTC" />
        </NextIntlClientProvider>
      </QueryClientProvider>);
    expect(await screen.findByText('Ban dich')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'View original' }));
    expect(screen.getByText('Original text')).toBeTruthy();
  });
});
