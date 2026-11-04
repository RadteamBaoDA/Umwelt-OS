'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { MessageSquare, RefreshCw, Sparkles } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { ApiError, apiRequest } from '@/core/api';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { DateSelector } from './date-selector';
import { dailyKeys, generateBrief, getDailyContext, type DailyBriefRevision, type DailyContextData, type DailyWidgetData } from './daily-api';
import { useChatController } from '@/core/app-shell/chat-controller';
import { useSelectedDay } from './selected-day';
import { useWorkspace } from '@/core/workspace-context';
import { BriefShareButton, SharedBriefs } from './brief-sharing';
import { BriefText } from './brief-text';

export { BriefText };

/** Renders one current-record widget summary with its own updated-at and source status. */
function WidgetSummary({ widget }: { widget: DailyWidgetData }) {
  const t = useTranslations('daily');
  const display = useDisplayPreferences();
  const titles = widget.items.slice(0, 5).map((item) => String(item.title));
  return (
    <section aria-label={t(widget.title_key)} className="py-2.5 text-xs">
      <header className="flex items-center justify-between gap-2">
        <h4 className="font-semibold">{t(widget.title_key)}</h4>
        <span className="text-muted-foreground">{t('itemCount', { count: widget.items.length })}</span>
      </header>
      {widget.status === 'not_applicable' && <p className="mt-1 text-muted-foreground">{t('futureNoNews')}</p>}
      {widget.status === 'empty' && <p className="mt-1 text-muted-foreground">{t('emptyWidget')}</p>}
      {widget.status === 'unavailable' && <p role="alert" className="mt-1 text-destructive">{t('widgetUnavailable')}</p>}
      {titles.length > 0 && <ul className="mt-1 list-disc space-y-0.5 pl-4">{titles.map((title, index) => <li key={`${index}-${title}`}><span className="line-clamp-1">{title}</span></li>)}</ul>}
      {widget.source_status === 'manual_or_imported_only' && <p className="mt-1 text-muted-foreground">{t('manualEventsOnly')}</p>}
      {widget.updated_at && <p className="mt-1 text-muted-foreground">{t('updatedAt', { time: formatDateTime(widget.updated_at, display.locale, display.timezone) })}</p>}
    </section>
  );
}

/** Selected-day display preferences passed to the brief sections. */
type DisplayPrefs = ReturnType<typeof useDisplayPreferences>;

/**
 * Loads the selected day's daily context and owns the owner-only generate mutation.
 *
 * Side effects: fetches `/api/v1/auth/session` (CSRF token) and the daily context (only when the viewer may read
 * the owner workspace); after generation settles it invalidates the context query of the selected day.
 *
 * @param date Selected local date.
 * @param timezone Selected IANA timezone.
 * @param canReadOwnerWorkspace Whether the viewer is the owner or a read-only administrator.
 * @returns Session, context and generate mutation plus the translation key of the last generation error.
 */
function useDailyBrief(date: string, timezone: string, canReadOwnerWorkspace: boolean) {
  const client = useQueryClient();
  const session = useQuery({ queryKey: ['session'], queryFn: () => apiRequest<{ authenticated: true; csrfToken: string }>('/api/v1/auth/session') });
  const context = useQuery({
    queryKey: dailyKeys.context(date, timezone),
    queryFn: ({ signal }) => getDailyContext(date, timezone, signal),
    enabled: canReadOwnerWorkspace,
  });
  const generate = useMutation({
    mutationFn: () => generateBrief(date, timezone, session.data!.csrfToken),
    onSettled: () => client.invalidateQueries({ queryKey: dailyKeys.context(date, timezone) }),
  });
  const errorKey = generate.error instanceof ApiError
    ? (generate.error.status === 409 ? 'generateNoInputs' : 'generateUnavailable')
    : generate.error ? 'generateUnavailable' : null;
  return { session, context, generate, errorKey };
}

/** Saved-brief heading row: title plus revision metadata when a brief exists. */
function SavedBriefHeader({ data, display }: { data: DailyContextData; display: DisplayPrefs }) {
  const t = useTranslations('daily');
  const brief = data.brief ?? null;
  return (
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 id="saved-brief-heading" className="flex items-center gap-1.5 text-sm font-semibold">
        <Sparkles className="h-4 w-4 text-muted-foreground" aria-hidden />
        {t('savedBrief')}
      </h3>
      {brief && <span className="text-xs text-muted-foreground">{t('revisionMeta', { revision: brief.revision, total: data.brief_revisions, time: formatDateTime(brief.generated_at, display.locale, display.timezone) })}</span>}
    </div>
  );
}

