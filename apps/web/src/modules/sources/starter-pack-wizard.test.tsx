import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/core/api';
import { sourcesMessages } from '@/core/messages/sources';
import type { BuiltinSource } from './api';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'c' }) }));
const api = vi.hoisted(() => ({ listBuiltinSources: vi.fn(), listSources: vi.fn(), getConnectorCatalog: vi.fn(), enableBuiltinSource: vi.fn(), createScopedSource: vi.fn() }));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));

import { StarterPackWizard } from './starter-pack-wizard';

const row = (id: string, over: Partial<BuiltinSource> = {}): BuiltinSource => ({
  id, label: `Label ${id}`, category: 'news', eligibility: 'open', terms_url: 'https://e.org', terms_checked_on: '2026-11-04', attribution: 'x',
  execution: 'supported', source_id: null, enabled: false, data_shapes: ['news'], ...over,
});
const wrap = (onAddConnector?: () => void, onBuildDashboard?: () => void) => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}><StarterPackWizard onClose={() => undefined} onChanged={() => undefined} onAddConnector={onAddConnector} onBuildDashboard={onBuildDashboard} /></NextIntlClientProvider>
  </QueryClientProvider>);
const checkbox = (name: string) => screen.findByRole('checkbox', { name: `Add ${name}` });
/** Unchecks every row except the given built-in ids, so a run touches a known, ordered subset. */
async function keepOnly(keep: string[]) {
  await checkbox('Label hn_top');
  const boxes = (await screen.findAllByRole('checkbox')).filter((box) => box.getAttribute('aria-checked') === 'true');
  for (const box of boxes) {
    const label = box.getAttribute('aria-label') ?? '';
    if (!keep.some((id) => label === `Add Label ${id}`)) await userEvent.click(box);
  }
}

beforeEach(() => {
  Object.values(api).forEach((mock) => mock.mockReset());
  api.listSources.mockResolvedValue({ items: [], next_cursor: null });
  api.getConnectorCatalog.mockResolvedValue([]);
  api.listBuiltinSources.mockResolvedValue([row('hn_top'), row('lobsters', { source_id: 'x', enabled: true }), row('vn_stock', { min_interval_minutes: 360 }), row('wwr_jobs'), row('itviec_jobs', { min_interval_minutes: 360 }), row('vietnamworks_jobs', { min_interval_minutes: 360 }), row('bbc_world')]);
  api.enableBuiltinSource.mockResolvedValue(row('hn_top'));
  api.createScopedSource.mockResolvedValue({ id: 'new' });
});
afterEach(() => cleanup());

