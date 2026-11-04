'use client';

import Link from 'next/link';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check } from 'lucide-react';
import { useTranslations } from 'next-intl';
import type { ReactNode } from 'react';
import { Button } from '@/components/ui/button';
import { apiRequest } from '@/core/api';
import { useDisplayPreferences } from '@/core/query-provider';
import { fetchTopics } from '@/modules/news';
import { dailyKeys, generateBrief, getDailyContext } from './daily-api';
import { isoDateInZone } from './selected-day';

/** One numbered setup step; done steps show a check and no action. */
function Step({ n, done, title, desc, children }: { n: number; done: boolean; title: string; desc: string; children?: ReactNode }) {
  const t = useTranslations('dashboard');
  return (
    <li className="flex flex-wrap items-center gap-3 rounded-md border border-border bg-background p-3">
      <span aria-hidden="true" className="flex size-7 shrink-0 items-center justify-center rounded-full border border-border text-xs font-semibold tabular-nums">
        {done ? <Check className="size-4" /> : n}
      </span>
      <div className="min-w-0 flex-1">
        <h3 className="m-0 text-sm font-semibold text-foreground">{title}</h3>
        <p className="m-0 text-xs text-muted-foreground">{desc}</p>
      </div>
      {done ? <span className="text-xs font-semibold text-muted-foreground">{t('setupDone')}</span> : children}
    </li>
  );
}

/**
 * First-run panel shown instead of the empty gadget grid while the workspace has no sources.
 * Step state is read from real data: sources count (supplied by the caller), topics, and today's saved brief;
 * the brief step offers generation only when a model provider exists, else points at Models settings.
 * Writes: the generate-brief mutation requires the owner CSRF token.
 * @param sourceCount Number of sources already connected (0 while this panel is visible).
 * @param csrfToken Trusted session token for brief generation.
 * @param onPickTopics Opens the existing Topics dialog.
 */
export function FirstRunSetup({ sourceCount, csrfToken, onPickTopics }: { sourceCount: number; csrfToken: string; onPickTopics: () => void }) {
  const t = useTranslations('dashboard');
  const client = useQueryClient();
  const { timezone } = useDisplayPreferences();
  const date = isoDateInZone(new Date(), timezone);
  const topics = useQuery({ queryKey: ['topics', 'setup-count'], queryFn: ({ signal }) => fetchTopics({ limit: 1, signal }) });
  const models = useQuery({ queryKey: ['ai-settings'], queryFn: () => apiRequest<{ providers: unknown[] }>('/api/v1/settings/ai') });
  const context = useQuery({ queryKey: dailyKeys.context(date, timezone), queryFn: ({ signal }) => getDailyContext(date, timezone, signal) });
  const generate = useMutation({
    mutationFn: () => generateBrief(date, timezone, csrfToken),
    onSettled: () => client.invalidateQueries({ queryKey: dailyKeys.context(date, timezone) }),
  });
  const hasModel = (models.data?.providers.length ?? 0) > 0;
  return (
    <section aria-labelledby="first-run-title" className="flex flex-col gap-4 rounded-lg border border-border bg-card p-6">
      <div>
        <h2 id="first-run-title" className="m-0 text-xl font-semibold text-foreground">{t('setupTitle')}</h2>
        <p className="m-0 mt-1 text-sm text-muted-foreground">{t('setupDesc')}</p>
      </div>
      <ol className="m-0 flex list-none flex-col gap-2 p-0">
        <Step n={1} done={sourceCount > 0} title={t('setupStepSource')} desc={t('setupStepSourceDesc')}>
          {/* ponytail: wizard has no provider param, so link to the sources page; preselect later if it grows one. */}
          <Button asChild><Link href="/settings/sources">{t('setupConnect')}</Link></Button>
        </Step>
        <Step n={2} done={(topics.data?.items.length ?? 0) > 0} title={t('setupStepTopics')} desc={t('setupStepTopicsDesc')}>
          <Button type="button" variant="outline" onClick={onPickTopics}>{t('setupPickTopics')}</Button>
        </Step>
        <Step n={3} done={Boolean(context.data?.brief)} title={t('setupStepBrief')} desc={t('setupStepBriefDesc')}>
          {hasModel ? (
            <Button type="button" variant="outline" disabled={generate.isPending} onClick={() => generate.mutate()}>
              {generate.isPending ? t('setupGenerating') : t('setupGenerate')}
            </Button>
          ) : (
            <Button asChild variant="outline"><Link href="/settings/models">{t('setupConnectModel')}</Link></Button>
          )}
        </Step>
      </ol>
      {generate.isError && <p role="alert" className="m-0 text-sm text-destructive">{t('setupGenerateFailed')}</p>}
    </section>
  );
}
