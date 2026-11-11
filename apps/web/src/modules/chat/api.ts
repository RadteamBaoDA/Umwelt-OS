import { apiRequest, csrfHeaders, workspaceHeaders, workspaceTargetFor } from '@/core/api';
import type { ApiResponse } from '@/core/api-contract';

/**
 * Citation evidence reference pointing to an exact grounded revision chunk.
 */
export interface DocumentCitation {
  /** Server dumps this as `source_type`, so it is usually absent after parsing; discriminate with `=== 'web'`, never `=== 'document'`. */
  sourceType?: 'document';
  sourceId: string;
  documentId: string;
  documentVersionId: string;
  chunkId: string;
  title: string;
  url: string | null;
  observedAt: string | null;
  quote: string;
}

/**
 * Public-web citation built by the server from a cited search result (P15 contract).
 * Title and quote are untrusted third-party text: render as plain text only.
 */
export interface WebCitation {
  sourceType: 'web';
  url: string;
  title: string;
  quote: string;
  provider?: string;
  retrievedAt?: string;
}

/** Citation shown with an answer; discriminated by `sourceType`. */
export type Citation = DocumentCitation | WebCitation;

/**
 * Gates the web_search toggle and request flag.
 * Enabled with W2 (worker search + outcome); set false to hide the toggle without a backend change.
 */
export const WEB_SEARCH_SEND_ENABLED = true;

/** Reason codes the backend may report for a skipped or unavailable web search. */
export type WebSearchReason =
  | 'not_configured'
  | 'query_too_long'
  | 'empty_query'
  | 'local_only_context'
  | 'daily_limit'
  | 'timeout'
  | 'provider_error'
  | 'network_denied'
  | 'run_inactive'
  | 'no_results';

/** Outcome of a requested web search; absent when the message did not opt in. */
export interface WebSearchOutcome {
  status: 'used' | 'unavailable' | 'skipped';
  reason?: WebSearchReason | string | null;
  result_count?: number;
  /** The query actually sent to the search provider (may be an LLM rewrite of the prompt). */
  query?: string | null;
  /** Provider that answered (`omniroute|searxng|tavily|brave`); absent on older rows. */
  provider?: string | null;
  /** True when the answering provider was not the first configured target. */
  fallback?: boolean;
}

/** Progress of a server-side tool (`tool.status` SSE event). */
export interface ToolStatus {
  tool: 'web_search' | 'query_collected_data';
  state: 'running' | 'done';
}

/**
 * One step of the generic progress list (`tool.status` for every tool and the retrieval phase). The optional
 * fields are additive: `label` is a short subject such as a skill name, `detail` carries counts only
 * (retrieval `done`: `{ documents }`). `ToolStatus`/`onToolStatus` keep receiving only the two legacy tools.
 */
export interface ToolStep {
  tool: string;
  state: 'running' | 'done';
  label?: string;
  detail?: Record<string, number>;
}

/** `message.suggestions` SSE payload: follow-up questions for one assistant message (also stored on the message). */
export interface MessageSuggestions {
  message_id: string;
  suggestions: string[];
}

/**
 * Bounded client context linking a chat session to specific resources or filters.
 */
export interface ChatContext {
  kind: 'general' | 'document' | 'entity' | 'day' | 'selection' | 'gadget';
  resource_id?: string;
  date?: string;
  timezone?: string;
  gadgetId?: string;
  title?: string;
  renderer?: string;
  sourceIds?: string[];
  scope?: Record<string, unknown>;
  items?: Array<{
    sourceId: string;
    documentId: string;
    documentVersionId?: string;
    chunkId?: string;
    /** Client-only chip label; stripped before sending (the server forbids extra fields). */
    title?: string;
  }>;
  [key: string]: unknown;
}

/**
 * Public summary of a chat conversation thread.
 */
export type Conversation = Required<ApiResponse<'/api/v1/conversations'>[number]>;

/**
 * Public representation of an individual message within a conversation.
 */
