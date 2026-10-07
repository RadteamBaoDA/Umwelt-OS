import { apiRequest, csrfHeaders } from '@/core/api';

/**
 * Citation evidence reference pointing to an exact grounded revision chunk.
 */
export interface DocumentCitation {
  sourceType: 'document';
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
 * ponytail: W3 (backend accepts `web_search`) flips this to true; until then the field would 422 (extra="forbid").
 */
export const WEB_SEARCH_SEND_ENABLED = false;

/** Reason codes the backend may report for a skipped or unavailable web search. */
export type WebSearchReason =
  | 'not_configured' | 'query_too_long' | 'local_only_context' | 'daily_limit'
  | 'timeout' | 'provider_error' | 'network_denied' | 'run_inactive' | 'no_results';

/** Outcome of a requested web search; absent when the message did not opt in. */
export interface WebSearchOutcome {
  status: 'used' | 'unavailable' | 'skipped';
  reason?: WebSearchReason | string | null;
  result_count?: number;
}

/**
 * Bounded client context linking a chat session to specific resources or filters.
 */
export interface ChatContext {
  kind: 'general' | 'document' | 'entity' | 'day' | 'selection';
  resource_id?: string;
  date?: string;
  timezone?: string;
  items?: Array<{
    sourceId: string;
    documentId: string;
    documentVersionId?: string;
    chunkId?: string;
  }>;
  [key: string]: unknown;
}

/**
 * Public summary of a chat conversation thread.
 */
export interface Conversation {
  id: string;
  title: string;
  context_kind: string | null;
  context_resource_id: string | null;
  pinned: boolean;
  archived: boolean;
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
}

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
export interface StreamEventsOptions {
  lastEventId?: string;
  signal?: AbortSignal;
  onDelta?: (text: string) => void;
  onCitations?: (citations: Citation[]) => void;
  onWebSearch?: (outcome: WebSearchOutcome) => void;
  onStatus?: (status: string, payload?: unknown) => void;
  onDone?: (result: { text: string; citations: Citation[]; model?: string; status: string }) => void;
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
 * @param params - Optional pagination and archive filters.
 * @returns Array of conversation header records.
 */
export async function listConversations(params?: {
  limit?: number;
  offset?: number;
  archived?: boolean;
  q?: string;
}): Promise<Conversation[]> {
  const query = new URLSearchParams();
  if (params?.limit !== undefined) query.set('limit', String(params.limit));
  if (params?.offset !== undefined) query.set('offset', String(params.offset));
  if (params?.archived !== undefined) query.set('archived', String(params.archived));
  if (params?.q?.trim()) query.set('q', params.q.trim().slice(0, 200));
  const queryString = query.toString();
  const path = `/api/v1/conversations${queryString ? `?${queryString}` : ''}`;
  return apiRequest<Conversation[]>(path);
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
export async function createConversation(
  payload: ConversationCreate,
  csrfToken: string,
): Promise<Conversation> {
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
export async function patchConversation(
  conversationId: string,
  payload: ConversationPatch,
  csrfToken: string,
): Promise<Conversation> {
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
export async function deleteConversation(
  conversationId: string,
  csrfToken: string,
): Promise<void> {
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
export async function sendMessage(
  conversationId: string,
  payload: SendMessagePayload,
  csrfToken: string,
): Promise<SendMessageResponse> {
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
  return apiRequest<SendMessageResponse>(
    `/api/v1/conversations/${conversationId}/messages/${messageId}/mutations`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...csrfHeaders(csrfToken),
      },
      body: JSON.stringify(payload),
    },
  );
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
export async function cancelResponse(
  responseId: string,
  csrfToken: string,
): Promise<CancelResponse> {
  return apiRequest<CancelResponse>(`/api/v1/responses/${responseId}/cancel`, {
    method: 'POST',
    headers: csrfHeaders(csrfToken),
  });
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
export async function streamResponseEvents(
  responseId: string,
  options: StreamEventsOptions = {},
): Promise<void> {
  const query = new URLSearchParams();
  if (options.lastEventId) {
    query.set('last_event_id', options.lastEventId);
  }
  const queryString = query.toString();
  const url = `/api/v1/responses/${responseId}/events${queryString ? `?${queryString}` : ''}`;

  const headers: HeadersInit = {
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
      window.dispatchEvent(new Event('bbd:unauthorized'));
    }
    const errText = await response.text().catch(() => 'Streaming request failed');
    const error = new Error(`Streaming failed (${response.status}): ${errText}`);
    options.onError?.(error);
    throw error;
  }

  const reader = response.body?.getReader();
  if (!reader) {
    const error = new Error('ReadableStream not supported on response body');
    options.onError?.(error);
    throw error;
  }

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

        if (eventId) {
          options.lastEventId = eventId;
        }

        // Process typed events
        if (eventType === 'message.delta') {
          try {
            const parsed = JSON.parse(eventData);
            if (typeof parsed?.text === 'string') {
              options.onDelta?.(parsed.text);
            }
          } catch {
            options.onDelta?.(eventData);
          }
        } else if (eventType === 'message.citations') {
          try {
            const parsed = JSON.parse(eventData);
            const citationsList = Array.isArray(parsed?.citations) ? (parsed.citations as Citation[]) : [];
            options.onCitations?.(citationsList);
          } catch (err) {
            console.warn('Failed to parse citations SSE payload', err);
          }
        } else if (eventType === 'message.done') {
          try {
            const parsed = JSON.parse(eventData);
            options.onDone?.({
              text: parsed?.text ?? '',
              citations: Array.isArray(parsed?.citations) ? parsed.citations : [],
              model: parsed?.model,
              status: parsed?.status ?? 'completed',
            });
          } catch {
            options.onDone?.({
              text: eventData,
              citations: [],
              status: 'completed',
            });
          }
        } else if (eventType === 'web_search') {
          try {
            const parsed = JSON.parse(eventData);
            if (parsed && typeof parsed.status === 'string') options.onWebSearch?.(parsed as WebSearchOutcome);
          } catch (err) {
            console.warn('Failed to parse web_search SSE payload', err);
          }
        } else if (eventType === 'status') {
          try {
            const parsed = JSON.parse(eventData);
            const statusVal = parsed?.status || eventData;
            options.onStatus?.(statusVal, parsed);
          } catch {
            options.onStatus?.(eventData);
          }
        }
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
