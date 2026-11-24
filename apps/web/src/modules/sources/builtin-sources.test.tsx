import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/core/api';
import { getCatalog } from '@locales';
import type { DataShape } from '@/core/data-shapes';
import type { BuiltinSource } from './api';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'c' }) }));
const api = vi.hoisted(() => ({
  listBuiltinSources: vi.fn(),
  enableBuiltinSource: vi.fn(),
  disableBuiltinSource: vi.fn(),
  archiveSource: vi.fn(),
}));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));
const actions = vi.hoisted(() => ({ waitForActivation: vi.fn(), collectAndWait: vi.fn() }));
vi.mock('./use-source-actions', async (original) => ({ ...(await original<typeof import('./use-source-actions')>()), ...actions }));

import { BuiltinWizard, builtinEnableErrorKey } from './builtin-sources';

const row = (over: Partial<BuiltinSource>): BuiltinSource => ({
  id: 'world_bank',
  label: 'World Bank GDP',
  category: 'economy',
  eligibility: 'open',
  terms_url: 'https://example.org/terms',
  terms_checked_on: '2026-10-07',
  attribution: 'Credit the World Bank.',
  execution: 'supported',
  source_id: null,
  enabled: false,
  data_shapes: ['measurement'],
  ...over,
});
const onClose = vi.fn();
const wrap = (shapeFilter?: DataShape[], onAddConnector?: () => void) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <NextIntlClientProvider locale="en-US" messages={{ sources: getCatalog('en-us', 'sources') }}>
        <BuiltinWizard shapeFilter={shapeFilter} onAddConnector={onAddConnector} onClose={onClose} onChanged={() => undefined} />
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
/** Picks the first selectable built-in and moves to the terms step. */
async function pickAndContinue(name: string) {
  await userEvent.click(await screen.findByRole('button', { name: new RegExp(name) }));
  await userEvent.click(screen.getByRole('button', { name: 'Next' }));
}

beforeEach(() => {
  [...Object.values(api), ...Object.values(actions)].forEach((mock) => mock.mockReset());
  onClose.mockClear();
  actions.waitForActivation.mockResolvedValue({ ok: true, code: null });
});
afterEach(() => cleanup());

