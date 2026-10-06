'use client';

import { createContext, useCallback, useContext, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import type { ChatContext } from '@/modules/chat/api';

/** Immutable request identity retained until the server acknowledges a message revision. */
export interface PendingMessageMutation {
  readonly action: 'edit' | 'regenerate';
  readonly targetMessageId: string;
  readonly baseContentHash: string;
  readonly content?: string;
  readonly clientRequestId: string;
}

/** Identifies an attempted replacement while an earlier durable result is still unknown. */
export class PendingMessageMutationConflictError extends Error {
  /** Construct the stable controller error used to block a different request envelope. */
  constructor() {
    super('PENDING_CHAT_MUTATION_UNRESOLVED');
    this.name = 'PendingMessageMutationConflictError';
  }
}

/**
 * Parameters passed when opening the chat drawer programmatically.
 */
export interface ChatOpenParams {
  /** Optional existing conversation identifier to display immediately. */
  conversationId?: string;
  /** Optional grounding context (e.g. document, entity, day, or selection). */
  context?: ChatContext;
}

/**
 * Immutable identity needed to retry an unacknowledged specialist start exactly.
 */
export interface PendingSpecialistRun {
  /** Original user prompt, kept byte-for-byte for the idempotent retry. */
  readonly prompt: string;
  /** Specialist profile selected when the first request was created. */
  readonly profileId: string;
  /** Immutable profile revision selected for that request. */
  readonly profileRevision: number;
  /** Conversation whose durable run may already have been committed. */
  readonly conversationId: string;
  /** Request identity reused until the server acknowledges durable creation. */
  readonly clientRequestId: string;
}

/**
 * Exposed controller interface for interacting with global chat drawer and state.
 */
export interface ChatControllerContextValue {
  /** Whether the quick-chat drawer Sheet is currently open. */
  isDrawerOpen: boolean;
  /** Currently selected or active conversation identifier. */
  activeConversationId: string | null;
  /** Monotonic generation advanced by every explicit conversation selection or reset. */
  activeConversationGeneration: number;
  /** Bounded grounding context associated with the active session. */
  context: ChatContext | null;
  /** Ephemeral in-memory composer draft retained across drawer closures. */
  draft: string;
  /** Unacknowledged specialist starts keyed by their original conversation ID. */
  pendingSpecialistRuns: Readonly<Record<string, PendingSpecialistRun>>;
  /** Unacknowledged append-only prompt/answer revisions retained in memory by conversation. */
  pendingMessageMutations: Readonly<Record<string, PendingMessageMutation>>;
  /** Opens the chat drawer, optionally pointing to a conversation or resource context. */
  openDrawer: (params?: ChatOpenParams) => void;
  /** Closes the chat drawer presentation without aborting any active server generation. */
  closeDrawer: () => void;
  /** Toggles drawer open/closed presentation state. */
  toggleDrawer: () => void;
  /** Sets or switches the active conversation ID. */
  setActiveConversationId: (id: string | null) => void;
  /** Sets or clears the active grounding context. */
  setContext: (context: ChatContext | null) => void;
  /** Updates the in-memory draft string. */
  setDraft: (draft: string) => void;
  /** Retains one immutable pending start under its conversation without browser persistence. */
  setPendingSpecialistRun: (run: PendingSpecialistRun) => void;
  /** Clears a pending start only after durable acknowledgement for that conversation. */
  clearPendingSpecialistRun: (conversationId: string) => void;
  /** Reuses one unresolved request envelope for the exact same mutation inputs. */
  getOrCreatePendingMessageMutation: (
    conversationId: string,
    input: Omit<PendingMessageMutation, 'clientRequestId'>,
  ) => PendingMessageMutation;
  /** Clears only the acknowledged request envelope, never a newer retry. */
  clearPendingMessageMutation: (conversationId: string, clientRequestId: string) => void;
  /** Starts a clean new conversation state while preserving or resetting context. */
  resetConversation: () => void;
  /**
   * Switches the drawer to the conversation bound to a day (or a fresh one if none exists yet).
   * Earlier day conversations and their background runs are never modified or cancelled.
   */
  selectDay: (day: { date: string; timezone: string }) => void;
  /** Explicitly binds a newly created conversation to the day captured before its create request. */
  bindDayConversation: (day: { date: string; timezone: string }, conversationId: string) => void;
  /**
   * Selects an existing conversation (e.g. from history). Never rebinds a day; drops a day context
   * that does not own this conversation so later sends cannot leak into it.
   */
  selectConversation: (id: string) => void;
  /** Forgets any day binding that points at a deleted conversation so that day starts a fresh one. */
  unbindConversation: (id: string) => void;
}

/** Builds the in-memory key binding one day context to its conversation. */
function dayKey(date: string | undefined, timezone: string | undefined): string {
  return `${date}|${timezone}`;
}

const ChatControllerContext = createContext<ChatControllerContextValue | null>(null);

/**
 * Custom hook to access global chat controls, drawer visibility, and active conversation state.
 * Throws an informative error if used outside a ChatControllerProvider.
 *
 * @returns ChatControllerContextValue interface.
 */
export function useChatController(): ChatControllerContextValue {
  const context = useContext(ChatControllerContext);
  if (!context) {
    throw new Error('useChatController must be used within a ChatControllerProvider');
  }
  return context;
}

/**
 * Global chat state provider that manages in-memory conversation selection and its monotonic
 * selection generation for scoping asynchronous UI work, composer drafts, per-conversation
 * specialist/message mutation retry envelopes, and drawer presentation toggles. Private prompt
 * text remains in memory only; an unresolved mutation cannot be replaced by a different one.
 *
 * @param children - React child components wrapped by this provider.
 * @returns Context provider element.
 */
export function ChatControllerProvider({ children }: { children: ReactNode }) {
  const [isDrawerOpen, setIsDrawerOpen] = useState(false);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [activeConversationGeneration, setActiveConversationGeneration] = useState(0);
  const [context, setContext] = useState<ChatContext | null>(null);
  const [draft, setDraft] = useState<string>('');
  const [pendingSpecialistRuns, setPendingSpecialistRuns] = useState<Record<string, PendingSpecialistRun>>({});
  const [pendingMessageMutations, setPendingMessageMutations] = useState<Record<string, PendingMessageMutation>>({});
  const pendingMessageMutationsRef = useRef<Record<string, PendingMessageMutation>>({});

  /** Retains the mutation identity synchronously so a lost acknowledgement can retry exactly. */
  const getOrCreatePendingMessageMutation = useCallback((
    conversationId: string,
    input: Omit<PendingMessageMutation, 'clientRequestId'>,
  ): PendingMessageMutation => {
    const existing = pendingMessageMutationsRef.current[conversationId];
    if (existing) {
      const matches = existing.action === input.action
        && existing.targetMessageId === input.targetMessageId
        && existing.baseContentHash === input.baseContentHash
        && existing.content === input.content;
      if (matches) return existing;
      throw new PendingMessageMutationConflictError();
    }
    const clientRequestId = typeof crypto !== 'undefined' && crypto.randomUUID
      ? crypto.randomUUID()
      : `mutation-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
    const mutation = { ...input, clientRequestId };
    const next = { ...pendingMessageMutationsRef.current, [conversationId]: mutation };
    pendingMessageMutationsRef.current = next;
    setPendingMessageMutations(next);
    return mutation;
  }, []);

  /** Removes a request only after its matching durable response acknowledgement arrives. */
  const clearPendingMessageMutation = useCallback((conversationId: string, clientRequestId: string) => {
    const current = pendingMessageMutationsRef.current;
    if (current[conversationId]?.clientRequestId !== clientRequestId) return;
    const next = { ...current };
    delete next[conversationId];
    pendingMessageMutationsRef.current = next;
    setPendingMessageMutations(next);
  }, []);

  /**
   * Selects a conversation and advances its generation even when the same ID is selected again.
   * The generation lets in-flight Chat work distinguish a later visit to the same conversation.
   */
  const selectConversationRaw = useCallback((id: string | null) => {
    setActiveConversationId(id);
    setActiveConversationGeneration((generation) => generation + 1);
  }, []);

  // Day -> conversation bindings for this browser session; a conversation keeps the day it was created with.
  const dayConversations = useRef(new Map<string, string>());
  const contextRef = useRef<ChatContext | null>(null);
  useLayoutEffect(() => { contextRef.current = context; });

  const bindDayConversation = useCallback((day: { date: string; timezone: string }, conversationId: string) => {
    dayConversations.current.set(dayKey(day.date, day.timezone), conversationId);
  }, []);

  /** Switches context and conversation together so a send can never mix two days. */
  const selectDay = useCallback((day: { date: string; timezone: string }) => {
    setContext({ kind: 'day', date: day.date, timezone: day.timezone });
    selectConversationRaw(dayConversations.current.get(dayKey(day.date, day.timezone)) ?? null);
  }, [selectConversationRaw]);

  const unbindConversation = useCallback((id: string) => {
    for (const [key, value] of dayConversations.current) {
      if (value === id) dayConversations.current.delete(key);
    }
    const pending = pendingMessageMutationsRef.current[id];
    if (pending) clearPendingMessageMutation(id, pending.clientRequestId);
  }, [clearPendingMessageMutation]);

  const selectHistoryConversation = useCallback((id: string) => {
    const now = contextRef.current;
    if (now?.kind === 'day' && dayConversations.current.get(dayKey(now.date, now.timezone)) !== id) {
      setContext(null);
    }
    selectConversationRaw(id);
  }, [selectConversationRaw]);

  /**
   * Stores a pending envelope by its immutable conversation identity for all Chat surfaces.
   */
  const setPendingSpecialistRun = useCallback((run: PendingSpecialistRun) => {
    setPendingSpecialistRuns((current) => ({ ...current, [run.conversationId]: run }));
  }, []);

  /**
   * Removes only the acknowledged pending envelope and leaves other conversations' retries intact.
   */
  const clearPendingSpecialistRun = useCallback((conversationId: string) => {
    setPendingSpecialistRuns((current) => {
      if (!current[conversationId]) return current;
      const next = { ...current };
      delete next[conversationId];
      return next;
    });
  }, []);

  /**
   * Opens the chat drawer and optionally focuses a specific conversation or context.
   */
  const openDrawer = useCallback((params?: ChatOpenParams) => {
    if (params?.conversationId !== undefined) {
      selectConversationRaw(params.conversationId);
    }
    if (params?.context !== undefined) {
      setContext(params.context);
    }
    setIsDrawerOpen(true);
  }, [selectConversationRaw]);

  /**
   * Closes the drawer presentation only. Active generation runs continue on the server.
   */
  const closeDrawer = useCallback(() => {
    setIsDrawerOpen(false);
  }, []);

  /**
   * Toggles drawer presentation open or closed.
   */
  const toggleDrawer = useCallback(() => {
    setIsDrawerOpen((prev) => !prev);
  }, []);

  /**
   * Clears the active conversation ID and composer draft while preserving thread-keyed retry envelopes.
   */
  const resetConversation = useCallback(() => {
    selectConversationRaw(null);
    setDraft('');
  }, [selectConversationRaw]);

  const value: ChatControllerContextValue = {
    isDrawerOpen,
    activeConversationId,
    activeConversationGeneration,
    context,
    draft,
    pendingSpecialistRuns,
    pendingMessageMutations,
    openDrawer,
    closeDrawer,
    toggleDrawer,
    setActiveConversationId: selectConversationRaw,
    setContext,
    setDraft,
    setPendingSpecialistRun,
    clearPendingSpecialistRun,
    getOrCreatePendingMessageMutation,
    clearPendingMessageMutation,
    resetConversation,
    selectDay,
    bindDayConversation,
    selectConversation: selectHistoryConversation,
    unbindConversation,
  };

  return (
    <ChatControllerContext.Provider value={value}>
      {children}
    </ChatControllerContext.Provider>
  );
}
