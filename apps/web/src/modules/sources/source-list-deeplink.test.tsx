import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sourcesMessages } from '@/core/messages/sources';
import type { Source } from './api';

const editorMounts = vi.fn();
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 't' }) }));
vi.mock('@/core/guarded-navigation', () => ({ useGuardedNavigation: () => ({ ensureSourcesDocument: () => true }) }));
vi.mock('./connector-editor', () => ({
  ConnectorEditor: ({ source, onClose }: { source: Source | null; onClose: () => void }) => {
    editorMounts(source?.id);
    return <div data-testid="editor">{source?.id}<button onClick={onClose}>close-editor</button></div>;
  },
}));
vi.mock('./api', async (original) => ({
  ...(await original<typeof import('./api')>()),
  listSources: vi.fn(async () => ({ items: [{ id: 's1', type: 'manual', name: 'One', status: 'active' }, { id: 's2', type: 'manual', name: 'Two', status: 'active' }], next_cursor: null })),
  getGitHubWebhookStatus: vi.fn(async () => null),
}));

import { SourceList } from './source-list';

const renderList = () => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}><SourceList /></NextIntlClientProvider>
  </QueryClientProvider>);

describe('SourceList ?source deep link', () => {
  beforeEach(() => editorMounts.mockClear());
  afterEach(() => { cleanup(); window.history.replaceState(null, '', '/'); });

  it('opens the editor for a loaded source id once', async () => {
    window.history.replaceState(null, '', '/?source=s2');
    renderList();
    expect((await screen.findByTestId('editor')).textContent).toContain('s2');
    expect(new Set(editorMounts.mock.calls.map((c) => c[0]))).toEqual(new Set(['s2']));
  });

  it.each(['/?source=nope', '/'])('opens nothing for %s', async (url) => {
    window.history.replaceState(null, '', url);
    renderList();
    await screen.findByText('One');
    expect(screen.queryByTestId('editor')).toBeNull();
  });

  it('does not re-open after the editor is closed', async () => {
    window.history.replaceState(null, '', '/?source=s1');
    renderList();
    await userEvent.click(await screen.findByText('close-editor'));
    await waitFor(() => expect(screen.queryByTestId('editor')).toBeNull());
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByTestId('editor')).toBeNull();
  });
});
