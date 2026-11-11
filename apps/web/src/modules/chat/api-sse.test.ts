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
    expect(onDone).toHaveBeenCalledWith(expect.objectContaining({ text: 'ok', generative_ui: done, status: 'completed' }));
  });

  it('carries the uncited flag of message.done (Q2)', async () => {
    mockSse(`event: message.done
data: ${JSON.stringify({ text: 'ok', uncited: true })}

`);
    const onDone = vi.fn();
    await streamResponseEvents('r1', { onDone });
    expect(onDone).toHaveBeenCalledWith(expect.objectContaining({ uncited: true }));
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

  it('dispatches tool.status and ignores malformed or redacted payloads', async () => {
    mockSse(`event: tool.status
data: {"tool":"web_search","state":"running"}

event: tool.status
data: {}

event: tool.status
data: {"tool":"other","state":"running"}

event: tool.status
data: {"tool":"query_collected_data","state":"done"}

`);
    const onToolStatus = vi.fn();
    await streamResponseEvents('r1', { onToolStatus });
    expect(onToolStatus.mock.calls.map((c) => c[0])).toEqual([
      { tool: 'web_search', state: 'running' },
      { tool: 'query_collected_data', state: 'done' },
    ]);
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

describe('streamResponseEvents progress, title and suggestion events', () => {
  it('keeps onToolStatus for legacy tools only and sends every tool to onToolStep with label/detail', async () => {
    mockSse(
      [
        `event: tool.status\ndata: ${JSON.stringify({ tool: 'web_search', state: 'running' })}\n\n`,
        `event: tool.status\ndata: ${JSON.stringify({ tool: 'retrieval', state: 'done', detail: { documents: 3, bad: 'x' } })}\n\n`,
        `event: tool.status\ndata: ${JSON.stringify({ tool: 'read_skill', state: 'running', label: 'tables' })}\n\n`,
        `event: tool.status\ndata: {}\n\n`,
        `event: message.done\ndata: ${JSON.stringify({ text: 'ok' })}\n\n`,
      ].join(''),
    );
    const onToolStatus = vi.fn();
    const onToolStep = vi.fn();
    await streamResponseEvents('r1', { onToolStatus, onToolStep, onDone: vi.fn() });
    expect(onToolStatus.mock.calls.map((c) => c[0])).toEqual([{ tool: 'web_search', state: 'running' }]);
    expect(onToolStep.mock.calls.map((c) => c[0])).toEqual([
      { tool: 'web_search', state: 'running' },
      { tool: 'retrieval', state: 'done', detail: { documents: 3 } },
      { tool: 'read_skill', state: 'running', label: 'tables' },
    ]);
  });

  it('parses conversation.title and message.suggestions and ignores malformed payloads', async () => {
    mockSse(
      [
        `event: conversation.title\ndata: ${JSON.stringify({ title: 'Trip to Hue' })}\n\n`,
        `event: conversation.title\ndata: {}\n\n`,
        `event: message.suggestions\ndata: ${JSON.stringify({ message_id: 'm1', suggestions: ['a?', '', 7, 'b?'] })}\n\n`,
        `event: message.suggestions\ndata: ${JSON.stringify({ suggestions: ['a?'] })}\n\n`,
        `event: message.done\ndata: ${JSON.stringify({ text: 'ok' })}\n\n`,
      ].join(''),
    );
    const onConversationTitle = vi.fn();
    const onSuggestions = vi.fn();
    await streamResponseEvents('r1', { onConversationTitle, onSuggestions, onDone: vi.fn() });
    expect(onConversationTitle.mock.calls).toEqual([['Trip to Hue']]);
    expect(onSuggestions.mock.calls).toEqual([[{ message_id: 'm1', suggestions: ['a?', 'b?'] }]]);
  });
});
