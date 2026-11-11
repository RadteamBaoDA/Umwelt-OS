import * as React from 'react';
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import type { ChatMessage } from './api';

/**
 * Cache key of the user messages shown before the server confirms them. It is separate from the conversation detail so a
 * detail refetch racing the send (first send of a new conversation) cannot wipe the entry.
 */
const optimisticKey = (conversationId: string) => ['chat', 'optimistic', conversationId] as const;

/**
 * Shows the user's message immediately, keyed by its `client_request_id`.
 *
 * @param queryClient - App query client.
 * @param conversationId - Conversation the message is being sent to.
 * @param clientRequestId - Idempotency key of the send; the server copy carries the same value.
 * @param content - Message text.
 */
export function addOptimisticMessage(queryClient: QueryClient, conversationId: string, clientRequestId: string, content: string): void {
  const message: ChatMessage = {
    id: `optimistic-${clientRequestId}`,
    conversation_id: conversationId,
    role: 'user',
    content,
    client_request_id: clientRequestId,
    model_identity: null,
    citations: [],
    response_id: null,
    revision_of_message_id: null,
    created_at: new Date().toISOString(),
  };
  queryClient.setQueryData<ChatMessage[]>(optimisticKey(conversationId), (prev) => [...(prev ?? []), message]);
}

/** Removes an optimistic message (the server rejected the send or already returned its own copy). */
export function removeOptimisticMessage(queryClient: QueryClient, conversationId: string, clientRequestId: string): void {
  queryClient.setQueryData<ChatMessage[]>(optimisticKey(conversationId), (prev) => (prev ?? []).filter((m) => m.client_request_id !== clientRequestId));
}

/**
 * Committed messages followed by the optimistic ones the server has not returned yet.
 *
 * @param server - Messages of the conversation detail.
 * @param optimistic - Pending optimistic messages.
 * @returns The same array as `server` when nothing is pending (referentially stable).
 */
export function mergeOptimistic(server: ChatMessage[], optimistic: ChatMessage[]): ChatMessage[] {
  const confirmed = new Set(server.map((m) => m.client_request_id).filter(Boolean));
  const pending = optimistic.filter((m) => !confirmed.has(m.client_request_id));
  return pending.length ? [...server, ...pending] : server;
}

/**
 * Subscribes to the optimistic messages of a conversation and prunes those the server has since returned.
 *
 * @param conversationId - Conversation being rendered (none for a fresh thread).
 * @param server - Committed messages, used to detect confirmation.
 * @returns Optimistic messages still waiting for their server copy.
 */
export function useOptimisticMessages(conversationId: string | null | undefined, server: ChatMessage[]): ChatMessage[] {
  const queryClient = useQueryClient();
  // Never fetches: the data only ever comes from setQueryData.
  const { data } = useQuery<ChatMessage[]>({
    queryKey: optimisticKey(conversationId ?? ''),
    queryFn: () => [],
    enabled: false,
    initialData: [],
  });
  React.useEffect(() => {
    if (!conversationId) return;
    const confirmed = new Set(server.map((m) => m.client_request_id).filter(Boolean));
    for (const m of data) if (confirmed.has(m.client_request_id)) removeOptimisticMessage(queryClient, conversationId, m.client_request_id!);
  }, [conversationId, server, data, queryClient]);
  return data;
}
