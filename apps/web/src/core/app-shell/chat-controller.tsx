'use client';

import { createContext, useCallback, useContext, useState, type ReactNode } from 'react';
import type { ChatContext } from '@/modules/chat/api';

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
  /** Starts a clean new conversation state while preserving or resetting context. */
  resetConversation: () => void;
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
 * selection generation for scoping asynchronous UI work,
 * composer drafts, per-conversation specialist retry envelopes, and drawer presentation toggles.
 * Does not store private draft content into persistent browser storage.
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

  /**
   * Selects a conversation and advances its generation even when the same ID is selected again.
   * The generation lets in-flight Chat work distinguish a later visit to the same conversation.
   */
  const selectConversation = useCallback((id: string | null) => {
    setActiveConversationId(id);
    setActiveConversationGeneration((generation) => generation + 1);
  }, []);

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
      selectConversation(params.conversationId);
    }
    if (params?.context !== undefined) {
      setContext(params.context);
    }
    setIsDrawerOpen(true);
  }, [selectConversation]);

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
    selectConversation(null);
    setDraft('');
  }, [selectConversation]);

  const value: ChatControllerContextValue = {
    isDrawerOpen,
    activeConversationId,
    activeConversationGeneration,
    context,
    draft,
    pendingSpecialistRuns,
    openDrawer,
    closeDrawer,
    toggleDrawer,
    setActiveConversationId: selectConversation,
    setContext,
    setDraft,
    setPendingSpecialistRun,
    clearPendingSpecialistRun,
    resetConversation,
  };

  return (
    <ChatControllerContext.Provider value={value}>
      {children}
    </ChatControllerContext.Provider>
  );
}
