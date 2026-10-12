'use client';

// Text sizing and Enter/Shift+Enter behavior are adapted from AnythingLLM's
// PromptInput source; ChatController remains the private in-memory draft owner.

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { SendIcon, SquareIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';

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
  webSearch?: { available: boolean; profileBlocked?: boolean; provider?: string; enabled: boolean; onChange: (enabled: boolean) => void };
  /** Attachment controls rendered above the input (full Chat page only). */
  attachments?: React.ReactNode;
  /** Surface mode: `full` renders the page composer (context badges + toolbar); `drawer` the quick-chat textarea/button pair. */
  mode?: 'drawer' | 'full';
  /** Context badges row rendered above the textarea (full Chat page). */
  contextBar?: React.ReactNode;
  /** Element id explaining why Send is blocked (e.g. attachments still processing); blocks Send when set. */
  sendBlockedBy?: string | null;
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
  attachments,
  sendBlockedBy = null,
  mode = 'drawer',
  contextBar,
}: ChatComposerProps) {
  const t = useTranslations('chat');
  const textareaRef = React.useRef<HTMLTextAreaElement>(null);

  /**
   * Submits the trimmed draft message if non-empty and not currently streaming.
   */
  const handleSend = React.useCallback(() => {
    const trimmed = value.trim();
    if (!trimmed || isStreaming || disabled || sendBlockedBy) return;
    onSend(trimmed);
  }, [value, isStreaming, disabled, sendBlockedBy, onSend]);

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

  const canSend = value.trim().length > 0 && !isStreaming && !disabled && !sendBlockedBy;

  const isFull = mode === 'full';
  const textarea = (
    <textarea
      ref={textareaRef}
      value={value}
      onChange={handleChange}
      onKeyDown={handleKeyDown}
      placeholder={placeholder ?? t('composerPlaceholder')}
      disabled={disabled}
      rows={2}
      className="block max-h-40 min-h-14 w-full resize-none rounded-lg border border-border bg-background p-3 text-base text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50 sm:text-[15px]"
      aria-label={t('composerPlaceholder')}
    />
  );
  const stopButton = (
    <Button
      type="button"
      variant="outline"
      onClick={onStop}
      className="min-h-11 border-destructive text-destructive hover:text-destructive"
      aria-label={t('stopGenerating')}
    >
      <SquareIcon className="size-3.5 fill-current" />
      <span>{t('stop')}</span>
    </Button>
  );
  const sendButton = (
    <Button
      type="button"
      onClick={handleSend}
      // aria-disabled (not disabled) keeps Send focusable so its blocked reason stays reachable;
      // handleSend is the click guard.
      aria-disabled={!canSend}
      className="min-h-11 aria-disabled:cursor-not-allowed aria-disabled:opacity-40"
      aria-label={t('send')}
      aria-describedby={sendBlockedBy ?? undefined}
    >
      <SendIcon className="size-4" />
      <span>{t('send')}</span>
    </Button>
  );

  if (!isFull) {
    return (
      <div className="relative flex items-end gap-2 border-t border-border bg-surface p-4 shrink-0 pb-[max(1rem,env(safe-area-inset-bottom))]">
        <div className="min-w-0 flex-1">{textarea}</div>
        {isStreaming ? stopButton : sendButton}
      </div>
    );
  }

  return (
    <div className="relative flex flex-col gap-2.5 border-t border-border bg-surface px-3 py-3 sm:px-5 sm:py-4 pb-[max(0.75rem,env(safe-area-inset-bottom))] shrink-0">
      {contextBar}
      {textarea}
      {attachments}
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline" className="min-h-11 px-3 text-sm font-semibold">
          {modelLabel ? t('modelLine', { model: modelLabel }) : t('modelNotReported')}{sourcesCount > 0 ? ` · ${t('sourcesCount', { count: sourcesCount })}` : ''}
        </Badge>
        {webSearch && (
          <div className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-border px-3">
            <Checkbox
              id="chat-web-search"
              checked={webSearch.available && !webSearch.profileBlocked && webSearch.enabled}
              disabled={!webSearch.available || webSearch.profileBlocked || isStreaming || disabled}
              onCheckedChange={(checked) => webSearch.onChange(checked === true)}
              aria-describedby="chat-web-search-help"
            />
            <Label htmlFor="chat-web-search" className="text-sm font-semibold text-foreground">{t('webSearchToggle')}</Label>
          </div>
        )}
        {webSearch && <span className="text-xs text-muted-foreground sm:ml-auto">{t('webSearchPersonalNote')}</span>}
        <div className={webSearch ? '' : 'ml-auto'}>{isStreaming ? stopButton : sendButton}</div>
      </div>
      {webSearch && (
        <p id="chat-web-search-help" className="px-1 text-xs text-muted-foreground">
          {!webSearch.available ? t('webSearchDisabledHelp') : webSearch.profileBlocked ? t('webSearchProfileHelp') : webSearch.enabled ? t('webSearchEnabledHelp', { provider: webSearch.provider ?? '' }) : t('webSearchIdleHelp')}
        </p>
      )}
      {showCapabilityNote && <p className="px-1 text-xs text-muted-foreground">{t('composerNote')}</p>}
    </div>
  );
}
