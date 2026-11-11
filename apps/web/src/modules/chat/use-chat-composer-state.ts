import * as React from 'react';

/** Composer-adjacent UI state of the chat surface. */
export interface ChatComposerState {
  /** Per-send web-search toggle (defaults on; sent only when search is usable). */
  webSearchOn: boolean;
  setWebSearchOn: (on: boolean) => void;
  /** Per-send database-query toggle (defaults off; forced on by a gadget context). */
  dbQueryOn: boolean;
  setDbQueryOn: (on: boolean) => void;
  /** The composer's + menu drives the context picker dialog. */
  contextPickerOpen: boolean;
  setContextPickerOpen: (open: boolean) => void;
  /** Hidden file input rendered by the attachment bar; the + menu clicks it. */
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  providerGateOpen: boolean;
  setProviderGateOpen: (open: boolean) => void;
  selectedProfileId: string;
  setSelectedProfileId: (id: string) => void;
  /** Message whose prompt is being edited locally (before any pending mutation is considered). */
  editingMessageId: string | null;
  setEditingMessageId: (id: string | null) => void;
  /** Draft typed before an edit began; restored when the edit is cancelled. */
  draftStashRef: React.MutableRefObject<string | null>;
  /** Restores the per-send toggles after the server accepted a send (web search on, db query off). */
  resetToggles: () => void;
}

/**
 * Owns the composer-adjacent UI state of ChatSession (tool toggles, picker/gate visibility, profile and edit selection).
 *
 * @returns State and stable setters; `resetToggles` is referentially stable.
 */
export function useChatComposerState(): ChatComposerState {
  const [webSearchOn, setWebSearchOn] = React.useState(true);
  const [dbQueryOn, setDbQueryOn] = React.useState(false);
  const [contextPickerOpen, setContextPickerOpen] = React.useState(false);
  const fileInputRef = React.useRef<HTMLInputElement>(null);
  const [providerGateOpen, setProviderGateOpen] = React.useState(false);
  const [editingMessageId, setEditingMessageId] = React.useState<string | null>(null);
  const draftStashRef = React.useRef<string | null>(null);
  const [selectedProfileId, setSelectedProfileId] = React.useState('assistant');
  const resetToggles = React.useCallback(() => {
    setWebSearchOn(true);
    setDbQueryOn(false);
  }, []);
  return {
    webSearchOn,
    setWebSearchOn,
    dbQueryOn,
    setDbQueryOn,
    contextPickerOpen,
    setContextPickerOpen,
    fileInputRef,
    providerGateOpen,
    setProviderGateOpen,
    selectedProfileId,
    setSelectedProfileId,
    editingMessageId,
    setEditingMessageId,
    draftStashRef,
    resetToggles,
  };
}
