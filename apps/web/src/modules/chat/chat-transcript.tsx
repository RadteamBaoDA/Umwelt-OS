'use client';

import { messageUiParts, uiPartKey, type UiPart } from './generative-ui-parts';
import * as React from 'react';
import { useTranslations } from 'next-intl';
import { AlertCircleIcon, ArrowDownIcon, BookOpenIcon, CheckIcon, CopyIcon, DatabaseIcon, GlobeIcon, Loader2Icon, PaperclipIcon, PencilIcon, RefreshCwIcon } from 'lucide-react';
import type { ChatMessage, Citation, ToolStatus, WebSearchOutcome } from './api';
import { Button } from '@/components/ui/button';
import { ChatMarkdown } from './chat-markdown';
import { focusCitations } from './chat-citation-focus';
import { useChatScroll } from './use-chat-scroll';
import { useSmoothedText } from './use-smoothed-text';
import { ChatReasoning } from './chat-reasoning';
import { useCopyMessage } from './use-copy-message';
import { useTopicNames } from './use-topic-names';
import { useDisplayPreferences } from '@/core/query-provider';
import { formatDateTime } from '@/core/i18n';
import { ToolRendering } from './generative-ui/components/tool-rendering';
import { SaveTemplateButton } from './generative-ui/templates/template-library';
import { genUiCopy, resolveWidgetLocale } from './generative-ui/copy';
import type { AppLocaleId } from '@/core/i18n';
import { useRevealBlocks } from './reveal-blocks';
import type { GenerativeUIPayload } from './generative-ui/sandbox/schema';

const KNOWN_REASONS = ['not_configured', 'query_too_long', 'empty_query', 'local_only_context', 'daily_limit', 'timeout', 'provider_error', 'network_denied', 'run_inactive'];

/**
 * Markdown for a settled assistant answer whose top-level blocks fade in once on first view.
 *
 * @param props - Markdown content, citations and locale (forwarded to ChatMarkdown).
 * @returns Wrapper element containing the rendered answer.
 */
function RevealMarkdown(props: React.ComponentProps<typeof ChatMarkdown>) {
  const ref = React.useRef<HTMLDivElement>(null);
  useRevealBlocks(ref, true, props.content);
  return <div ref={ref}><ChatMarkdown {...props} /></div>;
}

/** Display names for the web-search provider ids reported in the search outcome. */
const PROVIDER_NAMES: Record<string, string> = { omniroute: 'OmniRoute', searxng: 'SearXNG', tavily: 'Tavily', brave: 'Brave' };

/**
 * " · via {Provider}" (+ " (fallback)") suffix of the search line; renders nothing when the provider is unknown/absent.
 *
 * @param props - Provider id and whether it was not the first configured target.
 * @returns Inline text or null.
 */
function WebSearchProvider({ provider, fallback }: { provider?: string | null; fallback?: boolean }) {
  const t = useTranslations('chat');
  if (!provider) return null;
  return <>{t('webSearchedVia', { provider: PROVIDER_NAMES[provider] ?? provider })}{fallback ? t('webSearchedFallback') : null}</>;
}

/**
 * Announced notice for a requested web search: the searched query with its result count when used (older rows
 * without a count fall back to the query alone), or why it was skipped/unavailable. Never shows URLs.
 *
 * @param props - Persisted or streaming outcome; null/undefined or unrelated statuses render nothing.
 * @returns Muted status line, or null.
 */
function WebSearchNotice({ outcome }: { outcome?: WebSearchOutcome | null }) {
  const t = useTranslations('chat');
  if (outcome?.status === 'used' && outcome.reason !== 'no_results' && outcome.query) {
    const count = outcome.result_count ?? 0;
    return (
      <p data-testid="web-search-query" className="mt-1 text-xs text-muted-foreground">
        {count > 0
          ? t('webSearchedQueryCount', { query: outcome.query, count })
          : t('webSearchedQuery', { query: outcome.query })}
        <WebSearchProvider provider={outcome.provider} fallback={outcome.fallback} />
      </p>
    );
  }
  if (outcome?.status === 'used' && outcome.reason === 'no_results') {
    return <p data-testid="web-search-notice" className="mt-1 text-xs text-muted-foreground">{t('webNoResults')}</p>;
  }
  if (!outcome || (outcome.status !== 'unavailable' && outcome.status !== 'skipped')) return null;
  return <WebSearchSkippedNotice outcome={outcome} />;
}

