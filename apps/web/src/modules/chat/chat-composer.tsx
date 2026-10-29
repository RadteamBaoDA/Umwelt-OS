'use client';

// Text sizing and Enter/Shift+Enter behavior are adapted from AnythingLLM's
// PromptInput source; ChatController remains the private in-memory draft owner.

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { ArrowUpIcon, FileTextIcon, PaperclipIcon, PlusIcon, SendIcon, SquareIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { COMPOSER_MENU_ID } from '@/modules/chat/chat-attachments';
import { ModelPicker, type ChatModelPicker } from '@/modules/chat/model-picker';

/** Actions behind the full composer's + menu; the session owns the picker dialog and file input they drive. */
export interface ComposerMenuProps {
  /** Opens the context picker dialog; the item is omitted when context cannot be picked. */
  onAddContext?: () => void;
  /** Opens the native file chooser. */
  onAttach: () => void;
  /** Disables Attach (attachments unavailable, busy, or at the limit). */
  attachDisabled: boolean;
  /** Opt-in letting cloud models read files attached next. */
  share: { checked: boolean; disabled: boolean; onChange: (checked: boolean) => void };
}

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
  /** Optional callback invoked when the user pastes one or more files from the clipboard into the composer. */
  onAttachFiles?: (files: File[]) => void;
  /** Whether attachment upload or ingestion is currently busy/disabled, blocking clipboard file paste. */
  attachmentBusy?: boolean;
  /** Full-page + menu actions (context, attach, share); omitted in the drawer. */
  menu?: ComposerMenuProps;
  /** Per-chat model dropdown (full and drawer); omitted while no chat model is configured. */
  modelPicker?: ChatModelPicker;
}

