import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { gadgetSettingsMessages } from '@/core/messages/gadget-settings';
import { sourcesMessages } from '@/core/messages/sources';
import { uiSweepMessages } from '@/core/messages/ui-sweep';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'c' }) }));
vi.mock('next/link', () => ({ default: ({ href, children }: { href: string; children: React.ReactNode }) => <a href={href}>{children}</a> }));
const api = vi.hoisted(() => ({
  listGadgetSources: vi.fn(), listCompatibleSourceCounts: vi.fn(), listGadgetRenderers: vi.fn(), listGadgetTemplates: vi.fn(), createGadgetDefinition: vi.fn(), patchGadgetDefinition: vi.fn(), getMetricCatalog: vi.fn(),
}));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));

import { ApiError } from '@/core/api';
import gadgetRenderers from '@/test/fixtures/gadget-renderers.json';
import gadgetTemplates from '@/test/fixtures/gadget-templates.json';
import metricCatalog from '@/test/fixtures/metric-catalog.json';
import { GadgetEditor, fieldsFor } from './gadget-editor';

/** Backend renderer descriptor snapshot (verified against `renderer_reads()` by pytest). */
const backendRenderer = (id: string) => {
  const found = gadgetRenderers.find((row) => row.id === id);
  if (!found) throw new Error(`renderer fixture ${id} missing`);
  return found;
};
/** Backend template snapshot entry (verified against `list_templates()` by pytest). */
const backendTemplate = (id: string) => {
  const found = gadgetTemplates.find((row) => row.id === id);
  if (!found) throw new Error(`template fixture ${id} missing`);
  return found;
};
const renderers = [backendRenderer('provider_observation'), backendRenderer('news_feed')];
const financeRenderer = backendRenderer('finance_chart');
const templates = [backendTemplate('metric')];
/** Builds a source fixture with the shapes and provider the picker filters on; measurement sources also expose `series` (what the single-source Chart accepts). */
const source = (id: string, name: string, provider: string, shapes: string[], last: string | null = null) => ({ id, name, provider, status: 'active', type: 'api', generation: 1, local_only: false, last_success_at: last, data_shapes: shapes.includes('measurement') && !shapes.includes('series') ? [...shapes, 'series'] : shapes });
const messages = { gadgetSettings: gadgetSettingsMessages['en-us'], dashboard: dashboardMessages['en-us'], sources: sourcesMessages['en-us'], uiSweep: uiSweepMessages['en-us'] };
/** Backend metric catalog snapshot (verified against `metric_catalog_read()` by pytest). */
const catalog = metricCatalog;
/** Returns the backend renderer descriptor for a renderer id. */
const renderer = (id: string) => backendRenderer(id);
/** Returns the backend template for the renderer, under a test id, with the given filter overrides. */
const tpl = (id: string, rendererId: string, filters: Record<string, unknown> = {}) => {
  const base = gadgetTemplates.find((row) => row.renderer === rendererId);
  if (!base) throw new Error(`template fixture for ${rendererId} missing`);
  return { ...base, id, filters: { ...base.filters, ...filters } };
};
const onSaved = vi.fn();
/** Renders the editor with query and intl providers. */
const wrap = (props: Partial<React.ComponentProps<typeof GadgetEditor>> = {}) => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <NextIntlClientProvider locale="en-US" messages={messages}><GadgetEditor onSaved={onSaved} {...props} /></NextIntlClientProvider>
  </QueryClientProvider>);

beforeEach(() => {
  [...Object.values(api), onSaved].forEach((mock) => mock.mockReset());
  api.listGadgetRenderers.mockResolvedValue(renderers);
  api.listGadgetTemplates.mockResolvedValue(templates);
  api.getMetricCatalog.mockResolvedValue(catalog);
  api.listCompatibleSourceCounts.mockResolvedValue({ provider_observation: 2, news_feed: 1, finance_chart: 0 });
  api.listGadgetSources.mockResolvedValue({ items: [
    source('b1', 'Binance spot', 'binance', ['measurement'], '2026-10-28T08:30:00Z'),
    source('g1', 'My GitHub', 'github', ['project_activity']),
  ], next_cursor: null });
});
afterEach(() => cleanup());