/** Notice for a search that was skipped or unavailable, with a localized reason (unknown codes use generic copy). */
function WebSearchSkippedNotice({ outcome }: { outcome: WebSearchOutcome }) {
  const t = useTranslations('chat');
  const reason = outcome.reason && KNOWN_REASONS.includes(outcome.reason) ? t(`webReason_${outcome.reason}`) : t('webReason_unknown');
  const title = outcome.status === 'skipped' ? t('webSkipped') : t('webUnavailable');
  return (
    <p data-testid="web-search-notice" className={`mt-1 text-xs ${outcome.status === 'unavailable' ? 'text-destructive' : 'text-muted-foreground'}`}>
      {t('webNotice', { title, reason })}
    </p>
  );
}

/** Workspace-relevant starter prompts as chat message keys (title shown, prompt sent). */
const STARTER_PROMPTS = [
  { title: 'starterNewsTitle', prompt: 'starterNewsPrompt' },
  { title: 'starterWatchlistTitle', prompt: 'starterWatchlistPrompt' },
  { title: 'starterBtcTitle', prompt: 'starterBtcPrompt' },
] as const;

/** One capability chip of the empty state: toggles a composer option or opens the file picker. */
export interface CapabilityChip {
  /** Current on/off state for toggles; omit for one-shot actions (attach). */
  pressed?: boolean;
  /** Localized reason the chip is unavailable (shown on hover and announced); presence disables the chip. */
  disabledReason?: string;
  /** True when disabled without an explanation (busy or limit reached). */
  disabled?: boolean;
  onActivate: () => void;
}

/** Capability chips shown in the empty state; each key is omitted when the control does not exist for this chat. */
export interface EmptyCapabilities {
  webSearch?: CapabilityChip;
  dbQuery?: CapabilityChip;
  attach?: CapabilityChip;
}

/**
 * One outline chip. Toggles expose `aria-pressed`; a disabled chip names its reason for assistive tech and as a title.
 *
 * @param props - Icon, label and the chip model.
 */
function CapabilityChipButton({ icon, label, chip, testId }: { icon: React.ReactNode; label: string; chip: CapabilityChip; testId: string }) {
  const toggle = chip.pressed !== undefined;
  const disabled = Boolean(chip.disabledReason) || Boolean(chip.disabled);
  return (
    <Button
      type="button"
      variant="outline"
      data-testid={testId}
      aria-pressed={toggle ? chip.pressed : undefined}
      disabled={disabled}
      title={chip.disabledReason}
      onClick={chip.onActivate}
      className="h-auto gap-1.5 px-3 py-1.5 text-xs aria-pressed:border-data aria-pressed:bg-active"
    >
      {icon}
      {label}
      {chip.disabledReason && <span className="sr-only"> ({chip.disabledReason})</span>}
    </Button>
  );
}

/**
 * Three capability chips (web search, collected data, attach). They flip the same composer options as the + menu or open
 * the file picker; they never send a message.
 *
 * @param props - The chip models for this chat.
 * @returns A labelled group, or null when no capability exists.
 */
function CapabilityChips({ capabilities }: { capabilities: EmptyCapabilities }) {
  const t = useTranslations('chat');
  const { webSearch, dbQuery, attach } = capabilities;
  if (!webSearch && !dbQuery && !attach) return null;
  return (
    <div data-testid="capability-chips" role="group" aria-label={t('capabilitiesLabel')} className="flex flex-wrap gap-2">
      {webSearch && <CapabilityChipButton testId="capability-web" icon={<GlobeIcon className="size-3.5" aria-hidden="true" />} label={t('capabilityWebSearch')} chip={webSearch} />}
      {dbQuery && <CapabilityChipButton testId="capability-data" icon={<DatabaseIcon className="size-3.5" aria-hidden="true" />} label={t('capabilityData')} chip={dbQuery} />}
      {attach && <CapabilityChipButton testId="capability-attach" icon={<PaperclipIcon className="size-3.5" aria-hidden="true" />} label={t('capabilityAttach')} chip={attach} />}
    </div>
  );
}

/**
 * Composer-first empty state: one sentence about what Chat can do with this workspace plus starter prompts
 * (the owner's top topics, else three fixed ones) as plain outline buttons. Prompts are workspace-relevant message keys; selecting one sends it like a typed message.
 *
 * @param props - Send callback, active locale and the optional capability chips (full Chat).
 * @returns Left-aligned intro block with a labelled prompt group and the capability chips.
 */
