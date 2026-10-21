import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sourcesMessages } from '@/core/messages/sources';
import type { Source } from './api';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 't' }) }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
const api = vi.hoisted(() => ({ archiveSource: vi.fn(), createManualSource: vi.fn() }));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));
// Hoisted test control: what the mocked editor reports after "run-verify".
const control = vi.hoisted(() => ({ verifyResult: { phase: 'succeeded' } as { phase: string; code?: string } }));
vi.mock('./connector-editor', async () => {
  const { useEffect, useState } = await import('react');
  return {
    ConnectorEditor: ({ step, actionsRef, onSnapshot, source }: { step: string; actionsRef: { current: unknown }; onSnapshot: (s: unknown) => void; source: Source | null }) => {
      const [sourceId, setSourceId] = useState(source?.id ?? '');
      const [verify, setVerify] = useState<{ phase: string; code?: string }>({ phase: 'idle' });
      useEffect(() => {
        actionsRef.current = { createSource: async () => { setSourceId('new1'); return 'new1'; }, prepareNext: async () => true, verify: async () => setVerify(control.verifyResult) };
      });
      useEffect(() => { onSnapshot({ sourceId, provider: 'rss', busy: false, providerOk: true, connectOk: true, chooseOk: true, verify }); }, [sourceId, verify, onSnapshot]);
      return <div data-testid="editor">{step}<button onClick={() => void (actionsRef.current as { verify: () => Promise<void> }).verify()}>run-verify</button>{verify.phase === 'failed' && <p role="alert">{verify.code}</p>}</div>;
    },
  };
});

import { SourceWizard } from './source-wizard';

const queryClient = () => new QueryClient({ defaultOptions: { queries: { retry: false } } });
const onClose = vi.fn();
const onChanged = vi.fn();
const renderWizard = (request: Parameters<typeof SourceWizard>[0]['request']) => render(
  <QueryClientProvider client={queryClient()}>
    <NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}>
      <SourceWizard request={request} onClose={onClose} onChanged={onChanged} registerTransitionGuard={() => undefined} />
    </NextIntlClientProvider>
  </QueryClientProvider>);
const editSource = { id: 's9', type: 'rss', name: 'Existing', status: 'active' } as Source;

beforeEach(() => { Object.values(api).forEach((mock) => mock.mockReset()); onClose.mockClear(); onChanged.mockClear(); control.verifyResult = { phase: 'succeeded' }; });
afterEach(() => cleanup());

describe('SourceWizard connector add flow', () => {
  it('creates the source on Next, keeps the current step marked, and Back returns to the previous step', async () => {
    renderWizard({ kind: 'connector' });
    expect(screen.getByRole('dialog')).toBeTruthy();
    expect(screen.getByTestId('editor').textContent).toContain('provider');
    expect(screen.getByRole('button', { name: 'Back' }).hasAttribute('disabled')).toBe(true);
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(screen.getByTestId('editor').textContent).toContain('connect'));
    const current = screen.getAllByRole('button').filter((node) => node.getAttribute('aria-current') === 'step');
    expect(current).toHaveLength(1);
    expect(current[0].textContent).toContain('Connect');
    await userEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(screen.getByTestId('editor').textContent).toContain('provider');
  });

  it('does not allow jumping ahead in add mode', async () => {
    renderWizard({ kind: 'connector' });
    expect(screen.getByRole('button', { name: /Save and test/ }).hasAttribute('disabled')).toBe(true);
  });

  it('archives the created source after confirming discard when it is unverified', async () => {
    api.archiveSource.mockResolvedValue(undefined);
    renderWizard({ kind: 'connector' });
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(screen.getByTestId('editor').textContent).toContain('connect'));
    await userEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(await screen.findByText('Discard the new source?')).toBeTruthy();
    expect(api.archiveSource).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Discard source' }));
    await waitFor(() => expect(api.archiveSource).toHaveBeenCalledWith('new1', 't'));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('closes a verified source without archiving; a failed verify keeps Done disabled and shows the error code', async () => {
    renderWizard({ kind: 'connector' });
    // provider -> connect -> choose -> collect -> verify
    for (let i = 0; i < 4; i += 1) {
      await userEvent.click(screen.getByRole('button', { name: 'Next' }));
      await waitFor(() => expect(screen.getByTestId('editor').textContent).toContain(['connect', 'choose', 'collect', 'verify'][i]));
    }
    control.verifyResult = { phase: 'failed', code: 'upstream_timeout' };
    await userEvent.click(screen.getByText('run-verify'));
    expect((await screen.findByRole('alert')).textContent).toContain('upstream_timeout');
    expect(screen.getByRole('button', { name: 'Done' }).hasAttribute('disabled')).toBe(true);
    control.verifyResult = { phase: 'succeeded' };
    await userEvent.click(screen.getByText('run-verify'));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Done' }).hasAttribute('disabled')).toBe(false));
    await userEvent.click(screen.getByRole('button', { name: 'Done' }));
    expect(onClose).toHaveBeenCalled();
    expect(api.archiveSource).not.toHaveBeenCalled();
  });
});

describe('SourceWizard edit flow', () => {
  it('lets the owner jump to any step and never archives on close', async () => {
    renderWizard({ kind: 'edit', source: editSource });
    expect(screen.getByTestId('editor').textContent).toContain('connect');
    await userEvent.click(screen.getByRole('button', { name: /Save and test/ }));
    expect(screen.getByTestId('editor').textContent).toContain('verify');
    await userEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(onClose).toHaveBeenCalled();
    expect(api.archiveSource).not.toHaveBeenCalled();
  });
});

describe('SourceWizard manual flow', () => {
  it('keeps Save disabled until a name is entered, then creates the source and closes', async () => {
    api.createManualSource.mockResolvedValue({ id: 'm1' });
    renderWizard({ kind: 'manual' });
    expect(screen.getByRole('button', { name: 'Save source' }).hasAttribute('disabled')).toBe(true);
    await userEvent.type(screen.getByLabelText('Source name'), 'Notes');
    await userEvent.click(screen.getByRole('button', { name: 'Save source' }));
    await waitFor(() => expect(api.createManualSource).toHaveBeenCalledWith('Notes', 't', { graph_enabled: true, rag_index_enabled: true }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('sends the switches the owner turned off', async () => {
    api.createManualSource.mockResolvedValue({ id: 'm1' });
    renderWizard({ kind: 'manual' });
    await userEvent.type(screen.getByLabelText('Source name'), 'Notes');
    await userEvent.click(screen.getByLabelText(/Build knowledge graph/));
    await userEvent.click(screen.getByLabelText(/Index for Q&A/));
    await userEvent.click(screen.getByRole('button', { name: 'Save source' }));
    await waitFor(() => expect(api.createManualSource).toHaveBeenCalledWith('Notes', 't', { graph_enabled: false, rag_index_enabled: false }));
  });
});
