import * as React from 'react';
import type { Citation, InputRequest, ToolStatus, ToolStep, WebSearchOutcome } from './api';
import { markUiPartRebuilding, upsertUiPart, type UiPart } from './generative-ui-parts';
import type { InputRequestState } from './generative-ui/components/user-input-request';

/** The pending human-in-the-loop `request_user_input` of the active run plus its local lifecycle state. */
export interface PendingInputRequest {
  request: InputRequest;
  state: InputRequestState;
  label?: string;
}

/** Everything the chat surface renders while a send/stream is in flight (one reducer instead of a dozen useState). */
export interface ChatLiveState {
  streamingText: string;
  streamingReasoning: string;
  /** Neutral "generation stopped" outcome; kept apart from `error` so it never renders as a failure with Retry. */
  cancelled: boolean;
  /** Response id of the run the user stopped (set by Stop before `activeResponseId` clears); null otherwise. */
  stoppedResponseId: string | null;
  streamingCitations: Citation[];
  streamingWebSearch: WebSearchOutcome | null;
  /** Tool currently running for the active run (`tool.status`); drives the transcript status line. */
  activeTool: ToolStatus['tool'] | null;
  streamingUi: UiPart[];
  /** Processing steps of the active (or just settled) run, in arrival order; kept after `message.done` for the summary line. */
  steps: ToolStep[];
  inputRequest: PendingInputRequest | null;
  isStreaming: boolean;
  isPending: boolean;
  activeResponseId: string | null;
  error: string | null;
}

/** Initial (idle) live state. */
export const INITIAL_LIVE_STATE: ChatLiveState = {
  streamingText: '',
  streamingReasoning: '',
  cancelled: false,
  stoppedResponseId: null,
  streamingCitations: [],
  streamingWebSearch: null,
  activeTool: null,
  streamingUi: [],
  steps: [],
  inputRequest: null,
  isStreaming: false,
  isPending: false,
  activeResponseId: null,
  error: null,
};

/** Reducer actions; every action mirrors one of the setState calls it replaced so ordering stays identical. */
export type ChatLiveAction =
  | { type: 'patch'; patch: Partial<ChatLiveState> }
  | { type: 'appendReasoning'; chunk: string }
  | { type: 'appendDelta'; chunk: string }
  | { type: 'toolStatus'; tool: ToolStatus['tool']; state: string }
  | { type: 'toolStep'; step: ToolStep }
  | { type: 'webSearch'; outcome: WebSearchOutcome }
  | { type: 'uiUpsert'; ui: UiPart }
  | { type: 'uiDiscard'; partId: string }
  | { type: 'inputResolved'; resolved: { request_id: string; outcome: string; label?: string } }
  | { type: 'cancelPendingInput' }
  /** Resets text, reasoning, citations, web search, active tool and widget parts (steps survive until the next send). */
  | { type: 'clearOutput' }
  /** Resets only the replayable partial output (text, reasoning, citations, widget parts, steps). */
  | { type: 'clearPartial' };

/**
 * Folds one `tool.status` step into the step list. `running` appends (a duplicate running step of the same tool and
 * label is ignored, e.g. a replayed event); `done` completes the latest matching running step, or appends a finished
 * one when its `running` event was never seen.
 *
 * @param steps - Current steps.
 * @param step - Incoming step event.
 * @returns A new array, or the same reference when the event changes nothing.
 */
export function applyToolStep(steps: ToolStep[], step: ToolStep): ToolStep[] {
  if (step.state === 'running') {
    return steps.some((s) => s.state === 'running' && s.tool === step.tool && s.label === step.label) ? steps : [...steps, step];
  }
  for (let i = steps.length - 1; i >= 0; i -= 1) {
    const s = steps[i];
    // A missing label on either side still matches: only some events carry the subject.
    if (s.state === 'running' && s.tool === step.tool && (!s.label || !step.label || s.label === step.label)) {
      const next = [...steps];
      next[i] = { ...s, state: 'done', label: step.label ?? s.label, detail: step.detail ?? s.detail };
      return next;
    }
  }
  return [...steps, step];
}

/**
 * Applies a partial update, returning the same state object when nothing changed so React can bail out of re-rendering
 * exactly like the equivalent `useState` setter did.
 *
 * @param state - Current state.
 * @param patch - Fields to overwrite.
 * @returns The same reference when every field is already equal, otherwise a shallow copy with the patch applied.
 */
