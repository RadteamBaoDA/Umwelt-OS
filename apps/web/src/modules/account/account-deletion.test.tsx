import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { accountDeletionMessages } from '@/core/messages/account-deletion';
import { announceAccountErasure } from '@/core/account-erasure';
import { AccountDeletionSection } from './account-deletion';

vi.mock('@/core/account-erasure', () => ({ announceAccountErasure: vi.fn() }));
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks(); });

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><NextIntlClientProvider locale="en-US" messages={{ accountDeletion: accountDeletionMessages['en-us'] }}><AccountDeletionSection csrfToken="csrf" /></NextIntlClientProvider></QueryClientProvider>);
}

describe('AccountDeletionSection', () => {
  it('opens by keyboard with Cancel focused, traps focus and returns it on Escape without submitting', async () => {
    const user = userEvent.setup();
    const fetcher = vi.fn().mockResolvedValue(Response.json({ can_delete: true, blockers: [], workspace_count: 1, instance_operator: false }));
    vi.stubGlobal('fetch', fetcher);
    mount();
    const trigger = screen.getByRole('button', { name: 'Delete my account' });
    trigger.focus();
    await user.keyboard('{Enter}');
    const dialog = within(screen.getByRole('alertdialog'));
    await dialog.findByText('1 owned workspace will be erased.');
    const cancel = dialog.getByRole('button', { name: 'Cancel' });
    await waitFor(() => expect(cancel).toHaveFocus());
    await user.tab();
    expect(dialog.getByRole('button', { name: 'Retry' })).toHaveFocus();
    await user.tab({ shift: true });
    expect(cancel).toHaveFocus();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
    expect(fetcher.mock.calls.every(([, options]) => options?.method !== 'POST')).toBe(true);
  });

  it('cancels by keyboard and drops transient confirmation before reopening', async () => {
    const user = userEvent.setup();
    const fetcher = vi.fn().mockResolvedValue(Response.json({ can_delete: true, blockers: [], workspace_count: 1, instance_operator: false }));
    vi.stubGlobal('fetch', fetcher);
    mount();
    const trigger = screen.getByRole('button', { name: 'Delete my account' });
    await user.click(trigger);
    const dialog = within(screen.getByRole('alertdialog'));
    await dialog.findByText('1 owned workspace will be erased.');
    await user.type(dialog.getByLabelText('Current password'), 'not-persisted');
    await user.type(dialog.getByLabelText('Type DELETE to confirm'), 'DELETE');
    dialog.getByRole('button', { name: 'Cancel' }).focus();
    await user.keyboard('{Enter}');
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
    await user.keyboard('{Enter}');
    const reopened = within(screen.getByRole('alertdialog'));
    expect(reopened.getByLabelText('Current password')).toHaveValue('');
    expect(reopened.getByLabelText('Type DELETE to confirm')).toHaveValue('');
    expect(reopened.getByRole('button', { name: 'Delete my account' })).toBeDisabled();
    expect(fetcher.mock.calls.every(([, options]) => options?.method !== 'POST')).toBe(true);
  });

  it('shows workspace blockers and refuses submission even with a password and DELETE', async () => {
    const fetcher = vi.fn().mockResolvedValue(Response.json({ can_delete: false, blockers: ['workspace_has_members'], workspace_count: 1, instance_operator: true }));
    vi.stubGlobal('fetch', fetcher);
    mount();
    await userEvent.click(screen.getByRole('button', { name: 'Delete my account' }));
    const dialog = within(screen.getByRole('alertdialog'));
    expect(await dialog.findByText(/Remove the other members/)).toBeTruthy();
    await userEvent.type(dialog.getByLabelText('Current password'), 'password');
    await userEvent.type(dialog.getByLabelText('Type DELETE to confirm'), 'DELETE');
    expect(dialog.getByRole('button', { name: 'Delete my account' })).toBeDisabled();
    expect(fetcher.mock.calls.every(([, options]) => options?.method !== 'POST')).toBe(true);
  });

  it('requires exact typed confirmation and announces privacy cleanup only after acceptance', async () => {
    const fetcher = vi.fn().mockImplementation((path: string) => Promise.resolve(Response.json(path.endsWith('deletion-preview')
      ? { can_delete: true, blockers: [], workspace_count: 1, instance_operator: true }
      : { status: 'queued', phase: 'queued', error_code: null })));
    vi.stubGlobal('fetch', fetcher);
    mount();
    await userEvent.click(screen.getByRole('button', { name: 'Delete my account' }));
    const dialog = within(screen.getByRole('alertdialog'));
    await dialog.findByText('1 owned workspace will be erased.');
    const submit = dialog.getByRole('button', { name: 'Delete my account' });
    await userEvent.type(dialog.getByLabelText('Current password'), 'password');
    await userEvent.type(dialog.getByLabelText('Type DELETE to confirm'), 'delete');
    expect(submit).toBeDisabled();
    await userEvent.clear(dialog.getByLabelText('Type DELETE to confirm'));
    await userEvent.type(dialog.getByLabelText('Type DELETE to confirm'), 'DELETE');
    expect(submit).toBeEnabled();
    await userEvent.click(submit);
    await waitFor(() => expect(announceAccountErasure).toHaveBeenCalledOnce());
    expect(fetcher.mock.calls.find(([, options]) => options?.method === 'POST')?.[1].headers).toMatchObject({ 'X-CSRF-Token': 'csrf' });
    await waitFor(() => expect(dialog.getByLabelText('Current password')).toHaveValue(''));
  });
});
