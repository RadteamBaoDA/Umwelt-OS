import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/core/api';
import { dashboardMessages } from '@/core/messages/dashboard';
import { gadgetSettingsMessages } from '@/core/messages/gadget-settings';
import { sourcesMessages } from '@/core/messages/sources';
import { uiSweepMessages } from '@/core/messages/ui-sweep';
import type { BuiltinSource } from '@/modules/sources/api';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'c' }) }));
vi.mock('next/link', () => ({ default: ({ href, children }: { href: string; children: React.ReactNode }) => <a href={href}>{children}</a> }));
const api = vi.hoisted(() => ({
  listGadgetSources: vi.fn(), listGadgetRenderers: vi.fn(), listGadgetTemplates: vi.fn(), createGadgetDefinition: vi.fn(), patchGadgetDefinition: vi.fn(),
}));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));
const sourcesApi = vi.hoisted(() => ({ listBuiltinSources: vi.fn(), enableBuiltinSource: vi.fn() }));
vi.mock('@/modules/sources/api', async (original) => ({ ...(await original<typeof import('@/modules/sources/api')>()), ...sourcesApi }));
const actions = vi.hoisted(() => ({ waitForActivation: vi.fn() }));
vi.mock('@/modules/sources/use-source-actions', async (original) => ({ ...(await original<typeof import('@/modules/sources/use-source-actions')>()), ...actions }));

import { GadgetEditor } from './gadget-editor';

const renderers = [
  { id: 'provider_observation', config_version: 1, minimum_width: 3, minimum_height: 3, runtime_state: 'available', capability_keys: [], source_mode: 'single', source_providers: ['alternative_me', 'binance', 'usgs'] },
  { id: 'news_feed', config_version: 1, minimum_width: 3, minimum_height: 3, runtime_state: 'available', capability_keys: [], source_mode: 'multi', source_providers: null },
];
const templates = [{ id: 'fear_greed', renderer: 'provider_observation', source_mode: 'single', providers: ['alternative_me'], scope: { metrics: ['fear_greed_index'] }, filters: {} }];
const builtin = (over: Partial<BuiltinSource> = {}): BuiltinSource => ({
  id: 'alternative_me', label: 'Fear and Greed (Alternative.me)', category: 'economy', eligibility: 'open', terms_url: 'https://example.org/t', terms_checked_on: '2026-10-07',
  attribution: 'Credit Alternative.me.', execution: 'supported', source_id: null, enabled: false, ...over,
});
const messages = { gadgetSettings: gadgetSettingsMessages['en-us'], dashboard: dashboardMessages['en-us'], sources: sourcesMessages['en-us'], uiSweep: uiSweepMessages['en-us'] };
const onSaved = vi.fn();
/** Renders the editor with query and intl providers. */
const wrap = (props: Partial<React.ComponentProps<typeof GadgetEditor>> = {}) => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <NextIntlClientProvider locale="en-US" messages={messages}><GadgetEditor onSaved={onSaved} {...props} /></NextIntlClientProvider>
  </QueryClientProvider>);

beforeEach(() => {
  [...Object.values(api), ...Object.values(sourcesApi), ...Object.values(actions), onSaved].forEach((mock) => mock.mockReset());
  api.listGadgetRenderers.mockResolvedValue(renderers);
  api.listGadgetTemplates.mockResolvedValue(templates);
  api.listGadgetSources.mockResolvedValue({ items: [
    { id: 'b1', name: 'Binance spot', provider: 'binance', status: 'active', type: 'api', generation: 1, local_only: false },
    { id: 'g1', name: 'My GitHub', provider: 'github', status: 'active', type: 'api', generation: 1, local_only: false },
  ], next_cursor: null });
  sourcesApi.listBuiltinSources.mockResolvedValue([builtin(), builtin({ id: 'usgs', label: 'USGS quakes', source_id: 's9', enabled: true })]);
  actions.waitForActivation.mockResolvedValue({ ok: true, code: null });
});
afterEach(() => cleanup());

