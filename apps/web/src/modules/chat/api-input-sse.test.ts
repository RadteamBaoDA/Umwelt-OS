import { afterEach, describe, expect, it, vi } from 'vitest';
import { streamResponseEvents, submitResponseInput } from './api';

afterEach(() => vi.unstubAllGlobals());

/** Streams the given raw SSE text as one response body. */
function mockSse(text: string) {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(text));
      controller.close();
    },
  });
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body, { status: 200 })));
}

describe('user input SSE events', () => {
  it('dispatches message.input.request and message.input.resolved, ignoring redacted {} payloads', async () => {
    const request = {
      request_id: 'r1',
      kind: 'choice',
      prompt: 'Which?',
      options: [{ id: 'a', label: 'A' }],
      slots: [],
      expires_at: '2026-10-20T09:05:00+00:00',
    };
    mockSse(
      [
        `event: message.input.request\ndata: ${JSON.stringify(request)}\n\n`,
        `event: message.input.request\ndata: {}\n\n`,
        `event: message.input.resolved\ndata: ${JSON.stringify({ request_id: 'r1', outcome: 'expired' })}\n\n`,
      ].join(''),
    );
    const onInputRequest = vi.fn();
    const onInputResolved = vi.fn();
    await streamResponseEvents('run', { onInputRequest, onInputResolved });
    expect(onInputRequest).toHaveBeenCalledTimes(1);
    expect(onInputRequest).toHaveBeenCalledWith(request);
    expect(onInputResolved).toHaveBeenCalledWith({ request_id: 'r1', outcome: 'expired' });
  });

  it('posts the answer to the run input endpoint with the CSRF header', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ response_id: 'run', request_id: 'r1', status: 'streaming' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    await submitResponseInput('run', 'r1', 's1', 'csrf-token');
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe('/api/v1/responses/run/input');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ request_id: 'r1', answer: 's1' });
  });
});