export interface ChatMessage {
  id: string;
  conversation_id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  client_request_id: string | null;
  model_identity: string | null;
  citations: Citation[];
  /** Web search outcome for this answer; sent by the backend only for opted-in runs. */
  web_search?: WebSearchOutcome | null;
  /** Structured or sandboxed Generative UI payload attached to this assistant message. */
  generative_ui?: Record<string, unknown> | null;
  /** Every Generative UI widget of this message in render order; `generative_ui` is the first one. */
  generative_ui_parts?: Record<string, unknown>[];
  /** Model "thinking" text persisted with the answer; plain text only, null/absent when none was produced. */
  reasoning?: string | null;
  /** True when the answer has no verified document or web citation (shown as a small "not based on your documents" label). */
  uncited?: boolean;
  /** Follow-up question suggestions written after the answer settled; absent or empty when none. */
  suggestions?: string[];
  response_id: string | null;
  /** Message whose prompt or assistant response this append-only revision follows. */
  revision_of_message_id: string | null;
  created_at: string;
}

/**
 * Full conversation projection containing metadata and chronologically ordered messages.
 */
export interface ConversationDetail extends Conversation {
  messages: ChatMessage[];
  /** Owner-readable active response handle to reattach to its existing replayable SSE stream. */
  active_response_id: string | null;
}

/**
 * Payload for creating a new chat conversation.
 */
export interface ConversationCreate {
  title?: string;
  context_kind?: string;
  context_resource_id?: string;
  metadata?: Record<string, unknown>;
}

/**
 * Payload for updating conversation attributes.
 */
export interface ConversationPatch {
  title?: string;
  pinned?: boolean;
  archived?: boolean;
  metadata?: Record<string, unknown>;
}

/**
 * Payload for sending a user message within an existing conversation.
 */
export interface SendMessagePayload {
  content: string;
  client_request_id?: string;
  context?: ChatContext | Record<string, unknown>;
  /** Per-message opt-in; omit (never send false-by-default state) unless the user ticked it. */
  web_search?: boolean;
  /** Per-message opt-in for structured/SQL query over collected PostgreSQL datasources. */
  /** Optional per-message chat model override; both are sent together or not at all. */
  provider_id?: string;
  model?: string;
}

/**
 * Response acknowledging user message dispatch and response run scheduling.
 */
export interface SendMessageResponse {
  message_id: string;
  response_id: string;
  status: string;
}

/** Immutable idempotency envelope for one append-only message mutation. */
export interface MessageMutationPayload {
  action: 'edit' | 'regenerate';
  base_content_hash: string;
  client_request_id: string;
  content?: string;
  /** Per-mutation opt-in; not inherited from the original message. */
  web_search?: boolean;
  /** Per-mutation opt-in for structured/SQL query over collected PostgreSQL datasources. */
  /** Optional model choice for the new answer; both fields are sent together or not at all. */
  provider_id?: string;
  model?: string;
}

/**
 * Response acknowledging run cancellation.
 */
export interface CancelResponse {
  response_id: string;
  status: string;
}

/**
 * Streaming event callback options for Server-Sent Events (SSE) response consumption.
 */
/** Pending `request_user_input` published as the `message.input.request` SSE event (no answer/secret fields). */
export interface InputRequest {
  request_id: string;
  kind: 'time_slots' | 'choice' | 'text';
  prompt: string;
  options: Array<{ id: string; label: string }>;
  slots: Array<{
    id: string;
    label: string;
    date: string;
    start_time: string;
    end_time: string;
    timezone?: string;
    available?: boolean;
    recommended?: boolean;
  }>;
  expires_at: string;
}

/** Payload of the `message.input.resolved` SSE event. */
export interface InputResolved {
  request_id: string;
  outcome: 'answered' | 'declined' | 'expired';
  label?: string;
}

