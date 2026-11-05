import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { gadgetSettingsMessages } from '@/core/messages/gadget-settings';
import { uiSweepMessages } from '@/core/messages/ui-sweep';
import type { GadgetInstance } from './api';

const chat = vi.hoisted(() => ({ openDrawer: vi.fn() }));
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
  <NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'], uiSweep: uiSweepMessages['en-us'], gadgetSettings: gadgetSettingsMessages['en-us'] }}>
    <GadgetFrame instance={instance()} isEditMode={false} {...props}>{children}</GadgetFrame>
  </NextIntlClientProvider>,
);

beforeEach(() => { chat.openDrawer.mockReset(); });

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
});