describe('BuiltinWizard', () => {
  it('groups rows by category and disables already added built-ins with an Added badge', async () => {
    api.listBuiltinSources.mockResolvedValue([
      row({}),
      row({ id: 'usgs', label: 'USGS quakes', category: 'hazards', source_id: 's1', enabled: true }),
    ]);
    wrap();
    expect(await screen.findByRole('group', { name: 'Economy and markets' })).toBeTruthy();
    expect(screen.getByRole('group', { name: 'Hazards and weather' })).toBeTruthy();
    const added = screen.getByRole('button', { name: /USGS quakes/ });
    expect(added.hasAttribute('disabled')).toBe(true);
    expect(added.textContent).toContain('Added');
    expect(screen.getByRole('button', { name: 'Next' }).hasAttribute('disabled')).toBe(true);
  });

  it('enables after the terms step, runs the test fetch and allows Done only after success', async () => {
    api.listBuiltinSources.mockResolvedValue([row({})]);
    api.enableBuiltinSource.mockResolvedValue(row({ enabled: true, source_id: 's' }));
    actions.collectAndWait.mockResolvedValue({
      request_id: 'r',
      source_id: 's',
      status: 'succeeded',
      ingestion_run_id: 'run-1',
      error_code: null,
    });
    wrap();
    await pickAndContinue('World Bank GDP');
    expect(api.enableBuiltinSource).not.toHaveBeenCalled();
    expect(screen.queryByLabelText(/Build knowledge graph/)).toBeNull();
    expect(screen.queryByLabelText(/Index for Q&A/)).toBeNull();
    expect(screen.getByLabelText('Data tracking mode')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() =>
      expect(api.enableBuiltinSource).toHaveBeenCalledWith('world_bank', 'personal', 'c', undefined, {
        graph_enabled: false,
        rag_index_enabled: true,
      }),
    );
    expect(await screen.findByText(/Test fetch succeeded/)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Done' }));
    expect(onClose).toHaveBeenCalled();
    expect(api.archiveSource).not.toHaveBeenCalled();
  });

  it('shows the error code on a failed fetch and archives the new source when discarded', async () => {
    api.listBuiltinSources.mockResolvedValue([row({})]);
    api.enableBuiltinSource.mockResolvedValue(row({ enabled: true, source_id: 's' }));
    api.disableBuiltinSource.mockResolvedValue(row({ source_id: 's' }));
    api.archiveSource.mockResolvedValue(undefined);
    actions.collectAndWait.mockResolvedValue({
      request_id: 'r',
      source_id: 's',
      status: 'failed',
      ingestion_run_id: null,
      error_code: 'upstream_error',
    });
    wrap();
    await pickAndContinue('World Bank GDP');
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect((await screen.findByText(/upstream_error/)).textContent).toContain('failed');
    expect(screen.getByRole('button', { name: 'Done' }).hasAttribute('disabled')).toBe(true);
    await userEvent.click(screen.getByRole('button', { name: 'Close' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Discard source' }));
    await waitFor(() => expect(api.disableBuiltinSource).toHaveBeenCalledWith('world_bank', 'c'));
    await waitFor(() => expect(api.archiveSource).toHaveBeenCalledWith('s', 'c'));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('explains the operator requirement for review-class sources and shows the server refusal', async () => {
    api.listBuiltinSources.mockResolvedValue([row({ id: 'bbc_world', label: 'BBC World News', category: 'news', eligibility: 'review' })]);
    api.enableBuiltinSource.mockRejectedValue(new ApiError(403, 'operator_required'));
    wrap();
    await pickAndContinue('BBC World News');
    expect(await screen.findByText(/approve them as the instance operator/)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Only the instance operator');
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe('BuiltinWizard shape filter', () => {
  beforeEach(() => api.listBuiltinSources.mockReset());
  afterEach(() => cleanup());

  it('filters the grid and says all added when every compatible built-in is already added', async () => {
    api.listBuiltinSources.mockResolvedValue([
      row({ source_id: 's1', enabled: true }),
      row({ id: 'usgs', label: 'USGS quakes', data_shapes: ['event'] }),
    ]);
    wrap(['measurement']);
    expect(await screen.findByText(getCatalog('en-us', 'sources').add.builtinAllAdded)).toBeTruthy();
    expect(screen.queryByText(getCatalog('en-us', 'sources').shape.filterNone)).toBeNull();
    expect(screen.queryByRole('button', { name: /USGS quakes/ })).toBeNull();
  });

  it('the series filter (finance_chart link) lists a series provider and hides point-event USGS', async () => {
    api.listBuiltinSources.mockResolvedValue([
      row({ id: 'binance', label: 'Binance spot prices', data_shapes: ['measurement', 'series'] }),
      row({ id: 'usgs', label: 'USGS quakes', data_shapes: ['event', 'located', 'measurement'] }),
    ]);
    wrap(['series']);
    expect(await screen.findByText('Binance spot prices')).toBeTruthy();
    expect(screen.queryByText('USGS quakes')).toBeNull();
  });

  it('points to Connect a source when no built-in matches the shapes', async () => {
    api.listBuiltinSources.mockResolvedValue([row({})]);
    const addConnector = vi.fn();
    wrap(['weather'], addConnector);
    expect(await screen.findByText(new RegExp(getCatalog('en-us', 'sources').shape.filterNoBuiltin))).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: getCatalog('en-us', 'sources').add.connector }));
    expect(addConnector).toHaveBeenCalledTimes(1);
  });
});

describe('builtinEnableErrorKey', () => {
  it('maps operator, terms and duplicate failures and falls back to the generic message', () => {
    expect(builtinEnableErrorKey(new ApiError(403, 'operator_required'))).toBe('builtinError.operator');
    expect(builtinEnableErrorKey(new ApiError(403, 'provider_terms_ineligible'))).toBe('builtinError.terms');
    expect(builtinEnableErrorKey(new ApiError(409, 'conflict', { code: 'builtin_already_added' }))).toBe('builtin.alreadyAdded');
    expect(builtinEnableErrorKey(new Error('boom'))).toBe('builtinError.generic');
  });
  it('sends name, schedule and section options to enable when the built-in accepts them', async () => {
    api.listBuiltinSources.mockResolvedValue([
      row({
        id: 'tuoitre_news',
        label: 'Tuoi Tre',
        category: 'news',
        data_shapes: ['news'],
        applicable_options: [
          'name',
          'schedule_interval_minutes',
          'max_items',
          'include_keywords',
          'exclude_keywords',
          'max_age_days',
          'feed_sections',
        ],
        sections: [
          { slug: 'home', label_en: 'Home', label_vi: 'Trang chu' },
          { slug: 'world', label_en: 'World', label_vi: 'The gioi' },
        ],
        default_section: 'home',
      }),
    ]);
    api.enableBuiltinSource.mockResolvedValue(row({ enabled: true, source_id: 's' }));
    actions.collectAndWait.mockResolvedValue({
      request_id: 'r',
      source_id: 's',
      status: 'succeeded',
      ingestion_run_id: 'run-1',
      error_code: null,
    });
    wrap();
    await pickAndContinue('Tuoi Tre');
    const name = screen.getByLabelText('Source name') as HTMLInputElement;
    expect(name.value).toBe('Tuoi Tre');
    await userEvent.clear(name);
    await userEvent.type(name, 'My news');
    await userEvent.click(screen.getByRole('checkbox', { name: 'World' }));
    await userEvent.type(screen.getByLabelText('Max new items per sync'), '20');
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(api.enableBuiltinSource).toHaveBeenCalled());
    expect(api.enableBuiltinSource.mock.calls[0][4]).toMatchObject({ name: 'My news', feed_sections: ['home', 'world'], max_items: 20 });
  });
  it('sends chosen per-type values and omits empty ones', async () => {
    api.listBuiltinSources.mockResolvedValue([
      row({
        id: 'nvd',
        label: 'NVD CVEs',
        category: 'security',
        data_shapes: ['measurement'],
        applicable_options: ['name', 'nvd_severity', 'nvd_keyword', 'kev_ransomware_only', 'nvd_window_days'],
      }),
    ]);
    api.enableBuiltinSource.mockResolvedValue(row({ enabled: true, source_id: 's' }));
    actions.collectAndWait.mockResolvedValue({
      request_id: 'r',
      source_id: 's',
      status: 'succeeded',
      ingestion_run_id: 'run-1',
      error_code: null,
    });
    wrap();
    await pickAndContinue('NVD CVEs');
    await userEvent.click(screen.getByRole('checkbox', { name: 'CRITICAL' }));
    await userEvent.type(screen.getByLabelText('Keyword'), 'openssl');
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(api.enableBuiltinSource).toHaveBeenCalled());
    const body = api.enableBuiltinSource.mock.calls[0][4];
    expect(body).toMatchObject({ nvd_severity: ['CRITICAL'], nvd_keyword: 'openssl' });
    expect(body).not.toHaveProperty('nvd_window_days');
    expect(body).not.toHaveProperty('kev_ransomware_only');
  });
});

describe('media built-in option omission', () => {
  it('omits cleared media values and preserves selected false in the enable request', async () => {
    api.listBuiltinSources.mockResolvedValue([
      row({
        id: 'livechart_episodes',
        label: 'LiveChart',
        category: 'media',
        data_shapes: ['media'],
        applicable_options: ['media_genres', 'release_year', 'anime_season', 'exclude_adult'],
      }),
    ]);
    api.enableBuiltinSource.mockResolvedValue(row({ enabled: true, source_id: 's' }));
    actions.collectAndWait.mockResolvedValue({ status: 'no_changes', source_id: 's', request_id: 'r' });
    wrap();
    await pickAndContinue('LiveChart');
    await userEvent.click(screen.getByRole('checkbox', { name: 'Action' }));
    await userEvent.click(screen.getByRole('checkbox', { name: 'Action' }));
    await userEvent.type(screen.getByLabelText('Release year'), '2026');
    await userEvent.clear(screen.getByLabelText('Release year'));
    await userEvent.click(screen.getByLabelText('Exclude adult content'));
    await userEvent.click(screen.getByRole('option', { name: 'No' }));
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(api.enableBuiltinSource).toHaveBeenCalled());
    const body = api.enableBuiltinSource.mock.calls[0][4];
    expect(body).toMatchObject({ exclude_adult: false });
    expect(body).not.toHaveProperty('media_genres');
    expect(body).not.toHaveProperty('release_year');
    expect(body).not.toHaveProperty('anime_season');
  });
});
