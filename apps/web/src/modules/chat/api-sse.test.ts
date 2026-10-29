import { afterEach, describe, expect, it, vi } from 'vitest';
import { streamResponseEvents } from './api';

/** Builds a fetch mock whose body streams the given raw SSE text in two arbitrary chunks. */
function mockSse(text: string) {
  const bytes = new TextEncoder().encode(text);
  const mid = Math.floor(bytes.length / 2);
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(bytes.slice(0, mid));
      controller.enqueue(bytes.slice(mid));
      controller.close();
    },
  });
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body, { status: 200 })));
}

afterEach(() => vi.unstubAllGlobals());

describe('streamResponseEvents generative UI events', () => {
  it('dispatches cumulative message.ui.delta snapshots, message.ui.done and done.generative_ui', async () => {
    const d1 = { kind: 'sandboxed_ui', status: 'streaming', sandbox: { html: '<div>A' } };
    const d2 = { kind: 'sandboxed_ui', status: 'streaming', sandbox: { html: '<div>AB</div>' } };
    const done = { kind: 'sandboxed_ui', status: 'completed', sandbox: { html: '<div>AB</div>' } };
    mockSse(
      [
        `event: message.ui.delta\ndata: ${JSON.stringify(d1)}\n\n`,
        `event: message.ui.delta\ndata: ${JSON.stringify(d2)}\n\n`,
        `event: message.ui.done\ndata: ${JSON.stringify(done)}\n\n`,
        `event: message.ui.delta\ndata: not-json\n\n`,
        `event: message.done\ndata: ${JSON.stringify({ text: 'ok', generative_ui: done })}\n\n`,
      ].join(''),
    );
    const onUiDelta = vi.fn();
    const onUiDone = vi.fn();
    const onDone = vi.fn();
    vi.spyOn(console, 'warn').mockImplementation(() => undefined);

    await streamResponseEvents('r1', { onUiDelta, onUiDone, onDone });

    // Each delta is the full snapshot (consumer replaces, never appends); malformed JSON is dropped.
    expect(onUiDelta.mock.calls.map((c) => c[0])).toEqual([d1, d2]);
    expect(onUiDone).toHaveBeenCalledWith(done);
    expect(onDone).toHaveBeenCalledWith(
      expect.objectContaining({ text: 'ok', generative_ui: done, status: 'completed' }),
    );
  });

  it('dispatches message.ui.discard with the part id and ignores redacted payloads', async () => {
    mockSse(`event: message.ui.discard
data: {"part_id":"r1-t1"}

event: message.ui.discard
data: {}

`);
    const onUiDiscard = vi.fn();
    await streamResponseEvents('r1', { onUiDiscard });
    expect(onUiDiscard.mock.calls).toEqual([['r1-t1']]);
  });

  it('forwards part_id on ui events and done.generative_ui_parts', async () => {
    const a = { kind: 'table', part_id: 'a', status: 'completed', table: { rows: [] } };
    const b = { kind: 'plan', part_id: 'b', status: 'streaming', plan: { steps: [] } };
    mockSse(
      [
        `event: message.ui.delta
data: ${JSON.stringify(b)}

`,
        `event: message.ui.done
data: ${JSON.stringify(a)}

`,
        `event: message.done
data: ${JSON.stringify({ text: 'ok', generative_ui: a, generative_ui_parts: [a, b] })}

`,
      ].join(''),
    );
    const onUiDelta = vi.fn();
    const onUiDone = vi.fn();
    const onDone = vi.fn();

    await streamResponseEvents('r2', { onUiDelta, onUiDone, onDone });

    expect(onUiDelta).toHaveBeenCalledWith(b);
    expect(onUiDone).toHaveBeenCalledWith(a);
    expect(onDone).toHaveBeenCalledWith(expect.objectContaining({ generative_ui_parts: [a, b] }));
  });
});

describe('streamResponseEvents reasoning events', () => {
  it('dispatches message.reasoning.delta text before the answer deltas and ignores malformed payloads', async () => {
    mockSse(
      [
        `event: message.reasoning.delta\ndata: ${JSON.stringify({ text: 'think ' })}\n\n`,
        `event: message.reasoning.delta\ndata: not-json\n\n`,
        `event: message.reasoning.delta\ndata: ${JSON.stringify({ text: 'more' })}\n\n`,
        `event: message.delta\ndata: ${JSON.stringify({ text: 'Answer' })}\n\n`,
      ].join(''),
    );
    const order: string[] = [];
    vi.spyOn(console, 'warn').mockImplementation(() => undefined);

    await streamResponseEvents('r3', {
      onReasoning: (text) => order.push(`r:${text}`),
      onDelta: (text) => order.push(`d:${text}`),
    });

    expect(order).toEqual(['r:think ', 'r:more', 'd:Answer']);
  });
});
