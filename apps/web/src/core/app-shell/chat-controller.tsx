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
 * Exposed controller interface for interacting with global chat drawer and state.
 */
export interface ChatControllerContextValue {
  /** Whether the quick-chat drawer Sheet is currently open. */
  isDrawerOpen: boolean;
  /** Currently selected or active conversation identifier. */
  activeConversationId: string | null;
  /** Bounded grounding context associated with the active session. */
  context: ChatContext | null;
  /** Ephemeral in-memory composer draft retained across drawer closures. */
  draft: string;
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
 * Global chat state provider that manages in-memory conversation selection,
 * ephemeral message draft preservation, and drawer presentation toggles.
 * Does not store private draft content into persistent browser storage.
 *
 * @param children - React child components wrapped by this provider.
 * @returns Context provider element.
 */
export function ChatControllerProvider({ children }: { children: ReactNode }) {
  const [isDrawerOpen, setIsDrawerOpen] = useState(false);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [context, setContext] = useState<ChatContext | null>(null);
  const [draft, setDraft] = useState<string>('');

  /**
   * Opens the chat drawer and optionally focuses a specific conversation or context.
   */
  const openDrawer = useCallback((params?: ChatOpenParams) => {
    if (params?.conversationId !== undefined) {
      setActiveConversationId(params.conversationId);
    }
    if (params?.context !== undefined) {
      setContext(params.context);
    }
    setIsDrawerOpen(true);
  }, []);

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
   * Clears the active conversation ID to prepare for a fresh conversation.
   */
  const resetConversation = useCallback(() => {
    setActiveConversationId(null);
  }, []);

  const value: ChatControllerContextValue = {
    isDrawerOpen,
    activeConversationId,
    context,
    draft,
    openDrawer,
    closeDrawer,
    toggleDrawer,
    setActiveConversationId,
    setContext,
    setDraft,
    resetConversation,
  };

  return (
    <ChatControllerContext.Provider value={value}>
      {children}
    </ChatControllerContext.Provider>
  );
}
