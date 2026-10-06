'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, Bell, Plus, RotateCw } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { useState } from 'react';
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { apiFailureKey } from '@/core/api-failure-key';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import {
  dashboardKeys, evaluateGadgetHighlights, listGadgetDefinitions, patchGadgetDefinition, type GadgetDefinition, type HighlightRule,
} from '@/modules/dashboard/api';
import { parseKeywordList } from './gadget-library';

type Severity = HighlightRule['severity'];
type RuleDraft = { id: string; keywords: string; severity: Severity; notify: boolean };

const severityKeys = { info: 'severityInfo', warning: 'severityWarning', critical: 'severityCritical' } as const;

/**
 * Highlight rules tab: choose a gadget, then add, edit or delete its keyword rules.
 * The backend supports only keywords, severity and notify, so no other conditions are offered.
 */
export function HighlightRuleEditor({ initialDefinitionId }: { initialDefinitionId?: string | null }) {
  const t = useTranslations('gadgetSettings');
  const defs = useQuery({ queryKey: dashboardKeys.definitions, queryFn: ({ signal }) => listGadgetDefinitions(100, signal) });
  const [picked, setPicked] = useState<string | null>(initialDefinitionId ?? null);
  const definitions = defs.data ?? [];
  const def = definitions.find((item) => item.id === picked) ?? definitions[0] ?? null;
  return <div className="space-y-4">
    <div><h2 className="text-base font-semibold">{t('rulesTitle')}</h2><p className="muted">{t('rulesIntro')}</p><p className="muted">{t('unsupportedRules')}</p></div>
    {defs.isPending && <div className="skeleton h-16" role="status" aria-label={t('loading')} />}
    {defs.isError && <div role="alert" className="space-y-2"><p className="error">{t(apiFailureKey(defs.error) ?? 'loadFailed')}</p><Button type="button" className="secondary" onClick={() => defs.refetch()}><RotateCw className="size-4" aria-hidden="true" /> {t('retry')}</Button></div>}
    {defs.isSuccess && !def && <p className="muted rounded-lg border border-dashed border-border p-6">{t('noGadgets')}</p>}
    {def && <>
      <label className="field max-w-md"><span className="label">{t('pickGadget')}</span>
        <Select value={def.id} onValueChange={setPicked}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{definitions.map((item) => <SelectItem key={item.id} value={item.id}>{item.name}</SelectItem>)}</SelectContent></Select></label>
      <RuleList key={`${def.id}:${def.revision}`} def={def} />
    </>}
  </div>;
}