function EmptyIntro({ onSendPrompt, locale, capabilities }: { onSendPrompt: (prompt: string) => void; locale?: AppLocaleId; capabilities?: EmptyCapabilities }) {
  const t = useTranslations('chat');
  const copy = genUiCopy(resolveWidgetLocale(locale));
  // Real topics replace the fixed chips; empty/failed load keeps the fixed ones.
  const topics = useTopicNames();
  return (
    <div className="my-auto flex flex-col gap-4 py-8">
      <div className="flex flex-col gap-1">
        <h2 className="text-lg font-semibold text-foreground">{t('emptyStateTitle')}</h2>
        <p className="max-w-prose text-sm text-muted-foreground">{t('emptyStateDescription')}</p>
      </div>
      <div data-testid="starter-suggestions" role="group" aria-label={copy.shellSuggestionsLabel} className="flex flex-wrap gap-2">
        {topics.length > 0
          ? topics.map((topic) => (
            <Button key={topic} type="button" variant="outline" className="h-auto whitespace-normal text-left" onClick={() => onSendPrompt(t('starterTopicPrompt', { topic }))}>
              {t('starterTopicTitle', { topic })}
            </Button>
          ))
          : STARTER_PROMPTS.map(({ title, prompt }) => (
            <Button key={title} type="button" variant="outline" className="h-auto whitespace-normal text-left" onClick={() => onSendPrompt(t(prompt))}>
              {t(title)}
            </Button>
          ))}
      </div>
      {capabilities && <CapabilityChips capabilities={capabilities} />}
    </div>
  );
}

export interface ChatTranscriptProps {
  /** Currently selected conversation, used to reset the reading position between threads. */
  conversationId?: string | null;
  /** Session CSRF token enabling server-side widget templates (omit for browser-only). */
  csrfToken?: string;
  /** Surface mode; advanced mutation actions are available only on the full Chat page. */
  mode?: 'drawer' | 'full';
  /** Ordered list of persistent messages in the conversation. */
  messages: ChatMessage[];
  /** Active response generation streaming text, if currently in flight. */
  streamingText?: string;
  /** Plain-text reasoning ("thinking") streamed for the active run before the answer text. */
  streamingReasoning?: string;
  /** True after a user- or server-side cancellation; shown as a neutral status line, not an error. */
  cancelled?: boolean;
  /** Citations retrieved for the active response run, if available. */
  streamingCitations?: Citation[];
  /** Web search outcome streamed for the active run. */
  streamingWebSearch?: WebSearchOutcome | null;
  /** Server tool currently running for the active run; shown as a spinner status line. */
  activeTool?: ToolStatus['tool'] | null;
  /** Active streaming Generative UI widget parts in arrival order (one per `part_id`). */
  streamingUi?: UiPart[];
  /** Whether response generation is actively streaming tokens. */
  isStreaming?: boolean;
  /** Whether a response run has been dispatched and is awaiting initial tokens. */
  isPending?: boolean;
  /** Error message string if generation or sending encountered a failure. */
  error?: string | null;
  /** Callback to retry sending after an error. */
  onRetry?: () => void;
  /** Starts an append-only edit from a user prompt in full Chat. */
  onEditMessage?: (message: ChatMessage) => void;
  /** Starts an append-only regeneration from an assistant answer in full Chat. */
  onRegenerateMessage?: (message: ChatMessage) => void;
  /** Optional callback when user clicks an inline citation chip; defaults to opening the evidence rail in full Chat. */
  onSelectCitation?: (citation: Citation) => void;
  /** Capability chips for the empty state (full Chat); each toggles the matching composer option or opens the file picker. */
  capabilities?: EmptyCapabilities;
  /** Optional callback invoked when an interactive widget or starter template dispatches a prompt. */
  onSendPrompt?: (prompt: string) => void;
  /**
   * Promise-returning prompt handler for widgets that can retry (form). It rejects when the host is busy or the
   * send failed, which unlocks the form with a retryable error; other widgets keep the fire-and-forget handler.
   */
  onWidgetSendPrompt?: (prompt: string) => void | Promise<unknown>;
}

/** Props shared by the per-message pieces. */
interface MessageViewProps {
  msg: ChatMessage;
  mode: 'drawer' | 'full';
  locale: AppLocaleId;
}

/**
 * Localized label describing how a message relates to the one it revises (regenerated / edited), or null.
 *
 * @param msg - Message being rendered.
 * @param revised - The message it revises, when loaded.
 * @param t - `chat` translator.
 */
