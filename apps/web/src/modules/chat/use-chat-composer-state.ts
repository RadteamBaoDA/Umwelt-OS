import * as React from 'react';

/** Composer-adjacent UI state of the chat surface. */
export interface ChatComposerState {
  /** Web-search toggle (defaults on, sent only when search is usable). Sticky across sends; reset only by New chat. */
  webSearchOn: boolean;
  setWebSearchOn: (on: boolean) => void;
  /** The composer's + menu drives the context picker dialog. */
  contextPickerOpen: boolean;
  setContextPickerOpen: (open: boolean) => void;
  /** Hidden file input rendered by the attachment bar; the + menu clicks it. */
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  /** The template library dialog opened from the + menu. */
  templatesOpen: boolean;
  setTemplatesOpen: (open: boolean) => void;
  providerGateOpen: boolean;
  setProviderGateOpen: (open: boolean) => void;
  /** True once the agent picker was opened; the profile list is fetched only from then on (or in agent mode). */
  agentPickerOpened: boolean;
  openAgentPicker: () => void;
  selectedProfileId: string;
  setSelectedProfileId: (id: string) => void;
  /** Message whose prompt is being edited locally (before any pending mutation is considered). */
  editingMessageId: string | null;
  setEditingMessageId: (id: string | null) => void;
  /** Draft typed before an edit began; restored when the edit is cancelled. */
  draftStashRef: React.MutableRefObject<string | null>;
  /** Restores the toggle defaults (web search on) and the Assistant agent; called by New chat only. */
  resetToggles: () => void;
}

/**
 * Owns the composer-adjacent UI state of ChatSession (web-search toggle, picker/gate visibility, profile and edit selection).
 *
 * @returns State and stable setters; `resetToggles` is referentially stable.
 */
export function useChatComposerState(): ChatComposerState {
  const [webSearchOn, setWebSearchOn] = React.useState(true);
  const [contextPickerOpen, setContextPickerOpen] = React.useState(false);
  const fileInputRef = React.useRef<HTMLInputElement>(null);
  const [providerGateOpen, setProviderGateOpen] = React.useState(false);
  const [templatesOpen, setTemplatesOpen] = React.useState(false);
  const [editingMessageId, setEditingMessageId] = React.useState<string | null>(null);
  const draftStashRef = React.useRef<string | null>(null);
  const [agentPickerOpened, setAgentPickerOpened] = React.useState(false);
  const openAgentPicker = React.useCallback(() => setAgentPickerOpened(true), []);
  const [selectedProfileId, setSelectedProfileId] = React.useState('assistant');
  const resetToggles = React.useCallback(() => {
    setWebSearchOn(true);
    setSelectedProfileId('assistant');
  }, []);
  return {
    webSearchOn,
    setWebSearchOn,
    contextPickerOpen,
    setContextPickerOpen,
    fileInputRef,
    templatesOpen,
    setTemplatesOpen,
    providerGateOpen,
    setProviderGateOpen,
    agentPickerOpened,
    openAgentPicker,
    selectedProfileId,
    setSelectedProfileId,
    editingMessageId,
    setEditingMessageId,
    draftStashRef,
    resetToggles,
  };
}