describe('GadgetEditor', () => {
  it('shows compatible counts on tiles', async () => {
    wrap();
    expect(await screen.findByText('2 compatible datasources')).toBeTruthy();
  });

  it('shows a retry notice when the compatible-counts query fails while tiles stay usable', async () => {
    api.listCompatibleSourceCounts.mockRejectedValueOnce(new Error('down')).mockResolvedValue({ provider_observation: 2, news_feed: 1 });
    wrap();
    expect(await screen.findByText(gadgetSettingsMessages['en-us'].compatibleCountsFailed)).toBeTruthy();
    expect(screen.getByRole('button', { name: /Metric/ })).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: gadgetSettingsMessages['en-us'].retry }));
    expect(await screen.findByText('2 compatible datasources')).toBeTruthy();
    expect(screen.queryByText(gadgetSettingsMessages['en-us'].compatibleCountsFailed)).toBeNull();
  });

  it('zero-count tile links to filtered sources', async () => {
    api.listGadgetRenderers.mockResolvedValue([...renderers, financeRenderer]);
    api.listGadgetTemplates.mockResolvedValue([...templates, backendTemplate('chart')]);
    wrap();
    const link = await screen.findByRole('link', { name: /Add a compatible datasource/ });
    expect(link.getAttribute('href')).toBe('/settings/sources?shapes=series&for=finance_chart');
  });

  it('picker hides incompatible sources and shows provider and last update', async () => {
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Metric/ }));
    expect(await screen.findByText('Binance spot')).toBeTruthy();
    expect(screen.queryByText('My GitHub')).toBeNull();
    expect(screen.getByText(/Binance prices/)).toBeTruthy();
    expect(screen.getByText(/Oct 28, 2026/)).toBeTruthy();
  });

  it('says never updated when a source has no success yet', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('b2', 'Fresh feed', 'binance', ['measurement'])], next_cursor: null });
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Metric/ }));
    expect(await screen.findByText(/never updated/i)).toBeTruthy();
  });

  it('loads next source page', async () => {
    api.listGadgetSources.mockResolvedValueOnce({ items: [source('b1', 'Binance spot', 'binance', ['measurement'])], next_cursor: 'c2' })
      .mockResolvedValueOnce({ items: [source('n1', 'NOAA Kp', 'noaa_kp', ['measurement'])], next_cursor: null });
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Metric/ }));
    await userEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    expect(await screen.findByText('NOAA Kp')).toBeTruthy();
    expect(api.listGadgetSources).toHaveBeenLastCalledWith(100, 'c2', expect.anything());
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();
  });

  it('picks a template and keeps Advanced collapsed', async () => {
    const { container } = wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Metric/ }));
    expect(await screen.findByLabelText('Name')).toHaveProperty('value', 'Metric');
    expect((container.querySelector('details') as HTMLDetailsElement).open).toBe(false);
  });

  it('disables Save until a source is picked, then saves it', async () => {
    api.createGadgetDefinition.mockResolvedValue({ id: 'def1', revision: 1, renderer: 'provider_observation' });
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Metric/ }));
    const save = await screen.findByRole('button', { name: 'Save to library' });
    expect(save.hasAttribute('disabled')).toBe(true);
    await userEvent.click(await screen.findByRole('radio', { name: /Binance spot/ }));
    expect(save.hasAttribute('disabled')).toBe(false);
    await userEvent.click(save);
    await waitFor(() => expect(api.createGadgetDefinition).toHaveBeenCalled());
    expect(api.createGadgetDefinition.mock.calls[0][0]).toMatchObject({ renderer: 'provider_observation', source_ids: ['b1'], filters: { limit: 25 } });
    expect(onSaved).toHaveBeenCalled();
  });

  it('does not lock out an existing definition whose saved source is now incompatible', async () => {
    const definition = { id: 'd1', name: 'FX', revision: 3, renderer: 'provider_observation', source_ids: ['g1'], scope: {}, filters: { limit: 25 }, highlight_rules: [], config_version: 1, runtime_state: 'available' as const };
    api.patchGadgetDefinition.mockResolvedValue({ ...definition, revision: 4 });
    wrap({ definition });
    expect(await screen.findByText('Not compatible with this gadget')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.patchGadgetDefinition).toHaveBeenCalled());
    // Unchanged sources are not re-sent, so the backend compatibility check never trips on legacy data.
    expect(api.patchGadgetDefinition.mock.calls[0][1]).not.toHaveProperty('source_ids');
  });

  it('resets every draft field when switching templates', async () => {
    api.listGadgetRenderers.mockResolvedValue([renderer('news_feed'), renderer('text_panel')]);
    api.listGadgetTemplates.mockResolvedValue([tpl('a', 'news_feed', { limit: 10 }), tpl('b', 'text_panel', { limit: 40 })]);
    const { container } = wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Feed/ }));
    (container.querySelector('details') as HTMLDetailsElement).open = true;
    await userEvent.type(await screen.findByLabelText('Keywords'), 'oil');
    await userEvent.click(screen.getByRole('button', { name: 'Change template' }));
    await userEvent.click(await screen.findByRole('button', { name: /Text/ }));
    (container.querySelector('details') as HTMLDetailsElement).open = true;
    expect(await screen.findByLabelText('Keywords')).toHaveProperty('value', '');
    expect(screen.getByLabelText('Items shown')).toHaveProperty('value', '40');
  });

  it('shows why Save is disabled and opens Advanced when the limit is invalid', async () => {
    const definition = { id: 'd1', name: 'News', revision: 1, renderer: 'news_feed', source_ids: [], scope: {}, filters: { limit: 25 }, highlight_rules: [], config_version: 1, runtime_state: 'available' as const };
    const { container } = wrap({ definition });
    const details = container.querySelector('details') as HTMLDetailsElement;
    expect(details.open).toBe(false);
    const limit = await screen.findByLabelText('Items shown');
    await userEvent.clear(limit);
    await userEvent.type(limit, '0');
    await waitFor(() => expect(details.open).toBe(true));
    const save = screen.getByRole('button', { name: 'Save to library' });
    expect(save.hasAttribute('disabled')).toBe(true);
    const reasonId = save.getAttribute('aria-describedby') as string;
    expect(document.getElementById(reasonId)?.textContent).toBe('Between 1 and 100.');
  });

  it('exposes renderer-relevant fields', () => {
    expect(fieldsFor('weather').has('keywords')).toBe(false);
    expect(fieldsFor('weather').has('metrics')).toBe(true);
    expect(fieldsFor('news_feed').has('limit')).toBe(true);
    expect(fieldsFor('map').has('map')).toBe(true);
  });

  it('hides keywords and limit for weather', async () => {
    api.listGadgetRenderers.mockResolvedValue([renderer('weather')]);
    api.listGadgetTemplates.mockResolvedValue([tpl('weather', 'weather')]);
    const { container } = wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Weather/ }));
    await screen.findByLabelText('Name');
    (container.querySelector('details') as HTMLDetailsElement | null)?.setAttribute('open', '');
    expect(screen.queryByLabelText('Keywords')).toBeNull();
    expect(screen.queryByLabelText('Items shown')).toBeNull();
  });

  /** Opens the chart template and ticks the source row with the given name. */
  async function pickChart(sourceName: string) {
    api.listCompatibleSourceCounts.mockResolvedValue({ finance_chart: 1 });
    api.listGadgetRenderers.mockResolvedValue([renderer('finance_chart')]);
    api.listGadgetTemplates.mockResolvedValue([tpl('fin', 'finance_chart')]);
    const view = wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Chart/ }));
    await userEvent.click(await screen.findByRole('radio', { name: new RegExp(sourceName) }));
    return view;
  }

  /** Opens the single-source Metric template and selects the radio row with the given name. */
  async function pickMetric(sourceName: string) {
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Metric/ }));
    await userEvent.click(await screen.findByRole('radio', { name: new RegExp(sourceName) }));
  }

  it('chart metric picker lists noaa_kp metrics', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('n1', 'NOAA Kp', 'noaa_kp', ['measurement'])], next_cursor: null });
    await pickChart('NOAA Kp');
    const trigger = await screen.findByRole('combobox', { name: 'Metric' });
    await waitFor(() => expect(trigger.textContent).toContain('Kp index'));
    await userEvent.click(trigger);
    expect(await screen.findByRole('option', { name: /Kp index/ })).toBeTruthy();
    expect(screen.queryByRole('option', { name: /kp_max/ })).toBeNull();
    expect(screen.queryByRole('option', { name: /volume/ })).toBeNull();
  });

  it('symbol field only for symbol-keyed provider', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('n1', 'NOAA Kp', 'noaa_kp', ['measurement'])], next_cursor: null });
    await pickChart('NOAA Kp');
    await screen.findByRole('combobox', { name: 'Metric' });
    expect(screen.queryByRole('combobox', { name: 'Symbols' })).toBeNull();
    cleanup();
    api.listGadgetSources.mockResolvedValue({ items: [source('b1', 'Binance spot', 'binance', ['measurement'])], next_cursor: null });
    await pickChart('Binance spot');
    expect(await screen.findByRole('combobox', { name: 'Symbols' })).toBeTruthy();
  });

  it('maps 422 symbol_required to the symbol field', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('b1', 'Binance spot', 'binance', ['measurement'])], next_cursor: null });
    api.createGadgetDefinition.mockRejectedValue(new ApiError(422, 'bad', { code: 'symbol_required', details: { field: 'symbols' } }));
    await pickChart('Binance spot');
    await screen.findByRole('combobox', { name: 'Symbols' });
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(document.querySelector('[data-field="symbols"]')?.textContent).toContain('This datasource needs at least one symbol.'));
    expect(screen.queryByText('Could not save the gadget.')).toBeNull();
  });

  /** A saved NVDA chart whose bound source is not in the loaded first page of source rows. */
  const orphanChart = { id: 'd2', name: 'NVDA', revision: 2, renderer: 'finance_chart', source_ids: ['not-loaded'], scope: { symbols: ['NVDA'], metrics: ['close'], lookback_days: 90 }, filters: { limit: 25 }, highlight_rules: [], config_version: 1, runtime_state: 'available' as const };

  it('keeps saved symbols when the bound source is not among the loaded rows', async () => {
    api.listGadgetRenderers.mockResolvedValue([...renderers, financeRenderer]);
    api.patchGadgetDefinition.mockResolvedValue({ ...orphanChart, revision: 3 });
    wrap({ definition: orphanChart });
    await userEvent.click(await screen.findByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.patchGadgetDefinition).toHaveBeenCalled());
    expect(api.patchGadgetDefinition.mock.calls[0][1].scope.symbols).toEqual(['NVDA']);
  });

  it('shows a free-text symbols field prefilled with saved symbols when the provider is unknown, and does not resend unchanged symbols as changes', async () => {
    api.listGadgetRenderers.mockResolvedValue([...renderers, financeRenderer]);
    api.patchGadgetDefinition.mockResolvedValue({ ...orphanChart, revision: 3 });
    wrap({ definition: orphanChart });
    const field = await screen.findByRole('textbox', { name: 'Symbols' });
    expect((field as HTMLInputElement).value).toBe('NVDA');
    expect(screen.getByText('The provider of this datasource is unknown, so symbols cannot be suggested. Type them separated by commas.')).toBeTruthy();
    await userEvent.clear(field);
    await userEvent.type(field, 'NVDA, AMD');
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.patchGadgetDefinition).toHaveBeenCalled());
    expect(api.patchGadgetDefinition.mock.calls[0][1].scope.symbols).toEqual(['NVDA', 'AMD']);
  });

  it('shows the free-text symbols field when the metric catalog fails to load', async () => {
    api.listGadgetRenderers.mockResolvedValue([...renderers, financeRenderer]);
    api.getMetricCatalog.mockRejectedValue(new Error('down'));
    wrap({ definition: { ...orphanChart, source_ids: ['b1'] } });
    expect(await screen.findByRole('textbox', { name: 'Symbols' })).toBeTruthy();
  });

  it('shows a 422 for a field that is not rendered near Save', async () => {
    api.listGadgetRenderers.mockResolvedValue([...renderers, financeRenderer]);
    api.patchGadgetDefinition.mockRejectedValue(new ApiError(422, 'bad', { code: 'symbol_required', details: { field: 'symbols' } }));
    wrap({ definition: orphanChart });
    await userEvent.click(await screen.findByRole('button', { name: 'Save to library' }));
    expect(await screen.findByText('This datasource needs at least one symbol.')).toBeTruthy();
    expect(screen.queryByText('Could not save the gadget.')).toBeNull();
  });

  it('choose applies defaults of the chosen template', async () => {
    api.listGadgetRenderers.mockResolvedValue([renderer('news_feed'), renderer('text_panel')]);
    api.listGadgetTemplates.mockResolvedValue([tpl('a', 'news_feed', { limit: 10 }), tpl('b', 'text_panel', { limit: 40 })]);
    const { container } = wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Feed/ }));
    await userEvent.click(await screen.findByRole('button', { name: 'Change template' }));
    await userEvent.click(await screen.findByRole('button', { name: /Text/ }));
    (container.querySelector('details') as HTMLDetailsElement).open = true;
    expect(await screen.findByLabelText('Items shown')).toHaveProperty('value', '40');
  });

  it('unavailable map layers disabled', async () => {
    api.listGadgetRenderers.mockResolvedValue([renderer('map')]);
    api.listGadgetTemplates.mockResolvedValue([tpl('m', 'map')]);
    const { container } = wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Map/ }));
    (container.querySelector('details') as HTMLDetailsElement).open = true;
    expect((await screen.findByRole('checkbox', { name: /Military/ })).hasAttribute('disabled')).toBe(true);
    expect(screen.getByRole('checkbox', { name: /World observations/ }).hasAttribute('disabled')).toBe(false);
  });

  it('rename of a legacy [close, volume] chart sends its stored metrics exactly and no invented lookback', async () => {
    api.listGadgetRenderers.mockResolvedValue([...renderers, financeRenderer]);
    api.listGadgetSources.mockResolvedValue({ items: [source('a1', 'Alpha', 'alpha_vantage', ['measurement'])], next_cursor: null });
        const legacy = { ...orphanChart, id: 'd3', source_ids: ['a1'], scope: { symbols: ['NVDA'], metrics: ['close', 'volume'], lookback_days: null } };
    api.patchGadgetDefinition.mockResolvedValue({ ...legacy, revision: 3 });
    wrap({ definition: legacy });
    const nameInput = await screen.findByLabelText('Name');
    await userEvent.clear(nameInput);
    await userEvent.type(nameInput, 'Renamed');
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.patchGadgetDefinition).toHaveBeenCalled());
    const body = api.patchGadgetDefinition.mock.calls[0][1];
    expect(body.scope.metrics).toEqual(['close', 'volume']);
    expect(body.scope.lookback_days ?? null).toBeNull();
    expect(body).not.toHaveProperty('source_ids');
  });

  it('keeps Save disabled until the metric catalog has loaded for a measurement gadget', async () => {
    api.listGadgetRenderers.mockResolvedValue([...renderers, financeRenderer]);
    api.getMetricCatalog.mockReturnValue(new Promise(() => undefined));
    wrap({ definition: orphanChart });
    await screen.findByLabelText('Name');
    expect((screen.getByRole('button', { name: 'Save to library' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('names the multi-metric checkbox group by its visible label', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('n1', 'NOAA Kp', 'noaa_kp', ['measurement'])], next_cursor: null });
    await pickMetric('NOAA Kp');
    const group = await screen.findByRole('group', { name: 'Metric' });
    const labelId = group.getAttribute('aria-labelledby');
    expect(labelId && document.getElementById(labelId)?.textContent).toBe('Metric');
    expect(document.getElementById(labelId!)?.getAttribute('for')).toBeNull();
  });

  it('Open Sources link in the picker is filtered by renderer shapes', async () => {
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Metric/ }));
    const link = await screen.findByRole('link', { name: 'Open Settings > Data sources' });
    expect(link.getAttribute('href')).toBe('/settings/sources?shapes=measurement&for=provider_observation');
  });

  it('a saved non-world map layer stays checked and removable', async () => {
    api.listGadgetRenderers.mockResolvedValue([renderer('map')]);
    const saved = { id: 'm1', name: 'Map', revision: 1, renderer: 'map', source_ids: [], scope: { map_layer_ids: ['military'], lookback_days: 30 }, filters: { limit: 25 }, highlight_rules: [], config_version: 1, runtime_state: 'available' as const };
    const { container } = wrap({ definition: saved });
    (await screen.findByLabelText('Name'));
    (container.querySelector('details') as HTMLDetailsElement).open = true;
    const military = await screen.findByRole('checkbox', { name: /Military/ });
    expect(military.getAttribute('aria-checked')).toBe('true');
    expect(military.hasAttribute('disabled')).toBe(false);
  });

  it('shows format help under the Telegram channels input', async () => {
    api.listGadgetRenderers.mockResolvedValue([renderer('telegram_feed')]);
    const saved = { id: 't1', name: 'TG', revision: 1, renderer: 'telegram_feed', source_ids: [], scope: { channel_ids: [] }, filters: { limit: 25 }, highlight_rules: [], config_version: 1, runtime_state: 'available' as const };
    const { container } = wrap({ definition: saved });
    await screen.findByLabelText('Name');
    (container.querySelector('details') as HTMLDetailsElement).open = true;
    expect(await screen.findByText(dashboardMessages['en-us'].telegramChannelsHelp)).toBeTruthy();
  });

  it('shows a readable provider label for keyed and catalog providers', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('f1', 'Rates', 'fred', ['measurement']), source('o1', 'CLI', 'oecd_cli', ['measurement'])], next_cursor: null });
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Metric/ }));
    expect(await screen.findByText(/FRED · never updated/)).toBeTruthy();
    expect(screen.getByText(/OECD composite leading indicator/)).toBeTruthy();
  });

  it('sends the symbol picked for a Metric gadget on a symbol-keyed provider', async () => {
    api.createGadgetDefinition.mockResolvedValue({ id: 'n1' });
    await pickMetric('Binance spot');
    await userEvent.click(await screen.findByRole('combobox', { name: 'Symbols' }));
    await userEvent.click(await screen.findByRole('option', { name: 'BTCUSDT' }));
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.createGadgetDefinition).toHaveBeenCalled());
    expect(api.createGadgetDefinition.mock.calls[0][0].scope.symbols).toEqual(['BTCUSDT']);
  });

  it('persists removing a legacy symbol from a Metric gadget', async () => {
    const saved = { id: 'm2', name: 'BTC', revision: 1, renderer: 'provider_observation', source_ids: ['b1'], scope: { metrics: ['close'], symbols: ['BTCUSDT'] }, filters: { limit: 25 }, highlight_rules: [], config_version: 1, runtime_state: 'available' as const };
    api.patchGadgetDefinition.mockResolvedValue({ ...saved, revision: 2 });
    wrap({ definition: saved });
    await userEvent.click(await screen.findByRole('button', { name: /Remove BTCUSDT/i }));
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.patchGadgetDefinition).toHaveBeenCalled());
    expect(api.patchGadgetDefinition.mock.calls[0][1].scope.symbols).toEqual([]);
  });

  /** Picks a symbol on the chart editor through the Symbols combobox. */
  async function addSymbol(symbol: string) {
    await userEvent.click(await screen.findByRole('combobox', { name: 'Symbols' }));
    await userEvent.click(await screen.findByRole('option', { name: symbol }));
  }

  it('replaces symbols the new provider does not suggest when the datasource changes', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('p1', 'Paprika', 'coinpaprika', ['measurement']), source('f1', 'Frank', 'frankfurter', ['measurement'])], next_cursor: null });
    api.createGadgetDefinition.mockResolvedValue({ id: 'n1' });
    await pickChart('Paprika');
    await userEvent.click(screen.getByRole('radio', { name: /Frank/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.createGadgetDefinition).toHaveBeenCalled());
    // BTC (preselected for CoinPaprika) is not a Frankfurter suggestion: dropped, and the empty list takes its first suggestion.
    expect(api.createGadgetDefinition.mock.calls[0][0].scope.symbols).toEqual(['USD/VND']);
  });

  it('keeps symbols the new provider also suggests when the datasource changes', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('p1', 'Paprika', 'coinpaprika', ['measurement']), source('c1', 'Gecko', 'coingecko', ['measurement'])], next_cursor: null });
    api.createGadgetDefinition.mockResolvedValue({ id: 'n1' });
    await pickChart('Paprika');
    await userEvent.click(screen.getByRole('radio', { name: /Gecko/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.createGadgetDefinition).toHaveBeenCalled());
    expect(api.createGadgetDefinition.mock.calls[0][0].scope.symbols).toEqual(['BTC']);
  });

  it('preselects the first catalog metric and the first suggested symbol when a fixed-symbol source is picked for a chart', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('p1', 'Paprika', 'coinpaprika', ['measurement'])], next_cursor: null });
    api.createGadgetDefinition.mockResolvedValue({ id: 'n1' });
    await pickChart('Paprika');
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.createGadgetDefinition).toHaveBeenCalled());
    const scope = api.createGadgetDefinition.mock.calls[0][0].scope;
    expect(scope.metrics).toEqual(['spot_price']);
    expect(scope.symbols).toEqual(['BTC']);
  });

  it('does not preselect a symbol for owner-configured sources (Binance) and shows a hint', async () => {
    api.createGadgetDefinition.mockResolvedValue({ id: 'n1' });
    await pickChart('Binance spot');
    expect(await screen.findByText(/depend on how this datasource is configured/i)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.createGadgetDefinition).toHaveBeenCalled());
    expect(api.createGadgetDefinition.mock.calls[0][0].scope.symbols).toEqual([]);
  });

  it('drops metrics the new provider does not list when a Metric gadget changes datasource (Binance to NOAA Kp)', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('b1', 'Binance spot', 'binance', ['measurement']), source('n1', 'NOAA Kp', 'noaa_kp', ['measurement'])], next_cursor: null });
    const saved = { id: 'm9', name: 'Spot', revision: 1, renderer: 'provider_observation', source_ids: ['b1'], scope: { metrics: ['spot_price'], symbols: ['BTCUSDT'] }, filters: { limit: 25 }, highlight_rules: [], config_version: 1, runtime_state: 'available' as const };
    api.patchGadgetDefinition.mockResolvedValue({ ...saved, revision: 2 });
    wrap({ definition: saved });
    await userEvent.click(await screen.findByRole('radio', { name: /NOAA Kp/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.patchGadgetDefinition).toHaveBeenCalled());
    expect(api.patchGadgetDefinition.mock.calls[0][1].scope.metrics).not.toContain('spot_price');
  });

  it('keeps symbols when the chosen datasource keeps the same provider', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('f1', 'FRED one', 'fred', ['measurement']), source('f2', 'FRED two', 'fred', ['measurement'])], next_cursor: null });
    api.createGadgetDefinition.mockResolvedValue({ id: 'n1' });
    await pickChart('FRED one');
    await addSymbol('CPIAUCSL');
    await userEvent.click(screen.getByRole('radio', { name: /FRED two/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.createGadgetDefinition).toHaveBeenCalled());
    expect(api.createGadgetDefinition.mock.calls[0][0].scope.symbols).toEqual(['CPIAUCSL']);
  });

  it('a symbol edit on a legacy two-metric chart sends exactly one metric', async () => {
    api.listGadgetRenderers.mockResolvedValue([...renderers, financeRenderer]);
    api.listGadgetSources.mockResolvedValue({ items: [source('f1', 'FRED', 'fred', ['measurement'])], next_cursor: null });
    const legacy = { ...orphanChart, id: 'd4', source_ids: ['f1'], scope: { symbols: ['DFF'], metrics: ['fred_series', 'other'], lookback_days: null } };
    api.patchGadgetDefinition.mockResolvedValue({ ...legacy, revision: 3 });
    wrap({ definition: legacy });
    await userEvent.click(await screen.findByRole('button', { name: /Remove DFF/i }));
    await addSymbol('CPIAUCSL');
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.patchGadgetDefinition).toHaveBeenCalled());
    const scope = api.patchGadgetDefinition.mock.calls[0][1].scope;
    expect(scope.metrics).toEqual(['fred_series']);
    expect(scope.symbols).toEqual(['CPIAUCSL']);
  });

  it('re-binding a legacy multi-metric chart to a provider without a catalog entry sends only the first metric', async () => {
    api.listGadgetRenderers.mockResolvedValue([...renderers, financeRenderer]);
    api.listGadgetSources.mockResolvedValue({ items: [source('x1', 'Custom series', 'custom_ts', ['measurement'])], next_cursor: null });
    const legacy = { ...orphanChart, id: 'd8', source_ids: ['gone'], scope: { symbols: ['NVDA'], metrics: ['close', 'volume'], lookback_days: null } };
    api.patchGadgetDefinition.mockResolvedValue({ ...legacy, revision: 3 });
    wrap({ definition: legacy });
    await userEvent.click(await screen.findByRole('radio', { name: /Custom series/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.patchGadgetDefinition).toHaveBeenCalled());
    expect(api.patchGadgetDefinition.mock.calls[0][1].scope.metrics).toEqual(['close']);
  });

  it('a new chart bound to World Bank starts at the provider default lookback', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [
      source('w1', 'World Bank GDP', 'world_bank', ['measurement', 'series']),
      source('n1', 'NOAA Kp', 'noaa_kp', ['measurement', 'series']),
    ], next_cursor: null });
    api.createGadgetDefinition.mockResolvedValue({ id: 'n1' });
    await pickChart('World Bank GDP');
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.createGadgetDefinition).toHaveBeenCalled());
    expect(api.createGadgetDefinition.mock.calls[0][0].scope.lookback_days).toBe(3650);
  });

  it('keeps a lookback the owner edited when the datasource provider changes on a new chart', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('a1', 'Alpha', 'alpha_vantage', ['measurement']), source('w1', 'World Bank GDP', 'world_bank', ['measurement'])], next_cursor: null });
    api.createGadgetDefinition.mockResolvedValue({ id: 'n1' });
    await pickChart('Alpha');
    const lookback = screen.getByLabelText('Date range (days)');
    await userEvent.clear(lookback);
    await userEvent.type(lookback, '45');
    await userEvent.click(screen.getByRole('radio', { name: /World Bank GDP/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.createGadgetDefinition).toHaveBeenCalled());
    expect(api.createGadgetDefinition.mock.calls[0][0].scope.lookback_days).toBe(45);
  });

  it('a new Metric gadget without a catalog default starts at the runtime fallback of 30 days', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('a1', 'Alpha', 'alpha_vantage', ['measurement'])], next_cursor: null });
    api.createGadgetDefinition.mockResolvedValue({ id: 'n1' });
    await pickMetric('Alpha');
    await userEvent.click(await screen.findByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.createGadgetDefinition).toHaveBeenCalled());
    expect(api.createGadgetDefinition.mock.calls[0][0].scope.lookback_days).toBe(30);
  });

  it('blocks Save with a localized message when the lookback is outside 1-3660', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('a1', 'Alpha', 'alpha_vantage', ['measurement'])], next_cursor: null });
    await pickMetric('Alpha');
    const lookback = await screen.findByLabelText('Date range (days)');
    await userEvent.clear(lookback);
    await userEvent.type(lookback, '5000');
    const save = screen.getByRole('button', { name: 'Save to library' });
    expect(save.hasAttribute('disabled')).toBe(true);
    expect(document.getElementById(save.getAttribute('aria-describedby') as string)?.textContent).toBe(gadgetSettingsMessages['en-us'].lookbackHelp);
    expect(screen.getAllByText(gadgetSettingsMessages['en-us'].lookbackHelp).length).toBeGreaterThan(0);
  });

  it('maps a code-less backend 422 that names lookback_days to the lookback field message', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('a1', 'Alpha', 'alpha_vantage', ['measurement'])], next_cursor: null });
    api.createGadgetDefinition.mockRejectedValue(new ApiError(422, 'bad', { code: 'VALIDATION_ERROR', details: [{ type: 'less_than_equal', loc: ['body', 'scope', 'lookback_days'], msg: 'x' }] }));
    await pickMetric('Alpha');
    await userEvent.click(await screen.findByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(document.querySelector('[data-field="lookback_days"]')?.textContent).toContain(gadgetSettingsMessages['en-us'].lookbackHelp));
    expect(screen.queryByText('Could not save the gadget.')).toBeNull();
  });

  it('a new World Bank Metric gadget starts at the provider default lookback', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('w1', 'World Bank GDP', 'world_bank', ['measurement'])], next_cursor: null });
    api.createGadgetDefinition.mockResolvedValue({ id: 'n1' });
    await pickMetric('World Bank GDP');
    await userEvent.click(await screen.findByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.createGadgetDefinition).toHaveBeenCalled());
    expect(api.createGadgetDefinition.mock.calls[0][0].scope.lookback_days).toBe(3650);
  });

  it('single-country providers (world_bank) show no Region field', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('w1', 'World Bank GDP', 'world_bank', ['measurement', 'series'])], next_cursor: null });
    await pickChart('World Bank GDP');
    await screen.findByRole('combobox', { name: 'Metric' });
    expect(screen.queryByRole('combobox', { name: 'Region' })).toBeNull();
  });

  it('region-keyed chart sources (openaq) show a Region field and send the typed region', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('q1', 'OpenAQ', 'openaq', ['measurement', 'series'])], next_cursor: null });
    api.createGadgetDefinition.mockResolvedValue({ id: 'n1' });
    await pickChart('OpenAQ');
    await userEvent.click(await screen.findByRole('combobox', { name: 'Region' }));
    await userEvent.type(await screen.findByRole('textbox', { name: /Search or type a region/i }), 'location 8118');
    await userEvent.click(await screen.findByRole('option', { name: 'location 8118' }));
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.createGadgetDefinition).toHaveBeenCalled());
    expect(api.createGadgetDefinition.mock.calls[0][0].scope.regions).toEqual(['location 8118']);
  });

  it('shows the localized region_required error under the Region field', async () => {
        api.listGadgetSources.mockResolvedValue({ items: [source('q1', 'OpenAQ', 'openaq', ['measurement', 'series'])], next_cursor: null });
    api.createGadgetDefinition.mockRejectedValue(new ApiError(422, 'bad', { code: 'region_required', details: { field: 'regions' } }));
    await pickChart('OpenAQ');
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    expect(await screen.findByText(gadgetSettingsMessages['en-us'].fieldErrors.region_required)).toBeTruthy();
  });

  it('keeps paging while no compatible source is loaded and never shows the empty message', async () => {
    api.listGadgetSources.mockImplementation(async (_limit: number, cursor?: string) => cursor
      ? { items: [source('b1', 'Binance spot', 'binance', ['measurement'])], next_cursor: null }
      : { items: [source('g1', 'My GitHub', 'github', ['project_activity'])], next_cursor: 'p2' });
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Metric/ }));
    expect(await screen.findByText('Binance spot')).toBeTruthy();
    expect(screen.queryByText(gadgetSettingsMessages['en-us'].datasourceNoneAddInSources)).toBeNull();
    expect(api.listGadgetSources).toHaveBeenCalledTimes(2);
  });
  it('a saved source on page 2 keeps paging, shows no Inactive badge, shows symbols and keeps stored scope on rename', async () => {
    api.listGadgetRenderers.mockResolvedValue([...renderers, financeRenderer]);
    let release: (value: unknown) => void = () => undefined;
    const pageTwo = new Promise((resolve) => { release = resolve; });
    api.listGadgetSources.mockImplementation(async (_limit: number, cursor?: string) => cursor
      ? pageTwo
      : { items: [source('g1', 'My GitHub', 'github', ['project_activity'])], next_cursor: 'p2' });
    const legacy = { ...orphanChart, id: 'd5', source_ids: ['b1'], scope: { symbols: ['BTCUSDT'], metrics: [], lookback_days: null } };
    api.patchGadgetDefinition.mockResolvedValue({ ...legacy, revision: 3 });
    wrap({ definition: legacy });
    await waitFor(() => expect(api.listGadgetSources).toHaveBeenCalledTimes(2));
    // While page 2 is pending the placeholder must not claim the source is inactive.
    expect(screen.queryByText(gadgetSettingsMessages['en-us'].datasourceInactive)).toBeNull();
    release({ items: [source('b1', 'Binance spot', 'binance', ['measurement'])], next_cursor: null });
    expect(await screen.findByRole('button', { name: /Remove BTCUSDT/i })).toBeTruthy();
    expect(screen.queryByText(gadgetSettingsMessages['en-us'].datasourceInactive)).toBeNull();
    const nameInput = await screen.findByLabelText('Name');
    await userEvent.clear(nameInput);
    await userEvent.type(nameInput, 'Renamed');
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.patchGadgetDefinition).toHaveBeenCalled());
    const scope = api.patchGadgetDefinition.mock.calls[0][1].scope;
    // Unchanged source: stored scope goes back exactly as stored (no injected default metric).
    expect(scope.metrics).toEqual([]);
    expect(scope.symbols).toEqual(['BTCUSDT']);
  });

  it('a saved source that no page contains is reported Inactive once every page is loaded and metrics stay unchanged', async () => {
    api.listGadgetRenderers.mockResolvedValue([...renderers, financeRenderer]);
    api.listGadgetSources.mockResolvedValue({ items: [source('g1', 'My GitHub', 'github', ['project_activity'])], next_cursor: null });
    const legacy = { ...orphanChart, id: 'd6', source_ids: ['gone'], scope: { symbols: ['NVDA'], metrics: [], lookback_days: null } };
    api.patchGadgetDefinition.mockResolvedValue({ ...legacy, revision: 3 });
    wrap({ definition: legacy });
    expect(await screen.findByText(gadgetSettingsMessages['en-us'].datasourceInactive)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.patchGadgetDefinition).toHaveBeenCalled());
    const scope = api.patchGadgetDefinition.mock.calls[0][1].scope;
    expect(scope.metrics).toEqual([]);
    expect(scope.symbols).toEqual(['NVDA']);
  });
  it('explains the "location <id>" region format under the Region field', async () => {
        api.listGadgetSources.mockResolvedValue({ items: [source('q1', 'OpenAQ', 'openaq', ['measurement', 'series'])], next_cursor: null });
    await pickChart('OpenAQ');
    expect(await screen.findByText(gadgetSettingsMessages['en-us'].regionHelp)).toBeTruthy();
    expect(gadgetSettingsMessages['en-us'].regionHelp).toContain('location');
  });

  it('rename of a preset-created Alpha Vantage chart sends its stored empty scope unchanged', async () => {
    api.listGadgetRenderers.mockResolvedValue([...renderers, financeRenderer]);
    api.listGadgetSources.mockResolvedValue({ items: [source('a1', 'Alpha', 'alpha_vantage', ['measurement'])], next_cursor: null });
    const stored = { ...orphanChart, id: 'd7', source_ids: ['a1'], scope: { symbols: [], metrics: [], regions: [], lookback_days: null } };
    api.patchGadgetDefinition.mockResolvedValue({ ...stored, revision: 3 });
    wrap({ definition: stored });
    await screen.findByText('Alpha');
    const nameInput = await screen.findByLabelText('Name');
    await userEvent.clear(nameInput);
    await userEvent.type(nameInput, 'Renamed');
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.patchGadgetDefinition).toHaveBeenCalled());
    const body = api.patchGadgetDefinition.mock.calls[0][1];
    expect(body.name).toBe('Renamed');
    expect(body.scope).toMatchObject({ metrics: [], symbols: [], regions: [] });
    expect(body).not.toHaveProperty('source_ids');
  });

  it('rename of a legacy chart with several stored regions sends the stored regions unchanged', async () => {
    api.listGadgetRenderers.mockResolvedValue([...renderers, financeRenderer]);
    api.listGadgetSources.mockResolvedValue({ items: [source('w1', 'Air', 'openaq', ['measurement'])], next_cursor: null });
    const stored = { ...orphanChart, id: 'd11', source_ids: ['w1'], scope: { symbols: [], metrics: ['pm25', 'pm10'], regions: ['Hanoi', 'Saigon'], lookback_days: null } };
    api.patchGadgetDefinition.mockResolvedValue({ ...stored, revision: 3 });
    wrap({ definition: stored });
    await screen.findByText('Air');
    const nameInput = await screen.findByLabelText('Name');
    await userEvent.clear(nameInput);
    await userEvent.type(nameInput, 'Renamed');
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.patchGadgetDefinition).toHaveBeenCalled());
    expect(api.patchGadgetDefinition.mock.calls[0][1].scope).toMatchObject({ regions: ['Hanoi', 'Saigon'], metrics: ['pm25', 'pm10'] });
  });

  it('adding a symbol to a preset-created Alpha Vantage chart injects the default metric', async () => {
    api.listGadgetRenderers.mockResolvedValue([...renderers, financeRenderer]);
    api.listGadgetSources.mockResolvedValue({ items: [source('a1', 'Alpha', 'alpha_vantage', ['measurement'])], next_cursor: null });
    const stored = { ...orphanChart, id: 'd10', source_ids: ['a1'], scope: { symbols: [], metrics: [], regions: [], lookback_days: null } };
    api.patchGadgetDefinition.mockResolvedValue({ ...stored, revision: 3 });
    wrap({ definition: stored });
    await screen.findByText('Alpha');
    await userEvent.click(await screen.findByRole('combobox', { name: 'Symbols' }));
    await userEvent.type(await screen.findByRole('textbox', { name: /Search or type a symbol/i }), 'nvda');
    await userEvent.click(await screen.findByRole('option', { name: 'nvda' }));
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.patchGadgetDefinition).toHaveBeenCalled());
    const scope = api.patchGadgetDefinition.mock.calls[0][1].scope;
    expect(scope.metrics).toEqual(['close']);
    expect(scope.symbols).toEqual(['NVDA']);
  });

  it('adding a region to a preset-created OpenAQ chart injects its first metric', async () => {
    api.listGadgetRenderers.mockResolvedValue([...renderers, financeRenderer]);
    api.listGadgetSources.mockResolvedValue({ items: [source('q1', 'OpenAQ', 'openaq', ['measurement', 'series'])], next_cursor: null });
    const stored = { ...orphanChart, id: 'd11', source_ids: ['q1'], scope: { symbols: [], metrics: [], regions: [], lookback_days: null } };
    api.patchGadgetDefinition.mockResolvedValue({ ...stored, revision: 3 });
    wrap({ definition: stored });
    await userEvent.click(await screen.findByRole('combobox', { name: 'Region' }));
    await userEvent.type(await screen.findByRole('textbox', { name: /Search or type a region/i }), 'location 8118');
    await userEvent.click(await screen.findByRole('option', { name: 'location 8118' }));
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.patchGadgetDefinition).toHaveBeenCalled());
    const scope = api.patchGadgetDefinition.mock.calls[0][1].scope;
    expect(scope.metrics).toEqual([catalog.openaq.metrics[0].id]);
    expect(scope.regions).toEqual(['location 8118']);
  });

  it('shows a neutral loading hint, not Inactive, for a saved source while the first page is pending', async () => {
    api.listGadgetRenderers.mockResolvedValue([...renderers, financeRenderer]);
    api.listGadgetSources.mockReturnValue(new Promise(() => undefined));
    wrap({ definition: { ...orphanChart, id: 'd8', source_ids: ['b1'] } });
    expect((await screen.findAllByText(gadgetSettingsMessages['en-us'].datasourceLoading)).length).toBeGreaterThan(0);
    expect(screen.queryByText(gadgetSettingsMessages['en-us'].datasourceInactive)).toBeNull();
  });

  it('shows no Inactive badge for a saved source when the source read failed', async () => {
    api.listGadgetRenderers.mockResolvedValue([...renderers, financeRenderer]);
    api.listGadgetSources.mockRejectedValue(new ApiError(500, 'boom', { code: 'x' }));
    wrap({ definition: { ...orphanChart, id: 'd9', source_ids: ['b1'] } });
    expect(await screen.findByText(gadgetSettingsMessages['en-us'].datasourceLoadFailed)).toBeTruthy();
    expect(screen.queryByText(gadgetSettingsMessages['en-us'].datasourceInactive)).toBeNull();
  });

  it('resets a metric the new provider does not offer when the datasource changes (Alpha Vantage to World Bank)', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('a1', 'Alpha', 'alpha_vantage', ['measurement']), source('w1', 'World Bank GDP', 'world_bank', ['measurement'])], next_cursor: null });
    api.createGadgetDefinition.mockResolvedValue({ id: 'n1' });
    await pickChart('Alpha');
    await userEvent.click(screen.getByRole('radio', { name: /World Bank GDP/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.createGadgetDefinition).toHaveBeenCalled());
    expect(api.createGadgetDefinition.mock.calls[0][0].scope.metrics).toEqual(['gdp_current_usd']);
  });

  it('clears symbols the new provider cannot suggest when the datasource changes (Binance to Alpha Vantage)', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('b1', 'Binance spot', 'binance', ['measurement']), source('a1', 'Alpha', 'alpha_vantage', ['measurement'])], next_cursor: null });
    api.createGadgetDefinition.mockResolvedValue({ id: 'n1' });
    await pickChart('Binance spot');
    await userEvent.click(screen.getByRole('radio', { name: /Alpha/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.createGadgetDefinition).toHaveBeenCalled());
    expect(api.createGadgetDefinition.mock.calls[0][0].scope.symbols).toEqual([]);
  });

  it('shows datasource-specific loading copy while sources and the metric catalog load', async () => {
    api.listGadgetRenderers.mockResolvedValue([...renderers, financeRenderer]);
    api.listGadgetSources.mockReturnValue(new Promise(() => undefined));
    api.getMetricCatalog.mockReturnValue(new Promise(() => undefined));
    wrap({ definition: orphanChart });
    expect((await screen.findAllByText(gadgetSettingsMessages['en-us'].datasourceLoading)).length).toBeGreaterThan(0);
    expect(await screen.findByText(gadgetSettingsMessages['en-us'].metricCatalogLoading)).toBeTruthy();
  });

  it('labels the Region and Symbols fields with real labels and links the region help', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [source('q1', 'OpenAQ', 'openaq', ['measurement', 'series']), source('b1', 'Binance spot', 'binance', ['measurement'])], next_cursor: null });
    await pickChart('OpenAQ');
    const region = await screen.findByRole('combobox', { name: 'Region' });
    const label = screen.getByText('Region');
    expect(label.tagName).toBe('LABEL');
    expect(region.getAttribute('aria-labelledby')).toBe(label.id);
    const help = document.getElementById(region.getAttribute('aria-describedby') ?? '');
    expect(help?.textContent).toBe(gadgetSettingsMessages['en-us'].regionHelp);
    await userEvent.click(screen.getByRole('radio', { name: /Binance spot/ }));
    const symbols = await screen.findByRole('combobox', { name: 'Symbols' });
    expect(screen.getByText('Symbols').tagName).toBe('LABEL');
    expect(symbols.getAttribute('aria-labelledby')).toBe(screen.getByText('Symbols').id);
  });
});
