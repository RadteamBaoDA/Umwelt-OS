import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { getCatalog } from '@locales';

const api = vi.hoisted(() => ({ fetchTranslationSettings: vi.fn(), submitTranslationBatch: vi.fn(), readTranslationBatch: vi.fn() }));
vi.mock('@/modules/translations/api', () => api);
vi.mock('@/core/app-shell/workspace-session', () => ({ useWorkspaceSession: () => ({ csrfToken: 'c' }) }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ selection: { id: 'w1' } }) }));
vi.mock('@/core/api', () => ({ retrySessionConflict: () => false, workspaceGeneration: () => 1, ApiError: class extends Error {} }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/modules/knowledge/api', () => ({}));
vi.mock('@/modules/knowledge/document-sharing', () => ({ ShareDialog: () => null, shareErrorKey: () => 'loadFailed' }));
vi.mock('./daily-api', () => ({ dailyKeys: { briefs: (d: string, z: string) => ['b', d, z] }, listBriefRevisions: vi.fn() }));

import { BriefText } from './daily-brief';
import { SharedBriefs } from './brief-sharing';
import { listBriefRevisions } from './daily-api';

const brief = (revision: number, content = 'Original text') => ({
  id: 'b1',
  brief_date: '2026-10-09',
  timezone: 'UTC',
  revision,
  status: 'current' as const,
  content,
  citations: [],
  model_alias: 'm',
  generated_at: '2026-10-09T00:00:00Z',
});
const view = (b: ReturnType<typeof brief>) => (
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <NextIntlClientProvider locale="en-US" messages={{ translations: getCatalog('en-us', 'translations') }}>
      <BriefText brief={b} />
    </NextIntlClientProvider>
  </QueryClientProvider>
);

describe('Daily Brief on-demand translation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchTranslationSettings.mockResolvedValue({ enabled: true, target_language: 'vi', configuration_revision: 1 });
    api.submitTranslationBatch.mockResolvedValue({
      batch_id: 'batch-1',
      items: [{ resource_type: 'daily_brief', resource_id: 'b1', status: 'ready' }],
    });
    api.readTranslationBatch.mockResolvedValue({
      batch_id: 'batch-1',
      target_language: 'vi',
      items: [
        {
          resource_type: 'daily_brief',
          resource_id: 'b1',
          status: 'ready',
          translation: { content: 'Ban dich' },
          original_revision: '2',
          error_code: null,
        },
      ],
    });
  });

  it('does not auto-translate on mount; translates on click, shows the translation and toggles to the original', async () => {
    render(view(brief(2)));
    expect(screen.getByText('Original text')).toBeTruthy();
    const translateBtn = await screen.findByRole('button', { name: 'Translate to Vietnamese' });
    expect(api.submitTranslationBatch).not.toHaveBeenCalled();

    await userEvent.click(translateBtn);
    expect(await screen.findByText('Ban dich')).toBeTruthy();
    expect(api.submitTranslationBatch.mock.calls[0][0]).toEqual([
      { resource_type: 'daily_brief', resource_id: 'b1', resource_revision: '2' },
    ]);
    await userEvent.click(screen.getByRole('button', { name: 'View original' }));
    expect(screen.getByText('Original text')).toBeTruthy();
    expect(screen.queryByText('Ban dich')).toBeNull();
  });

  it('shows a short reason line with settings link when the translation is blocked', async () => {
    api.submitTranslationBatch.mockResolvedValue({
      batch_id: 'batch-2',
      items: [{ resource_type: 'daily_brief', resource_id: 'b1', status: 'blocked' }],
    });
    api.readTranslationBatch.mockResolvedValue({
      batch_id: 'batch-2',
      target_language: 'vi',
      items: [
        {
          resource_type: 'daily_brief',
          resource_id: 'b1',
          status: 'blocked',
          translation: null,
          original_revision: '3',
          error_code: 'privacy_blocked',
        },
      ],
    });
    render(view(brief(3)));
    await userEvent.click(await screen.findByRole('button', { name: 'Translate to Vietnamese' }));
    expect(await screen.findByText('Not translated because of your privacy settings.')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Review privacy settings' }).getAttribute('href')).toBe('/settings/privacy');
    expect(screen.getByText('Original text')).toBeTruthy();
  });

  it('shows no translate button and requests nothing when translation is disabled', async () => {
    api.fetchTranslationSettings.mockResolvedValue({ enabled: false, target_language: 'vi', configuration_revision: 1 });
    render(view(brief(1)));
    await screen.findByText('Original text');
    expect(api.submitTranslationBatch).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /Translate/i })).toBeNull();
  });

  it('translates the member shared brief on demand and toggles to the original', async () => {
    vi.mocked(listBriefRevisions).mockResolvedValue([brief(2)]);
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <NextIntlClientProvider
          locale="en-US"
          messages={{
            translations: getCatalog('en-us', 'translations'),
            sharing: { sharedBriefs: 'Shared briefs', loading: '…', noSharedBriefs: '-', briefMeta: 'r{revision} {time}' },
          }}
        >
          <SharedBriefs date="2026-10-09" timezone="UTC" />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    await userEvent.click(await screen.findByRole('button', { name: 'Translate to Vietnamese' }));
    expect(await screen.findByText('Ban dich')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'View original' }));
    expect(screen.getByText('Original text')).toBeTruthy();
  });
});
