import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/core/api';
import { sourcesMessages } from '@/core/messages/sources';
import type { DataShape } from '@/core/data-shapes';
import type { BuiltinSource } from './api';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'c' }) }));
const api = vi.hoisted(() => ({ listBuiltinSources: vi.fn(), enableBuiltinSource: vi.fn(), disableBuiltinSource: vi.fn(), archiveSource: vi.fn() }));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));
const actions = vi.hoisted(() => ({ waitForActivation: vi.fn(), collectAndWait: vi.fn() }));
vi.mock('./use-source-actions', async (original) => ({ ...(await original<typeof import('./use-source-actions')>()), ...actions }));

import { BuiltinWizard, builtinEnableErrorKey } from './builtin-sources';

const row = (over: Partial<BuiltinSource>): BuiltinSource => ({
  id: 'world_bank', label: 'World Bank GDP', category: 'economy', eligibility: 'open', terms_url: 'https://example.org/terms',
  terms_checked_on: '2026-10-07', attribution: 'Credit the World Bank.', execution: 'supported', source_id: null, enabled: false, data_shapes: ['measurement'], ...over,
});
const onClose = vi.fn();
const wrap = (shapeFilter?: DataShape[], onAddConnector?: () => void) => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}><BuiltinWizard shapeFilter={shapeFilter} onAddConnector={onAddConnector} onClose={onClose} onChanged={() => undefined} /></NextIntlClientProvider>
  </QueryClientProvider>);
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
    api.listBuiltinSources.mockResolvedValue([row({}), row({ id: 'usgs', label: 'USGS quakes', category: 'hazards', source_id: 's1', enabled: true })]);
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
    actions.collectAndWait.mockResolvedValue({ request_id: 'r', source_id: 's', status: 'succeeded', ingestion_run_id: 'run-1', error_code: null });
    wrap();
    await pickAndContinue('World Bank GDP');
    expect(api.enableBuiltinSource).not.toHaveBeenCalled();
    expect(screen.queryByLabelText(/Build knowledge graph/)).toBeNull();
    expect(screen.queryByLabelText(/Index for Q&A/)).toBeNull();
    expect(screen.getByLabelText('Data tracking mode')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(api.enableBuiltinSource).toHaveBeenCalledWith('world_bank', 'personal', 'c', undefined, { graph_enabled: false, rag_index_enabled: false }));
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
    actions.collectAndWait.mockResolvedValue({ request_id: 'r', source_id: 's', status: 'failed', ingestion_run_id: null, error_code: 'upstream_error' });
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
    api.listBuiltinSources.mockResolvedValue([row({ source_id: 's1', enabled: true }), row({ id: 'usgs', label: 'USGS quakes', data_shapes: ['event'] })]);
    wrap(['measurement']);
    expect(await screen.findByText(sourcesMessages['en-us'].addBuiltinAllAdded)).toBeTruthy();
    expect(screen.queryByText(sourcesMessages['en-us'].shapeFilterNone)).toBeNull();
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
    expect(await screen.findByText(new RegExp(sourcesMessages['en-us'].shapeFilterNoBuiltin))).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: sourcesMessages['en-us'].addConnector }));
    expect(addConnector).toHaveBeenCalledTimes(1);
  });
});

describe('builtinEnableErrorKey', () => {
  it('maps operator, terms and duplicate failures and falls back to the generic message', () => {
    expect(builtinEnableErrorKey(new ApiError(403, 'operator_required'))).toBe('builtinErrorOperator');
    expect(builtinEnableErrorKey(new ApiError(403, 'provider_terms_ineligible'))).toBe('builtinErrorTerms');
    expect(builtinEnableErrorKey(new ApiError(409, 'conflict', { code: 'builtin_already_added' }))).toBe('builtinAlreadyAdded');
    expect(builtinEnableErrorKey(new Error('boom'))).toBe('builtinErrorGeneric');
  });
});
