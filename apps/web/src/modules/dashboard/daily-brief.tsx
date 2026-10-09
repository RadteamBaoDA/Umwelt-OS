'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { MessageSquare, RefreshCw, Sparkles } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { ApiError, apiRequest } from '@/core/api';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { DateSelector } from './date-selector';
import { dailyKeys, generateBrief, getDailyContext, type DailyWidgetData } from './daily-api';
import { useChatController } from '@/core/app-shell/chat-controller';
import { useSelectedDay } from './selected-day';
import { useWorkspace } from '@/core/workspace-context';
import { BriefShareButton, SharedBriefs } from './brief-sharing';

/** Renders one current-record widget summary with its own updated-at and source status. */
function WidgetSummary({ widget }: { widget: DailyWidgetData }) {
  const t = useTranslations('daily');
  const display = useDisplayPreferences();
  const titles = widget.items.slice(0, 5).map((item) => String(item.title));
  return (
    <section aria-label={t(widget.title_key)} className="rounded-lg border border-border p-2.5 text-xs">
      <header className="flex items-center justify-between gap-2">
        <h4 className="font-semibold">{t(widget.title_key)}</h4>
        <span className="text-muted-foreground">{t('itemCount', { count: widget.items.length })}</span>
      </header>
      {widget.status === 'not_applicable' && <p className="mt-1 text-muted-foreground">{t('futureNoNews')}</p>}
      {widget.status === 'empty' && <p className="mt-1 text-muted-foreground">{t('emptyWidget')}</p>}
      {widget.status === 'unavailable' && <p role="alert" className="mt-1 text-destructive">{t('widgetUnavailable')}</p>}
      {titles.length > 0 && <ul className="mt-1 list-disc space-y-0.5 pl-4">{titles.map((title, index) => <li key={`${index}-${title}`} className="line-clamp-1">{title}</li>)}</ul>}
      {widget.source_status === 'manual_or_imported_only' && <p className="mt-1 text-muted-foreground">{t('manualEventsOnly')}</p>}
      {widget.updated_at && <p className="mt-1 text-muted-foreground">{t('updatedAt', { time: formatDateTime(widget.updated_at, display.locale, display.timezone) })}</p>}
    </section>
  );
}

/**
 * Daily brief gadget body: the saved brief for the selected day (historical, revisioned) beside
 * clearly labelled current-record summaries. Regenerating appends a revision and never replaces text.
 */
export function DailyBrief() {
  const t = useTranslations('daily');
  const display = useDisplayPreferences();
  const client = useQueryClient();
  const { date, timezone } = useSelectedDay();
  const chat = useChatController();
  const { isOwner } = useWorkspace();
  const session = useQuery({ queryKey: ['session'], queryFn: () => apiRequest<{ authenticated: true; csrfToken: string }>('/api/v1/auth/session') });
  const context = useQuery({
    queryKey: dailyKeys.context(date, timezone),
    queryFn: ({ signal }) => getDailyContext(date, timezone, signal),
  });
  const generate = useMutation({
    mutationFn: () => generateBrief(date, timezone, session.data!.csrfToken),
    onSettled: () => client.invalidateQueries({ queryKey: dailyKeys.context(date, timezone) }),
  });
  const brief = context.data?.brief ?? null;
  const errorKey = generate.error instanceof ApiError
    ? (generate.error.status === 409 ? 'generateNoInputs' : 'generateUnavailable')
    : generate.error ? 'generateUnavailable' : null;

  return (
    <div className="flex h-full min-h-0 flex-col gap-3 overflow-y-auto p-3.5">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <DateSelector />
        {isOwner && context.data && <span className="rounded bg-secondary px-2 py-0.5 text-xs font-semibold">{t(`relation_${context.data.relation}`)}</span>}
      </header>

      {!isOwner && <SharedBriefs date={date} timezone={timezone} />}

      {isOwner && context.isLoading && <p role="status" className="text-sm text-muted-foreground">{t('loading')}</p>}
      {isOwner && context.isError && <p role="alert" className="text-sm text-destructive">{t('loadError')}</p>}

      {isOwner && context.data && (
        <>
          <section aria-labelledby="saved-brief-heading" className="space-y-2 border-b border-border pb-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 id="saved-brief-heading" className="flex items-center gap-1.5 text-sm font-semibold">
                <Sparkles className="h-4 w-4 text-primary" aria-hidden />
                {t('savedBrief')}
              </h3>
              {brief && <span className="text-xs text-muted-foreground">{t('revisionMeta', { revision: brief.revision, total: context.data.brief_revisions, time: formatDateTime(brief.generated_at, display.locale, display.timezone) })}</span>}
            </div>
            {brief ? (
              <>
                {brief.status === 'stale' && <p role="status" className="rounded border border-destructive p-2 text-xs text-destructive">{t('staleBrief')}</p>}
                <p className="whitespace-pre-wrap text-sm leading-relaxed">{brief.content}</p>
                {brief.citations.length > 0 && (
                  <ol className="space-y-0.5 text-xs text-muted-foreground" aria-label={t('citations')}>
                    {brief.citations.map((citation) => <li key={citation.ref}>[{citation.ref}] {citation.title}</li>)}
                  </ol>
                )}
              </>
            ) : <p className="text-sm text-muted-foreground">{t('noBrief')}</p>}
            {errorKey && <p role="alert" className="text-xs text-destructive">{t(errorKey)}</p>}
            <div className="flex flex-wrap gap-2">
              <Button type="button" size="sm" disabled={!session.data || generate.isPending} onClick={() => generate.mutate()}>
                <RefreshCw className="h-3.5 w-3.5" aria-hidden />
                {generate.isPending ? t('generating') : brief ? t('regenerate') : t('generate')}
              </Button>
              <Button type="button" size="sm" variant="secondary" onClick={() => { chat.selectDay({ date, timezone }); chat.openDrawer(); }}>
                <MessageSquare className="h-3.5 w-3.5" aria-hidden />
                {t('askAboutDay')}
              </Button>
              {brief && <BriefShareButton brief={brief} />}
            </div>
          </section>

          <section aria-labelledby="current-records-heading" className="space-y-2">
            <h3 id="current-records-heading" className="text-sm font-semibold">{t('currentRecords')}</h3>
            <p className="text-xs text-muted-foreground">{t('currentRecordsNote', { time: formatDateTime(context.data.widgets_updated_at, display.locale, display.timezone) })}</p>
            <div className="grid gap-2 sm:grid-cols-2">
              {context.data.widgets.map((widget) => <WidgetSummary key={widget.id} widget={widget} />)}
            </div>
          </section>
        </>
      )}
    </div>
  );
}