function applyPatch(state: ChatLiveState, patch: Partial<ChatLiveState>): ChatLiveState {
  const keys = Object.keys(patch) as (keyof ChatLiveState)[];
  if (keys.every((key) => Object.is(state[key], patch[key]))) return state;
  return { ...state, ...patch };
}

type ActionOf<K extends ChatLiveAction['type']> = Extract<ChatLiveAction, { type: K }>;
type Handlers = { [K in ChatLiveAction['type']]: (state: ChatLiveState, action: ActionOf<K>) => ChatLiveState };

/** Per-action transitions; a lookup table keeps the reducer flat (each handler is independently simple). */
const HANDLERS: Handlers = {
  patch: (state, action) => applyPatch(state, action.patch),
  appendReasoning: (state, action) => ({ ...state, streamingReasoning: state.streamingReasoning + action.chunk }),
  // A text delta means any tool finished, so the tool status line clears in the same render.
  appendDelta: (state, action) => ({ ...state, activeTool: null, streamingText: state.streamingText + action.chunk }),
  toolStatus: (state, action) => {
    const activeTool = action.state === 'running' ? action.tool : state.activeTool === action.tool ? null : state.activeTool;
    return applyPatch(state, { activeTool });
  },
  toolStep: (state, action) => {
    const steps = applyToolStep(state.steps, action.step);
    return steps === state.steps ? state : { ...state, steps };
  },
  webSearch: (state, action) => ({
    ...state,
    activeTool: state.activeTool === 'web_search' ? null : state.activeTool,
    streamingWebSearch: action.outcome,
  }),
  uiUpsert: (state, action) => ({ ...state, streamingUi: upsertUiPart(state.streamingUi, action.ui) }),
  uiDiscard: (state, action) => ({ ...state, streamingUi: markUiPartRebuilding(state.streamingUi, action.partId) }),
  inputResolved: (state, action) => {
    const prev = state.inputRequest;
    if (!prev || prev.request.request_id !== action.resolved.request_id) return state;
    return {
      ...state,
      inputRequest: { ...prev, state: action.resolved.outcome === 'expired' ? 'expired' : 'answered', label: action.resolved.label },
    };
  },
  // A still-pending request is shown as cancelled (an expired one keeps its own state).
  cancelPendingInput: (state) => {
    const prev = state.inputRequest;
    return prev && prev.state === 'pending' ? { ...state, inputRequest: { ...prev, state: 'cancelled' } } : state;
  },
  clearOutput: (state) => ({
    ...state,
    streamingText: '',
    streamingReasoning: '',
    streamingCitations: [],
    streamingWebSearch: null,
    activeTool: null,
    streamingUi: [],
  }),
  // A replay from the start re-sends the step events too, so partial resets drop the steps as well.
  clearPartial: (state) => ({ ...state, streamingText: '', streamingReasoning: '', streamingCitations: [], streamingUi: [], steps: [] }),
};

/**
 * Pure reducer for {@link ChatLiveState}.
 *
 * @param state - Current live state.
 * @param action - Transition to apply.
 * @returns Next state (same reference when the action is a no-op).
 */
export function chatLiveReducer(state: ChatLiveState, action: ChatLiveAction): ChatLiveState {
  return (HANDLERS[action.type] as (s: ChatLiveState, a: ChatLiveAction) => ChatLiveState)(state, action);
}

/** Stable dispatch-backed commands over the live state, shared by the stream, send and selection hooks. */
export interface ChatLiveActions {
  /** Overwrites the given fields (no-op re-render when nothing changes). */
  patch: (patch: Partial<ChatLiveState>) => void;
  dispatch: React.Dispatch<ChatLiveAction>;
  /** Resets the streamed output (text, reasoning, citations, web search, tool, widgets). */
  clearOutput: () => void;
}

/**
 * Owns the chat surface's live (per-send) state.
 *
 * @returns The current state and referentially stable actions (safe in hook dependency lists).
 */
export function useChatLiveState(): { live: ChatLiveState; actions: ChatLiveActions } {
  const [live, dispatch] = React.useReducer(chatLiveReducer, INITIAL_LIVE_STATE);
  const actions = React.useMemo<ChatLiveActions>(
    () => ({
      dispatch,
      patch: (patch) => dispatch({ type: 'patch', patch }),
      clearOutput: () => dispatch({ type: 'clearOutput' }),
    }),
    [],
  );
  return { live, actions };
}