describe('StarterPackWizard', () => {
  it('shows Build my dashboard after a successful run only when the prop is given', async () => {
    const build = vi.fn();
    wrap(undefined, build);
    await keepOnly(['hn_top']);
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Build my dashboard' }));
    expect(build).toHaveBeenCalledTimes(1);
    cleanup();
    wrap();
    await keepOnly(['hn_top']);
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    await screen.findByTestId('starter-row-hn_top');
    await waitFor(() => expect(within(screen.getByTestId('starter-row-hn_top')).getByText('Done')).toBeTruthy());
    expect(screen.queryByRole('button', { name: 'Build my dashboard' })).toBeNull();
  });

  it('notes when the provider floor raises a starter interval', async () => {
    api.getConnectorCatalog.mockResolvedValue([{ provider_id: 'arxiv', min_interval_minutes: 720 }]);
    wrap();
    expect(await screen.findByText('Raised to every 1440 min to respect the provider limit.')).toBeTruthy();
  });

  it('preselects available rows, unchecks already added ones and hides builtins the server lacks', async () => {
    wrap();
    expect((await checkbox('Label hn_top')).getAttribute('aria-checked')).toBe('true');
    const added = await checkbox('Label lobsters');
    expect(added.getAttribute('aria-checked')).toBe('false');
    expect(screen.getAllByText('Already added').length).toBeGreaterThan(0);
    expect(screen.queryByRole('checkbox', { name: 'Add Label nvd' })).toBeNull();
    expect(screen.getByTestId('starter-linkedin-guide')).toBeTruthy();
  });

  it('creates sequentially, continues after a failure and retries only the failed row', async () => {
    api.enableBuiltinSource.mockImplementation(async (provider: string) => {
      if (provider === 'vn_stock') throw new ApiError(500, 'boom');
      return row(provider);
    });
    wrap();
    await keepOnly(['hn_top', 'vn_stock', 'bbc_world']);
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(screen.getByTestId('starter-row-vn_stock').textContent).toContain('Failed'));
    expect(api.enableBuiltinSource.mock.calls.map((call) => call[0])).toEqual(['hn_top', 'vn_stock', 'bbc_world']);
    expect(within(screen.getByTestId('starter-row-hn_top')).getByText('Done')).toBeTruthy();

    api.enableBuiltinSource.mockClear();
    api.enableBuiltinSource.mockResolvedValue(row('vn_stock'));
    await userEvent.click(screen.getByRole('button', { name: 'Retry failed' }));
    await waitFor(() => expect(within(screen.getByTestId('starter-row-vn_stock')).getByText('Done')).toBeTruthy());
    expect(api.enableBuiltinSource.mock.calls.map((call) => call[0])).toEqual(['vn_stock']);
  });

  it('treats 409 builtin_already_added as present and sends tickers, edited name and personal use', async () => {
    api.enableBuiltinSource.mockImplementation(async (provider: string) => {
      if (provider === 'hn_top') throw new ApiError(409, 'builtin_already_added', { code: 'builtin_already_added' });
      return row(provider);
    });
    wrap();
    await keepOnly(['hn_top', 'vn_stock']);
    const tickers = await screen.findByLabelText('Tickers (comma-separated)');
    await userEvent.clear(tickers);
    await userEvent.type(tickers, 'fpt, vcb');
    const name = screen.getByLabelText('Name for Label vn_stock');
    await userEvent.clear(name);
    await userEvent.type(name, 'My stocks');
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(within(screen.getByTestId('starter-row-hn_top')).getByText('Already added')).toBeTruthy());
    const call = api.enableBuiltinSource.mock.calls.find((args) => args[0] === 'vn_stock')!;
    expect(call[1]).toBe('personal');
    expect(call[4]).toMatchObject({ vn_symbols: ['FPT', 'VCB'], name: 'My stocks' });
  });

  it('sends editable job keywords with the default Ho Chi Minh city and blocks more than five', async () => {
    wrap();
    await keepOnly(['vietnamworks_jobs']);
    const keywordsField = await screen.findByLabelText('Search keywords (comma-separated, up to 5)');
    expect((keywordsField as HTMLInputElement).value).toBe('developer');
    fireEvent.change(keywordsField, { target: { value: 'a, b, c, d, e, f' } });
    expect(screen.getByRole('button', { name: 'Next' }).hasAttribute('disabled')).toBe(true);
    fireEvent.change(keywordsField, { target: { value: 'Data Engineer, python, data engineer' } });
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(api.enableBuiltinSource).toHaveBeenCalled());
    const call = api.enableBuiltinSource.mock.calls.find((args) => args[0] === 'vietnamworks_jobs')!;
    expect(call[4]).toMatchObject({ job_keywords: ['Data Engineer', 'python'], job_cities: ['ho_chi_minh'] });
  }, 20000);

  it('enables ITviec jobs with the default developer keyword and Ho Chi Minh city', async () => {
    wrap();
    await keepOnly(['itviec_jobs']);
    await userEvent.click(await screen.findByRole('button', { name: 'Next' }));
    await waitFor(() => expect(api.enableBuiltinSource).toHaveBeenCalled());
    const call = api.enableBuiltinSource.mock.calls.find((args) => args[0] === 'itviec_jobs')!;
    expect(call[4]).toMatchObject({ job_keywords: ['developer'], job_cities: ['ho_chi_minh'] });
  }, 20000);

  it('blocks Next on an invalid ticker', async () => {
    wrap();
    const tickers = await screen.findByLabelText('Tickers (comma-separated)');
    await userEvent.clear(tickers);
    await userEvent.type(tickers, 'F');
    expect(screen.getByRole('button', { name: 'Next' }).hasAttribute('disabled')).toBe(true);
  });

  it('opens the connector flow from the LinkedIn guide', async () => {
    const onAddConnector = vi.fn();
    wrap(onAddConnector);
    await userEvent.click(await screen.findByRole('button', { name: 'Add Gmail source' }));
    expect(onAddConnector).toHaveBeenCalled();
  });
});
