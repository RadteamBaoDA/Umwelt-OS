import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider, useTranslations } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/core/api';
import { messages } from '@/core/messages';
import { OpenRouterModelBrowser, typeForModel, type BrowseOpenRouter, type OpenRouterModel } from './openrouter-model-browser';

const flux: OpenRouterModel = { id: 'black-forest-labs/flux-2', name: 'FLUX.2', created: 1, context_length: 8000, input_modalities: ['text'], output_modalities: ['image'], pricing_prompt: '0.000002', pricing_completion: '0.000004', supported_parameters: [] };
const ok = (models: OpenRouterModel[] = [], next: number | null = null) => ({ ok: true, error: null, models, total_count: models.length, next_offset: next });

function Browser({ browse, onPick = vi.fn(), excluded = new Set<string>() }: { browse: BrowseOpenRouter; onPick?: (m: OpenRouterModel, type: string) => void; excluded?: Set<string> }) {
  const t = useTranslations('aiSettings');
  return <OpenRouterModelBrowser providerId="or" browse={browse} excluded={excluded} disabled={false} onPick={onPick as never} t={t as never} />;
}
const renderBrowser = (props: Parameters<typeof Browser>[0]) => render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}><QueryClientProvider client={new QueryClient()}><Browser {...props} /></QueryClientProvider></NextIntlClientProvider>);
const slow = { timeout: 3000 };

describe('openrouter model browser', () => {
  it('requests the active tab, then the debounced search', async () => {
    const browse = vi.fn().mockResolvedValue(ok());
    renderBrowser({ browse });
    await waitFor(() => expect(browse).toHaveBeenLastCalledWith(expect.objectContaining({ output_modalities: 'all', sort: 'newest' }), 0, 50));
    await userEvent.click(screen.getByRole('tab', { name: 'Image' }));
    await waitFor(() => expect(browse).toHaveBeenLastCalledWith(expect.objectContaining({ output_modalities: 'image' }), 0, 50));
    const calls = browse.mock.calls.length;
    await userEvent.type(screen.getByRole('searchbox'), 'flux');
    expect(browse.mock.calls.length).toBe(calls); // typing alone does not fetch
    await waitFor(() => expect(browse).toHaveBeenLastCalledWith(expect.objectContaining({ output_modalities: 'image', q: 'flux' }), 0, 50), slow);
    expect(browse.mock.calls.length).toBe(calls + 1);
  });

  it('sends advanced filters', async () => {
    const browse = vi.fn().mockResolvedValue(ok());
    renderBrowser({ browse });
    await userEvent.click(screen.getByRole('button', { name: 'More filters' }));
    await userEvent.click(screen.getByRole('checkbox', { name: 'Zero data retention only' }));
    await userEvent.click(screen.getByRole('combobox', { name: 'Region' }));
    await userEvent.click(screen.getByRole('option', { name: 'EU' }));
    await userEvent.type(screen.getByLabelText('Max prompt price ($/M)'), '2');
    await waitFor(() => expect(browse).toHaveBeenLastCalledWith(expect.objectContaining({ zdr: true, region: 'eu', max_price: 2 }), 0, 50), slow);
  });

  it('pages with next_offset', async () => {
    const browse = vi.fn().mockResolvedValue(ok([flux], 50));
    renderBrowser({ browse });
    await screen.findByRole('button', { name: /FLUX\.2/ });
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(browse).toHaveBeenLastCalledWith(expect.anything(), 50, 50));
  });

  it('picks a model with the type from the active tab', async () => {
    const browse = vi.fn().mockResolvedValue(ok([flux]));
    const onPick = vi.fn();
    renderBrowser({ browse, onPick });
    await userEvent.click(screen.getByRole('tab', { name: 'Image' }));
    await userEvent.click(await screen.findByRole('button', { name: /FLUX\.2/ }));
    expect(onPick).toHaveBeenCalledWith(flux, 'image');
    expect(screen.getByText(/\$2\.00/)).toBeTruthy();
  });

  it('infers the type on the All tab and greys out excluded models', async () => {
    expect(typeForModel({ ...flux, output_modalities: ['text', 'embeddings'] })).toBe('embedding');
    renderBrowser({ browse: vi.fn().mockResolvedValue(ok([flux])), excluded: new Set([flux.id]) });
    expect(((await screen.findByRole('button', { name: /FLUX\.2/ })) as HTMLButtonElement).disabled).toBe(true);
  });

  it('shows friendly errors for gateway codes and rate limiting', async () => {
    const bad = vi.fn().mockResolvedValue({ ok: false, error: 'credential_required', models: [], total_count: 0, next_offset: null });
    const first = renderBrowser({ browse: bad });
    expect((await screen.findByRole('alert')).textContent).toMatch(/API key/);
    first.unmount();
    renderBrowser({ browse: vi.fn().mockRejectedValue(new ApiError(429, 'Too Many Requests')) });
    expect((await screen.findByRole('alert')).textContent).toMatch(/Too many searches/);
  });
});
