import { describe, expect, it, vi } from 'vitest';
import { buildContentHandlers } from './use-chat-stream';
import type { ChatLiveActions } from './chat-live-state';

/** Live actions whose dispatch/patch are spies, so tests assert exactly which transitions a handler emits. */
function spyActions(): ChatLiveActions {
  return { dispatch: vi.fn(), patch: vi.fn(), clearOutput: vi.fn() };
}

describe('buildContentHandlers', () => {
  it('forwards stream content to the live state while the view is current', () => {
    const live = spyActions();
    const theme = vi.fn();
    const handlers = buildContentHandlers(live, () => true, theme);
    handlers.onDelta?.('hi');
    handlers.onReasoning?.('think');
    handlers.onCitations?.([]);
    handlers.onThemeRequest?.('dark');
    expect(live.dispatch).toHaveBeenCalledWith({ type: 'appendDelta', chunk: 'hi' });
    expect(live.dispatch).toHaveBeenCalledWith({ type: 'appendReasoning', chunk: 'think' });
    expect(live.patch).toHaveBeenCalledWith({ streamingCitations: [] });
    expect(theme).toHaveBeenCalledWith('dark');
  });

  it('drops every event once the originating view is no longer current', () => {
    const live = spyActions();
    const theme = vi.fn();
    const handlers = buildContentHandlers(live, () => false, theme);
    handlers.onDelta?.('hi');
    handlers.onUiDelta?.({ part_id: 'p' });
    handlers.onUiDiscard?.('p');
    handlers.onInputResolved?.({ request_id: 'r', outcome: 'answered' });
    handlers.onThemeRequest?.('light');
    expect(live.dispatch).not.toHaveBeenCalled();
    expect(live.patch).not.toHaveBeenCalled();
    expect(theme).not.toHaveBeenCalled();
  });

  it('parks a new input request as pending', () => {
    const live = spyActions();
    const handlers = buildContentHandlers(live, () => true, vi.fn());
    const request = { request_id: 'r1', kind: 'text' as const, prompt: 'p', options: [], slots: [], expires_at: '' };
    handlers.onInputRequest?.(request);
    expect(live.patch).toHaveBeenCalledWith({ inputRequest: { request, state: 'pending' } });
  });
});
