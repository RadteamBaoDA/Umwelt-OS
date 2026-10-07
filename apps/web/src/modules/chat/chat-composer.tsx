'use client';

// Text sizing and Enter/Shift+Enter behavior are adapted from AnythingLLM's
// PromptInput source; ChatController remains the private in-memory draft owner.

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { SendIcon, SquareIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Label } from '@/components/ui/label';

export interface ChatComposerProps {
  /** Current in-memory draft string. */
  value: string;
  /** Callback invoked when the user modifies draft text. */
  onChange: (value: string) => void;
  /** Callback invoked when the user submits a message to be sent. */
  onSend: (content: string) => void;
  /** Optional callback invoked when the user explicitly stops active generation. */
  onStop?: () => void;
  /** Whether response generation is actively streaming from the server. */
  isStreaming: boolean;
  /** Whether the composer input is disabled (e.g. during submission dispatch). */
  disabled?: boolean;
  /** Custom placeholder string override. */
  placeholder?: string;
  /** Model identity reported by the latest assistant answer, if any. */
  modelLabel?: string | null;
  /** Number of distinct sources cited by the latest assistant answer. */
  sourcesCount?: number;
  /** Full page shows the truthful web search and attachment availability note. */
  showCapabilityNote?: boolean;
  /** Per-message web search opt-in; passed only by the full Chat page. */
  webSearch?: { available: boolean; enabled: boolean; onChange: (enabled: boolean) => void };
}

/**
 * Message input composer featuring multi-line textarea, keyboard shortcut submission (Enter),
 * separate Stop button during streaming, and in-memory draft preservation across drawer toggles.
 *
 * @param props - ChatComposerProps interface.
 * @returns Accessible composer control component.
 */
export function ChatComposer({
  value,
  onChange,
  onSend,
  onStop,
  isStreaming,
  disabled = false,
  placeholder,
  modelLabel = null,
  sourcesCount = 0,
  showCapabilityNote = false,
  webSearch,
}: ChatComposerProps) {
  const t = useTranslations('chat');
  const textareaRef = React.useRef<HTMLTextAreaElement>(null);

  /**
   * Submits the trimmed draft message if non-empty and not currently streaming.
   */
  const handleSend = React.useCallback(() => {
    const trimmed = value.trim();
    if (!trimmed || isStreaming || disabled) return;
    onSend(trimmed);
  }, [value, isStreaming, disabled, onSend]);

  /**
   * Handles keyboard shortcuts in the textarea: Enter submits, Shift+Enter inserts newline.
   */
  const handleKeyDown = React.useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      // Enter during IME composition commits text instead of submitting the chat turn.
      if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && e.nativeEvent.keyCode !== 229) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend],
  );

  /**
   * Handles textarea input changes and adjusts row height dynamically.
   */
  const handleChange = React.useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      onChange(e.target.value);
      e.target.style.height = 'auto';
      e.target.style.height = `${Math.min(e.target.scrollHeight, 160)}px`;
    },
    [onChange],
  );

  React.useLayoutEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = 'auto';
    textarea.style.height = `${Math.min(textarea.scrollHeight, 160)}px`;
  }, [value]);

  const canSend = value.trim().length > 0 && !isStreaming && !disabled;

  return (
    <div className="relative flex flex-col gap-2 border-t border-border bg-background p-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
      <div className="relative flex items-end gap-2 rounded-xl border border-border bg-surface p-2 shadow-sm focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-1">
        <textarea
          ref={textareaRef}
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          placeholder={placeholder ?? t('composerPlaceholder')}
          disabled={disabled}
          rows={1}
          className="flex-1 max-h-40 min-h-[44px] resize-none bg-transparent p-2 text-sm text-foreground placeholder:text-muted-foreground outline-none disabled:opacity-50"
          aria-label={t('composerPlaceholder')}
        />

        <div className="flex items-center gap-1 shrink-0 pb-1 pr-1">
          {isStreaming ? (
            <Button
              type="button"
              onClick={onStop}
              className="flex items-center gap-1.5 px-3 py-1.5 h-9 rounded-lg bg-destructive text-destructive-foreground hover:opacity-90 font-medium text-xs shadow-sm transition-opacity"
              aria-label={t('stopGenerating')}
            >
              <SquareIcon className="size-3.5 fill-current" />
              <span>{t('stop')}</span>
            </Button>
          ) : (
            <Button
              type="button"
              onClick={handleSend}
              disabled={!canSend}
              className="flex items-center gap-1.5 px-3 py-1.5 h-9 rounded-lg bg-primary text-primary-foreground disabled:opacity-40 disabled:cursor-not-allowed font-medium text-xs shadow-sm transition-opacity"
              aria-label={t('send')}
            >
              <SendIcon className="size-3.5" />
              <span>{t('send')}</span>
            </Button>
          )}
        </div>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-x-3 px-1 text-[11px] text-muted-foreground">
        <span>{modelLabel ? t('modelLine', { model: modelLabel }) : t('modelNotReported')}{sourcesCount > 0 ? ` · ${t('sourcesCount', { count: sourcesCount })}` : ''}</span>
        <span className="hidden sm:inline">Enter ↵ · Shift+Enter</span>
      </div>
      {webSearch && (
        <div className="flex flex-col gap-0.5 px-1">
          <div className="flex items-center gap-2">
            <Checkbox
              id="chat-web-search"
              checked={webSearch.available && webSearch.enabled}
              disabled={!webSearch.available || isStreaming}
              onCheckedChange={(checked) => webSearch.onChange(checked === true)}
              aria-describedby="chat-web-search-help"
            />
            <Label htmlFor="chat-web-search" className="text-xs text-foreground">{t('webSearchToggle')}</Label>
          </div>
          <p id="chat-web-search-help" className="text-[11px] text-muted-foreground">
            {!webSearch.available ? t('webSearchDisabledHelp') : webSearch.enabled ? t('webSearchEnabledHelp') : t('webSearchIdleHelp')}
          </p>
        </div>
      )}
      {showCapabilityNote && <p className="px-1 text-[11px] text-muted-foreground">{t('composerNote')}</p>}
    </div>
  );
}
