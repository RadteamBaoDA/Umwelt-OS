'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useState } from 'react';
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { ApiError } from '@/core/api';
import { safeHttpUrl } from '@/core/safe-url';
import { connectorKeys, disableBuiltinSource, enableBuiltinSource, listBuiltinSources, sourceKeys, type BuiltinCategory, type BuiltinSource, type DeclaredUse } from './api';

const CATEGORY_ORDER: BuiltinCategory[] = ['news', 'economy', 'hazards', 'space', 'security', 'tech'];
const CATEGORY_KEY = {
  news: 'builtinCategoryNews', economy: 'builtinCategoryEconomy', hazards: 'builtinCategoryHazards',
  space: 'builtinCategorySpace', security: 'builtinCategorySecurity', tech: 'builtinCategoryTech',
} as const;
const USES = [['personal', 'termsUsePersonal'], ['noncommercial', 'termsUseNoncommercial'], ['commercial', 'termsUseCommercial']] as const;

/** Groups built-ins by category in the fixed display order, dropping empty categories. */
function groupByCategory(items: BuiltinSource[]): [BuiltinCategory, BuiltinSource[]][] {
  return CATEGORY_ORDER.map((category): [BuiltinCategory, BuiltinSource[]] => [category, items.filter((item) => item.category === category)])
    .filter(([, rows]) => rows.length > 0);
}

/**
 * Lists the built-in data sources grouped by category with one switch each.
 * Turning a source on opens a confirmation (attribution, terms link, declared use) because the
 * server records that confirmation as the terms acknowledgement; turning it off pauses it immediately.
 * Server refusals (operator required, terms ineligible) are shown as localized text; the list is
 * refetched and the source list invalidated after every successful change.
 */
export function BuiltinSources() {
  const t = useTranslations('sources');
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();
  const [pending, setPending] = useState<BuiltinSource | null>(null);
  const [use, setUse] = useState<DeclaredUse>('personal');
  const [error, setError] = useState<string | null>(null);
  const builtins = useQuery({ queryKey: connectorKeys.builtins, queryFn: ({ signal }) => listBuiltinSources(signal) });

  /** Refreshes toggle state and the source list so a newly created or paused source appears. */
  const changed = () => {
    void queryClient.invalidateQueries({ queryKey: connectorKeys.builtins });
    void queryClient.invalidateQueries({ queryKey: sourceKeys.all });
  };
  /** Maps a failed toggle to the most specific localized message. */
  const fail = (cause: unknown) => {
    const message = cause instanceof ApiError ? cause.message : '';
    setError(message === 'operator_required' ? t('builtinErrorOperator') : message === 'provider_terms_ineligible' ? t('builtinErrorTerms') : t('builtinErrorGeneric'));
  };
  const enable = useMutation({
    mutationFn: (input: { id: string; use: DeclaredUse }) => enableBuiltinSource(input.id, input.use, csrfToken),
    onSuccess: () => { setError(null); setPending(null); changed(); },
    onError: (cause) => { setPending(null); fail(cause); },
  });
  const disable = useMutation({
    mutationFn: (id: string) => disableBuiltinSource(id, csrfToken),
    onSuccess: () => { setError(null); changed(); },
    onError: fail,
  });
  const busy = enable.isPending || disable.isPending;

  return <section className="flex flex-col gap-3 rounded-lg border border-border bg-card p-5" aria-labelledby="builtin-sources-heading">
    <header><h2 id="builtin-sources-heading" className="m-0 text-lg font-bold">{t('builtinTitle')}</h2><p className="text-muted-foreground">{t('builtinDescription')}</p></header>
    {builtins.isPending && <p className="text-muted-foreground">{t('loading')}</p>}
    {builtins.isError && <p className="text-danger" role="alert">{t('builtinLoadFailed')} <Button variant="outline" onClick={() => builtins.refetch()}>{t('retry')}</Button></p>}
    {error && <p className="text-danger" role="alert">{error}</p>}
    {groupByCategory(builtins.data ?? []).map(([category, rows]) => <div key={category} className="flex flex-col gap-2" role="group" aria-label={t(CATEGORY_KEY[category])}>
      <h3 className="m-0 text-sm font-semibold uppercase text-muted-foreground">{t(CATEGORY_KEY[category])}</h3>
      {rows.map((row) => <div key={row.id} className="flex items-start justify-between gap-3 rounded-md border border-border p-3">
        <div className="min-w-0">
          <p className="m-0 flex flex-wrap items-center gap-2 font-medium">{row.label}
            {row.execution === 'experimental' && <Badge variant="outline">{t('builtinExperimental')}</Badge>}
            {row.eligibility === 'review' && <Badge variant="secondary">{t('builtinReviewClass')}</Badge>}
          </p>
          <p className="m-0 text-sm text-muted-foreground">{row.attribution} <a className="text-accent underline" href={safeHttpUrl(row.terms_url)} target="_blank" rel="noopener noreferrer">{t('providerTerms')}</a></p>
        </div>
        <Switch checked={row.enabled} disabled={busy} aria-label={t('builtinToggle', { name: row.label })}
          onCheckedChange={(next) => { if (next) { setUse('personal'); setPending(row); } else disable.mutate(row.id); }} />
      </div>)}
    </div>)}
    <AlertDialog open={pending !== null} onOpenChange={(open) => { if (!open && !enable.isPending) setPending(null); }}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{t('builtinDialogTitle', { name: pending?.label ?? '' })}</AlertDialogTitle>
          <AlertDialogDescription>{t('builtinDialogBody')}</AlertDialogDescription>
        </AlertDialogHeader>
        {pending && <div className="grid gap-2 text-sm">
          <p><strong>{t('guideAttribution')}:</strong> {pending.attribution}</p>
          <p><a className="text-accent underline" href={safeHttpUrl(pending.terms_url)} target="_blank" rel="noopener noreferrer">{t('providerTerms')}</a> · {t('termsVersion')}: {pending.terms_checked_on}</p>
          {pending.eligibility === 'review' && <p role="note">{t('builtinOperatorNote')}</p>}
          <div className="field"><Label htmlFor="builtin-declared-use">{t('termsDeclaredUse')}</Label>
            <Select value={use} onValueChange={(value) => setUse(value as DeclaredUse)}>
              <SelectTrigger id="builtin-declared-use"><SelectValue /></SelectTrigger>
              <SelectContent>{USES.map(([value, label]) => <SelectItem key={value} value={value}>{t(label)}</SelectItem>)}</SelectContent>
            </Select></div>
        </div>}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={enable.isPending}>{t('cancel')}</AlertDialogCancel>
          <AlertDialogAction disabled={enable.isPending} onClick={(event) => { event.preventDefault(); if (pending) enable.mutate({ id: pending.id, use }); }}>{enable.isPending ? t('builtinWorking') : t('builtinConfirm')}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </section>;
}