function relationshipLabelFor(msg: ChatMessage, revised: ChatMessage | undefined, t: ReturnType<typeof useTranslations>): string | null {
  const isUser = msg.role === 'user';
  if (revised?.role === 'assistant') return t(isUser ? 'regeneratedPrompt' : 'regeneratedAnswer');
  if (revised?.role === 'user') return t(isUser ? 'editedPrompt' : 'editedPromptAnswer');
  return null;
}

/**
 * Text part of a message: speaker label, revision relationship, web search notice, reasoning and the markdown body.
 *
 * @param props - Message, host mode, locale, the optional relationship label and the inline-citation click handler.
 */
function MessageBody({ msg, mode, locale, relationshipLabel, onSelectCitation }: MessageViewProps & { relationshipLabel: string | null; onSelectCitation?: (citation: Citation) => void }) {
  const t = useTranslations('chat');
  const isUser = msg.role === 'user';
  return (
    <div
      className={isUser ? 'rounded-lg bg-raised px-4 py-3 text-sm text-foreground' : 'text-sm text-foreground'}
    >
      <span className="sr-only">{isUser ? t('userSpeaker') : msg.model_identity || t('assistantFallback')}</span>

      {relationshipLabel && (
        <p className="text-xs text-muted-foreground" aria-label={relationshipLabel}>
          {relationshipLabel}
        </p>
      )}

      {/* Search info precedes reasoning and the answer so the answer sits right above the action row. */}
      {mode === 'full' && !isUser && <WebSearchNotice outcome={msg.web_search} />}

      {!isUser && msg.reasoning ? <ChatReasoning text={msg.reasoning} /> : null}

      {isUser
        ? <ChatMarkdown content={msg.content} locale={locale} />
        : <RevealMarkdown content={msg.content} citations={msg.citations} locale={locale} onSelectCitation={onSelectCitation} />}
    </div>
  );
}

/**
 * One generative-UI widget part (committed or streaming) and, in full Chat, the save-as-template action for sandboxed UI.
 *
 * @param props - The raw part, test id, prompt handler, host mode, locale and CSRF token.
 */
function WidgetPart({ part, testId, onSendPrompt, mode, locale, csrfToken, allowSave, citations }: {
  part: UiPart;
  testId: string;
  onSendPrompt?: (prompt: string) => void | Promise<unknown>;
  mode: 'drawer' | 'full';
  locale: AppLocaleId;
  csrfToken?: string;
  allowSave: boolean;
  /** Grounded citations of the owning message, used by rich answers for source chips. */
  citations?: Citation[];
}) {
  const sandbox = (part as { sandbox?: Record<string, unknown> }).sandbox;
  const canSave = allowSave && mode === 'full' && (part as { kind?: string }).kind === 'sandboxed_ui' && sandbox;
  return (
    <div className="w-full" data-testid={testId}>
      <ToolRendering
        payload={part as unknown as GenerativeUIPayload}
        onSendPrompt={onSendPrompt}
        locale={locale}
        citations={citations}
      />
      {canSave && (
        <SaveTemplateButton
          sandbox={sandbox}
          title={(part as { title?: string }).title}
          locale={locale}
          csrfToken={csrfToken}
        />
      )}
    </div>
  );
}

