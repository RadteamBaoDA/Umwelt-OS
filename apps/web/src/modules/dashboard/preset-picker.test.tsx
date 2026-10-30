import { InfiniteQueryObserver, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/core/api';
import { dashboardMessages } from '@/core/messages/dashboard';
import { gadgetSettingsMessages } from '@/core/messages/gadget-settings';
import { sourcesMessages } from '@/core/messages/sources';
import { uiSweepMessages } from '@/core/messages/ui-sweep';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'c' }) }));
vi.mock('next/link', () => ({ default: ({ href, children }: { href: string; children: React.ReactNode }) => <a href={href}>{children}</a> }));
const api = vi.hoisted(() => ({
  listDashboardPresets: vi.fn(), previewDashboardPreset: vi.fn(), applyDashboardPreset: vi.fn(), listGadgetRenderers: vi.fn(), listGadgetSources: vi.fn(),
}));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));

import gadgetRenderers from '@/test/fixtures/gadget-renderers.json';
import { PresetPicker } from './preset-picker';

const FINGERPRINT = 'abcdef0123456789abcdef';
/** Returns the backend renderer descriptor snapshot (verified against `renderer_reads()` by pytest). */
const renderer = (id: string) => {
  const found = gadgetRenderers.find((row) => row.id === id);
  if (!found) throw new Error(`renderer fixture ${id} missing`);
  return found;
};
const messages = { dashboard: dashboardMessages['en-us'], gadgetSettings: gadgetSettingsMessages['en-us'], sources: sourcesMessages['en-us'], uiSweep: uiSweepMessages['en-us'] };

/** Builds an active datasource fixture; measurement sources also carry `series`, the shape the single-source Chart accepts. */
const source = (id: string, name: string, shapes: string[]) => ({ id, name, provider: 'binance', status: 'active', type: 'api', generation: 1, local_only: false, last_success_at: null, data_shapes: shapes.includes('measurement') ? [...shapes, 'series'] : shapes });
/** Builds a finance preview whose chart slot carries the given warning codes. */
const financePreview = (chartWarnings: string[], fingerprint = FINGERPRINT) => ({
  template_version: 1, preset_id: 'finance', name: 'Finance', preview_fingerprint: fingerprint, target_dashboard_id: null, target_revision: null,
  slots: [
    { slot_id: 'finance_chart', renderer: 'finance_chart', source_ids: [], scope: {}, filters: {}, highlight_rules: [], sources: [], warnings: chartWarnings.map((code) => ({ code })) },
    { slot_id: 'news_feed', renderer: 'news_feed', source_ids: [], scope: {}, filters: {}, highlight_rules: [], sources: [], warnings: [] },
  ],
});

/** Renders the picker open with query and intl providers. */
const wrap = (target?: { id: string; revision: number }) => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <NextIntlClientProvider locale="en-US" messages={messages}>
      <PresetPicker open onOpenChange={() => {}} onPresetApplied={() => {}} currentDashboardId={target?.id} currentDashboardRevision={target?.revision} />
    </NextIntlClientProvider>
  </QueryClientProvider>);

beforeEach(async () => {
  Object.values(api).forEach((mock) => mock.mockReset());
  api.listDashboardPresets.mockResolvedValue([
    { id: 'finance', label: 'Finance', family: 'purpose', slots: [{ slot_id: 'finance_chart', renderer: 'finance_chart' }, { slot_id: 'news_feed', renderer: 'news_feed' }] },
  ]);
  api.listGadgetRenderers.mockResolvedValue([renderer('finance_chart'), renderer('news_feed')]);
  api.listGadgetSources.mockResolvedValue({ items: [], next_cursor: null });
  api.previewDashboardPreset.mockResolvedValue({
    template_version: 1, preset_id: 'finance', name: 'Finance', preview_fingerprint: FINGERPRINT, target_dashboard_id: null, target_revision: null,
    slots: [
      { slot_id: 'finance_chart', renderer: 'finance_chart', source_ids: [], scope: {}, filters: {}, highlight_rules: [], sources: [], warnings: [{ code: 'missing_source' }] },
      { slot_id: 'news_feed', renderer: 'news_feed', source_ids: [], scope: {}, filters: {}, highlight_rules: [], sources: [], warnings: [{ code: 'missing_source' }] },
    ],
  });
});
afterEach(() => cleanup());

