import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/core/api';
import { sourcesMessages } from '@/core/messages/sources';
import type { BuiltinSource } from './api';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'c' }) }));
const api = vi.hoisted(() => ({ listBuiltinSources: vi.fn(), enableBuiltinSource: vi.fn(), disableBuiltinSource: vi.fn() }));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));

import { BuiltinSources } from './builtin-sources';

const row = (over: Partial<BuiltinSource>): BuiltinSource => ({
  id: 'world_bank', label: 'World Bank GDP', category: 'economy', eligibility: 'open', terms_url: 'https://example.org/terms',
  terms_checked_on: '2026-10-07', attribution: 'Credit the World Bank.', execution: 'supported', source_id: null, enabled: false, ...over,
});
const wrap = () => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}><BuiltinSources /></NextIntlClientProvider>
  </QueryClientProvider>);

beforeEach(() => { Object.values(api).forEach((mock) => mock.mockReset()); });

describe('BuiltinSources', () => {
  it('groups rows by category and shows attribution with a terms link', async () => {
    api.listBuiltinSources.mockResolvedValue([row({}), row({ id: 'usgs', label: 'USGS quakes', category: 'hazards' })]);
    wrap();
    expect(await screen.findByRole('group', { name: 'Economy and markets' })).toBeTruthy();
    expect(screen.getByRole('group', { name: 'Hazards and weather' })).toBeTruthy();
    expect(screen.getAllByText(/Credit the World Bank/).length).toBe(2);
    expect(screen.getAllByRole('link', { name: 'Usage terms' })[0].getAttribute('href')).toBe('https://example.org/terms');
  });

  it('enables only after confirming the declared use in the dialog', async () => {
    api.listBuiltinSources.mockResolvedValue([row({})]);
    api.enableBuiltinSource.mockResolvedValue(row({ enabled: true, source_id: 's' }));
    wrap();
    fireEvent.click(await screen.findByRole('switch', { name: 'Collect World Bank GDP' }));
    expect(api.enableBuiltinSource).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole('button', { name: 'Enable source' }));
    await waitFor(() => expect(api.enableBuiltinSource).toHaveBeenCalledWith('world_bank', 'personal', 'c'));
  });

  it('disables immediately without a dialog', async () => {
    api.listBuiltinSources.mockResolvedValue([row({ enabled: true, source_id: 's' })]);
    api.disableBuiltinSource.mockResolvedValue(row({ source_id: 's' }));
    wrap();
    fireEvent.click(await screen.findByRole('switch', { name: 'Collect World Bank GDP' }));
    await waitFor(() => expect(api.disableBuiltinSource).toHaveBeenCalledWith('world_bank', 'c'));
  });

  it('explains the operator requirement for review-class sources and shows the server refusal', async () => {
    api.listBuiltinSources.mockResolvedValue([row({ id: 'bbc_world', label: 'BBC World News', category: 'news', eligibility: 'review' })]);
    api.enableBuiltinSource.mockRejectedValue(new ApiError(403, 'operator_required'));
    wrap();
    fireEvent.click(await screen.findByRole('switch', { name: 'Collect BBC World News' }));
    expect(await screen.findByText(/approve them as the instance operator/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Enable source' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Only the instance operator');
  });
});
