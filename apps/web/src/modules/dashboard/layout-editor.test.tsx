import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { gadgetSettingsMessages } from '@/core/messages/gadget-settings';
import { uiSweepMessages } from '@/core/messages/ui-sweep';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'c' }) }));
const api = vi.hoisted(() => ({ deleteGadgetDefinition: vi.fn(), listGadgetDefinitions: vi.fn() }));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));
// The editor is covered by its own test; here it only reports a freshly created definition.
vi.mock('./gadget-editor', () => ({
  GadgetEditor: ({ onSaved }: { onSaved: (definition: { id: string; renderer: string; revision: number }) => void }) =>
    <button type="button" onClick={() => onSaved({ id: 'new-def', renderer: 'news_feed', revision: 1 })}>save-new</button>,
}));

import { LayoutEditor } from './layout-editor';

const messages = { dashboard: dashboardMessages['en-us'], gadgetSettings: gadgetSettingsMessages['en-us'], uiSweep: uiSweepMessages['en-us'] };
const onAddGadgetInstance = vi.fn();
const onCancelPendingAdd = vi.fn();
/** Renders the toolbar with a reservation callback that returns the given id (or null for no space). */
const wrap = (reservation: string | null = 'r1') => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <NextIntlClientProvider locale="en-US" messages={messages}>
      <LayoutEditor dashboardName="Home" editCount={0} isDirty={false} isSaving={false} canUndo={false} canRedo={false} onSave={() => undefined} onCancel={() => undefined}
        onUndo={() => undefined} onRedo={() => undefined} onOpenPresetPicker={() => undefined} onAddGadgetInstance={onAddGadgetInstance}
        onBeginAddGadgetInstance={() => reservation} onCancelPendingAdd={onCancelPendingAdd} groups={[{ id: 'g1', name: 'Main' } as never]}
        dirtyModalOpen={false} onDirtyModalResolution={() => undefined} />
    </NextIntlClientProvider>
  </QueryClientProvider>);
/** Opens the Add dialog and triggers the mocked editor save. */
async function addNew() {
  await userEvent.click(screen.getByRole('button', { name: 'Add gadget' }));
  await userEvent.click(await screen.findByRole('button', { name: 'save-new' }));
}

beforeEach(() => {
  [...Object.values(api), onAddGadgetInstance, onCancelPendingAdd].forEach((mock) => mock.mockReset());
  api.listGadgetDefinitions.mockResolvedValue([]);
  api.deleteGadgetDefinition.mockResolvedValue(undefined);
});
afterEach(() => cleanup());

describe('LayoutEditor Add dialog', () => {
  it('deletes the just-created definition when the instance cannot be created', async () => {
    onAddGadgetInstance.mockRejectedValue(new Error('instance failed'));
    wrap();
    await addNew();
    await waitFor(() => expect(api.deleteGadgetDefinition).toHaveBeenCalledWith('new-def', 1, 'c'));
    expect(onCancelPendingAdd).toHaveBeenCalledWith('r1');
    expect((await screen.findByRole('alert')).textContent).toContain('Could not add the gadget');
  });

  it('compensates and keeps the dialog open when the handler resolves false', async () => {
    onAddGadgetInstance.mockResolvedValue(false);
    wrap();
    await addNew();
    await waitFor(() => expect(api.deleteGadgetDefinition).toHaveBeenCalledWith('new-def', 1, 'c'));
    expect((await screen.findByRole('alert')).textContent).toContain('Could not add the gadget');
    expect(screen.getByRole('button', { name: 'save-new' })).toBeTruthy();
    expect(onCancelPendingAdd).not.toHaveBeenCalled();
  });

  it('keeps the definition when the instance is created', async () => {
    onAddGadgetInstance.mockResolvedValue(true);
    wrap();
    await addNew();
    await waitFor(() => expect(onAddGadgetInstance).toHaveBeenCalledWith(expect.objectContaining({ definitionId: 'new-def', renderer: 'news_feed' }), 'r1'));
    expect(api.deleteGadgetDefinition).not.toHaveBeenCalled();
  });

  it('deletes the new definition when no layout space can be reserved', async () => {
    wrap(null);
    await addNew();
    await waitFor(() => expect(api.deleteGadgetDefinition).toHaveBeenCalledWith('new-def', 1, 'c'));
    expect(onAddGadgetInstance).not.toHaveBeenCalled();
  });

  it('shows a message when no layout space can be reserved', async () => {
    wrap(null);
    await addNew();
    expect((await screen.findByRole('alert')).textContent).toContain('Could not add the gadget');
  });

  it('clears a previous add error when the dialog is reopened', async () => {
    onAddGadgetInstance.mockRejectedValue(new Error('instance failed'));
    wrap();
    await addNew();
    expect(await screen.findByRole('alert')).toBeTruthy();
    await userEvent.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('button', { name: 'save-new' })).toBeNull());
    await userEvent.click(screen.getByRole('button', { name: 'Add gadget' }));
    await screen.findByRole('button', { name: 'save-new' });
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('add dialog has one name field', async () => {
    wrap();
    await userEvent.click(screen.getByRole('button', { name: 'Add gadget' }));
    await screen.findByRole('button', { name: 'save-new' });
    expect(screen.queryByLabelText(/instance title/i)).toBeNull();
    expect(screen.queryByRole('textbox')).toBeNull();
  });
});
