import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { uiSweepMessages } from '@/core/messages/ui-sweep';
import type { Dashboard, DashboardPlacement } from './api';

const nav = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
vi.mock('next/navigation', () => ({
  useRouter: () => nav,
  usePathname: () => '/dashboard/d1',
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock('next/link', () => ({ default: ({ href, children }: { href: string; children: React.ReactNode }) => <a href={href}>{children}</a> }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ isOwner: true, isAdminViewer: false }) }));
vi.mock('@/core/guarded-navigation', () => ({
  useGuardedNavigation: () => ({ registerLeaveGuard: () => () => undefined, continueNavigation: vi.fn() }),
}));
vi.mock('@/modules/sources', () => ({ listSources: () => Promise.resolve({ items: [{ id: 's' }] }), sourceKeys: { list: ['sources', 'list'] } }));
vi.mock('@/modules/news', () => ({ TopicSettings: () => null }));
vi.mock('@/modules/settings', () => ({ HighlightRuleEditor: () => null }));
vi.mock('./today-header', () => ({ TodayHeader: () => null }));
vi.mock('./freshness-strip', () => ({ FreshnessStrip: () => null }));
vi.mock('./first-run-setup', () => ({ FirstRunSetup: () => <p>first-run</p> }));
vi.mock('./preset-picker', () => ({ PresetPicker: () => null }));
vi.mock('./gadget-settings', () => ({ GadgetSettings: () => null }));

const api = vi.hoisted(() => ({ listDashboards: vi.fn(), getDashboard: vi.fn(), replaceDashboardLayout: vi.fn() }));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));

/** Stub grid: exposes the placements it receives and lets the test move gadget `a` (layout change). */
vi.mock('./dashboard-grid', async (original) => ({
  ...(await original<typeof import('./dashboard-grid')>()),
  DashboardGrid: (props: { placements: DashboardPlacement[]; onLayoutChange: (next: DashboardPlacement[]) => void }) => (
    <div>
      <p data-testid="grid">{props.placements.map((p) => `${p.instance_id}@${p.x},${p.y}`).join('|')}</p>
      <button type="button" onClick={() => props.onLayoutChange(props.placements.map((p) => (p.instance_id === 'a' ? { ...p, x: 8 } : p)))}>move-a</button>
    </div>
  ),
}));
/** Stub editor: exposes dirty/undo/redo state and the save/undo/redo/cancel callbacks. */
vi.mock('./layout-editor', () => ({
  LayoutEditor: (props: {
    isDirty: boolean; canUndo: boolean; canRedo: boolean; dirtyModalOpen: boolean; saveError: string | null;
    onSave: () => void; onUndo: () => void; onRedo: () => void; onCancel: () => void;
    onDirtyModalResolution: (action: 'save' | 'discard' | 'stay') => void;
  }) => (
    <div>
      <p data-testid="editor">{`dirty=${props.isDirty} undo=${props.canUndo} redo=${props.canRedo} modal=${props.dirtyModalOpen} err=${props.saveError ?? ''}`}</p>
      <button type="button" onClick={props.onSave}>save</button>
      <button type="button" onClick={props.onUndo}>undo</button>
      <button type="button" onClick={props.onRedo}>redo</button>
      <button type="button" onClick={props.onCancel}>cancel</button>
      <button type="button" onClick={() => props.onDirtyModalResolution('discard')}>discard</button>
      <button type="button" onClick={() => props.onDirtyModalResolution('stay')}>stay</button>
    </div>
  ),
}));

import { DashboardPage } from './dashboard-page';

/** Builds a gadget instance of an unregistered-minimum renderer (4x3 minimum). */
const instance = (id: string) => ({ id, group_id: 'g', definition_id: `def-${id}`, title: null, position: 0, definition: { renderer: 'unknown_renderer', source_ids: [] } });
const rect = (id: string, x: number, y: number): DashboardPlacement => ({ instance_id: id, x, y, w: 4, h: 3 });
/** Builds a two-gadget dashboard fixture. */
const dashboard = (): Dashboard => ({
  id: 'd1', name: 'Overview', revision: 3,
  groups: [{ id: 'g', dashboard_id: 'd1', name: 'G', position: 0 }],
  instances: [instance('a'), instance('b')],
  layouts: {
    desktop: { columns: 20, items: [rect('a', 0, 0), rect('b', 4, 0)] },
    mobile: { columns: 8, items: [{ ...rect('a', 0, 0), w: 8 }, { ...rect('b', 0, 3), w: 8 }] },
  },
} as unknown as Dashboard);

/** Renders the page with providers. */
const renderPage = () => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'], uiSweep: uiSweepMessages['en-us'] }}>
      <DashboardPage dashboardId="d1" />
    </NextIntlClientProvider>
  </QueryClientProvider>,
);

