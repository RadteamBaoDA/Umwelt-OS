import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { uiSweepMessages } from '@/core/messages/ui-sweep';

const core = vi.hoisted(() => ({ apiRequest: vi.fn() }));
vi.mock('@/core/api', async (original) => ({ ...(await original<typeof import('@/core/api')>()), apiRequest: core.apiRequest }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 't' }) }));
vi.mock('@/modules/sources/api', async (original) => ({
  ...(await original<typeof import('@/modules/sources/api')>()),
  listSources: vi.fn(async () => ({ items: [], next_cursor: null })),
  getRun: vi.fn(async () => ({ status: 'succeeded', stages: [], error_code: null })),
}));

import { Upload } from './upload';

describe('Upload', () => {
  afterEach(() => { cleanup(); core.apiRequest.mockReset(); });

  it('submits without source_id so the server files it under the default Uploads source', async () => {
    core.apiRequest.mockResolvedValue({ run_id: 'r1' });
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <NextIntlClientProvider locale="en-US" messages={{ uiSweep: uiSweepMessages['en-us'] }}><Upload /></NextIntlClientProvider>
    </QueryClientProvider>);
    await userEvent.upload(screen.getByLabelText('File'), new File(['hi'], 'a.txt', { type: 'text/plain' }));
    // jsdom's required-file validity ignores userEvent's injected files, so dispatch submit directly.
    fireEvent.submit(screen.getByLabelText('File').closest('form') as HTMLFormElement);
    await waitFor(() => expect(core.apiRequest).toHaveBeenCalled());
    const [endpoint, init] = core.apiRequest.mock.calls[0];
    expect(endpoint).toBe('/api/v1/documents/upload');
    expect((init.body as FormData).has('source_id')).toBe(false);
  });
});
