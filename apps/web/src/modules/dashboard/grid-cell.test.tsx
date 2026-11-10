import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { gadgetSettingsMessages } from '@/core/messages/gadget-settings';
import { sourcesMessages } from '@/core/messages/sources';
import { uiSweepMessages } from '@/core/messages/ui-sweep';
import type { GadgetInstance } from './api';

const chat = vi.hoisted(() => ({ openDrawer: vi.fn() }));
vi.mock('@/core/app-shell/chat-controller', () => ({ useChatController: () => chat }));
const sourceApi = vi.hoisted(() => ({ listSources: vi.fn() }));
vi.mock('@/modules/sources/api', async (original) => ({ ...(await original<object>()), listSources: sourceApi.listSources }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));

vi.mock('./widget-registry', async (original) => ({
  ...(await original<object>()),
  isGadgetRendererRegistered: (id: string) => id === 'reports_unread',
  RegisteredGadget: ({ onUnreadCountChange }: { onUnreadCountChange?: (n: number) => void }) => {
    React.useEffect(() => onUnreadCountChange?.(3), [onUnreadCountChange]);
    return <div>fake feed</div>;
  },
}));

import { GridCell, type GridCellProps } from './grid-cell';

const instance = (renderer: string): GadgetInstance => ({
  id: 'inst-1', group_id: 'g', definition_id: 'd', title: 'Planned gadget', position: 0,
  definition: { id: 'd', name: 'Def', revision: 4, renderer, source_ids: ['s1'], scope: {}, filters: {}, highlight_rules: [], config_version: 1, runtime_state: 'available' },
} as GadgetInstance);

const noop = () => undefined;
const renderCell = (renderer: string) => render(
  <QueryClientProvider client={new QueryClient()}>
  <NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'], uiSweep: uiSweepMessages['en-us'], gadgetSettings: gadgetSettingsMessages['en-us'], sources: sourcesMessages['en-us'] }}>
    <GridCell
      {...({
        placement: { instance_id: 'inst-1', x: 0, y: 0, w: 4, h: 4 }, instance: instance(renderer), isEditMode: false, isSelected: false,
        gesture: null, overlapCount: 0, gestureNotice: '', projectionPending: false, readingQueueCount: 0,
        onKeyDown: noop, onSelect: noop, onDragStart: noop, onResizeStart: noop, onHeaderMeasured: noop, onConfigure: noop, onRemove: noop, onApplyReadingQueue: noop,
      } satisfies GridCellProps)}
    />
  </NextIntlClientProvider>
  </QueryClientProvider>,
);

beforeEach(() => { chat.openDrawer.mockReset(); sourceApi.listSources.mockReset(); sourceApi.listSources.mockResolvedValue({ items: [] }); });

describe('GridCell', () => {
  it('shows the planned body for a renderer that is not registered', async () => {
    renderCell('definitely_not_a_renderer');
    expect(await screen.findByText('1 sources linked')).toBeInTheDocument();
    expect(screen.getByText('rev.4')).toBeInTheDocument();
  });

  it('shows a failing badge when a bound active source has a newer error than success', async () => {
    sourceApi.listSources.mockResolvedValue({ items: [{ id: 's1', name: 'Src', status: 'active', last_success_at: '2020-01-01T00:00:00Z', last_error_at: '2020-01-02T00:00:00Z', last_error_code: 'provider_revoked', collection_error_code: null, next_due_at: null }] });
    renderCell('definitely_not_a_renderer');
    expect(await screen.findByText(dashboardMessages['en-us'].sourceFailingBadge)).toBeInTheDocument();
  });

  it('shows the unread count reported by the renderer on the frame badge', async () => {
    renderCell('reports_unread');
    expect(await screen.findByText(dashboardMessages['en-us'].unreadBadge.replace('{count}', '3'))).toBeInTheDocument();
  });
});