/**
 * Message input composer featuring multi-line auto-resizing textarea, clipboard file paste,
 * keyboard shortcut submission (Enter), separate Stop button during streaming, and in-memory
 * draft preservation across drawer toggles.
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
  webSearch,
  attachments,
  sendBlockedBy = null,
  onAttachFiles,
  attachmentBusy = false,
  mode = 'drawer',
  contextBar,
  menu,
  modelPicker,
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
   * Intercepts clipboard paste events containing binary files (e.g. screenshots or PDFs) and
   * forwards them to `onAttachFiles` when file attachment is supported and not busy.
   */
  const handlePaste = React.useCallback(
    (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
      const files = event.clipboardData?.files;
      if (files && files.length > 0 && onAttachFiles && !attachmentBusy && !disabled) {
        event.preventDefault();
        onAttachFiles(Array.from(files));
      }
    },
    [onAttachFiles, attachmentBusy, disabled],
  );

  /**
   * Handles textarea input changes and adjusts row height dynamically up to 200px.
   */
  const handleChange = React.useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      onChange(e.target.value);
      e.target.style.height = 'auto';
      e.target.style.height = `${Math.min(e.target.scrollHeight, 200)}px`;
    },
    [onChange],
  );

  React.useLayoutEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    // Reset height before measuring scrollHeight so clearing the draft shrinks back to baseline.
    textarea.style.height = 'auto';
    textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`;
  }, [value]);

  const canSend = value.trim().length > 0 && !isStreaming && !disabled && !sendBlockedBy;

  const isFull = mode === 'full';
  const textarea = (
    <textarea
      ref={textareaRef}
      value={value}
      onChange={handleChange}
      onKeyDown={handleKeyDown}
      onPaste={handlePaste}
      placeholder={placeholder ?? t('composerPlaceholder')}
      // readOnly + aria-disabled (not disabled) so the textarea keeps focus while a send is dispatching.
      readOnly={disabled}
      aria-disabled={disabled || undefined}
      rows={2}
      className={isFull
        // Full layout: the surrounding box owns the border and focus ring, so the field itself is borderless.
        ? 'block max-h-[200px] min-h-14 w-full resize-none overflow-y-auto bg-transparent px-1 py-1 text-base text-foreground outline-none placeholder:text-muted-foreground aria-disabled:opacity-50 sm:text-[15px]'
        : 'block max-h-[200px] min-h-14 w-full resize-none overflow-y-auto rounded-lg border border-border bg-background p-3 text-base text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring aria-disabled:opacity-50 sm:text-[15px]'}
      aria-label={t('composerPlaceholder')}
    />
  );
  const stopButton = (
    <Button
      type="button"
      variant="outline"
      onClick={onStop}
      data-testid="chat-stop"
      className="min-h-11 border-destructive text-destructive hover:text-destructive"
      aria-label={t('stopGenerating')}
    >
      <SquareIcon className="size-3.5 fill-current" />
      <span className="hidden sm:inline">{t('stop')}</span>
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
      data-testid="chat-send"
      aria-label={t('send')}
      aria-describedby={sendBlockedBy ?? undefined}
    >
      <SendIcon className="size-4" />
      <span className="hidden sm:inline">{t('send')}</span>
    </Button>
  );

  if (!isFull) {
    return (
      <div className="relative flex flex-wrap items-end gap-2 border-t border-border bg-surface p-4 shrink-0 pb-[max(1rem,env(safe-area-inset-bottom))] sm:flex-nowrap">
        {/* Below sm the picker takes its own row above the textarea so Send keeps room beside it. */}
        {modelPicker && <div className="order-first w-full sm:order-none sm:w-auto"><ModelPicker picker={modelPicker} disabled={isStreaming || disabled} /></div>}
        <div className="min-w-0 flex-1">{textarea}</div>
        {isStreaming ? stopButton : sendButton}
      </div>
    );
  }

  // Round icon-only controls for the full layout; the aria-labels keep them named for assistive tech.
  const roundStop = (
    <Button type="button" variant="outline" size="icon" onClick={onStop} className="rounded-full border-destructive text-destructive hover:text-destructive" data-testid="chat-stop" aria-label={t('stopGenerating')}>
      <SquareIcon className="size-3.5 fill-current" aria-hidden="true" />
    </Button>
  );
  const roundSend = (
    <Button
      type="button"
      size="icon"
      onClick={handleSend}
      aria-disabled={!canSend}
      className="rounded-full aria-disabled:cursor-not-allowed aria-disabled:opacity-40"
      data-testid="chat-send"
      aria-label={t('send')}
      aria-describedby={sendBlockedBy ?? undefined}
    >
      <ArrowUpIcon className="size-4" aria-hidden="true" />
    </Button>
  );
  const modelText = modelLabel ? t('modelLine', { model: modelLabel }) : t('modelNotReported');
  const webSearchChecked = !!webSearch && webSearch.available && !webSearch.profileBlocked && webSearch.enabled;

  return (
    <div className="relative shrink-0 border-t border-border bg-surface px-3 py-3 sm:px-5 sm:py-4 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
      <div className="flex flex-col gap-2 rounded-2xl border border-border bg-background p-3 focus-within:ring-2 focus-within:ring-ring">
        {contextBar}
        {attachments}
        {textarea}
        <div className="flex items-center gap-2">
          {menu && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button id={COMPOSER_MENU_ID} type="button" variant="ghost" size="icon" className="rounded-full text-muted-foreground" aria-label={t('composerMenu')} disabled={disabled}>
                  <PlusIcon className="size-4" aria-hidden="true" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" side="top">
                {menu.onAddContext && (
                  <DropdownMenuItem onSelect={menu.onAddContext}>
                    <FileTextIcon aria-hidden="true" />{t('addContext')}
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem disabled={menu.attachDisabled} onSelect={menu.onAttach}>
                  <PaperclipIcon aria-hidden="true" />{t('attach')}
                </DropdownMenuItem>
                <DropdownMenuCheckboxItem
                  checked={menu.share.checked}
                  disabled={menu.share.disabled}
                  // Keep the menu open so the owner sees the toggle change before attaching.
                  onSelect={(e) => e.preventDefault()}
                  onCheckedChange={(checked) => menu.share.onChange(checked === true)}
                >
                  {t('attachmentShareToggle')}
                </DropdownMenuCheckboxItem>
                {webSearch && (
                  <>
                    <DropdownMenuSeparator />
                    <DropdownMenuCheckboxItem
                      id="chat-web-search"
                      checked={webSearchChecked}
                      disabled={!webSearch.available || webSearch.profileBlocked || isStreaming || disabled}
                      onSelect={(e) => e.preventDefault()}
                      onCheckedChange={(checked) => webSearch.onChange(checked === true)}
                    >
                      {t('webSearchToggle')}
                    </DropdownMenuCheckboxItem>
                  </>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
          <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
            {modelText}{sourcesCount > 0 ? ` · ${t('sourcesCount', { count: sourcesCount })}` : ''}
          </span>
          {modelPicker && <ModelPicker picker={modelPicker} disabled={isStreaming || disabled} />}
          {isStreaming ? roundStop : roundSend}
        </div>
      </div>
    </div>
  );
}
