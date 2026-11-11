'use client';

// Text sizing and Enter/Shift+Enter behavior are adapted from AnythingLLM's
// PromptInput source; ChatController remains the private in-memory draft owner.

import * as React from 'react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { ArrowUpIcon, CloudIcon, DatabaseIcon, FileTextIcon, GlobeIcon, PaperclipIcon, PlusIcon, SquareIcon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { COMPOSER_MENU_ID } from './chat-attachments';
import { ModelPicker, type ChatModelPicker } from './model-picker';

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
  /** Sticky (default-on) web search preference; passed only by the full Chat page. */
  webSearch?: { available: boolean; profileBlocked?: boolean; provider?: string; enabled: boolean; onChange: (enabled: boolean) => void };
  /** Per-message structured/SQL database query opt-in over collected datasources. */
  dbQuery?: { enabled: boolean; onChange: (enabled: boolean) => void };
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
  /** Edit mode (full Chat): shows the "editing a previous prompt" banner inside the box; Esc or Cancel calls `onCancel`. */
  editing?: { onCancel: () => void };
  /** When defined, the textarea takes focus each time this value changes (opening/switching a conversation), on fine pointers only. */
  autoFocusKey?: string;
}

/** Pieces of composer state shared by the drawer and full layouts. */
type ComposerShared = Pick<ChatComposerProps, 'isStreaming' | 'dbQuery' | 'modelPicker'> & {
  disabled: boolean;
  textarea: React.ReactNode;
  action: React.ReactNode;
};

/**
 * Composer behaviour: trimmed send gate, Enter-to-send (IME safe), file paste forwarding and textarea auto-resize.
 *
 * @param props - Composer props (value, callbacks and the flags that block sending).
 * @param textareaRef - Ref of the textarea being resized.
 * @returns Event handlers and whether Send is currently allowed.
 */
function useComposerHandlers(props: ChatComposerProps, textareaRef: React.RefObject<HTMLTextAreaElement | null>) {
  const {
    value,
    onChange,
    onSend,
    isStreaming,
    disabled = false,
    sendBlockedBy = null,
    onAttachFiles,
    attachmentBusy = false,
    editing,
  } = props;

  /** Submits the trimmed draft message if non-empty and not currently streaming. */
  const handleSend = React.useCallback(() => {
    const trimmed = value.trim();
    if (!trimmed || isStreaming || disabled || sendBlockedBy) return;
    onSend(trimmed);
  }, [value, isStreaming, disabled, sendBlockedBy, onSend]);

  /** Enter submits, Shift+Enter inserts a newline. */
  const handleKeyDown = React.useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Escape' && editing && !e.nativeEvent.isComposing) {
        e.preventDefault();
        editing.onCancel();
        return;
      }
      // Enter during IME composition commits text instead of submitting the chat turn.
      if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && e.nativeEvent.keyCode !== 229) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend, editing],
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

  /** Handles textarea input changes and adjusts row height dynamically up to 200px. */
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
  }, [value, textareaRef]);

  const canSend = value.trim().length > 0 && !isStreaming && !disabled && !sendBlockedBy;
  return { handleSend, handleKeyDown, handlePaste, handleChange, canSend };
}

/** Round stop button shared by the Chat page and the quick chat sheet; the aria-label keeps it named for assistive tech. */
function StopButton({ onStop }: { onStop?: () => void }) {
  const t = useTranslations('chat');
  return (
    <Button
      type="button"
      variant="outline"
      size="icon"
      onClick={onStop}
      className="border-destructive text-destructive hover:text-destructive"
      data-testid="chat-stop"
      aria-label={t('stopGenerating')}
      title={`${t('stopGenerating')} (Esc)`}
    >
      <SquareIcon className="size-3.5 fill-current" aria-hidden="true" />
    </Button>
  );
}

/** Round send button; aria-disabled (not disabled) keeps it focusable while the reason is announced via `describedBy`. */
function SendButton({ onClick, canSend, describedBy }: { onClick: () => void; canSend: boolean; describedBy: string | null }) {
  const t = useTranslations('chat');
  return (
    <Button
      type="button"
      size="icon"
      onClick={onClick}
      aria-disabled={!canSend}
      className="aria-disabled:cursor-not-allowed aria-disabled:opacity-40"
      data-testid="chat-send"
      aria-label={t('send')}
      aria-describedby={describedBy ?? undefined}
    >
      <ArrowUpIcon className="size-4" aria-hidden="true" />
    </Button>
  );
}