export interface StreamEventsOptions {
  lastEventId?: string;
  signal?: AbortSignal;
  onDelta?: (text: string) => void;
  /** Plain-text "thinking" chunk (`message.reasoning.delta`); arrives before the answer deltas. */
  onReasoning?: (text: string) => void;
  onCitations?: (citations: Citation[]) => void;
  onWebSearch?: (outcome: WebSearchOutcome) => void;
  onToolStatus?: (status: ToolStatus) => void;
  /** Every `tool.status` event (any tool, retrieval phase) with optional label/detail, for a step list. */
  onToolStep?: (step: ToolStep) => void;
  /** `conversation.title`: the server auto-titled the conversation (best effort, may arrive after the stream closed). */
  onConversationTitle?: (title: string) => void;
  /** `message.suggestions`: follow-up questions for a message (best effort, same timing as the title). */
  onSuggestions?: (payload: MessageSuggestions) => void;
  onUiDelta?: (ui: Record<string, unknown>) => void;
  onUiDone?: (ui: Record<string, unknown>) => void;
  /** The server rejected a streamed widget (`message.ui.discard`); drop its partial snapshots. */
  onUiDiscard?: (partId: string) => void;
  onStatus?: (status: string, payload?: unknown) => void;
  /** A run parked on `request_user_input`; also replayed on reconnect. */
  onInputRequest?: (request: InputRequest) => void;
  /** The request was answered, declined or expired. */
  onInputResolved?: (resolved: InputResolved) => void;
  /** The assistant asked for a theme through `set_theme` (`ui.theme` event); applied transiently by the UI. */
  onThemeRequest?: (mode: 'light' | 'dark' | 'system') => void;
  onDone?: (result: {
    text: string;
    citations: Citation[];
    model?: string;
    status: string;
    generative_ui?: Record<string, unknown> | null;
    generative_ui_parts?: Record<string, unknown>[];
    /** Final answer carries no verified citation (`message.done.uncited`). */
    uncited?: boolean;
  }) => void;
  onError?: (error: Error) => void;
}

/**
 * TanStack Query key factory for chat conversations and details.
 */
export const chatKeys = {
  all: ['chat'] as const,
  conversations: () => ['chat', 'conversations'] as const,
  conversation: (id: string) => ['chat', 'conversations', id] as const,
};

/**
 * Lists persistent owner conversations ordered by most recent update.
 *
 * @param params - Optional pagination, archive and title/content search (`q`) filters plus an abort signal.
 * @returns Array of conversation header records, pinned first.
 */
export async function listConversations(params?: {
  limit?: number;
  offset?: number;
  archived?: boolean;
  q?: string;
  /** Aborts the request (TanStack Query passes its signal so stale searches are cancelled). */
  signal?: AbortSignal;
}): Promise<Conversation[]> {
  const query = new URLSearchParams();
  if (params?.limit !== undefined) query.set('limit', String(params.limit));
  if (params?.offset !== undefined) query.set('offset', String(params.offset));
  if (params?.archived !== undefined) query.set('archived', String(params.archived));
  if (params?.q?.trim()) query.set('q', params.q.trim().slice(0, 200));
  const queryString = query.toString();
  const path = `/api/v1/conversations${queryString ? `?${queryString}` : ''}`;
  return apiRequest<Conversation[]>(path, params?.signal ? { signal: params.signal } : {});
}

/**
 * Retrieves full conversation details including all ordered messages.
 *
 * @param conversationId - Unique identifier of the conversation.
 * @returns Complete conversation detail record.
 */
export async function getConversation(conversationId: string): Promise<ConversationDetail> {
  return apiRequest<ConversationDetail>(`/api/v1/conversations/${conversationId}`);
}

/**
 * Creates a new chat conversation with optional context linking.
 *
 * @param payload - Conversation initialization parameters.
 * @param csrfToken - Session CSRF token for write authorization.
 * @returns Newly created conversation header.
 */
export async function createConversation(payload: ConversationCreate, csrfToken: string): Promise<Conversation> {
  return apiRequest<Conversation>('/api/v1/conversations', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...csrfHeaders(csrfToken),
    },
    body: JSON.stringify(payload),
  });
}

/**
 * Updates conversation title, pinned status, archived state, or metadata.
 *
 * @param conversationId - Unique identifier of the conversation.
 * @param payload - Partial patch fields.
 * @param csrfToken - Session CSRF token for write authorization.
 * @returns Updated conversation header.
 */
export async function patchConversation(conversationId: string, payload: ConversationPatch, csrfToken: string): Promise<Conversation> {
  return apiRequest<Conversation>(`/api/v1/conversations/${conversationId}`, {
    method: 'PATCH',
    headers: {
      'Content-Type': 'application/json',
      ...csrfHeaders(csrfToken),
    },
    body: JSON.stringify(payload),
  });
}

/**
 * Permanently deletes a conversation along with its messages and response runs.
 *
 * @param conversationId - Unique identifier of the conversation to delete.
 * @param csrfToken - Session CSRF token for write authorization.
 */