/** Copy, edit and regenerate buttons under a message. */
function MessageActions({ msg, mode, locale, timezone, canEdit, canRegenerate, copy, onEditMessage, onRegenerateMessage }: MessageViewProps & {
  timezone: string;
  canEdit: boolean;
  canRegenerate: boolean;
  copy: ReturnType<typeof useCopyMessage>;
  onEditMessage?: (message: ChatMessage) => void;
  onRegenerateMessage?: (message: ChatMessage) => void;
}) {
  const t = useTranslations('chat');
  const isUser = msg.role === 'user';
  const { copyMessage, copiedMessageId, failedMessageId } = copy;
  const formattedDate = msg.created_at ? formatDateTime(msg.created_at, locale, timezone) : null;
  return (
    <div className="flex flex-wrap items-center gap-2">
      {formattedDate && <span className="text-xs text-muted-foreground">{formattedDate}</span>}
      {/* Expose one-click copy for all full-page messages and completed assistant messages in the drawer. */}
      {(mode === 'full' || (!isUser && Boolean(msg.content))) && <Button
        type="button"
        variant="ghost"
        onClick={() => void copyMessage(msg.id, msg.content)}
        className="min-h-8 gap-1 px-2 text-xs font-normal text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        aria-label={t('copyMessage')}
      >
        {copiedMessageId === msg.id ? <CheckIcon className="size-3.5" /> : <CopyIcon className="size-3.5" />}
        <span>{copiedMessageId === msg.id ? t('copiedMessage') : t('copyMessage')}</span>
      </Button>}
      {(mode === 'full' || !isUser) && failedMessageId === msg.id && (
        <span role="status" className="text-xs text-destructive">{t('copyFailed')}</span>
      )}
      {canEdit && (
        <Button
          type="button"
          variant="ghost"
          className="min-h-8 gap-1 px-2 text-xs font-normal text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          onClick={() => onEditMessage?.(msg)}
          aria-label={t('editPrompt')}
        >
          <PencilIcon className="size-3.5" />
          <span>{t('editPrompt')}</span>
        </Button>
      )}
      {canRegenerate && (
        <Button
          type="button"
          variant="ghost"
          className="min-h-8 gap-1 px-2 text-xs font-normal text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          onClick={() => onRegenerateMessage?.(msg)}
          aria-label={t('regenerateAnswer')}
        >
          <RefreshCwIcon className="size-3.5" />
          <span>{t('regenerateAnswer')}</span>
        </Button>
      )}
    </div>
  );
}

/**
 * Model line under an assistant message. The drawer also shows the source count here; the full page shows sources as
 * the clickable {@link SourcesStrip} instead, so only the model name remains.
 */
function ModelLine({ msg, mode }: { msg: ChatMessage; mode: 'drawer' | 'full' }) {
  const t = useTranslations('chat');
  if (msg.role !== 'assistant' || !msg.model_identity) return null;
  return (
    <span data-testid="message-model" className="text-xs text-muted-foreground">
      {t('modelLine', { model: msg.model_identity })}{mode === 'drawer' && msg.citations?.length ? ` · ${t('sourcesCount', { count: msg.citations.length })}` : ''}
    </span>
  );
}

/**
 * Compact "N sources" button under a grounded assistant answer; opens the evidence rail on that answer's sources.
 *
 * @param props - The message, host mode and the open handler (absent outside full Chat).
 * @returns The strip, or null for user turns, the drawer and answers that cite nothing.
 */
function SourcesStrip({ msg, mode, onOpen }: { msg: ChatMessage; mode: 'drawer' | 'full'; onOpen?: (citations: Citation[]) => void }) {
  const t = useTranslations('chat');
  const citations = msg.citations;
  if (mode !== 'full' || msg.role !== 'assistant' || !citations?.length || !onOpen) return null;
  return (
    <Button
      type="button"
      variant="outline"
      data-testid="sources-strip"
      onClick={() => onOpen(citations)}
      className="min-h-8 w-fit gap-1.5 px-2.5 text-xs font-normal text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <BookOpenIcon className="size-3.5" aria-hidden="true" />
      {t('sourcesCount', { count: citations.length })}
    </Button>
  );
}

/** Props of {@link TranscriptMessage}. */
interface TranscriptMessageProps extends MessageViewProps {
  timezone: string;
  csrfToken?: string;
  revised?: ChatMessage;
  isRevised: boolean;
  copy: ReturnType<typeof useCopyMessage>;
  widgetPromptHandler: (part: unknown) => ((prompt: string) => void | Promise<unknown>) | undefined;
  onEditMessage?: (message: ChatMessage) => void;
  onRegenerateMessage?: (message: ChatMessage) => void;
  /** Inline citation chip click (full Chat); undefined leaves chips as plain links. */
  onSelectCitation?: (citation: Citation) => void;
  /** "N sources" strip click (full Chat); the strip is hidden without it. */
  onOpenSources?: (citations: Citation[]) => void;
}

/**
 * One committed message: body, widgets, actions and citations/model line. Edit and regenerate are offered only in full
 * Chat for messages that have a response id and have not already been revised.
 *
 * @param props - See {@link TranscriptMessageProps}.
 */