describe('PresetPicker', () => {
  it('slots show localized names', async () => {
    wrap();
    expect(await screen.findByText(/Chart, Feed/)).toBeTruthy();
    expect(screen.queryByText(/finance_chart/)).toBeNull();
    await userEvent.click(await screen.findByRole('button', { name: /Finance/ }));
    expect(await screen.findByText('Chart')).toBeTruthy();
    expect(screen.queryByText('finance_chart')).toBeNull();
  });

  it('no fingerprint text', async () => {
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Finance/ }));
    await screen.findByText('Chart');
    expect(screen.queryByText(/fingerprint/i)).toBeNull();
    expect(screen.queryByText(/20 cols/)).toBeNull();
    expect(screen.queryByText(/stack/)).toBeNull();
    expect(screen.queryByText(/abcdef0123/)).toBeNull();
  });

  it('needs-setup link is shape filtered', async () => {
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Finance/ }));
    await screen.findByText('Chart');
    const hrefs = screen.getAllByRole('link', { name: /Configure|Set up|Add/i }).map((a) => a.getAttribute('href'));
    expect(hrefs).toContain('/settings/sources?shapes=series&for=finance_chart');
    expect(hrefs).toContain('/settings/sources?shapes=event,news&for=news_feed');
  });

  it('renderers without data sources never offer a needs-setup link', async () => {
    api.listDashboardPresets.mockResolvedValue([{ id: 'personal', label: 'Personal', family: 'purpose', slots: [{ slot_id: 'tasks', renderer: 'tasks' }] }]);
    api.listGadgetRenderers.mockResolvedValue([renderer('tasks')]);
    api.previewDashboardPreset.mockResolvedValue({
      template_version: 1, preset_id: 'personal', name: 'Personal', preview_fingerprint: FINGERPRINT, target_dashboard_id: null, target_revision: null,
      slots: [{ slot_id: 'tasks', renderer: 'tasks', source_ids: [], scope: {}, filters: {}, highlight_rules: [], sources: [], warnings: [{ code: 'missing_source' }] }],
    });
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Personal/ }));
    await screen.findByText('Needs setup');
    expect(screen.queryByRole('link', { name: /Configure|Set up|Add/i })).toBeNull();
  });

  it('a scope_required-only slot shows its warning and no Configure link', async () => {
    api.previewDashboardPreset.mockResolvedValue({
      template_version: 1, preset_id: 'finance', name: 'Finance', preview_fingerprint: FINGERPRINT, target_dashboard_id: null, target_revision: null,
      slots: [{ slot_id: 'finance_chart', renderer: 'finance_chart', source_ids: ['s1'], scope: {}, filters: {}, highlight_rules: [], sources: [], warnings: [{ code: 'scope_required' }] }],
    });
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Finance/ }));
    expect((await screen.findAllByText(/Choose a symbol or region for this chart after applying/)).length).toBeGreaterThan(0);
    expect(screen.queryByRole('link', { name: /Configure|Set up|Add/i })).toBeNull();
  });

  it('pages sources with cancelRefetch disabled so a pending page is never cancelled', async () => {
    const spy = vi.spyOn(InfiniteQueryObserver.prototype, 'fetchNextPage');
    api.listGadgetSources.mockImplementation(async (_limit: number, cursor?: string) => cursor
      ? { items: [], next_cursor: null } : { items: [], next_cursor: 'p2' });
    wrap();
    await waitFor(() => expect(spy).toHaveBeenCalled());
    expect(spy.mock.calls.every(([options]) => options?.cancelRefetch === false)).toBe(true);
    spy.mockRestore();
  });

  it('shows localized warning text instead of raw codes', async () => {
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Finance/ }));
    await screen.findByText('Chart');
    expect((await screen.findAllByText(/No compatible datasource exists yet/)).length).toBeGreaterThan(0);
    expect(screen.queryByText('missing_source')).toBeNull();
    expect(screen.queryByText(/group:/)).toBeNull();
  });

  it('preselects the only compatible source and sends it with preview and apply', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('s1', 'Binance spot', ['measurement'])], next_cursor: null });
    api.previewDashboardPreset.mockResolvedValue(financePreview([]));
    api.applyDashboardPreset.mockResolvedValue({ id: 'd1' });
    wrap();
    await screen.findByText('Binance spot', {}, { timeout: 100 }).catch(() => undefined);
    await userEvent.click(await screen.findByRole('button', { name: /Finance/ }));
    await screen.findByText('Chart');
    expect(api.previewDashboardPreset.mock.calls[0][1].slot_sources).toEqual({ finance_chart: ['s1'] });
    await userEvent.click(screen.getByRole('button', { name: 'Apply preset' }));
    await waitFor(() => expect(api.applyDashboardPreset).toHaveBeenCalled());
    expect(api.applyDashboardPreset.mock.calls[0][1]).toMatchObject({ slot_sources: { finance_chart: ['s1'] }, preview_fingerprint: FINGERPRINT });
  });

  it('leaves a slot unbound with several compatible sources, then re-previews on selection', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('s1', 'Binance spot', ['measurement']), source('s2', 'Kp feed', ['measurement'])], next_cursor: null });
    api.previewDashboardPreset.mockResolvedValueOnce(financePreview(['source_unbound'])).mockResolvedValue(financePreview([], 'f'.repeat(22)));
    api.applyDashboardPreset.mockResolvedValue({ id: 'd1' });
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Finance/ }));
    expect(await screen.findByText('Choose a datasource')).toBeTruthy();
    expect(api.previewDashboardPreset.mock.calls[0][1].slot_sources).toEqual({});
    await userEvent.click((await screen.findAllByRole('radio', { name: /Kp feed/ }))[0]);
    await waitFor(() => expect(api.previewDashboardPreset).toHaveBeenCalledTimes(2));
    expect(api.previewDashboardPreset.mock.calls[1][1].slot_sources).toEqual({ finance_chart: ['s2'] });
    await waitFor(() => expect(screen.queryByText('Choose a datasource')).toBeNull());
    await userEvent.click(screen.getByRole('button', { name: 'Apply preset' }));
    await waitFor(() => expect(api.applyDashboardPreset).toHaveBeenCalled());
    expect(api.applyDashboardPreset.mock.calls[0][1]).toMatchObject({ slot_sources: { finance_chart: ['s2'] }, preview_fingerprint: 'f'.repeat(22) });
  });
  it('re-previews when switching to Replace and applies with the replace-target fingerprint', async () => {
    const REPLACE = '9'.repeat(22);
    api.listGadgetSources.mockResolvedValue({ items: [source('s1', 'Binance spot', ['measurement'])], next_cursor: null });
    api.previewDashboardPreset.mockImplementation(async (_id: string, body: { target_dashboard_id: string | null }) =>
      body.target_dashboard_id ? { ...financePreview([], REPLACE), target_dashboard_id: 'd9' } : financePreview([]));
    api.applyDashboardPreset.mockResolvedValue({ id: 'd9' });
    wrap({ id: 'd9', revision: 4 });
    await userEvent.click(await screen.findByRole('button', { name: /Finance/ }));
    await screen.findByText('Chart');
    await userEvent.click(screen.getByRole('button', { name: 'Replace current dashboard' }));
    await waitFor(() => expect(api.previewDashboardPreset.mock.calls.at(-1)?.[1].target_dashboard_id).toBe('d9'));
    await userEvent.click(screen.getByRole('checkbox', { name: /I confirm replacing/ }));
    const apply = screen.getByRole('button', { name: 'Apply preset' });
    await waitFor(() => expect((apply as HTMLButtonElement).disabled).toBe(false));
    await userEvent.click(apply);
    await waitFor(() => expect(api.applyDashboardPreset).toHaveBeenCalled());
    expect(api.applyDashboardPreset.mock.calls[0][1]).toMatchObject({ mode: 'replace', target_dashboard_id: 'd9', preview_fingerprint: REPLACE });
  });

  it('does not preselect a lone compatible source while more source pages remain', async () => {
    api.listGadgetSources.mockImplementation(async (_limit: number, cursor?: string) => cursor
      ? { items: [source('s2', 'Second page', ['measurement'])], next_cursor: null }
      : { items: [source('s1', 'Binance spot', ['measurement'])], next_cursor: 'p2' });
    api.previewDashboardPreset.mockResolvedValue(financePreview(['source_unbound']));
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Finance/ }));
    await screen.findByText('Chart');
    expect(api.previewDashboardPreset.mock.calls[0][1].slot_sources).toEqual({});
  });
  it('preselects a lone compatible source once the remaining pages prove it is the only one', async () => {
    let release: (value: unknown) => void = () => undefined;
    const pageTwo = new Promise((resolve) => { release = resolve; });
    api.listGadgetSources.mockImplementation(async (_limit: number, cursor?: string) => cursor
      ? pageTwo
      : { items: [source('s1', 'Binance spot', ['measurement'])], next_cursor: 'p2' });
    api.previewDashboardPreset.mockResolvedValue(financePreview(['source_unbound']));
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Finance/ }));
    await screen.findByText('Chart');
    expect(api.previewDashboardPreset.mock.calls[0][1].slot_sources).toEqual({});
    release({ items: [source('g1', 'My GitHub', ['project_activity'])], next_cursor: null });
    await waitFor(() => expect(api.previewDashboardPreset.mock.calls.at(-1)?.[1].slot_sources).toEqual({ finance_chart: ['s1'] }));
  });

  it('preselects the lone compatible source when the preset is clicked before renderers load', async () => {
    let release: (value: unknown) => void = () => undefined;
    api.listGadgetRenderers.mockImplementation(() => new Promise((resolve) => { release = resolve; }));
    api.listGadgetSources.mockResolvedValue({ items: [source('s1', 'Binance spot', ['measurement'])], next_cursor: null });
    api.previewDashboardPreset.mockResolvedValue(financePreview(['source_unbound']));
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Finance/ }));
    await waitFor(() => expect(api.previewDashboardPreset).toHaveBeenCalled());
    expect(api.previewDashboardPreset.mock.calls[0][1].slot_sources).toEqual({});
    release([renderer('finance_chart'), renderer('news_feed')]);
    await waitFor(() => expect(api.previewDashboardPreset.mock.calls.at(-1)?.[1].slot_sources).toMatchObject({ finance_chart: ['s1'] }));
  });

  it('keeps a slot the owner already chose when late pages finish loading', async () => {
    let release: (value: unknown) => void = () => undefined;
    const pageTwo = new Promise((resolve) => { release = resolve; });
    api.listGadgetSources.mockImplementation(async (_limit: number, cursor?: string) => cursor
      ? pageTwo
      : { items: [source('s1', 'Binance spot', ['measurement'])], next_cursor: 'p2' });
    api.previewDashboardPreset.mockResolvedValue(financePreview(['source_unbound']));
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Finance/ }));
    await screen.findByText('Chart');
    await userEvent.click((await screen.findAllByRole('radio', { name: /Binance spot/ }))[0]);
    await waitFor(() => expect(api.previewDashboardPreset).toHaveBeenCalledTimes(2));
    release({ items: [], next_cursor: null });
    await waitFor(() => expect(api.listGadgetSources).toHaveBeenCalledTimes(2));
    expect(api.previewDashboardPreset.mock.calls.at(-1)?.[1].slot_sources).toEqual({ finance_chart: ['s1'] });
  });

  it('Replace then Create re-previews with a null target and applies with that fingerprint', async () => {
    const CREATE = '7'.repeat(22);
    const REPLACE = '9'.repeat(22);
    api.listGadgetSources.mockResolvedValue({ items: [source('s1', 'Binance spot', ['measurement'])], next_cursor: null });
    api.previewDashboardPreset.mockImplementation(async (_id: string, body: { target_dashboard_id: string | null }) =>
      body.target_dashboard_id ? { ...financePreview([], REPLACE), target_dashboard_id: 'd9' } : financePreview([], CREATE));
    api.applyDashboardPreset.mockResolvedValue({ id: 'n1' });
    wrap({ id: 'd9', revision: 4 });
    await userEvent.click(await screen.findByRole('button', { name: /Finance/ }));
    await screen.findByText('Chart');
    await userEvent.click(screen.getByRole('button', { name: 'Replace current dashboard' }));
    await waitFor(() => expect(api.previewDashboardPreset.mock.calls.at(-1)?.[1].target_dashboard_id).toBe('d9'));
    await userEvent.click(screen.getByRole('button', { name: 'Create as a new dashboard' }));
    await waitFor(() => expect(api.previewDashboardPreset.mock.calls.at(-1)?.[1].target_dashboard_id).toBeNull());
    const apply = screen.getByRole('button', { name: 'Apply preset' });
    await waitFor(() => expect((apply as HTMLButtonElement).disabled).toBe(false));
    await userEvent.click(apply);
    await waitFor(() => expect(api.applyDashboardPreset).toHaveBeenCalled());
    expect(api.applyDashboardPreset.mock.calls[0][1]).toMatchObject({ mode: 'create', target_dashboard_id: null, preview_fingerprint: CREATE });
  });

  it.each([['dashboard_limit'], ['preview_changed']])('maps the %s 409 code to its own apply message', async (code) => {
    api.listGadgetSources.mockResolvedValue({ items: [source('s1', 'Binance spot', ['measurement'])], next_cursor: null });
    api.previewDashboardPreset.mockResolvedValue(financePreview([]));
    api.applyDashboardPreset.mockRejectedValue(new ApiError(409, 'x', { code }));
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Finance/ }));
    await screen.findByText('Chart');
    await userEvent.click(screen.getByRole('button', { name: 'Apply preset' }));
    const expected = (dashboardMessages['en-us'].presetError as Record<string, string>)[code];
    expect(expected).toBeTruthy();
    expect(await screen.findByText(expected)).toBeTruthy();
  });
});