describe('GadgetEditor', () => {
  it('picks a template, merges enabled sources with enable-able built-ins and keeps Advanced collapsed', async () => {
    const { container } = wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Fear & Greed index/ }));
    expect(await screen.findByLabelText('Name')).toHaveProperty('value', 'Fear & Greed index');
    // Template providers narrow the renderer's accepted list: Binance and GitHub sources are not offered.
    expect(await screen.findByText('Fear and Greed (Alternative.me)')).toBeTruthy();
    expect(screen.queryByText('Binance spot')).toBeNull();
    expect(screen.queryByText('My GitHub')).toBeNull();
    expect(screen.getByText('Enable')).toBeTruthy();
    expect((container.querySelector('details') as HTMLDetailsElement).open).toBe(false);
  });

  it('disables Save until a source is picked', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [{ id: 'a1', name: 'Fear feed', provider: 'alternative_me', status: 'active', type: 'api', generation: 1, local_only: false }], next_cursor: null });
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Fear & Greed index/ }));
    const save = await screen.findByRole('button', { name: 'Save to library' });
    expect(save.hasAttribute('disabled')).toBe(true);
    await userEvent.click(await screen.findByRole('radio', { name: /Fear feed/ }));
    expect(save.hasAttribute('disabled')).toBe(false);
  });

  it('enables a built-in inline, waits for activation, then saves with the new source id', async () => {
    sourcesApi.enableBuiltinSource.mockResolvedValue(builtin({ source_id: 'new-src', enabled: true }));
    let release: (value: { ok: boolean; code: null }) => void = () => undefined;
    actions.waitForActivation.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    api.createGadgetDefinition.mockResolvedValue({ id: 'def1', revision: 1, renderer: 'provider_observation' });
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Fear & Greed index/ }));
    await userEvent.click(await screen.findByRole('radio', { name: /Fear and Greed/ }));
    await userEvent.click(await screen.findByRole('button', { name: 'Enable and use' }));
    expect((await screen.findAllByText('Activating…')).length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: 'Save to library' }).hasAttribute('disabled')).toBe(true);
    release({ ok: true, code: null });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save to library' }).hasAttribute('disabled')).toBe(false));
    await userEvent.click(screen.getByRole('button', { name: 'Save to library' }));
    await waitFor(() => expect(api.createGadgetDefinition).toHaveBeenCalled());
    expect(api.createGadgetDefinition.mock.calls[0][0]).toMatchObject({ renderer: 'provider_observation', source_ids: ['new-src'], filters: { limit: 25 } });
    expect(onSaved).toHaveBeenCalled();
  });

  it('keeps the enable dialog open and shows the mapped error on operator_required', async () => {
    sourcesApi.listBuiltinSources.mockResolvedValue([builtin({ eligibility: 'review' })]);
    sourcesApi.enableBuiltinSource.mockRejectedValue(new ApiError(403, 'operator_required'));
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Fear & Greed index/ }));
    expect(await screen.findByText('Operator approval')).toBeTruthy();
    await userEvent.click(await screen.findByRole('radio', { name: /Fear and Greed/ }));
    await userEvent.click(await screen.findByRole('button', { name: 'Enable and use' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Only the instance operator');
    expect(screen.getByRole('button', { name: 'Enable and use' })).toBeTruthy();
  });

  it('degrades to enabled sources when the built-in catalog fails', async () => {
    sourcesApi.listBuiltinSources.mockRejectedValue(new Error('down'));
    api.listGadgetSources.mockResolvedValue({ items: [{ id: 'a1', name: 'Fear feed', provider: 'alternative_me', status: 'active', type: 'api', generation: 1, local_only: false }], next_cursor: null });
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Fear & Greed index/ }));
    expect(await screen.findByRole('radio', { name: /Fear feed/ })).toBeTruthy();
    expect(await screen.findByText(/Built-in sources could not be loaded/)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Open Settings > Sources' }).getAttribute('href')).toBe('/settings/sources');
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
    api.listGadgetTemplates.mockResolvedValue([...templates,
      { id: 'world_news', renderer: 'news_feed', source_mode: 'multi', providers: [], scope: {}, filters: { limit: 10 } }]);
    const { container } = wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Fear & Greed index/ }));
    const details = container.querySelector('details') as HTMLDetailsElement;
    details.open = true;
    await userEvent.type(await screen.findByLabelText('Keywords'), 'oil');
    await userEvent.click(screen.getByRole('button', { name: 'Change template' }));
    await userEvent.click(await screen.findByRole('button', { name: /World news/ }));
    (container.querySelector('details') as HTMLDetailsElement).open = true;
    expect(await screen.findByLabelText('Keywords')).toHaveProperty('value', '');
    expect(screen.getByLabelText('Items shown')).toHaveProperty('value', '10');
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

  it('keeps a later selection when an earlier activation fails', async () => {
    api.listGadgetTemplates.mockResolvedValue([{ id: 'tech_news', renderer: 'news_feed', source_mode: 'multi', providers: ['alternative_me'], scope: {}, filters: {} }]);
    api.listGadgetSources.mockResolvedValue({ items: [{ id: 'a1', name: 'Fear feed', provider: 'alternative_me', status: 'active', type: 'api', generation: 1, local_only: false }], next_cursor: null });
    sourcesApi.enableBuiltinSource.mockResolvedValue(builtin({ source_id: 'new-src', enabled: true }));
    let release: (value: { ok: boolean; code: string }) => void = () => undefined;
    actions.waitForActivation.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: /Tech news/ }));
    await userEvent.click(await screen.findByRole('checkbox', { name: /Fear and Greed/ }));
    await userEvent.click(await screen.findByRole('button', { name: 'Enable and use' }));
    // The activating source shows its display name, not its id, and its built-in row is gone at once.
    expect(await screen.findByRole('checkbox', { name: /Fear and Greed \(Alternative.me\)/ })).toBeTruthy();
    expect(screen.queryByText('Enable')).toBeNull();
    await userEvent.click(await screen.findByRole('checkbox', { name: /Fear feed/ }));
    release({ ok: false, code: 'boom' });
    expect(await screen.findByText(/did not activate/)).toBeTruthy();
    expect(screen.getByRole('checkbox', { name: /Fear feed/ }).getAttribute('aria-checked')).toBe('true');
  });
});