beforeEach(() => {
  Object.values(api).forEach((mock) => mock.mockReset());
  nav.push.mockReset();
  nav.replace.mockReset();
  window.matchMedia = ((query: string) => ({
    matches: false, media: query, addEventListener: () => undefined, removeEventListener: () => undefined,
  })) as unknown as typeof window.matchMedia;
  api.listDashboards.mockResolvedValue([{ id: 'd1', name: 'Overview', revision: 3 }]);
  api.getDashboard.mockResolvedValue(dashboard());
});

describe('DashboardPage layout session', () => {
  it('renders the dashboard name and its desktop placements once loaded', async () => {
    renderPage();
    expect(await screen.findByTestId('grid')).toHaveTextContent('a@0,0|b@4,0');
    expect(screen.getByRole('button', { name: dashboardMessages['en-us'].switchDashboard })).toHaveTextContent('Overview');
    expect(document.querySelector('[data-dashboard-ready="true"]')).not.toBeNull();
  });

  it('tracks dirty state, undo and redo while editing', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('grid');
    await user.click(screen.getByRole('button', { name: dashboardMessages['en-us'].toggleEdit }));
    expect(screen.getByTestId('editor')).toHaveTextContent('dirty=false undo=false redo=false');
    await user.click(screen.getByText('move-a'));
    expect(screen.getByTestId('grid')).toHaveTextContent('a@8,0|b@4,0');
    expect(screen.getByTestId('editor')).toHaveTextContent('dirty=true undo=true redo=false');
    await user.click(screen.getByText('undo'));
    expect(screen.getByTestId('grid')).toHaveTextContent('a@0,0|b@4,0');
    expect(screen.getByTestId('editor')).toHaveTextContent('dirty=false undo=false redo=true');
    await user.click(screen.getByText('redo'));
    expect(screen.getByTestId('editor')).toHaveTextContent('dirty=true');
  });

  it('saves the captured desktop snapshot with the base revision and rebaselines on success', async () => {
    const user = userEvent.setup();
    const saved = dashboard();
    saved.revision = 4;
    saved.layouts.desktop.items = [rect('a', 8, 0), rect('b', 4, 0)];
    api.replaceDashboardLayout.mockResolvedValue(saved);
    renderPage();
    await screen.findByTestId('grid');
    await user.click(screen.getByRole('button', { name: dashboardMessages['en-us'].toggleEdit }));
    await user.click(screen.getByText('move-a'));
    await user.click(screen.getByText('save'));
    await waitFor(() => expect(api.replaceDashboardLayout).toHaveBeenCalledTimes(1));
    expect(api.replaceDashboardLayout).toHaveBeenCalledWith(
      'd1',
      { expected_revision: 3, breakpoint: 'desktop', columns: 20, items: [rect('a', 8, 0), rect('b', 4, 0)] },
      'csrf',
    );
    await waitFor(() => expect(screen.getByTestId('editor')).toHaveTextContent('dirty=false'));
  });

  it('keeps the draft and shows the error when saving fails', async () => {
    const user = userEvent.setup();
    api.replaceDashboardLayout.mockRejectedValue(new Error('conflict'));
    renderPage();
    await screen.findByTestId('grid');
    await user.click(screen.getByRole('button', { name: dashboardMessages['en-us'].toggleEdit }));
    await user.click(screen.getByText('move-a'));
    await user.click(screen.getByText('save'));
    await waitFor(() => expect(screen.getByTestId('editor')).toHaveTextContent('err=conflict'));
    expect(screen.getByTestId('editor')).toHaveTextContent('dirty=true');
    expect(screen.getByTestId('grid')).toHaveTextContent('a@8,0|b@4,0');
  });

  it('asks before leaving a dirty edit and discards back to the saved layout', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('grid');
    await user.click(screen.getByRole('button', { name: dashboardMessages['en-us'].toggleEdit }));
    await user.click(screen.getByText('move-a'));
    await user.click(screen.getByText('cancel'));
    expect(screen.getByTestId('editor')).toHaveTextContent('modal=true');
    await user.click(screen.getByText('stay'));
    expect(screen.getByTestId('editor')).toHaveTextContent('modal=false');
    await user.click(screen.getByText('cancel'));
    await user.click(screen.getByText('discard'));
    await waitFor(() => expect(screen.queryByTestId('editor')).toBeNull());
    expect(screen.getByTestId('grid')).toHaveTextContent('a@0,0|b@4,0');
  });

  it('leaves edit mode immediately when nothing changed', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('grid');
    await user.click(screen.getByRole('button', { name: dashboardMessages['en-us'].toggleEdit }));
    await user.click(screen.getByText('cancel'));
    expect(screen.queryByTestId('editor')).toBeNull();
  });
});
