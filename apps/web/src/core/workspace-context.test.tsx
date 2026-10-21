import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useWorkspace, WorkspaceProvider, type WorkspaceContextValue, type WorkspaceRead } from '@/core/workspace-context';
import { workspaceKeys } from '@/core/query-keys';

const items: WorkspaceRead[] = [
  { id: 'ws-a', name: 'Work', owner_user_id: 1, is_default: false, role: 'owner', configuration_revision: 1 },
  { id: 'ws-b', name: 'Home', owner_user_id: 1, is_default: true, role: 'owner', configuration_revision: 3 },
  { id: 'ws-c', name: 'Observed', owner_user_id: 7, is_default: true, role: 'admin_viewer', configuration_revision: 2 },
];

function Probe({ onRender }: { onRender: (value: WorkspaceContextValue) => void }) {
  onRender(useWorkspace());
  return null;
}

function renderProvider(client: QueryClient) {
  const onRender = vi.fn<(value: WorkspaceContextValue) => void>();
  const latest = () => onRender.mock.lastCall![0];
  render(
    <QueryClientProvider client={client}>
      <WorkspaceProvider>
        <Probe onRender={onRender} />
      </WorkspaceProvider>
    </QueryClientProvider>,
  );
  return latest;
}

beforeEach(() => {
  window.sessionStorage.clear();
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === '/api/v1/workspaces') {
      return new Response(JSON.stringify({ items }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    throw new Error(`unexpected fetch ${path}`);
  }));
});

describe('WorkspaceProvider', () => {
  it('loads the workspace list and defaults to the is_default workspace', async () => {
    const latest = renderProvider(new QueryClient({ defaultOptions: { queries: { retry: false } } }));

    await waitFor(() => expect(latest().workspaces).toHaveLength(3));
    expect(latest().defaultWorkspaceId).toBe('ws-b');
    expect(latest().selection?.id).toBe('ws-b');
    expect(latest().isOwner).toBe(true);
  });

  it('bumps the generation and clears scoped queries when switching workspace', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const latest = renderProvider(client);
    await waitFor(() => expect(latest().selection?.id).toBe('ws-b'));
    const before = latest().generation;
    client.setQueryData(['documents'], { items: [] });

    act(() => latest().selectWorkspace('ws-a'));

    await waitFor(() => expect(latest().generation).toBe(before + 1));
    expect(latest().selection?.id).toBe('ws-a');
    expect(client.getQueryData(['documents'])).toBeUndefined();
    expect(client.getQueryData(workspaceKeys.list())).toEqual({ items });
  });

  it('marks a selected cross-workspace view read-only and clears the prior workspace cache', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const latest = renderProvider(client);
    await waitFor(() => expect(latest().selection?.id).toBe('ws-b'));
    client.setQueryData(['documents'], { items: ['private-to-old-workspace'] });

    act(() => latest().selectWorkspace('ws-c'));

    await waitFor(() => expect(latest().isAdminViewer).toBe(true));
    expect(latest().isOwner).toBe(false);
    expect(latest().selection?.role).toBe('admin_viewer');
    expect(client.getQueryData(['documents'])).toBeUndefined();
  });
});
