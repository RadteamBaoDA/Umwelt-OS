import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sourcesMessages } from '@/core/messages/sources';
import type { BuiltinSource } from './api';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'c' }) }));
const api = vi.hoisted(() => ({ listBuiltinSources: vi.fn(), enableBuiltinSource: vi.fn() }));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));

import { BuiltinResumeList } from './builtin-sources';

const row = (over: Partial<BuiltinSource>): BuiltinSource => ({
  id: 'bbc_world', label: 'BBC World News', category: 'news', eligibility: 'review', terms_url: 'https://example.org/terms',
  terms_checked_on: '2026-10-07', attribution: 'Credit the BBC.', execution: 'supported', source_id: 'src-1', enabled: false, data_shapes: ['measurement'], ...over,
});
const onChanged = vi.fn();
const wrap = () => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}><BuiltinResumeList onChanged={onChanged} /></NextIntlClientProvider>
  </QueryClientProvider>);

beforeEach(() => {
  [...Object.values(api)].forEach((mock) => mock.mockReset());
  onChanged.mockClear();
});
afterEach(() => cleanup());

describe('BuiltinResumeList', () => {
  it('renders nothing when no added built-in is paused', async () => {
    api.listBuiltinSources.mockResolvedValue([row({ enabled: true }), row({ id: 'ecb', source_id: null })]);
    const { container } = wrap();
    await waitFor(() => expect(api.listBuiltinSources).toHaveBeenCalled());
    expect(container.querySelector('section')).toBeNull();
  });

  it('resumes a paused built-in with the personal use and reports the change', async () => {
    api.listBuiltinSources.mockResolvedValue([row({})]);
    api.enableBuiltinSource.mockResolvedValue(row({ enabled: true }));
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: 'Resume' }));
    await waitFor(() => expect(api.enableBuiltinSource).toHaveBeenCalledWith('bbc_world', 'personal', 'c'));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it('shows the failure message when resume is refused', async () => {
    api.listBuiltinSources.mockResolvedValue([row({})]);
    api.enableBuiltinSource.mockRejectedValue(new Error('operator_required'));
    wrap();
    await userEvent.click(await screen.findByRole('button', { name: 'Resume' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not resume this built-in source.');
  });
});
