'use client';

import { messageUiParts, uiPartKey, type UiPart } from './generative-ui-parts';
import * as React from 'react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { AlertCircleIcon, BotIcon, CheckIcon, CopyIcon, Loader2Icon, PencilIcon, RefreshCwIcon } from 'lucide-react';
import type { ChatMessage, Citation, WebSearchOutcome } from '@/modules/chat/api';
import { Button } from '@/components/ui/button';
import { CitationPanel, webHost } from './citation-panel';
import { safeHttpUrl } from '@/core/safe-url';
import { ChatMarkdown } from './chat-markdown';
import { useChatScroll } from './use-chat-scroll';
import { useCopyMessage } from './use-copy-message';
import { useDisplayPreferences } from '@/core/query-provider';
import { formatDateTime } from '@/core/i18n';
import { ToolRendering } from './generative-ui/components/tool-rendering';
import { GenerativeUIStarterGallery } from './generative-ui/templates';
import { SaveTemplateButton } from './generative-ui/templates/template-library';
import { ChatShellTools, StarterSuggestions } from './generative-ui/templates/shell-extras';
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

/** Announced notice for a requested web search that was skipped or unavailable; renders nothing otherwise. */
function WebSearchNotice({ outcome }: { outcome?: WebSearchOutcome | null }) {
  const t = useTranslations('chat');
  if (outcome?.status === 'used' && outcome.reason === 'no_results') {
    return <p data-testid="web-search-notice" className="mt-1 text-xs text-muted-foreground">{t('webNoResults')}</p>;
  }
  if (!outcome || (outcome.status !== 'unavailable' && outcome.status !== 'skipped')) return null;
  const reason = outcome.reason && KNOWN_REASONS.includes(outcome.reason) ? t(`webReason_${outcome.reason}`) : t('webReason_unknown');
  const title = outcome.status === 'skipped' ? t('webSkipped') : t('webUnavailable');
  return (
    <p data-testid="web-search-notice" className={`mt-1 text-xs ${outcome.status === 'unavailable' ? 'text-destructive' : 'text-muted-foreground'}`}>
      {t('webNotice', { title, reason })}
    </p>
  );
}

/**
 * Compact numbered source links for the quick-chat drawer, where the full citation panel is not shown.
 * Encodes document path parameters and validates external web URLs before rendering navigation links.
 *
 * @param props - Ordered citations attached to the assistant response.
 * @returns Ordered list of citation chips.
 */