/** Saved brief body: stale notice, text and citations, or the empty message. */
function SavedBriefBody({ brief }: { brief: DailyBriefRevision | null }) {
  const t = useTranslations('daily');
  if (!brief) return <p className="text-sm text-muted-foreground">{t('noBrief')}</p>;
  return (
    <>
      {brief.status === 'stale' && <p role="status" className="rounded border border-destructive p-2 text-xs text-destructive">{t('staleBrief')}</p>}
      <BriefText key={`${brief.id}:${brief.revision}`} brief={brief} />
      {brief.citations.length > 0 && (
        <ol className="space-y-0.5 text-xs text-muted-foreground" aria-label={t('citations')}>
          {brief.citations.map((citation) => <li key={citation.ref}>[{citation.ref}] {citation.title}</li>)}
        </ol>
      )}
    </>
  );
}

/** Owner-only action row: generate/regenerate, ask about the day and share. */
function BriefOwnerActions({ brief, canGenerate, generating, onGenerate }: {
  brief: DailyBriefRevision | null;
  canGenerate: boolean;
  generating: boolean;
  onGenerate: () => void;
}) {
  const t = useTranslations('daily');
  const chat = useChatController();
  const { date, timezone } = useSelectedDay();
  return (
    <div className="flex flex-wrap gap-2">
      <Button type="button" size="sm" disabled={!canGenerate || generating} onClick={onGenerate}>
        <RefreshCw className="h-3.5 w-3.5" aria-hidden />
        {generating ? t('generating') : brief ? t('regenerate') : t('generate')}
      </Button>
      <Button type="button" size="sm" variant="secondary" onClick={() => { chat.selectDay({ date, timezone }); chat.openDrawer(); }}>
        <MessageSquare className="h-3.5 w-3.5" aria-hidden />
        {t('askAboutDay')}
      </Button>
      {brief && <BriefShareButton brief={brief} />}
    </div>
  );
}

/** Current-record summaries section. */
function CurrentRecords({ data, display }: { data: DailyContextData; display: DisplayPrefs }) {
  const t = useTranslations('daily');
  return (
    <section aria-labelledby="current-records-heading" className="space-y-2">
      <h3 id="current-records-heading" className="text-sm font-semibold">{t('currentRecords')}</h3>
      <p className="text-xs text-muted-foreground">{t('currentRecordsNote', { time: formatDateTime(data.widgets_updated_at, display.locale, display.timezone) })}</p>
      <div className="divide-y divide-border">
        {data.widgets.map((widget) => <WidgetSummary key={widget.id} widget={widget} />)}
      </div>
    </section>
  );
}

/** Owner/admin body: loading and error notices, saved brief section and current records. */
function OwnerBriefBody({ context, session, generate, errorKey, isOwner, display }: {
  context: ReturnType<typeof useDailyBrief>['context'];
  session: ReturnType<typeof useDailyBrief>['session'];
  generate: ReturnType<typeof useDailyBrief>['generate'];
  errorKey: string | null;
  isOwner: boolean;
  display: DisplayPrefs;
}) {
  const t = useTranslations('daily');
  const brief = context.data?.brief ?? null;
  return (
    <>
      {context.isLoading && <p role="status" className="text-sm text-muted-foreground">{t('loading')}</p>}
      {context.isError && <p role="alert" className="text-sm text-destructive">{t('loadError')}</p>}
      {context.data && (
        <>
          <section aria-labelledby="saved-brief-heading" className="space-y-2 border-b border-border pb-3">
            <SavedBriefHeader data={context.data} display={display} />
            <SavedBriefBody brief={brief} />
            {errorKey && <p role="alert" className="text-xs text-destructive">{t(errorKey)}</p>}
            {isOwner && <BriefOwnerActions brief={brief} canGenerate={!!session.data} generating={generate.isPending} onGenerate={() => generate.mutate()} />}
          </section>
          <CurrentRecords data={context.data} display={display} />
        </>
      )}
    </>
  );
}

/**
 * Daily brief gadget body for owners and read-only administrators: saved revisions and current records
 * stay visible, while generation, chat, and sharing actions remain owner-only.
 */
export function DailyBrief() {
  const t = useTranslations('daily');
  const display = useDisplayPreferences();
  const { date, timezone } = useSelectedDay();
  const { isOwner, isAdminViewer } = useWorkspace();
  const canReadOwnerWorkspace = isOwner || isAdminViewer;
  const { session, context, generate, errorKey } = useDailyBrief(date, timezone, canReadOwnerWorkspace);

  return (
    <div className="flex h-full min-h-0 flex-col gap-3 overflow-y-auto p-3.5 pb-4">
      <header className="flex flex-nowrap items-center justify-between gap-2">
        <DateSelector />
        {canReadOwnerWorkspace && context.data && context.data.relation !== 'today' && <span className="rounded bg-secondary px-2 py-0.5 text-xs font-semibold">{t(`relation_${context.data.relation}`)}</span>}
      </header>
      {!canReadOwnerWorkspace && <SharedBriefs date={date} timezone={timezone} />}
      {canReadOwnerWorkspace && <OwnerBriefBody context={context} session={session} generate={generate} errorKey={errorKey} isOwner={isOwner} display={display} />}
    </div>
  );
}