/** Rule list, inline rule editor and current-match preview for one saved gadget. */
function RuleList({ def }: { def: GadgetDefinition }) {
  const t = useTranslations('gadgetSettings');
  const session = useWorkspaceSession();
  const client = useQueryClient();
  const [editing, setEditing] = useState<RuleDraft | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [touched, setTouched] = useState(false);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const rules = def.highlight_rules ?? [];
  const matches = useQuery({ queryKey: [...dashboardKeys.definition(def.id), 'highlights', def.revision], queryFn: () => evaluateGadgetHighlights(def.id) });
  const write = useMutation({
    mutationFn: (next: HighlightRule[]) => patchGadgetDefinition(def.id, { expected_revision: def.revision, highlight_rules: next }, session.csrfToken),
    onSuccess: () => { setEditing(null); setDeleteId(null); setTouched(false); void client.invalidateQueries({ queryKey: dashboardKeys.definitions }); },
  });
  const keywords = editing ? parseKeywordList(editing.keywords) : [];
  const valid = keywords.length >= 1 && keywords.length <= 16;
  /** Persists the edited rule, replacing an existing rule by id or appending a new one. */
  const saveRule = () => {
    setTouched(true);
    if (!editing || !valid) return;
    const rule: HighlightRule = { id: editing.id, keywords, severity: editing.severity, notify: editing.notify };
    write.mutate(isNew ? [...rules, rule] : rules.map((item) => (item.id === rule.id ? rule : item)));
  };
  const open = (rule: HighlightRule | null) => {
    setTouched(false); write.reset(); setIsNew(rule === null);
    setEditing(rule ? { id: rule.id, keywords: rule.keywords.join(', '), severity: rule.severity, notify: rule.notify } : { id: crypto.randomUUID(), keywords: '', severity: 'info', notify: false });
  };
  return <div className="space-y-4">
    <div className="flex items-center justify-between gap-2">
      <h3 className="font-semibold">{t('rulesListLabel')}</h3>
      <Button type="button" className="secondary" disabled={write.isPending} onClick={() => open(null)}><Plus className="size-4" aria-hidden="true" /> {t('addRule')}</Button>
    </div>
    {rules.length === 0 && !editing && <p className="muted">{t('noRules')}</p>}
    <ul aria-label={t('rulesListLabel')} className="space-y-2">{rules.map((rule) => <li key={rule.id} className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-3">
      <span className="min-w-0 flex-1 font-medium">{rule.keywords.join(', ')}</span>
      <span className="muted text-xs">{t(severityKeys[rule.severity])}</span>
      <span className="muted inline-flex items-center gap-1 text-xs">{rule.notify && <Bell className="size-3" aria-hidden="true" />}{rule.notify ? t('notifyOn') : t('notifyOff')}</span>
      <Button type="button" className="secondary" disabled={write.isPending} onClick={() => open(rule)}>{t('editRule')}</Button>
      <Button type="button" variant="destructive" disabled={write.isPending} onClick={() => setDeleteId(rule.id)}>{t('deleteRule')}</Button>
    </li>)}</ul>
    {editing && <form className="space-y-3 rounded-lg border border-border p-4" aria-label={isNew ? t('ruleNew') : t('ruleTitle')} onSubmit={(event) => { event.preventDefault(); saveRule(); }}>
      <fieldset disabled={write.isPending} className="space-y-3">
        <h3 className="font-semibold">{isNew ? t('ruleNew') : t('ruleTitle')}</h3>
        <label className="field"><span className="label">{t('ruleKeywords')}</span><Input value={editing.keywords} aria-invalid={touched && !valid} onChange={(event) => setEditing({ ...editing, keywords: event.target.value })} /><span className="muted text-xs">{t('ruleKeywordsHelp')}</span>{touched && !valid && <span className="error" role="alert">{t('ruleNeedsKeyword')}</span>}</label>
        <label className="field max-w-xs"><span className="label">{t('severity')}</span>
          <Select value={editing.severity} onValueChange={(value) => setEditing({ ...editing, severity: value as Severity })}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{(Object.keys(severityKeys) as Severity[]).map((item) => <SelectItem key={item} value={item}>{t(severityKeys[item])}</SelectItem>)}</SelectContent></Select></label>
        <label className="field"><Checkbox checked={editing.notify} onCheckedChange={(checked) => setEditing({ ...editing, notify: checked === true })} /> {t('notify')}</label>
      </fieldset>
      {write.error && <p className="error" role="alert">{t(apiFailureKey(write.error) ?? 'saveFailed')}</p>}
      <div className="form-actions"><Button type="submit" disabled={write.isPending}>{write.isPending ? t('saving') : t('saveRule')}</Button><Button type="button" className="secondary" disabled={write.isPending} onClick={() => setEditing(null)}>{t('cancelRule')}</Button></div>
    </form>}
    <section aria-labelledby="hr-matches" className="space-y-2 rounded-lg border border-border p-3">
      <h3 id="hr-matches" className="font-semibold">{t('matches')}</h3>
      {matches.isPending && <p className="muted" role="status">{t('previewLoading')}</p>}
      {matches.isError && <p className="error" role="alert"><AlertCircle className="inline size-4" aria-hidden="true" /> {t('previewError')}</p>}
      {matches.isSuccess && matches.data.length === 0 && <p className="muted">{t('matchesEmpty')}</p>}
      {matches.isSuccess && <ul className="space-y-1">{matches.data.map((match) => <li key={`${match.rule_id}:${match.document_version_id}`}><span className="font-medium">{match.title}</span> <span className="muted text-xs">{t('previewMatch', { keywords: match.matched_keywords.join(', ') })} · {t(severityKeys[match.severity])}</span></li>)}</ul>}
      <p className="muted text-xs">{t('matchesNote')}</p>
    </section>
    {write.error && !editing && <p className="error" role="alert">{t(apiFailureKey(write.error) ?? 'saveFailed')}</p>}
    <AlertDialog open={deleteId !== null} onOpenChange={(value) => { if (!value) setDeleteId(null); }}>
      <AlertDialogContent><AlertDialogHeader><AlertDialogTitle>{t('deleteRuleTitle')}</AlertDialogTitle><AlertDialogDescription>{t('deleteRuleBody')}</AlertDialogDescription></AlertDialogHeader>
        <AlertDialogFooter><AlertDialogCancel>{t('cancelRule')}</AlertDialogCancel><AlertDialogAction onClick={() => write.mutate(rules.filter((item) => item.id !== deleteId))}>{t('deleteRule')}</AlertDialogAction></AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </div>;
}
