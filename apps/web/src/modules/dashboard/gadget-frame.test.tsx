import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { gadgetSettingsMessages } from '@/core/messages/gadget-settings';
import { sourcesMessages } from '@/core/messages/sources';
import { uiSweepMessages } from '@/core/messages/ui-sweep';
import type { GadgetInstance } from './api';

const chat = vi.hoisted(() => ({ openDrawer: vi.fn() }));
const collect = vi.hoisted(() => ({ collectAndWait: vi.fn() }));
vi.mock('@/modules/sources/use-source-actions', () => ({ collectAndWait: collect.collectAndWait }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));
vi.mock('@/core/app-shell/chat-controller', () => ({ useChatController: () => chat }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));

import { GadgetFrame, type GadgetFrameProps } from './gadget-frame';
import { useReportGadgetFreshness } from './gadget-freshness';

const instance = (over: Partial<GadgetInstance> = {}): GadgetInstance => ({
  id: 'inst-1', group_id: 'g', definition_id: 'd', title: 'My gadget', position: 0,
  definition: { id: 'd', name: 'Def', revision: 3, renderer: 'text_panel', source_ids: ['s1'], scope: {}, filters: {}, highlight_rules: [], config_version: 1, runtime_state: 'available' },
  ...over,
} as GadgetInstance);

/** Child that reports freshness, as real gadgets do. */
function Reporter() {
  useReportGadgetFreshness('2026-01-01T00:00:00Z', 'attrib');
  return <p>body-content</p>;
}

const renderFrame = (props: Partial<GadgetFrameProps> = {}, children: React.ReactNode = <p>body-content</p>) => render(
  <QueryClientProvider client={new QueryClient()}>
    <NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'], uiSweep: uiSweepMessages['en-us'], gadgetSettings: gadgetSettingsMessages['en-us'], sources: sourcesMessages['en-us'] }}>
      <GadgetFrame instance={instance()} isEditMode={false} {...props}>{children}</GadgetFrame>
    </NextIntlClientProvider>
  </QueryClientProvider>,
);

beforeEach(() => { chat.openDrawer.mockReset(); collect.collectAndWait.mockReset(); });

const tick = (status: 'fresh' | 'stale' | 'failing', errorCode: string | null = null) => ({ id: 's1', name: 'Src', position: 50, status, clamped: false, lastSuccessMs: null, errorCode });

describe('GadgetFrame', () => {
  it('shows the instance title in a labelled region and portals the body into the inline host', async () => {
    renderFrame();
    expect(screen.getByRole('heading', { name: 'My gadget' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: /My gadget/ })).toBeInTheDocument();
    expect(await screen.findByText('body-content')).toBeInTheDocument();
  });

  it('falls back to the definition name and renders the planned body without children', async () => {
    renderFrame({ instance: instance({ title: null }) }, null);
    expect(screen.getByRole('heading', { name: 'Def' })).toBeInTheDocument();
    expect(await screen.findByText('1 sources linked')).toBeInTheDocument();
    expect(screen.getByText('rev.3')).toBeInTheDocument();
  });

  it('opens the quick-chat drawer with the gadget grounding context', async () => {
    const user = userEvent.setup();
    renderFrame();
    await user.click(screen.getByTestId('gadget-ask-with-umwelt'));
    expect(chat.openDrawer).toHaveBeenCalledWith({ context: { kind: 'gadget', gadgetId: 'inst-1', title: 'My gadget', renderer: 'text_panel', sourceIds: ['s1'], scope: {} } });
  });

  it('hides the ask button in edit mode and offers configure / remove in the menu', async () => {
    const user = userEvent.setup();
    const onConfigure = vi.fn();
    const onRemove = vi.fn();
    renderFrame({ isEditMode: true, onConfigure, onRemove });
    expect(screen.queryByTestId('gadget-ask-with-umwelt')).toBeNull();
    await user.click(screen.getByRole('button', { name: dashboardMessages['en-us'].configureGadget }));
    await user.click(await screen.findByRole('menuitem', { name: dashboardMessages['en-us'].removeGadget }));
    expect(onRemove).toHaveBeenCalledTimes(1);
    expect(onConfigure).not.toHaveBeenCalled();
  });

  it('expands into a dialog and keeps the same body instance', async () => {
    const user = userEvent.setup();
    renderFrame();
    await user.click(screen.getByRole('button', { name: dashboardMessages['en-us'].expand }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('My gadget');
    expect(dialog).toHaveTextContent('body-content');
  });

  it('shows the reading-queue banner and applies queued updates', async () => {
    const user = userEvent.setup();
    const onApply = vi.fn();
    renderFrame({ readingQueueCount: 3, onApplyReadingQueue: onApply });
    await user.click(screen.getByRole('button', { name: dashboardMessages['en-us'].showUpdates }));
    expect(onApply).toHaveBeenCalledTimes(1);
  });

  it('renders the provenance footer from renderer-reported freshness', async () => {
    renderFrame({}, <Reporter />);
    expect(await screen.findByTestId('gadget-provenance')).toHaveTextContent(/^attrib · /);
  });

  it('shows severity from the first highlight rule', () => {
    const base = instance();
    renderFrame({ instance: { ...base, definition: { ...base.definition, highlight_rules: [{ id: 'r', keywords: ['k'], severity: 'critical', notify: false }] } } });
    expect(screen.getByText(dashboardMessages['en-us'].severityCritical)).toBeInTheDocument();
  });

  it('shows no source badge while every bound source is fresh', () => {
    renderFrame({ sourceHealth: [tick('fresh')] });
    expect(screen.queryByText(dashboardMessages['en-us'].sourceStaleBadge)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Collect now' })).toBeNull();
  });

  it('shows a stale badge, and a failing badge with its plain-language reason, and collects on demand', async () => {
    const user = userEvent.setup();
    collect.collectAndWait.mockResolvedValue({ status: 'succeeded' });
    const view = renderFrame({ sourceHealth: [tick('stale')] });
    expect(screen.getByText(dashboardMessages['en-us'].sourceStaleBadge)).toBeInTheDocument();
    view.unmount();
    renderFrame({ sourceHealth: [tick('stale'), tick('failing', 'provider_rate_limited')] });
    const badge = screen.getByText(dashboardMessages['en-us'].sourceFailingBadge).closest('[title]');
    expect(badge?.getAttribute('title')).toBe(sourcesMessages['en-us'].collectError_provider_rate_limited);
    await user.click(screen.getByRole('button', { name: 'Collect now' }));
    expect(collect.collectAndWait).toHaveBeenCalledWith('s1', 'csrf', expect.objectContaining({ signal: expect.anything() }));
  });
});
