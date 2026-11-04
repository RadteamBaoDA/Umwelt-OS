import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sourcesMessages } from '@/core/messages/sources';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'c' }) }));
const api = vi.hoisted(() => ({ renameSource: vi.fn() }));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));

import { RenameSource, SOURCE_NAME_MAX } from './source-rename';

const source = { id: 's1', name: 'Old name', status: 'active' as const };
const onRenamed = vi.fn();
const view = (over = {}) => render(
  <QueryClientProvider client={new QueryClient()}>
    <NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}><RenameSource source={{ ...source, ...over }} onRenamed={onRenamed} /></NextIntlClientProvider>
  </QueryClientProvider>);
beforeEach(() => { api.renameSource.mockReset(); onRenamed.mockClear(); });
afterEach(() => cleanup());

describe('RenameSource', () => {
  it('saves the trimmed name with Enter and shows it', async () => {
    api.renameSource.mockResolvedValue({});
    view();
    await userEvent.click(screen.getByRole('button', { name: 'Rename Old name' }));
    const input = screen.getByLabelText('Source name');
    await userEvent.clear(input);
    await userEvent.type(input, '  New name {Enter}');
    await waitFor(() => expect(api.renameSource).toHaveBeenCalledWith('s1', 'New name', 'c'));
    await waitFor(() => expect(screen.getByText('New name')).toBeTruthy());
    expect(onRenamed).toHaveBeenCalled();
  });

  it('blocks an empty name', async () => {
    view();
    await userEvent.click(screen.getByRole('button', { name: 'Rename Old name' }));
    await userEvent.clear(screen.getByLabelText('Source name'));
    expect(screen.getByRole('button', { name: 'Save name' }).hasAttribute('disabled')).toBe(true);
    await userEvent.type(screen.getByLabelText('Source name'), '   {Enter}');
    expect(api.renameSource).not.toHaveBeenCalled();
  });

  it('caps the name at 200 characters', async () => {
    view();
    await userEvent.click(screen.getByRole('button', { name: 'Rename Old name' }));
    const input = screen.getByLabelText('Source name') as HTMLInputElement;
    expect(input.maxLength).toBe(SOURCE_NAME_MAX);
    await userEvent.clear(input);
    await userEvent.click(input);
    await userEvent.paste('x'.repeat(250));
    expect(input.value.length).toBe(200);
  });

  it('cancels with Escape and offers no rename for archived sources', async () => {
    view();
    await userEvent.click(screen.getByRole('button', { name: 'Rename Old name' }));
    await userEvent.type(screen.getByLabelText('Source name'), 'zzz{Escape}');
    expect(screen.getByText('Old name')).toBeTruthy();
    expect(api.renameSource).not.toHaveBeenCalled();
    cleanup();
    view({ status: 'archived' });
    expect(screen.queryByRole('button', { name: /Rename/ })).toBeNull();
  });
});