export async function deleteConversation(conversationId: string, csrfToken: string): Promise<void> {
  return apiRequest<void>(`/api/v1/conversations/${conversationId}`, {
    method: 'DELETE',
    headers: csrfHeaders(csrfToken),
  });
}

/**
 * Dispatches a user message into a conversation and triggers background response generation.
 * Enforces client_request_id idempotency to prevent duplicated generation on retry.
 *
 * @param conversationId - Target conversation identifier.
 * @param payload - Message content and optional client request ID / context.
 * @param csrfToken - Session CSRF token for write authorization.
 * @returns Immediate acknowledgment containing message_id and response_id.
 */
export async function sendMessage(conversationId: string, payload: SendMessagePayload, csrfToken: string): Promise<SendMessageResponse> {
  return apiRequest<SendMessageResponse>(`/api/v1/conversations/${conversationId}/messages`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...csrfHeaders(csrfToken),
    },
    body: JSON.stringify(payload),
  });
}

/**
 * Appends an edited prompt or regenerated assistant answer without replacing transcript history.
 * The server binds the operation to its original captured context and rejects request-key reuse
 * when the payload digest differs.
 *
 * @param conversationId - Conversation whose history receives the new branch.
 * @param messageId - Existing user prompt (edit) or assistant answer (regenerate).
 * @param payload - Immutable content hash and request identity for safe retries.
 * @param csrfToken - Session CSRF token for owner-write authorization.
 * @returns Durable message and response-run acknowledgement.
 */
export async function mutateMessage(
  conversationId: string,
  messageId: string,
  payload: MessageMutationPayload,
  csrfToken: string,
): Promise<SendMessageResponse> {
  return apiRequest<SendMessageResponse>(`/api/v1/conversations/${conversationId}/messages/${messageId}/mutations`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...csrfHeaders(csrfToken),
    },
    body: JSON.stringify(payload),
  });
}

/**
 * Hashes exact UTF-8 message bytes for optimistic concurrency checks on edit/regenerate actions.
 *
 * @param content - Persisted message content shown to the user.
 * @returns Lowercase SHA-256 digest accepted by the mutation API.
 * @throws Error when secure browser hashing is unavailable.
 */
export async function hashMessageContent(content: string): Promise<string> {
  if (!globalThis.crypto?.subtle) throw new Error('Secure message hashing is unavailable');
  const digest = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(content));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

/**
 * Requests immediate cancellation of an active response run.
 *
 * @param responseId - Unique identifier of the response run.
 * @param csrfToken - Session CSRF token for write authorization.
 * @returns Cancel acknowledgment with updated status.
 */
export async function cancelResponse(responseId: string, csrfToken: string): Promise<CancelResponse> {
  return apiRequest<CancelResponse>(`/api/v1/responses/${responseId}/cancel`, {
    method: 'POST',
    headers: csrfHeaders(csrfToken),
  });
}

/**
 * Answers the pending user-input request of a parked run so the SAME run resumes (no new message/run).
 *
 * @param responseId - Parked response run.
 * @param requestId - Id from the `message.input.request` event.
 * @param answer - Slot id, option id, `__decline__` or text, as validated by the server.
 * @param csrfToken - Session CSRF token for write authorization.
 * @throws ApiError 404 (stale request), 409 (already answered/expired/cancelled), 422 (invalid answer).
 */
export async function submitResponseInput(
  responseId: string,
  requestId: string,
  answer: string,
  csrfToken: string,
): Promise<{ response_id: string; request_id: string; status: string }> {
  return apiRequest(`/api/v1/responses/${responseId}/input`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify({ request_id: requestId, answer }),
  });
}

/** Reserved answer meaning "none of the offered options" (mirrors `INPUT_DECLINE` in tool_specs.py). */
export const INPUT_DECLINE = '__decline__';

/** Handler for one SSE event type: receives the raw `data:` payload and the caller's callbacks. */
type SseHandler = (eventData: string, options: StreamEventsOptions) => void;