function CitationChips({ citations }: { citations: Citation[] }) {
  const t = useTranslations('chat');
  return (
    <ol aria-label={t('sourceLinks')} className="mt-1 flex flex-wrap gap-2 border-t border-border pt-2 text-xs">
      {citations.map((citation, index) => {
        const chipClass = 'inline-flex min-h-11 max-w-[14rem] items-center gap-1 rounded-md border border-border px-2 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring';
        if (citation.sourceType === 'web') {
          const href = safeHttpUrl(citation.url);
          return (
            <li key={`web-${index}`}>
              {href ? (
                <a href={href} target="_blank" rel="noopener noreferrer nofollow" title={`${citation.title} (${webHost(citation.url)})`} className={chipClass}>
                  <sup className="font-semibold text-foreground">{index + 1}</sup>
                  <span className="truncate">{webHost(citation.url)}</span>
                </a>
              ) : (
                <span className={chipClass.replace('hover:text-foreground', '')}>
                  <sup className="font-semibold text-foreground">{index + 1}</sup>
                  <span className="truncate">{webHost(citation.url)}</span>
                </span>
              )}
            </li>
          );
        }
        return (
        <li key={`${citation.chunkId}-${index}`}>
          <Link
            href={`/knowledge/documents/${encodeURIComponent(citation.documentId)}?${new URLSearchParams({ versionId: citation.documentVersionId, chunkId: citation.chunkId })}#cited-chunk`}
            title={`${citation.title} (${t('viewCitation')})`}
            className={chipClass}
          >
            <sup className="font-semibold text-foreground">{index + 1}</sup>
            <span className="truncate">{citation.title}</span>
          </Link>
        </li>
        );
      })}
    </ol>
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
  /** Citations retrieved for the active response run, if available. */
  streamingCitations?: Citation[];
  /** Web search outcome streamed for the active run. */
  streamingWebSearch?: WebSearchOutcome | null;
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
  /** Optional callback when user clicks a citation badge. */
  onSelectCitation?: (citation: Citation) => void;
  /** Optional callback invoked when an interactive widget or starter template dispatches a prompt. */
  onSendPrompt?: (prompt: string) => void;
  /**
   * Promise-returning prompt handler for widgets that can retry (form). It rejects when the host is busy or the
   * send failed, which unlocks the form with a retryable error; other widgets keep the fire-and-forget handler.
   */
  onWidgetSendPrompt?: (prompt: string) => void | Promise<unknown>;
}

/**
 * Conversation transcript rendering ordered messages, append-only revision relationships,
 * full-page copy/edit/regenerate actions, full-page grounded citations, streaming deltas,
 * Generative UI widgets (Sandboxed UI and Native Structured Widgets), and error states.
 *
 * @param props - ChatTranscriptProps interface; mutation actions are rendered only for full Chat.
 * @returns Accessible conversation transcript viewport.
 */
export function ChatTranscript({
  conversationId,
  csrfToken,
  mode = 'drawer',
  messages,
  streamingText,
  streamingCitations,
  streamingWebSearch,
  streamingUi = [],
  isStreaming = false,
  isPending = false,
  error = null,
  onRetry,
  onEditMessage,
  onRegenerateMessage,
  onSendPrompt,
  onWidgetSendPrompt,
}: ChatTranscriptProps) {
  const t = useTranslations('chat');
  /** Form widgets get the rejecting handler (retryable errors); every other widget keeps the plain one. */
  const widgetPromptHandler = (part: unknown) =>
    (part as { kind?: string }).kind === 'form' && onWidgetSendPrompt ? onWidgetSendPrompt : onSendPrompt;
  const display = useDisplayPreferences();
  const timezone = display.confirmedPreferences?.timezone || 'UTC';
  const locale = display.confirmedPreferences?.locale || 'en-us';
  const { containerRef, scrollHandlers } = useChatScroll({
    conversationId,
    messageCount: messages.length,
    lastMessageId: messages[messages.length - 1]?.id,
    lastMessageRole: messages[messages.length - 1]?.role,
    isStreaming: isStreaming || isPending,
    // Reader mode on the full page: the new question is parked at the top, never chased while streaming.
    reader: mode === 'full',
  });
  const { copyMessage, copiedMessageId, failedMessageId } = useCopyMessage(conversationId);
  const messageById = React.useMemo(() => new Map(messages.map((message) => [message.id, message])), [messages]);
  const revisedMessageIds = React.useMemo(
    () => new Set(messages.flatMap((message) => message.revision_of_message_id ? [message.revision_of_message_id] : [])),
    [messages],
  );

  return (
    <div
      ref={containerRef}
      {...scrollHandlers}
      className="flex-1 overflow-y-auto p-4 flex flex-col gap-4 focus:outline-none"
      role="log"
      aria-live="polite"
      aria-label={t('title')}
    >
      {mode === 'full' && onSendPrompt && <ChatShellTools onSendPrompt={onSendPrompt} locale={locale} csrfToken={csrfToken} conversationId={conversationId} />}

      {messages.length === 0 && !streamingText && streamingUi.length === 0 && !isPending && (
        <div className="flex-1 flex flex-col items-center justify-center p-6 text-center text-muted-foreground my-auto gap-6">
          <div className="flex flex-col items-center">
            <div className="size-12 rounded-full bg-primary/10 text-primary flex items-center justify-center mb-3">
              <BotIcon className="size-6" />
            </div>
            <h3 className="text-base font-semibold text-foreground mb-1">
              {t('emptyStateTitle')}
            </h3>
            <p className="text-xs max-w-sm text-muted-foreground leading-normal">
              {t('emptyStateDescription')}
            </p>
          </div>
          {mode === 'full' && onSendPrompt && (
            <div className="w-full max-w-3xl text-left">
              <GenerativeUIStarterGallery onSelectPrompt={onSendPrompt} locale={locale} />
            </div>
          )}
          {mode === 'full' && onSendPrompt && <StarterSuggestions onSendPrompt={onSendPrompt} locale={locale} />}
        </div>
      )}

      {messages.map((msg) => {
        const isUser = msg.role === 'user';
        const revisedMessage = msg.revision_of_message_id
          ? messageById.get(msg.revision_of_message_id)
          : undefined;
        const relationshipLabel = revisedMessage?.role === 'assistant'
          ? t(isUser ? 'regeneratedPrompt' : 'regeneratedAnswer')
          : revisedMessage?.role === 'user'
            ? t(isUser ? 'editedPrompt' : 'editedPromptAnswer')
            : null;
        const canEdit = mode === 'full' && isUser && Boolean(msg.response_id)
          && !revisedMessageIds.has(msg.id) && Boolean(onEditMessage);
        const canRegenerate = mode === 'full' && !isUser && msg.role === 'assistant'
          && Boolean(msg.response_id) && !revisedMessageIds.has(msg.id) && Boolean(onRegenerateMessage);
        const formattedDate = msg.created_at
          ? formatDateTime(msg.created_at, locale, timezone)
          : null;

        return (
          <div
            key={msg.id}
            data-role={msg.role}
            data-message-id={msg.id}
            className={`flex flex-col gap-2.5 ${isUser ? 'ml-auto max-w-[80%] items-end' : 'mr-auto max-w-[88%]'}`}
          >
            <div
              className={isUser ? 'rounded-xl border border-border bg-background px-4 py-3 text-sm text-foreground' : 'text-sm text-foreground'}
            >
              <span className="sr-only">{isUser ? t('userSpeaker') : msg.model_identity || t('assistantFallback')}</span>

              {relationshipLabel && (
                <p className="text-xs text-muted-foreground" aria-label={relationshipLabel}>
                  {relationshipLabel}
                </p>
              )}

              {isUser
                ? <ChatMarkdown content={msg.content} locale={locale} />
                : <RevealMarkdown content={msg.content} citations={msg.citations} locale={locale} />}
            </div>

            {/* Widget parts render in order after the text; older rows carry only `generative_ui`. */}
            {!isUser && messageUiParts(msg).map((part, index) => (
              <div className="w-full" data-testid="generative-ui-widget" key={uiPartKey(part, index)}>
                <ToolRendering
                  payload={part as unknown as GenerativeUIPayload}
                  onSendPrompt={widgetPromptHandler(part)}
                  locale={locale}
                />
                {mode === 'full' && (part as { kind?: string }).kind === 'sandboxed_ui'
                  && (part as { sandbox?: Record<string, unknown> }).sandbox && (
                  <SaveTemplateButton
                    sandbox={(part as { sandbox: Record<string, unknown> }).sandbox}
                    title={(part as { title?: string }).title}
                    locale={locale}
                    csrfToken={csrfToken}
                  />
                )}
              </div>
            ))}

            <div className="flex flex-wrap items-center gap-2">
              {formattedDate && <span className="text-xs text-muted-foreground">{formattedDate}</span>}
              {/* Expose one-click copy for all full-page messages and completed assistant messages in the drawer. */}
              {(mode === 'full' || (!isUser && Boolean(msg.content))) && <button
                type="button"
                onClick={() => void copyMessage(msg.id, msg.content)}
                className="inline-flex min-h-8 items-center gap-1 rounded-md px-2 text-xs text-muted-foreground hover:bg-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                aria-label={t('copyMessage')}
              >
                {copiedMessageId === msg.id ? <CheckIcon className="size-3.5" /> : <CopyIcon className="size-3.5" />}
                <span>{copiedMessageId === msg.id ? t('copiedMessage') : t('copyMessage')}</span>
              </button>}
              {(mode === 'full' || !isUser) && failedMessageId === msg.id && (
                <span role="status" className="text-xs text-destructive">{t('copyFailed')}</span>
              )}
              {canEdit && (
                <Button
                  type="button"
                  className="secondary inline-flex min-h-8 items-center gap-1 px-2 text-xs"
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
                  className="secondary inline-flex min-h-8 items-center gap-1 px-2 text-xs"
                  onClick={() => onRegenerateMessage?.(msg)}
                  aria-label={t('regenerateAnswer')}
                >
                  <RefreshCwIcon className="size-3.5" />
                  <span>{t('regenerateAnswer')}</span>
                </Button>
              )}
            </div>

            {mode === 'drawer' && !isUser && msg.model_identity && (
              <span className="text-xs text-muted-foreground">
                {t('modelLine', { model: msg.model_identity })}{msg.citations?.length ? ` · ${t('sourcesCount', { count: msg.citations.length })}` : ''}
              </span>
            )}
            {mode === 'full' && !isUser && msg.citations && msg.citations.length > 0 && (
              <CitationPanel citations={msg.citations} variant="inline" />
            )}
            {mode === 'drawer' && !isUser && msg.citations && msg.citations.length > 0 && (
              <CitationChips citations={msg.citations} />
            )}
            {mode === 'full' && !isUser && <WebSearchNotice outcome={msg.web_search} />}
          </div>
        );
      })}

      {/* Pending status indicator */}
      {isPending && !streamingText && streamingUi.length === 0 && (
        <div className="mr-auto flex max-w-[88%] items-center gap-2 text-xs text-muted-foreground">
          <Loader2Icon className="size-4 animate-spin text-primary" />
          <span>{t('thinking')}</span>
        </div>
      )}

      {/* Active streaming assistant bubble */}
      {(streamingText || streamingUi.length > 0 || isStreaming) && (
        <div className="mr-auto flex max-w-[88%] flex-col gap-2.5">
          <div className="text-sm text-foreground">
            <div className="flex items-center gap-2 text-xs text-muted-foreground mb-0.5">
              <span className="inline-flex items-center gap-1.5 text-xs text-primary">
                <span className="size-2 rounded-full bg-primary animate-pulse" />
                {t('streaming')}
              </span>
            </div>

            <ChatMarkdown content={streamingText || ''} citations={streamingCitations} locale={locale} />

            {/* Always-mounted live region so the notice text change is announced. */}
            <div role="status">{mode === 'full' && <WebSearchNotice outcome={streamingWebSearch} />}</div>
          </div>
            {streamingUi.map((part, index) => (
              <div className="w-full" data-testid="streaming-generative-ui-widget" key={uiPartKey(part, index)}>
                <ToolRendering
                  payload={part as unknown as GenerativeUIPayload}
                  onSendPrompt={widgetPromptHandler(part)}
                  locale={locale}
                />
              </div>
            ))}
            {mode === 'full' && streamingCitations && streamingCitations.length > 0 && (
              <CitationPanel citations={streamingCitations} variant="inline" />
            )}
            {mode === 'drawer' && streamingCitations && streamingCitations.length > 0 && (
              <CitationChips citations={streamingCitations} />
            )}
        </div>
      )}

      {/* Error banner */}
      {error && (
        <div role="alert" className="flex items-center justify-between gap-2 rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive">
          <div className="flex items-center gap-2">
            <AlertCircleIcon className="size-4 shrink-0" />
            <span>{error}</span>
          </div>
          {onRetry && (
            <button
              type="button"
              onClick={onRetry}
              className="min-h-8 rounded bg-destructive px-2 py-1 text-xs font-medium text-destructive-foreground hover:opacity-90"
            >
              {t('retry')}
            </button>
          )}
        </div>
      )}

    </div>
  );
}