function TranscriptMessage(props: TranscriptMessageProps) {
  const { msg, mode, locale, csrfToken, revised, isRevised, widgetPromptHandler, onEditMessage, onRegenerateMessage, onSelectCitation, onOpenSources } = props;
  const t = useTranslations('chat');
  const isUser = msg.role === 'user';
  const canEdit = mode === 'full' && isUser && Boolean(msg.response_id) && !isRevised && Boolean(onEditMessage);
  const canRegenerate = mode === 'full' && !isUser && msg.role === 'assistant'
    && Boolean(msg.response_id) && !isRevised && Boolean(onRegenerateMessage);
  return (
    <div
      data-role={msg.role}
      data-message-id={msg.id}
      className={`flex flex-col gap-2.5 ${isUser ? 'ml-auto max-w-[80%] items-end' : 'mr-auto w-full'}`}
    >
      <MessageBody msg={msg} mode={mode} locale={locale} relationshipLabel={relationshipLabelFor(msg, revised, t)} onSelectCitation={onSelectCitation} />

      {/* Widget parts render in order after the text; older rows carry only `generative_ui`. */}
      {!isUser && messageUiParts(msg).map((part, index) => (
        <WidgetPart
          key={uiPartKey(part, index)}
          part={part}
          testId="generative-ui-widget"
          onSendPrompt={widgetPromptHandler(part)}
          mode={mode}
          locale={locale}
          csrfToken={csrfToken}
          allowSave
          citations={msg.citations}
        />
      ))}

      <SourcesStrip msg={msg} mode={mode} onOpen={onOpenSources} />

      <MessageActions {...props} canEdit={canEdit} canRegenerate={canRegenerate} />

      <ModelLine msg={msg} mode={mode} />
    </div>
  );
}

/** Props of {@link StreamingBubble}. */
interface StreamingBubbleProps {
  mode: 'drawer' | 'full';
  locale: AppLocaleId;
  csrfToken?: string;
  streamingText?: string;
  streamingReasoning: string;
  streamingCitations?: Citation[];
  streamingWebSearch?: WebSearchOutcome | null;
  activeTool: ToolStatus['tool'] | null;
  streamingUi: UiPart[];
  isStreaming: boolean;
  widgetPromptHandler: (part: unknown) => ((prompt: string) => void | Promise<unknown>) | undefined;
}

/** Status line of the streaming bubble: spinner plus tool / streaming / thinking copy. */
function StreamingStatus({ activeTool, label }: { activeTool: ToolStatus['tool'] | null; label: string }) {
  return (
    <div role="status" aria-live="polite" className="mb-1">
      <div data-testid={activeTool ? 'tool-status' : undefined} className="flex items-center gap-2 text-xs text-muted-foreground">
        <Loader2Icon className="size-4 animate-spin text-muted-foreground" />
        <span>{label}</span>
      </div>
    </div>
  );
}

/**
 * Active streaming assistant bubble: one spinner status row on top (send -> first token, and while tools run), then the
 * live reasoning, markdown, widgets and citations.
 *
 * @param props - See {@link StreamingBubbleProps}.
 */
function StreamingBubble(props: StreamingBubbleProps & { isPending: boolean }) {
  const { mode, locale, csrfToken, streamingText, streamingReasoning, streamingCitations, streamingWebSearch, activeTool, streamingUi, isStreaming, widgetPromptHandler } = props;
  const t = useTranslations('chat');
  const smoothText = useSmoothedText(streamingText || '');
  const label = activeTool
    ? t(activeTool === 'web_search' ? 'toolSearchingWeb' : 'toolQueryingData')
    : (isStreaming || streamingText || streamingReasoning ? t('streaming') : t('thinking'));
  return (
    <div data-streaming-answer className="mr-auto flex w-full scroll-mt-4 flex-col gap-2.5">
      <div className="text-sm text-foreground">
        {/* Live region stays mounted so tool-status changes are announced politely. */}
        <StreamingStatus activeTool={activeTool} label={label} />

        {mode === 'full' && <WebSearchNotice outcome={streamingWebSearch} />}

        <ChatReasoning text={streamingReasoning} streaming answerStarted={Boolean(streamingText) || streamingUi.length > 0} />

        <ChatMarkdown content={smoothText} citations={streamingCitations} locale={locale} />
      </div>
      {streamingUi.map((part, index) => (
        <WidgetPart
          key={uiPartKey(part, index)}
          part={part}
          testId="streaming-generative-ui-widget"
          onSendPrompt={widgetPromptHandler(part)}
          mode={mode}
          locale={locale}
          csrfToken={csrfToken}
          allowSave={false}
          citations={streamingCitations}
        />
      ))}
    </div>
  );
}