/**
 * Parses a JSON payload for a handler whose failures are only logged (malformed or redacted payloads are skipped).
 *
 * @param label - Event name used in the warning.
 * @param eventData - Raw payload.
 * @param apply - Invoked with the parsed value; runs inside the same guard as parsing, like the inline try/catch it replaced.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function withJsonPayload(label: string, eventData: string, apply: (parsed: any) => void): void {
  try {
    apply(JSON.parse(eventData));
  } catch (err) {
    console.warn(`Failed to parse ${label} SSE payload`, err);
  }
}

/**
 * Validates a parsed `tool.status` payload into a {@link ToolStep}; non-numeric `detail` values and a blank `label` are dropped.
 *
 * @param parsed - JSON-decoded event data (any shape).
 * @returns The step, or null for redacted/malformed payloads.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function parseToolStep(parsed: any): ToolStep | null {
  if (typeof parsed?.tool !== 'string' || !parsed.tool) return null;
  if (parsed.state !== 'running' && parsed.state !== 'done') return null;
  const raw = parsed.detail && typeof parsed.detail === 'object' && !Array.isArray(parsed.detail) ? parsed.detail : {};
  const detail = Object.fromEntries(Object.entries(raw).filter(([, v]) => typeof v === 'number')) as Record<string, number>;
  return {
    tool: parsed.tool,
    state: parsed.state,
    ...(typeof parsed.label === 'string' && parsed.label ? { label: parsed.label } : {}),
    ...(Object.keys(detail).length ? { detail } : {}),
  };
}

/** Typed SSE event handlers keyed by event name; unknown events are ignored (forward compatible). */
const SSE_HANDLERS: Record<string, SseHandler> = {
  'message.delta': (eventData, options) => {
    try {
      const parsed = JSON.parse(eventData);
      if (typeof parsed?.text === 'string') {
        options.onDelta?.(parsed.text);
      }
    } catch {
      options.onDelta?.(eventData);
    }
  },
  'message.reasoning.delta': (eventData, options) =>
    withJsonPayload('message.reasoning.delta', eventData, (parsed) => {
      if (typeof parsed?.text === 'string' && parsed.text) options.onReasoning?.(parsed.text);
    }),
  'message.citations': (eventData, options) =>
    withJsonPayload('citations', eventData, (parsed) => {
      const citationsList = Array.isArray(parsed?.citations) ? (parsed.citations as Citation[]) : [];
      options.onCitations?.(citationsList);
    }),
  'message.ui.delta': (eventData, options) =>
    withJsonPayload('message.ui.delta', eventData, (parsed) => {
      if (parsed && typeof parsed === 'object') options.onUiDelta?.(parsed as Record<string, unknown>);
    }),
  'message.ui.done': (eventData, options) =>
    withJsonPayload('message.ui.done', eventData, (parsed) => {
      if (parsed && typeof parsed === 'object') options.onUiDone?.(parsed as Record<string, unknown>);
    }),
  'message.ui.discard': (eventData, options) =>
    withJsonPayload('message.ui.discard', eventData, (parsed) => {
      // Privacy redaction rewrites old events to `{}`; ignore anything without a part id.
      if (typeof parsed?.part_id === 'string') options.onUiDiscard?.(parsed.part_id);
    }),
  'message.input.request': (eventData, options) =>
    withJsonPayload('message.input.request', eventData, (parsed) => {
      // Privacy redaction rewrites old events to `{}`; ignore anything without an id.
      if (parsed && typeof parsed.request_id === 'string') {
        options.onInputRequest?.({
          ...parsed,
          options: Array.isArray(parsed.options) ? parsed.options : [],
          slots: Array.isArray(parsed.slots) ? parsed.slots : [],
        } as InputRequest);
      }
    }),
  'ui.theme': (eventData, options) =>
    withJsonPayload('ui.theme', eventData, (parsed) => {
      // Only the three known modes are ever applied; anything else (including redacted {}) is ignored.
      if (parsed?.mode === 'light' || parsed?.mode === 'dark' || parsed?.mode === 'system') options.onThemeRequest?.(parsed.mode);
    }),
  'message.input.resolved': (eventData, options) =>
    withJsonPayload('message.input.resolved', eventData, (parsed) => {
      if (parsed && typeof parsed.request_id === 'string') options.onInputResolved?.(parsed as InputResolved);
    }),
  'message.done': handleDoneEvent,
  web_search: (eventData, options) =>
    withJsonPayload('web_search', eventData, (parsed) => {
      if (parsed && typeof parsed.status === 'string') options.onWebSearch?.(parsed as WebSearchOutcome);
    }),
  'tool.status': (eventData, options) =>
    withJsonPayload('tool.status', eventData, (parsed) => {
      const step = parseToolStep(parsed);
      if (!step) return; // redacted/malformed payloads (`{}`) are ignored
      if (step.tool === 'web_search' || step.tool === 'query_collected_data')
        options.onToolStatus?.({ tool: step.tool, state: step.state });
      options.onToolStep?.(step);
    }),
  'conversation.title': (eventData, options) =>
    withJsonPayload('conversation.title', eventData, (parsed) => {
      if (typeof parsed?.title === 'string' && parsed.title.trim()) options.onConversationTitle?.(parsed.title);
    }),
  'message.suggestions': (eventData, options) =>
    withJsonPayload('message.suggestions', eventData, (parsed) => {
      const list = Array.isArray(parsed?.suggestions)
        ? parsed.suggestions.filter((s: unknown): s is string => typeof s === 'string' && s !== '')
        : [];
      if (typeof parsed?.message_id === 'string' && list.length)
        options.onSuggestions?.({ message_id: parsed.message_id, suggestions: list });
    }),
  status: (eventData, options) => {
    try {
      const parsed = JSON.parse(eventData);
      const statusVal = parsed?.status || eventData;
      options.onStatus?.(statusVal, parsed);
    } catch {
      options.onStatus?.(eventData);
    }
  },
};

