import * as React from 'react';
import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PromptBridgeProvider, usePromptBridge } from '../bridge';
import { IMAGE_RATE_MAX } from '../bridge/prompt-bridge';
import { resolveMediaUrl } from '../bridge/image-loader';
import { CSP_META_TAG, SANDBOX_BRIDGE_JS } from '../sandbox/frame-content';

const apiRequest = vi.hoisted(() => vi.fn());
vi.mock('@/core/api', () => ({ apiRequest }));

const NONCE = 'n1';
const PROXY = `${window.location.origin}/api/v1/media/proxy?token=abc`;

let unregister: (() => void) | undefined;
const frame = { postMessage: vi.fn() };

/** Registers the fake frame window through the real bridge context. */
function Registrar() {
  const { registerFrame } = usePromptBridge();
  React.useEffect(() => {
    unregister = registerFrame(frame, NONCE);
    return unregister;
  }, [registerFrame]);
  return null;
}

/** Dispatches a frame->host load-image message. */
function ask(url: string, over: Record<string, unknown> = {}, id = 'i1') {
  window.dispatchEvent(
    new MessageEvent('message', {
      data: { type: 'umwelt:load-image', id, url, nonce: NONCE, ...over },
      source: frame as unknown as MessageEventSource,
      origin: 'null',
    }),
  );
}

/** Builds a fetch Response-like image reply. */
function imageResponse(type = 'image/png', size = 10) {
  return new Response(new Uint8Array(size), { status: 200, headers: { 'content-type': type } });
}

beforeEach(() => {
  frame.postMessage.mockReset();
  apiRequest.mockReset().mockResolvedValue({ privacy: { allow_widget_remote_media: true } });
  vi.stubGlobal('fetch', vi.fn(async () => imageResponse()));
  URL.createObjectURL = vi.fn(() => 'blob:never');
  render(
    <PromptBridgeProvider onSendPrompt={() => undefined}>
      <Registrar />
    </PromptBridgeProvider>,
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const lastReply = () => frame.postMessage.mock.calls.at(-1)?.[0] as Record<string, unknown> | undefined;

describe('sandbox image loader bridge', () => {
  it('replies with a data url (no blob) for a same-origin proxy url and fetches with credentials', async () => {
    ask(PROXY);
    await waitFor(() => expect(lastReply()?.type).toBe('umwelt:load-image-result'));
    expect(lastReply()).toMatchObject({ id: 'i1', dataUrl: 'data:image/png;base64,AAAAAAAAAAAAAA==', nonce: NONCE });
    expect(lastReply()).not.toHaveProperty('blobUrl');
    expect(URL.createObjectURL).not.toHaveBeenCalled();
    expect(fetch).toHaveBeenCalledWith(PROXY, expect.objectContaining({ credentials: 'same-origin' }));
  });

  it('accepts relative proxy and tile paths', async () => {
    ask('/api/v1/media/tile/3/4/5');
    await waitFor(() => expect(lastReply()?.type).toBe('umwelt:load-image-result'));
  });

  it('rejects cross-origin and non-media urls without fetching', async () => {
    for (const bad of [
      'https://evil.example/api/v1/media/proxy?token=abc',
      '//evil.example/api/v1/media/proxy?token=abc',
      '/api/v1/settings/ai',
      '/api/v1/media/proxy?token=a&x=1',
      '/api/v1/media/tile/3/4/5?x=1',
    ]) {
      frame.postMessage.mockClear();
      ask(bad);
      await waitFor(() => expect(lastReply()?.type).toBe('umwelt:load-image-error'));
    }
    expect(fetch).not.toHaveBeenCalled();
    expect(resolveMediaUrl(PROXY, window.location.origin)).toBe(PROXY);
  });

  it('ignores a wrong nonce', async () => {
    ask(PROXY, { nonce: 'nope' });
    await new Promise((r) => setTimeout(r, 20));
    expect(frame.postMessage).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it('rejects when remote media is disabled', async () => {
    apiRequest.mockResolvedValue({ privacy: { allow_widget_remote_media: false } });
    ask(PROXY);
    await waitFor(() => expect(lastReply()?.type).toBe('umwelt:load-image-error'));
    expect(fetch).not.toHaveBeenCalled();
  });

  it('rejects non-image and svg responses', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => imageResponse('text/html')));
    ask(PROXY);
    await waitFor(() => expect(lastReply()?.type).toBe('umwelt:load-image-error'));
    vi.stubGlobal('fetch', vi.fn(async () => imageResponse('image/svg+xml')));
    frame.postMessage.mockClear();
    ask(PROXY, {}, 'i2');
    await waitFor(() => expect(lastReply()?.type).toBe('umwelt:load-image-error'));
  });

  it('rejects oversize images', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => imageResponse('image/png', 5 * 1024 * 1024 + 1)));
    ask(PROXY);
    await waitFor(() => expect(lastReply()?.type).toBe('umwelt:load-image-error'));
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it('caches the privacy flag for 30s across requests, then re-reads it', async () => {
    const now = vi.spyOn(Date, 'now');
    now.mockReturnValue(1_000_000);
    for (let i = 0; i < 3; i++) ask(PROXY, {}, `c${i}`);
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));
    expect(apiRequest).toHaveBeenCalledTimes(1);
    now.mockReturnValue(1_000_000 + 31_000);
    ask(PROXY, {}, 'late');
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(4));
    expect(apiRequest).toHaveBeenCalledTimes(2);
    now.mockRestore();
  });

  it('rate limits per frame', async () => {
    for (let i = 0; i < IMAGE_RATE_MAX; i++) ask(PROXY, {}, `r${i}`);
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(IMAGE_RATE_MAX));
    frame.postMessage.mockClear();
    ask(PROXY, {}, 'over');
    await waitFor(() => expect(lastReply()).toMatchObject({ type: 'umwelt:load-image-error', id: 'over' }));
    expect(fetch).toHaveBeenCalledTimes(IMAGE_RATE_MAX);
  });
});

describe('frame content', () => {
  it('keeps the CSP unchanged and exposes loadImage', () => {
    expect(CSP_META_TAG).toContain("img-src data: blob:;");
    expect(CSP_META_TAG).toContain("connect-src 'none'");
    expect(CSP_META_TAG).toContain("default-src 'none'");
    expect(SANDBOX_BRIDGE_JS).toContain('window.loadImage');
    expect(SANDBOX_BRIDGE_JS).toContain('data.dataUrl');
    expect(SANDBOX_BRIDGE_JS).not.toContain("indexOf('blob:')");
    // The in-frame guard accepts raster data:image/* replies only (never svg).
    expect(SANDBOX_BRIDGE_JS).toContain('data:image');
    expect(SANDBOX_BRIDGE_JS).toContain('(?!svg)');
  });
});