/** Error banner with an optional Retry. */
function TranscriptError({ error, onRetry }: { error: string; onRetry?: () => void }) {
  const t = useTranslations('chat');
  return (
    <div role="alert" className="flex items-center justify-between gap-2 rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive">
      <div className="flex items-center gap-2">
        <AlertCircleIcon className="size-4 shrink-0" />
        <span>{error}</span>
      </div>
      {onRetry && (
        <Button type="button" variant="destructive" size="sm" onClick={onRetry} className="min-h-8 px-2 py-1 text-xs font-medium">
          {t('retry')}
        </Button>
      )}
    </div>
  );
}

/** Starter gallery (or plain empty copy) for a brand-new view. */
function EmptyState({ onSendPrompt, locale, capabilities }: { onSendPrompt?: (prompt: string) => void; locale: AppLocaleId; capabilities?: EmptyCapabilities }) {
  const t = useTranslations('chat');
  if (onSendPrompt) return <EmptyIntro onSendPrompt={onSendPrompt} locale={locale} capabilities={capabilities} />;
  return (
    <div className="my-auto flex flex-col gap-1 py-8">
      <h2 className="text-base font-semibold text-foreground">{t('emptyStateTitle')}</h2>
      <p className="text-sm text-muted-foreground">{t('emptyStateDescription')}</p>
    </div>
  );
}

/**
 * Applies the transcript's optional-prop defaults in one place (keeps the component's branching low).
 *
 * @param props - Raw component props.
 * @returns The props with drawer mode, empty streaming fields and no error as defaults.
 */
function withTranscriptDefaults(props: ChatTranscriptProps) {
  return {
    mode: props.mode ?? 'drawer',
    streamingReasoning: props.streamingReasoning ?? '',
    cancelled: props.cancelled ?? false,
    activeTool: props.activeTool ?? null,
    streamingUi: props.streamingUi ?? [],
    isStreaming: props.isStreaming ?? false,
    isPending: props.isPending ?? false,
    error: props.error ?? null,
  };
}

/**
 * Which optional regions render: the starter gallery only for a brand-new view (once a conversation exists it never
 * reappears between turns), and the streaming bubble while anything is in flight.
 *
 * @param props - Raw transcript props.
 * @param d - Defaulted props.
 */
function transcriptVisibility(props: ChatTranscriptProps, d: ReturnType<typeof withTranscriptDefaults>) {
  const { conversationId, messages, streamingText } = props;
  return {
    showStarter: !conversationId && messages.length === 0 && !streamingText && d.streamingUi.length === 0 && !d.isPending,
    showStreaming: Boolean(streamingText || d.streamingReasoning || d.streamingUi.length > 0 || d.isStreaming || d.isPending || d.activeTool),
  };
}

/**
 * View model of the transcript: display preferences, scroll behaviour, copy state and revision lookups.
 *
 * @param props - Transcript props.
 * @param busy - True while pending or streaming (drives scroll following).
 */
function useTranscriptModel(props: ChatTranscriptProps, busy: boolean) {
  const { conversationId, messages, onSendPrompt, onWidgetSendPrompt } = props;
  const display = useDisplayPreferences();
  const timezone = display.confirmedPreferences?.timezone || 'UTC';
  const locale = display.confirmedPreferences?.locale || 'en-us';
  const last = messages[messages.length - 1];
  const scroll = useChatScroll({
    conversationId,
    messageCount: messages.length,
    lastMessageId: last?.id,
    lastMessageRole: last?.role,
    isStreaming: busy,
    contentSignal: `${messages.length}:${last?.id ?? ''}:${props.streamingText?.length ?? 0}:${props.streamingUi?.length ?? 0}`,
  });
  const copy = useCopyMessage(conversationId);
  const messageById = React.useMemo(() => new Map(messages.map((message) => [message.id, message])), [messages]);
  const revisedMessageIds = React.useMemo(
    () => new Set(messages.flatMap((message) => message.revision_of_message_id ? [message.revision_of_message_id] : [])),
    [messages],
  );
  /** The message that `msg` revises, when loaded. */
  const revisedOf = (msg: ChatMessage) => (msg.revision_of_message_id ? messageById.get(msg.revision_of_message_id) : undefined);
  /** Form widgets get the rejecting handler (retryable errors); every other widget keeps the plain one. */
  const widgetPromptHandler = (part: unknown) =>
    (part as { kind?: string }).kind === 'form' && onWidgetSendPrompt ? onWidgetSendPrompt : onSendPrompt;
  return { locale, timezone, copy, revisedOf, revisedMessageIds, widgetPromptHandler, ...scroll };
}