/** Normalizes a parsed `message.done` payload into the typed result (missing fields get safe defaults). */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function toDoneResult(parsed: any): Parameters<NonNullable<StreamEventsOptions['onDone']>>[0] {
  const ui = parsed?.generative_ui;
  const parts = parsed?.generative_ui_parts;
  return {
    text: parsed?.text ?? '',
    citations: Array.isArray(parsed?.citations) ? parsed.citations : [],
    model: parsed?.model,
    status: parsed?.status ?? 'completed',
    generative_ui: ui && typeof ui === 'object' ? (ui as Record<string, unknown>) : null,
    generative_ui_parts: Array.isArray(parts) ? (parts as Record<string, unknown>[]) : undefined,
    uncited: parsed?.uncited === true,
  };
}

/** `message.done`: terminal result; an unparsable payload is delivered as plain completed text. */
function handleDoneEvent(eventData: string, options: StreamEventsOptions): void {
  try {
    options.onDone?.(toDoneResult(JSON.parse(eventData)));
  } catch {
    options.onDone?.({
      text: eventData,
      citations: [],
      status: 'completed',
    });
  }
}

/**
 * Parses one SSE block (lines separated by `\n`): comments/heartbeats are skipped, multiple `data:` lines are joined
 * with newlines, and the last `id:` wins.
 *
 * @param block - Text between two blank lines.
 * @returns Event type (default `message`), joined data and the optional event id.
 */
function parseSseBlock(block: string): { eventType: string; eventData: string; eventId: string | null } {
  let eventType = 'message';
  let eventData = '';
  let eventId: string | null = null;
  for (const line of block.split('\n')) {
    if (line.startsWith(':')) {
      // SSE comment / heartbeat
      continue;
    }
    if (line.startsWith('event:')) {
      eventType = line.slice(6).trim();
    } else if (line.startsWith('data:')) {
      const dataSlice = line.slice(5).trimStart();
      eventData += (eventData ? '\n' : '') + dataSlice;
    } else if (line.startsWith('id:')) {
      eventId = line.slice(3).trim();
    }
  }
  return { eventType, eventData, eventId };
}

/**
 * Opens the response event stream.
 *
 * @param responseId - Response run id.
 * @param options - Resume cursor (sent as `last_event_id` and `Last-Event-ID`) and abort signal.
 * @returns The response body reader.
 * @throws Error (with `status` for HTTP failures) after reporting it through `options.onError`; a 401 also dispatches
 * `umwelt:unauthorized`.
 */