/** "Database query on" badge. */
function DbQueryBadge() {
  const t = useTranslations('chat');
  return (
    <Badge variant="secondary" className="shrink-0 gap-1 text-xs" data-testid="chat-db-query-badge">
      <DatabaseIcon className="size-3" aria-hidden="true" />
      {t('dbQueryBadge')}
    </Badge>
  );
}

/** The "+" trigger of the composer menu. */
function MenuTrigger({ disabled }: { disabled: boolean }) {
  const t = useTranslations('chat');
  return (
    <DropdownMenuTrigger asChild>
      <Button
        id={COMPOSER_MENU_ID}
        type="button"
        variant="ghost"
        size="icon"
        className="text-muted-foreground"
        aria-label={t('composerMenu')}
        disabled={disabled}
      >
        <PlusIcon className="size-4" aria-hidden="true" />
      </Button>
    </DropdownMenuTrigger>
  );
}

/** Drawer layout: one bordered box with the borderless field on top and "+", model and send in a toolbar row. */
function DrawerComposer({ dbQuery, modelPicker, isStreaming, disabled, textarea, action }: ComposerShared) {
  const t = useTranslations('chat');
  return (
    <div className="relative shrink-0 border-t border-border bg-surface p-4 pb-[max(1rem,env(safe-area-inset-bottom))]">
      <div className="flex w-full flex-col gap-2 rounded-lg border border-control bg-surface p-3 focus-within:ring-2 focus-within:ring-data">
        {textarea}
        <div className="flex items-center gap-2">
          {dbQuery && (
            <DropdownMenu>
              <MenuTrigger disabled={disabled} />
              <DropdownMenuContent align="start" side="top">
                <DropdownMenuCheckboxItem
                  id="chat-db-query"
                  checked={dbQuery.enabled}
                  disabled={isStreaming || disabled}
                  onSelect={(e) => e.preventDefault()}
                  onCheckedChange={(checked) => dbQuery.onChange(checked === true)}
                >
                  <DatabaseIcon className="size-3.5 text-muted-foreground" aria-hidden="true" />
                  {t('dbQueryToggle')}
                </DropdownMenuCheckboxItem>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
          {dbQuery?.enabled && <DbQueryBadge />}
          <span className="min-w-0 flex-1" />
          {modelPicker && <ModelPicker picker={modelPicker} disabled={isStreaming || disabled} />}
          {action}
        </div>
      </div>
    </div>
  );
}

/**
 * Web-search entry of the full menu. When no search provider is configured it is a link to the web-search settings
 * page (a disabled checkbox gave no way to fix it); when the active agent profile cannot search it stays a disabled
 * checkbox with that reason.
 */
function WebSearchMenuItem({
  webSearch,
  checked,
  busy,
}: {
  webSearch: NonNullable<ChatComposerProps['webSearch']>;
  checked: boolean;
  busy: boolean;
}) {
  const t = useTranslations('chat');
  const notSetUp = !webSearch.available && !webSearch.profileBlocked;
  return (
    <>
      <DropdownMenuSeparator />
      {notSetUp ? (
        <DropdownMenuItem inset asChild>
          <Link href="/settings/web-search" data-testid="chat-web-search-setup">
            <GlobeIcon className="size-4 text-muted-foreground" aria-hidden="true" />
            <span className="flex flex-col">
              {t('webSearchToggle')}
              <span className="text-xs text-muted-foreground">{t('webSearchNotSetUp')}</span>
            </span>
          </Link>
        </DropdownMenuItem>
      ) : (
        <DropdownMenuCheckboxItem
          id="chat-web-search"
          checked={checked}
          disabled={Boolean(webSearch.profileBlocked) || busy}
          onSelect={(e) => e.preventDefault()}
          onCheckedChange={(next) => webSearch.onChange(next === true)}
        >
          <GlobeIcon className="size-4 text-muted-foreground" aria-hidden="true" />
          <span className="flex flex-col">
            {t('webSearchToggle')}
            {/* Disabled items give no reason; explain why so the owner knows what to fix. */}
            {webSearch.profileBlocked && (
              <span className="text-xs text-muted-foreground" data-testid="chat-web-search-hint">
                {t('webSearchProfileOnly')}
              </span>
            )}
          </span>
        </DropdownMenuCheckboxItem>
      )}
    </>
  );
}

/** The full-page "+" menu: add context, attach, share toggle, web search and database query. */
function FullMenu({
  menu,
  webSearch,
  webSearchChecked,
  dbQuery,
  isStreaming,
  disabled,
}: {
  menu: ComposerMenuProps;
  webSearch: ChatComposerProps['webSearch'];
  webSearchChecked: boolean;
  dbQuery: ChatComposerProps['dbQuery'];
  isStreaming: boolean;
  disabled: boolean;
}) {
  const t = useTranslations('chat');
  return (
    <DropdownMenu>
      <MenuTrigger disabled={disabled} />
      <DropdownMenuContent align="start" side="top">
        {menu.onAddContext && (
          <DropdownMenuItem inset onSelect={menu.onAddContext}>
            <FileTextIcon aria-hidden="true" />
            {t('addContext')}
          </DropdownMenuItem>
        )}
        <DropdownMenuItem inset disabled={menu.attachDisabled} onSelect={menu.onAttach}>
          <PaperclipIcon aria-hidden="true" />
          {t('attach')}
        </DropdownMenuItem>
        <DropdownMenuCheckboxItem
          checked={menu.share.checked}
          disabled={menu.share.disabled}
          // Keep the menu open so the owner sees the toggle change before attaching.
          onSelect={(e) => e.preventDefault()}
          onCheckedChange={(checked) => menu.share.onChange(checked === true)}
        >
          <CloudIcon className="size-4 text-muted-foreground" aria-hidden="true" />
          {t('attachmentShareToggle')}
        </DropdownMenuCheckboxItem>
        {webSearch && <WebSearchMenuItem webSearch={webSearch} checked={webSearchChecked} busy={isStreaming || disabled} />}
        {dbQuery && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuCheckboxItem
              id="chat-db-query"
              checked={dbQuery.enabled}
              disabled={isStreaming || disabled}
              onSelect={(e) => e.preventDefault()}
              onCheckedChange={(checked) => dbQuery.onChange(checked === true)}
            >
              <DatabaseIcon className="size-4 text-muted-foreground" aria-hidden="true" />
              {t('dbQueryToggle')}
            </DropdownMenuCheckboxItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * Spacer at the end of the full-page toolbar. Without a model picker it also names the latest answer's model and source
 * count; with a picker the picker is the single model label (a second "Model: …" next to it said the same thing twice).
 */
function ModelLine({ modelLabel, sourcesCount, hasPicker }: { modelLabel: string | null; sourcesCount: number; hasPicker: boolean }) {
  const t = useTranslations('chat');
  if (hasPicker) return <span className="min-w-0 flex-1" />;
  const modelText = modelLabel ? t('modelLine', { model: modelLabel }) : null;
  return (
    <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
      {modelText}
      {sourcesCount > 0 ? `${modelText ? ' · ' : ''}${t('sourcesCount', { count: sourcesCount })}` : ''}
    </span>
  );
}

/** "Editing a previous prompt" banner shown at the top of the composer box, with a Cancel action. */
function EditBanner({ onCancel }: { onCancel: () => void }) {
  const t = useTranslations('chat');
  return (
    <div
      role="status"
      data-testid="chat-edit-banner"
      className="flex items-center justify-between gap-3 rounded-md bg-secondary px-3 py-2 text-xs"
    >
      <span className="text-muted-foreground">{t('editingPrompt')}</span>
      <Button type="button" variant="outline" className="h-8 px-2 text-xs" onClick={onCancel}>
        {t('cancelEdit')}
      </Button>
    </div>
  );
}

/**
 * Full-page layout: the one floating element, a single bordered field with a 12px radius and the 720px measure
 * matching the transcript.
 */
function FullComposer(props: ChatComposerProps & ComposerShared) {
  const {
    editing,
    contextBar,
    attachments,
    textarea,
    action,
    menu,
    webSearch,
    dbQuery,
    modelPicker,
    isStreaming,
    disabled,
    modelLabel = null,
    sourcesCount = 0,
  } = props;
  const webSearchChecked = !!webSearch && webSearch.available && !webSearch.profileBlocked && webSearch.enabled;
  const t = useTranslations('chat');
  return (
    <div className="relative shrink-0 bg-surface px-3 py-3 sm:px-5 sm:py-4 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
      <div className="mx-auto flex w-full max-w-[720px] flex-col gap-2 rounded-lg border border-control bg-surface p-3 focus-within:ring-2 focus-within:ring-data">
        {editing && <EditBanner onCancel={editing.onCancel} />}
        {contextBar}
        {attachments}
        {textarea}
        <div className="flex items-center gap-2">
          {menu && (
            <FullMenu
              menu={menu}
              webSearch={webSearch}
              webSearchChecked={webSearchChecked}
              dbQuery={dbQuery}
              isStreaming={isStreaming}
              disabled={disabled}
            />
          )}
          {webSearchChecked && (
            <Badge variant="secondary" className="shrink-0 gap-1 text-xs" data-testid="chat-web-search-badge">
              <GlobeIcon className="size-3" aria-hidden="true" />
              {t('webSearchBadge')}
            </Badge>
          )}
          {dbQuery?.enabled && <DbQueryBadge />}
          <ModelLine modelLabel={modelLabel} sourcesCount={sourcesCount} hasPicker={Boolean(modelPicker)} />
          {modelPicker && <ModelPicker picker={modelPicker} disabled={isStreaming || disabled} />}
          {action}
        </div>
      </div>
    </div>
  );
}

/**
 * Message input composer featuring multi-line auto-resizing textarea, clipboard file paste,
 * keyboard shortcut submission (Enter), separate Stop button during streaming, and in-memory
 * draft preservation across drawer toggles.
 *
 * @param props - ChatComposerProps interface.
 * @returns Accessible composer control component.
 */
export function ChatComposer(props: ChatComposerProps) {
  const { value, onStop, isStreaming, disabled = false, placeholder, sendBlockedBy = null, mode = 'drawer', dbQuery, modelPicker } = props;
  const t = useTranslations('chat');
  const textareaRef = React.useRef<HTMLTextAreaElement>(null);
  const { handleSend, handleKeyDown, handlePaste, handleChange, canSend } = useComposerHandlers(props, textareaRef);
  const { autoFocusKey } = props;
  React.useEffect(() => {
    // Touch devices would pop the on-screen keyboard over the transcript, so only fine pointers auto-focus.
    if (autoFocusKey === undefined || !window.matchMedia?.('(pointer: fine)')?.matches) return;
    textareaRef.current?.focus({ preventScroll: true });
  }, [autoFocusKey]);
  const textarea = (
    <Textarea
      ref={textareaRef}
      data-chat-composer-input
      value={value}
      onChange={handleChange}
      onKeyDown={handleKeyDown}
      onPaste={handlePaste}
      placeholder={placeholder ?? t(isStreaming ? 'composerPlaceholderStreaming' : 'composerPlaceholder')}
      // readOnly + aria-disabled (not disabled) so the textarea keeps focus while a send is dispatching.
      readOnly={disabled}
      aria-disabled={disabled || undefined}
      rows={1}
      // The surrounding box owns the border and focus ring in both layouts, so the field itself is borderless.
      className="block field-sizing-fixed max-h-[200px] min-h-9 w-full sm:min-h-14 resize-none overflow-y-auto rounded-none border-0 bg-transparent px-1 py-1 text-base text-foreground shadow-none outline-none placeholder:text-muted-foreground focus-visible:ring-0 aria-disabled:opacity-50 dark:bg-transparent"
      aria-label={t('composerPlaceholder')}
    />
  );
  const action = isStreaming ? (
    <StopButton onStop={onStop} />
  ) : (
    <SendButton onClick={handleSend} canSend={canSend} describedBy={sendBlockedBy} />
  );
  const shared: ComposerShared = { isStreaming, dbQuery, modelPicker, disabled, textarea, action };
  return mode === 'full' ? <FullComposer {...props} {...shared} /> : <DrawerComposer {...shared} />;
}