/**
 * Conversation transcript rendering ordered messages, append-only revision relationships,
 * full-page copy/edit/regenerate actions, full-page grounded citations, streaming deltas,
 * Generative UI widgets (Sandboxed UI and Native Structured Widgets), and error states.
 *
 * @param props - ChatTranscriptProps interface; mutation actions are rendered only for full Chat.
 * @returns Accessible conversation transcript viewport.
 */
export function ChatTranscript(props: ChatTranscriptProps) {
  const { csrfToken, messages, streamingText, streamingCitations, streamingWebSearch, onRetry, onEditMessage, onRegenerateMessage, onSendPrompt } = props;
  const { mode, streamingReasoning, cancelled, activeTool, streamingUi, isStreaming, isPending, error } = withTranscriptDefaults(props);
  const t = useTranslations('chat');
  const m = useTranscriptModel(props, isStreaming || isPending);
  const { locale, timezone, copy, containerRef, scrollHandlers, isAtBottom, hasUnread, jumpToLatest, widgetPromptHandler } = m;
  const { showStarter, showStreaming } = transcriptVisibility(props, withTranscriptDefaults(props));
  // Full Chat shows the evidence rail, so chip and strip clicks open it; the drawer has no rail and keeps plain links.
  const onOpenSources = mode === 'full' ? focusCitations : undefined;
  const onSelectCitation = props.onSelectCitation ?? (onOpenSources ? (citation: Citation) => onOpenSources([citation]) : undefined);

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
    <div
      ref={containerRef}
      {...scrollHandlers}
      className="flex-1 overflow-y-auto p-4 flex flex-col gap-4 focus:outline-none"
      role="log"
      aria-live="polite"
      // aria-busy keeps assistive tech from announcing every streamed token; the settled message is announced once.
      aria-busy={isStreaming || isPending}
      aria-label={t('title')}
    >
      {/* Readable measure: one centred 720px column inside the transcript region. */}
      <div className="mx-auto flex w-full max-w-[720px] flex-1 flex-col gap-4">

      {/* Starter gallery is only for a brand-new view: once a conversation exists it never reappears between turns. */}
      {showStarter && <EmptyState onSendPrompt={onSendPrompt} locale={locale} capabilities={props.capabilities} />}

      {messages.map((msg) => (
        <TranscriptMessage
          key={msg.id}
          msg={msg}
          mode={mode}
          locale={locale}
          timezone={timezone}
          csrfToken={csrfToken}
          revised={m.revisedOf(msg)}
          isRevised={m.revisedMessageIds.has(msg.id)}
          copy={copy}
          widgetPromptHandler={widgetPromptHandler}
          onEditMessage={onEditMessage}
          onRegenerateMessage={onRegenerateMessage}
          onSelectCitation={onSelectCitation}
          onOpenSources={onOpenSources}
        />
      ))}

      {showStreaming && (
        <StreamingBubble
          mode={mode}
          locale={locale}
          csrfToken={csrfToken}
          streamingText={streamingText}
          streamingReasoning={streamingReasoning}
          streamingCitations={streamingCitations}
          streamingWebSearch={streamingWebSearch}
          activeTool={activeTool}
          streamingUi={streamingUi}
          isStreaming={isStreaming}
          isPending={isPending}
          widgetPromptHandler={widgetPromptHandler}
        />
      )}

      {/* Cancellation is a neutral outcome chosen by the user (or the server), not a failure: no alert, no Retry. */}
      {cancelled && !error && (
        <p role="status" data-testid="chat-cancelled" className="text-xs text-muted-foreground">{t('cancelled')}</p>
      )}

      {error && <TranscriptError error={error} onRetry={onRetry} />}
      </div>
    </div>
      {!isAtBottom && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={jumpToLatest}
          className="absolute bottom-3 left-1/2 z-10 min-h-8 -translate-x-1/2 gap-1 rounded-full bg-surface px-3 text-xs"
        >
          <ArrowDownIcon className="size-3.5" aria-hidden="true" />
          {t('jumpToLatest')}
          {hasUnread && (
            <>
              <span data-testid="jump-unread-dot" aria-hidden="true" className="size-2 rounded-full bg-data" />
              <span className="sr-only">{t('jumpToLatestUnread')}</span>
            </>
          )}
        </Button>
      )}
    </div>
  );
}