async function openEventStream(responseId: string, options: StreamEventsOptions): Promise<ReadableStreamDefaultReader<Uint8Array>> {
  const query = new URLSearchParams();
  if (options.lastEventId) {
    query.set('last_event_id', options.lastEventId);
  }
  const queryString = query.toString();
  const url = `/api/v1/responses/${responseId}/events${queryString ? `?${queryString}` : ''}`;

  const headers: Record<string, string> = {
    ...(workspaceHeaders(workspaceTargetFor(url)) as Record<string, string>),
    Accept: 'text/event-stream',
    'Cache-Control': 'no-cache',
  };
  if (options.lastEventId) {
    headers['Last-Event-ID'] = options.lastEventId;
  }

  const response = await fetch(url, {
    method: 'GET',
    headers,
    credentials: 'same-origin',
    signal: options.signal,
  });

  if (!response.ok) {
    if (response.status === 401 && typeof window !== 'undefined') {
      window.dispatchEvent(new Event('umwelt:unauthorized'));
    }
    const errText = await response.text().catch(() => 'Streaming request failed');
    // Status lets callers retry transient 5xx/network failures but not 4xx rejections.
    const error = Object.assign(new Error(`Streaming failed (${response.status}): ${errText}`), { status: response.status });
    options.onError?.(error);
    throw error;
  }

  const reader = response.body?.getReader();
  if (!reader) {
    const error = new Error('ReadableStream not supported on response body');
    options.onError?.(error);
    throw error;
  }
  return reader;
}

/**
 * Consumes the SSE stream for a specific response run (/api/v1/responses/{id}/events).
 * Parses stream events ('message.delta', 'message.citations', 'message.done', 'status')
 * and dispatches typed callbacks. Supports reconnection using lastEventId.
 *
 * @param responseId - UUID of the response run to stream.
 * @param options - Event listeners, abort signal, and resume cursor.
 * @returns Promise that resolves when the stream completes or is aborted.
 */
export async function streamResponseEvents(responseId: string, options: StreamEventsOptions = {}): Promise<void> {
  const reader = await openEventStream(responseId, options);
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split('\n\n');
      // The last element is either empty (if ended with double newline) or an incomplete event block
      buffer = blocks.pop() ?? '';

      for (const block of blocks) {
        if (!block.trim()) continue;
        const { eventType, eventData, eventId } = parseSseBlock(block);
        if (eventId) {
          options.lastEventId = eventId;
        }
        // Own-property lookup so event names like "constructor" never resolve to Object.prototype members.
        if (Object.hasOwn(SSE_HANDLERS, eventType)) SSE_HANDLERS[eventType](eventData, options);
      }
    }
  } catch (err: unknown) {
    if (options.signal?.aborted) {
      // Aborted intentionally by client signal
      return;
    }
    const error = err instanceof Error ? err : new Error(String(err));
    options.onError?.(error);
    throw error;
  } finally {
    reader.releaseLock();
  }
}

/** File extensions accepted by the shared upload allowlist (`modules/ingestion/files.py`). */
export const ATTACHMENT_ACCEPT = '.txt,.md,.markdown,.json,.csv,.pdf,.docx';
/** Server-enforced per-message cap on Chat attachments. */
export const MAX_ATTACHMENTS = 5;
/** Local pre-check only, mirroring the server's default `upload_max_bytes`; the server stays authoritative and its 413 is mapped to copy without a number. */
export const ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024;

/** One chat attachment Document and whether it can be referenced as chat context. */
export interface ChatAttachment {
  document_id: string;
  source_id: string;
  title: string;
  status: 'pending' | 'ready' | 'too_large' | 'failed';
  document_version_id: string | null;
  local_only: boolean;
  run_id?: string | null;
}

/**
 * Uploads one file through the shared ingestion pipeline. The server picks the destination: the private,
 * local-only "Chat attachments" source by default, or "Chat attachments (shared)" when `shareWithModel` is set.
 */
export async function uploadChatAttachment(
  file: File,
  csrfToken: string,
  shareWithModel = false,
  signal?: AbortSignal,
): Promise<ChatAttachment> {
  const form = new FormData();
  form.set('file', file);
  if (shareWithModel) form.set('share_with_model', 'true');
  return apiRequest<ChatAttachment>('/api/v1/documents/chat-attachments', {
    method: 'POST',
    headers: csrfHeaders(csrfToken),
    body: form,
    signal,
  });
}

/** Reads a chat attachment's ingest status and current document version. */
export async function getChatAttachment(documentId: string, signal?: AbortSignal): Promise<ChatAttachment> {
  return apiRequest<ChatAttachment>(`/api/v1/documents/chat-attachments/${encodeURIComponent(documentId)}`, { signal });
}
