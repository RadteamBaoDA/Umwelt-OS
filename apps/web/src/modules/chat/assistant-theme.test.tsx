import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import type { OwnerPreferences } from '@/core/preferences';
import { AssistantThemeBanner } from './assistant-theme-banner';
import { streamResponseEvents } from './api';

const setPreview = vi.fn();
const confirmPreferences = vi.fn();
const confirmed = { theme: 'light', locale: 'en-us', timezone: 'UTC', configuration_revision: 7, persisted: true } as OwnerPreferences;

vi.mock('@/core/query-provider', () => ({
  useDisplayPreferences: () => ({
    confirmedPreferences: confirmed, confirmPreferences, setPreview, authGeneration: 3, locale: 'en-us', timezone: 'UTC',
  }),
}));

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

/** Renders the banner with English messages and a query client. */
function renderBanner(onResolved = vi.fn()) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <AssistantThemeBanner mode="dark" csrfToken="tok" onResolved={onResolved} />
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
  return onResolved;
}

describe('AssistantThemeBanner', () => {
  it('previews the requested mode transiently without saving, and Revert clears the preview', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const onResolved = renderBanner();
    expect(screen.getByText('Theme changed by assistant.')).toBeInTheDocument();
    expect(setPreview).toHaveBeenCalledWith({ theme: 'dark', locale: 'en-us', timezone: 'UTC' }, 3);
    expect(fetchMock).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Revert' }));
    expect(setPreview).toHaveBeenLastCalledWith(null, 3);
    expect(onResolved).toHaveBeenCalledTimes(1);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('Keep saves through the preference endpoint with the confirmed revision', async () => {
    const saved = { ...confirmed, theme: 'dark', configuration_revision: 8 };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(saved), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    const onResolved = renderBanner();
    await userEvent.click(screen.getByRole('button', { name: 'Keep' }));
    await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/v1/settings/preferences');
    expect(init.method).toBe('PUT');
    expect(JSON.parse(init.body)).toEqual({ expected_revision: 7, theme: 'dark', locale: 'en-us', timezone: 'UTC' });
    expect(confirmPreferences).toHaveBeenCalledWith(saved, 3);
  });

  it('keeps the preview and shows an error when Keep fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'conflict' }), { status: 409 })));
    const onResolved = renderBanner();
    await userEvent.click(screen.getByRole('button', { name: 'Keep' }));
    expect(await screen.findByText(/Could not save the theme/)).toBeInTheDocument();
    expect(onResolved).not.toHaveBeenCalled();
  });
});

describe('ui.theme SSE event', () => {
  it('dispatches known modes only and ignores invalid or redacted payloads', async () => {
    const text = [
      'event: ui.theme\ndata: {"mode":"dark"}\n\n',
      'event: ui.theme\ndata: {"mode":"neon"}\n\n',
      'event: ui.theme\ndata: {}\n\n',
    ].join('');
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(text));
        controller.close();
      },
    });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body, { status: 200 })));
    const onThemeRequest = vi.fn();
    await streamResponseEvents('run', { onThemeRequest });
    expect(onThemeRequest).toHaveBeenCalledTimes(1);
    expect(onThemeRequest).toHaveBeenCalledWith('dark');
  });
});
