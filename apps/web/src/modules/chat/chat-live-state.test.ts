import { describe, expect, it } from 'vitest';
import { applyToolStep, chatLiveReducer, INITIAL_LIVE_STATE, type ChatLiveState } from './chat-live-state';
import { ChatSessionRuntime } from './chat-session-refs';

const base = (over: Partial<ChatLiveState> = {}): ChatLiveState => ({ ...INITIAL_LIVE_STATE, ...over });

describe('chatLiveReducer', () => {
  it('returns the same reference for a no-op patch so React bails out', () => {
    const state = base({ isPending: false });
    expect(chatLiveReducer(state, { type: 'patch', patch: { isPending: false, error: null } })).toBe(state);
    expect(chatLiveReducer(state, { type: 'patch', patch: { isPending: true } }).isPending).toBe(true);
  });

  it('a text delta appends and clears the running tool in one transition', () => {
    const next = chatLiveReducer(base({ streamingText: 'ab', activeTool: 'web_search' }), { type: 'appendDelta', chunk: 'c' });
    expect(next.streamingText).toBe('abc');
    expect(next.activeTool).toBeNull();
  });

  it('tool status sets on running and only clears the same tool', () => {
    const running = chatLiveReducer(base(), { type: 'toolStatus', tool: 'web_search', state: 'running' });
    expect(running.activeTool).toBe('web_search');
    expect(chatLiveReducer(running, { type: 'toolStatus', tool: 'query_collected_data', state: 'done' }).activeTool).toBe('web_search');
    expect(chatLiveReducer(running, { type: 'toolStatus', tool: 'web_search', state: 'done' }).activeTool).toBeNull();
  });

  it('clearOutput resets streamed output but keeps flags; clearPartial keeps web search and tool', () => {
    const dirty = base({ streamingText: 'x', streamingReasoning: 'r', streamingUi: [{ part_id: 'p' }], isStreaming: true, activeTool: 'web_search' });
    const cleared = chatLiveReducer(dirty, { type: 'clearOutput' });
    expect(cleared).toMatchObject({ streamingText: '', streamingReasoning: '', streamingUi: [], activeTool: null, isStreaming: true });
    expect(chatLiveReducer(dirty, { type: 'clearPartial' }).activeTool).toBe('web_search');
  });

  it('input resolution only affects the matching request and cancel only touches pending ones', () => {
    const request = { request_id: 'r1', kind: 'text' as const, prompt: 'p', options: [], slots: [], expires_at: '' };
    const pending = base({ inputRequest: { request, state: 'pending' } });
    expect(chatLiveReducer(pending, { type: 'inputResolved', resolved: { request_id: 'other', outcome: 'answered' } })).toBe(pending);
    expect(chatLiveReducer(pending, { type: 'inputResolved', resolved: { request_id: 'r1', outcome: 'expired' } }).inputRequest?.state).toBe('expired');
    expect(chatLiveReducer(pending, { type: 'cancelPendingInput' }).inputRequest?.state).toBe('cancelled');
    const answered = chatLiveReducer(pending, { type: 'inputResolved', resolved: { request_id: 'r1', outcome: 'answered', label: 'L' } });
    expect(chatLiveReducer(answered, { type: 'cancelPendingInput' })).toBe(answered);
  });
});

describe('tool steps', () => {
  it('appends running steps, completes the matching one and ignores replayed running events', () => {
    let steps = applyToolStep([], { tool: 'retrieval', state: 'running' });
    expect(applyToolStep(steps, { tool: 'retrieval', state: 'running' })).toBe(steps);
    steps = applyToolStep(steps, { tool: 'retrieval', state: 'done', detail: { documents: 2 } });
    expect(steps).toEqual([{ tool: 'retrieval', state: 'done', detail: { documents: 2 } }]);
    steps = applyToolStep(steps, { tool: 'read_skill', state: 'running', label: 'x' });
    steps = applyToolStep(steps, { tool: 'read_skill', state: 'done', label: 'x' });
    expect(steps.map((s) => s.state)).toEqual(['done', 'done']);
  });

  it('records a done step whose running event was never seen; clearPartial drops steps, clearOutput keeps them', () => {
    const state = chatLiveReducer(base(), { type: 'toolStep', step: { tool: 'web_search', state: 'done' } });
    expect(state.steps).toHaveLength(1);
    expect(chatLiveReducer(state, { type: 'clearOutput' }).steps).toHaveLength(1);
    expect(chatLiveReducer(state, { type: 'clearPartial' }).steps).toHaveLength(0);
  });
});

describe('ChatSessionRuntime', () => {
  it('detaches the stream and bumps generations through its methods', () => {
    const rt = new ChatSessionRuntime({ conversationId: null, generation: 0 });
    const ctrl = new AbortController();
    rt.setAbortController(ctrl);
    rt.detachStream();
    expect(ctrl.signal.aborted).toBe(true);
    expect(rt.abortController).toBeNull();
    expect(rt.bumpSendGeneration()).toBe(1);
    expect(rt.bumpSendGeneration()).toBe(2);
  });
});
