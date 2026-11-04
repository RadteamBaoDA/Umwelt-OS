import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { gadgetSettingsMessages } from '@/core/messages/gadget-settings';
import { uiSweepMessages } from '@/core/messages/ui-sweep';
import type { GadgetInstance } from './api';

const chat = vi.hoisted(() => ({ openDrawer: vi.fn() }));
vi.mock('@/core/app-shell/chat-controller', () => ({ useChatController: () => chat }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));

import { GridCell, type GridCellProps } from './grid-cell';

const instance = (renderer: string): GadgetInstance => ({
  id: 'inst-1', group_id: 'g', definition_id: 'd', title: 'Planned gadget', position: 0,
  definition: { id: 'd', name: 'Def', revision: 4, renderer, source_ids: ['s1'], scope: {}, filters: {}, highlight_rules: [], config_version: 1, runtime_state: 'available' },
} as GadgetInstance);

const noop = () => undefined;
const renderCell = (renderer: string) => render(
  <NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'], uiSweep: uiSweepMessages['en-us'], gadgetSettings: gadgetSettingsMessages['en-us'] }}>
    <GridCell
      {...({
        placement: { instance_id: 'inst-1', x: 0, y: 0, w: 4, h: 4 }, instance: instance(renderer), isEditMode: false, isSelected: false,
        gesture: null, overlapCount: 0, gestureNotice: '', projectionPending: false, readingQueueCount: 0,
        onKeyDown: noop, onSelect: noop, onDragStart: noop, onResizeStart: noop, onHeaderMeasured: noop, onConfigure: noop, onRemove: noop, onApplyReadingQueue: noop,
      } satisfies GridCellProps)}
    />
  </NextIntlClientProvider>,
);

beforeEach(() => { chat.openDrawer.mockReset(); });

describe('GridCell', () => {
  it('shows the planned body for a renderer that is not registered', async () => {
    renderCell('definitely_not_a_renderer');
    expect(await screen.findByText('1 sources linked')).toBeInTheDocument();
    expect(screen.getByText('rev.4')).toBeInTheDocument();
  });
});
