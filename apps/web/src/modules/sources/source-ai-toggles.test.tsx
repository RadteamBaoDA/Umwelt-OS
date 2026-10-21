import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { sourcesMessages } from '@/core/messages/sources';

const request = vi.hoisted(() => ({ apiRequest: vi.fn() }));
vi.mock('@/core/api', async (original) => ({ ...(await original<typeof import('@/core/api')>()), ...request }));

import { updateSourceAiOptions } from './api';
import { SourceAiToggles } from './source-ai-toggles';

afterEach(() => cleanup());

describe('SourceAiToggles', () => {
  it('reports each switch change and keeps the other value', async () => {
    const onChange = vi.fn();
    render(<NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}>
      <SourceAiToggles idPrefix="t" value={{ graph_enabled: true, rag_index_enabled: true }} onChange={onChange} />
    </NextIntlClientProvider>);
    await userEvent.click(screen.getByLabelText(/Index for Q&A/));
    expect(onChange).toHaveBeenCalledWith({ graph_enabled: true, rag_index_enabled: false });
  });
});

describe('updateSourceAiOptions', () => {
  it('patches only the supplied keys on the source endpoint', async () => {
    request.apiRequest.mockResolvedValue({});
    await updateSourceAiOptions('s1', { graph_enabled: false }, 'tok');
    const [url, init] = request.apiRequest.mock.calls[0] as [string, { method: string; body: string }];
    expect(url).toBe('/api/v1/sources/s1');
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(init.body)).toEqual({ graph_enabled: false });
  });
});
