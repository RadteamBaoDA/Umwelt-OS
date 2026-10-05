'use client';

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { deleteAutomation, errorMessageKey, listAutomations, patchAutomation, runAutomation, type Automation } from './api';
import { RuleEditor } from './rule-editor';
import { RunDetail } from './run-detail';

type View = { kind: 'list' } | { kind: 'edit'; rule?: Automation } | { kind: 'runs'; ruleId: string };

/** Advanced Settings section for automation rules: list, edit, enable/disable, Run now and run history. */
export function AutomationSettings() {
  const t = useTranslations('automations');
  const { csrfToken } = useWorkspaceSession();
  const client = useQueryClient();
  const [view, setView] = useState<View>({ kind: 'list' });
  const [removing, setRemoving] = useState<Automation | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const rules = useQuery({ queryKey: ['automations'], queryFn: listAutomations });
  const refresh = () => { void client.invalidateQueries({ queryKey: ['automations'] }); };
  /** Maps a failed mutation to a localized notice by error code. */
  const fail = (error: unknown) => { setNotice(t(errorMessageKey(error, 'actionFailed'))); refresh(); };

  // Enabling is the only way a rule starts; pausing appends a revision that invalidates queued work.
  const toggle = useMutation({
    mutationFn: (rule: Automation) => patchAutomation(rule.id, rule.revision, { enabled: !rule.enabled }, csrfToken),
    onSuccess: () => { setNotice(null); refresh(); }, onError: fail,
  });
  const runNow = useMutation({
    mutationFn: (rule: Automation) => runAutomation(rule.id, rule.revision, crypto.randomUUID(), csrfToken),
    onSuccess: (_run, rule) => { setNotice(t('runQueued')); setView({ kind: 'runs', ruleId: rule.id }); }, onError: fail,
  });
  const remove = useMutation({
    mutationFn: (rule: Automation) => deleteAutomation(rule.id, rule.revision, csrfToken),
    onSuccess: () => { setRemoving(null); setNotice(null); refresh(); }, onError: (error) => { setRemoving(null); fail(error); },
  });

  const items = rules.data?.items ?? [];
  const selected = view.kind === 'runs' ? items.find((item) => item.id === view.ruleId) : undefined;
  return <section className="space-y-4 border-t border-border pt-6" aria-label={t('title')}>
    <header className="space-y-1"><h2 className="text-xl font-semibold">{t('title')}</h2><p className="text-sm text-muted-foreground">{t('description')}</p></header>
    {notice && <p role="status" className="text-sm text-foreground">{notice}</p>}
    {view.kind === 'edit' && <RuleEditor rule={view.rule} csrfToken={csrfToken} onDone={() => setView({ kind: 'list' })} />}
    {view.kind === 'runs' && selected && <RunDetail rule={selected} csrfToken={csrfToken} onBack={() => setView({ kind: 'list' })} />}
    {view.kind === 'list' && <>
      <Button type="button" onClick={() => { setNotice(null); setView({ kind: 'edit' }); }}>{t('newRule')}</Button>
      {rules.isPending && <p role="status" className="text-sm text-muted-foreground">{t('loading')}</p>}
      {rules.isError && <div role="alert" className="space-y-2 text-sm"><p className="text-destructive">{t('loadFailed')}</p><Button variant="outline" onClick={() => void rules.refetch()}>{t('retry')}</Button></div>}
      {rules.data && items.length === 0 && <p className="text-sm text-muted-foreground">{t('empty')}</p>}
      <ul className="space-y-3">{items.map((rule) => <li key={rule.id} className="space-y-2 rounded-lg border border-border bg-surface p-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0"><h3 className="font-semibold">{rule.name}</h3>
            <p className="text-sm text-muted-foreground">{t(`trigger_${rule.trigger.type}`)} · {t('actionCount', { count: rule.actions.length })} · {t('revision', { revision: rule.revision })}</p></div>
          <span className="rounded-full bg-muted px-2 py-1 text-xs">{rule.enabled ? t('enabled') : t('disabled')}</span>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button type="button" variant="outline" size="sm" onClick={() => { setNotice(null); setView({ kind: 'edit', rule }); }}>{t('edit')}</Button>
          <Button type="button" variant="outline" size="sm" disabled={toggle.isPending} onClick={() => toggle.mutate(rule)}>{rule.enabled ? t('disable') : t('enable')}</Button>
          <Button type="button" variant="outline" size="sm" disabled={!rule.enabled || runNow.isPending} onClick={() => runNow.mutate(rule)}>{t('runNow')}</Button>
          <Button type="button" variant="outline" size="sm" onClick={() => { setNotice(null); setView({ kind: 'runs', ruleId: rule.id }); }}>{t('history')}</Button>
          <Button type="button" variant="outline" size="sm" onClick={() => setRemoving(rule)}>{t('delete')}</Button>
        </div>
      </li>)}</ul>
    </>}
    <AlertDialog open={removing !== null} onOpenChange={(open) => { if (!open && !remove.isPending) setRemoving(null); }}>
      <AlertDialogContent>
        <AlertDialogHeader><AlertDialogTitle>{t('deleteTitle')}</AlertDialogTitle><AlertDialogDescription>{t('deleteDescription', { name: removing?.name ?? '' })}</AlertDialogDescription></AlertDialogHeader>
        <AlertDialogFooter><AlertDialogCancel disabled={remove.isPending}>{t('cancel')}</AlertDialogCancel>
          <Button type="button" variant="destructive" disabled={remove.isPending} onClick={() => removing && remove.mutate(removing)}>{t('delete')}</Button></AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </section>;
}
