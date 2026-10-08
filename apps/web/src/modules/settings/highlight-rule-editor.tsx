'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, Bell, Eye, Plus, RotateCw } from 'lucide-react';
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
  dashboardKeys, evaluateGadgetHighlights, getGadgetDefinitionUsage, highlightRuleErrorKey, listGadgetDefinitions, listGadgetSources, patchGadgetDefinition, previewHighlightRules,
  type GadgetDefinition, type HighlightRule,
} from '@/modules/dashboard/api';
import { fetchAllTopics } from '@/modules/news/api';
import { parseKeywordList } from './gadget-library';

type Severity = HighlightRule['severity'];
type SourceMode = 'any' | 'only' | 'exclude';
type RuleDraft = { id: string; keywords: string; severity: Severity; notify: boolean; topicIds: string[]; sourceModes: Record<string, SourceMode>; cooldown: string; expires: string; quietStart: string; quietEnd: string };
const MAX_COOLDOWN = 7 * 24 * 60;
const MAX_TOPICS = 8;

const sourceModeKeys = { any: 'sourceAny', only: 'sourceOnly', exclude: 'sourceExclude' } as const;

/** ISO instant to the `datetime-local` value in the browser time zone. */
const toLocalInput = (iso?: string | null) => {
  if (!iso) return '';
  const date = new Date(iso);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
};

/** Builds the API rule from a draft, keeping only conditions that are set. */
function toRule(draft: RuleDraft, keywords: string[]): HighlightRule {
  const ids = (mode: SourceMode) => Object.entries(draft.sourceModes).filter(([, value]) => value === mode).map(([id]) => id);
  return {
    id: draft.id, keywords, severity: draft.severity, notify: draft.notify,
    topic_ids: draft.topicIds, source_ids: ids('only'), exclude_source_ids: ids('exclude'),
    cooldown_minutes: Number(draft.cooldown) || 0,
    expires_at: draft.expires ? new Date(draft.expires).toISOString() : null,
    quiet_start: draft.quietStart || null, quiet_end: draft.quietEnd || null,
  };
}

const severityKeys = { info: 'severityInfo', warning: 'severityWarning', critical: 'severityCritical' } as const;

/**
 * Highlight rules tab: choose a gadget, then add, edit or delete its keyword rules.
 * Conditions: keywords, topics and per-source include/exclude; a 7-day preview never notifies or saves.
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
  const usage = useQuery({ queryKey: [...dashboardKeys.definition(def.id), 'usage'], queryFn: ({ signal }) => getGadgetDefinitionUsage(def.id, signal) });
  const usageNames = (usage.data ?? []).map((item) => item.name).join(', ');
  const topics = useQuery({ queryKey: ['highlight-rule-topics'], queryFn: ({ signal }) => fetchAllTopics({ isActive: true, signal }) });
  const sources = useQuery({ queryKey: ['highlight-rule-sources'], queryFn: ({ signal }) => listGadgetSources(100, undefined, signal) });
  const sourceName = (id: string) => sources.data?.items.find((item) => item.id === id)?.name ?? id;
  const preview = useMutation({ mutationFn: (rule: HighlightRule) => previewHighlightRules({ source_ids: def.source_ids, rules: [rule], days: 7, source_item_ids: def.scope.source_item_ids }, session.csrfToken) });
  const matches = useQuery({ queryKey: [...dashboardKeys.definition(def.id), 'highlights', def.revision], queryFn: () => evaluateGadgetHighlights(def.id) });
  const write = useMutation({
    mutationFn: (next: HighlightRule[]) => patchGadgetDefinition(def.id, { expected_revision: def.revision, highlight_rules: next }, session.csrfToken),
    onSuccess: () => { setEditing(null); setDeleteId(null); setTouched(false); void client.invalidateQueries({ queryKey: dashboardKeys.definitions }); },
  });
  const keywords = editing ? parseKeywordList(editing.keywords) : [];
  const cooldownNumber = Number(editing?.cooldown ?? 0);
  const deliveryValid = Number.isInteger(cooldownNumber) && cooldownNumber >= 0 && cooldownNumber <= MAX_COOLDOWN
    && Boolean(editing?.quietStart) === Boolean(editing?.quietEnd) && (!editing?.quietStart || editing.quietStart !== editing.quietEnd);
  const valid = (keywords.length >= 1 || (editing?.topicIds.length ?? 0) >= 1) && keywords.length <= 16 && (editing?.topicIds.length ?? 0) <= MAX_TOPICS && deliveryValid;
  /** Persists the edited rule, replacing an existing rule by id or appending a new one. */
  const saveRule = () => {
    setTouched(true);
    if (!editing || !valid) return;
    const rule = toRule(editing, keywords);
    write.mutate(isNew ? [...rules, rule] : rules.map((item) => (item.id === rule.id ? rule : item)));
  };
  const open = (rule: HighlightRule | null) => {
    setTouched(false); write.reset(); preview.reset(); setIsNew(rule === null);
    const sourceModes: Record<string, SourceMode> = {};
    rule?.source_ids?.forEach((id) => { sourceModes[id] = 'only'; });
    rule?.exclude_source_ids?.forEach((id) => { sourceModes[id] = 'exclude'; });
    setEditing(rule ? { id: rule.id, keywords: rule.keywords.join(', '), severity: rule.severity, notify: rule.notify, topicIds: rule.topic_ids ?? [], sourceModes, cooldown: String(rule.cooldown_minutes ?? 0), expires: toLocalInput(rule.expires_at), quietStart: rule.quiet_start ?? '', quietEnd: rule.quiet_end ?? '' } : { id: crypto.randomUUID(), keywords: '', severity: 'info', notify: false, topicIds: [], sourceModes, cooldown: '0', expires: '', quietStart: '', quietEnd: '' });
  };
  return <div className="space-y-4">
    <div className="flex items-center justify-between gap-2">
      <h3 className="font-semibold">{t('rulesListLabel')}</h3>
      <Button type="button" className="secondary" disabled={write.isPending} onClick={() => open(null)}><Plus className="size-4" aria-hidden="true" /> {t('addRule')}</Button>
    </div>
    {usageNames && <p className="muted rounded-lg border border-border p-3" role="status">{t('usageWarn', { count: usage.data?.length ?? 0, names: usageNames })}</p>}
    {rules.length === 0 && !editing && <p className="muted">{t('noRules')}</p>}
    <ul aria-label={t('rulesListLabel')} className="space-y-2">{rules.map((rule) => <li key={rule.id} className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-3">
      <span className="min-w-0 flex-1 font-medium">{[...rule.keywords, ...((rule.topic_ids?.length ?? 0) > 0 ? [t('ruleTopicCount', { count: rule.topic_ids?.length ?? 0 })] : [])].join(', ')}</span>
      <span className="muted text-xs">{t(severityKeys[rule.severity])}</span>
      <span className="muted inline-flex items-center gap-1 text-xs">{rule.notify && <Bell className="size-3" aria-hidden="true" />}{rule.notify ? t('notifyOn') : t('notifyOff')}</span>
      <Button type="button" className="secondary" disabled={write.isPending} onClick={() => open(rule)}>{t('editRule')}</Button>
      <Button type="button" variant="destructive" disabled={write.isPending} onClick={() => setDeleteId(rule.id)}>{t('deleteRule')}</Button>
    </li>)}</ul>
    {editing && <form className="space-y-3 rounded-lg border border-border p-4" aria-label={isNew ? t('ruleNew') : t('ruleTitle')} onSubmit={(event) => { event.preventDefault(); saveRule(); }}>
      <fieldset disabled={write.isPending} className="space-y-3">
        <h3 className="font-semibold">{isNew ? t('ruleNew') : t('ruleTitle')}</h3>
        <label className="field"><span className="label">{t('ruleKeywords')}</span><Input value={editing.keywords} aria-invalid={touched && !valid} onChange={(event) => setEditing({ ...editing, keywords: event.target.value })} /><span className="muted text-xs">{t('ruleKeywordsHelp')}</span>{touched && !valid && <span className="error" role="alert">{t('ruleNeedsCondition')}</span>}</label>
        <fieldset className="field"><legend className="label">{t('ruleTopics')}</legend><span className="muted text-xs">{t('ruleTopicsHelp')}</span>
          {topics.isSuccess && topics.data.length === 0 && editing.topicIds.length === 0 && <span className="muted">{t('ruleTopicsNone')}</span>}
          {topics.isSuccess && topics.data.map((topic) => <label key={topic.id} className="flex items-center gap-2"><Checkbox checked={editing.topicIds.includes(topic.id)} onCheckedChange={(checked) => setEditing({ ...editing, topicIds: checked === true ? [...editing.topicIds, topic.id] : editing.topicIds.filter((id) => id !== topic.id) })} /> {topic.name}</label>)}
          {topics.isSuccess && editing.topicIds.filter((id) => !topics.data.some((topic) => topic.id === id)).map((id) => <div key={id} className="flex items-center gap-2"><span className="muted min-w-0 flex-1">{t('ruleTopicUnavailable')}</span><Button type="button" className="secondary" onClick={() => setEditing({ ...editing, topicIds: editing.topicIds.filter((value) => value !== id) })}>{t('ruleTopicRemove')}</Button></div>)}
          {editing.topicIds.length > MAX_TOPICS && <span className="error" role="alert">{t('ruleTopicsLimit')}</span>}
        </fieldset>
        {(def.source_ids.length > 1 || Object.values(editing.sourceModes).some((mode) => mode !== 'any')) && <fieldset className="field"><legend className="label">{t('ruleSources')}</legend><span className="muted text-xs">{t('ruleSourcesHelp')}</span>
          {def.source_ids.map((id) => <div key={id} className="flex flex-wrap items-center gap-2"><span className="min-w-0 flex-1">{sourceName(id)}</span>
            <Select value={editing.sourceModes[id] ?? 'any'} onValueChange={(value) => setEditing({ ...editing, sourceModes: { ...editing.sourceModes, [id]: value as SourceMode } })}><SelectTrigger aria-label={`${t('ruleSourceMode')}: ${sourceName(id)}`} className="w-40"><SelectValue /></SelectTrigger><SelectContent>{(Object.keys(sourceModeKeys) as SourceMode[]).map((mode) => <SelectItem key={mode} value={mode}>{t(sourceModeKeys[mode])}</SelectItem>)}</SelectContent></Select></div>)}
        </fieldset>}
        <label className="field max-w-xs"><span className="label">{t('severity')}</span>
          <Select value={editing.severity} onValueChange={(value) => setEditing({ ...editing, severity: value as Severity })}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{(Object.keys(severityKeys) as Severity[]).map((item) => <SelectItem key={item} value={item}>{t(severityKeys[item])}</SelectItem>)}</SelectContent></Select></label>
        <label className="field"><Checkbox checked={editing.notify} onCheckedChange={(checked) => setEditing({ ...editing, notify: checked === true })} /> {t('notify')}</label>
        <fieldset className="field"><legend className="label">{t('ruleDelivery')}</legend><span className="muted text-xs">{t('ruleDeliveryHelp')}</span>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="field"><span className="label">{t('ruleCooldown')}</span><Input type="number" min={0} max={MAX_COOLDOWN} step={1} inputMode="numeric" value={editing.cooldown} aria-invalid={touched && !deliveryValid} aria-describedby={touched && !deliveryValid ? 'rule-cooldown-help rule-delivery-error' : 'rule-cooldown-help'} onChange={(event) => setEditing({ ...editing, cooldown: event.target.value })} /><span id="rule-cooldown-help" className="muted text-xs">{t('ruleCooldownHelp')}</span></label>
            <label className="field"><span className="label">{t('ruleExpires')}</span><Input type="datetime-local" value={editing.expires} onChange={(event) => setEditing({ ...editing, expires: event.target.value })} /><span className="muted text-xs">{t('ruleExpiresHelp')}</span></label>
            <label className="field"><span className="label">{t('ruleQuietStart')}</span><Input type="time" value={editing.quietStart} aria-invalid={touched && !deliveryValid} aria-describedby={touched && !deliveryValid ? 'rule-delivery-error rule-quiet-note' : 'rule-quiet-note'} onChange={(event) => setEditing({ ...editing, quietStart: event.target.value })} /></label>
            <label className="field"><span className="label">{t('ruleQuietEnd')}</span><Input type="time" value={editing.quietEnd} aria-invalid={touched && !deliveryValid} aria-describedby={touched && !deliveryValid ? 'rule-delivery-error rule-quiet-note' : 'rule-quiet-note'} onChange={(event) => setEditing({ ...editing, quietEnd: event.target.value })} /></label>
          </div>
          <span id="rule-quiet-note" className="muted text-xs">{t('ruleQuietNote')}</span>
          {touched && !deliveryValid && <span id="rule-delivery-error" className="error" role="alert">{t('ruleDeliveryInvalid')}</span>}
        </fieldset>
      </fieldset>
      {write.error && <p className="error" role="alert">{t((highlightRuleErrorKey(write.error) ?? apiFailureKey(write.error) ?? 'saveFailed') as 'saveFailed')}</p>}
      <div className="form-actions"><Button type="submit" disabled={write.isPending}>{write.isPending ? t('saving') : t('saveRule')}</Button><Button type="button" className="secondary" disabled={write.isPending || !valid || def.source_ids.length === 0 || preview.isPending} onClick={() => preview.mutate(toRule(editing, keywords))}><Eye className="size-4" aria-hidden="true" /> {preview.isPending ? t('rulePreviewRunning') : t('rulePreview')}</Button><Button type="button" className="secondary" disabled={write.isPending} onClick={() => setEditing(null)}>{t('cancelRule')}</Button></div>
      <div aria-live="polite" className="space-y-1">
        {preview.isError && <p className="error" role="alert">{t((highlightRuleErrorKey(preview.error) ?? apiFailureKey(preview.error) ?? 'rulePreviewFailed') as 'rulePreviewFailed')}</p>}
        {preview.data && <>
          <p>{t('rulePreviewSummary', { matches: preview.data.rules[0]?.match_count ?? 0, scanned: preview.data.scanned })}</p>
          {preview.data.truncated && <p className="muted text-xs">{t('rulePreviewTruncated')}</p>}
          {(preview.data.rules[0]?.unresolved_topic_ids.length ?? 0) > 0 && <p className="muted text-xs">{t('rulePreviewTopicGone')}</p>}
          <ul className="space-y-1">{preview.data.matches.map((match) => <li key={`${match.rule_id}:${match.document_version_id}`}><span className="font-medium">{match.title}</span> <span className="muted text-xs">{t('previewMatch', { keywords: match.matched_keywords.join(', ') })}</span></li>)}</ul>
        </>}
        <p className="muted text-xs">{t('rulePreviewNoSend')}</p>
      </div>
    </form>}
    <section aria-labelledby="hr-matches" className="space-y-2 rounded-lg border border-border p-3">
      <h3 id="hr-matches" className="font-semibold">{t('matches')}</h3>
      {matches.isPending && <p className="muted" role="status">{t('previewLoading')}</p>}
      {matches.isError && <p className="error" role="alert"><AlertCircle className="inline size-4" aria-hidden="true" /> {t('previewError')}</p>}
      {matches.isSuccess && matches.data.length === 0 && <p className="muted">{t('matchesEmpty')}</p>}
      {matches.isSuccess && <ul className="space-y-1">{matches.data.map((match) => <li key={`${match.rule_id}:${match.document_version_id}`}><span className="font-medium">{match.title}</span> <span className="muted text-xs">{t('previewMatch', { keywords: match.matched_keywords.join(', ') })} · {t(severityKeys[match.severity])}</span></li>)}</ul>}
      <p className="muted text-xs">{t('matchesNote')}</p>
    </section>
    {write.error && !editing && <p className="error" role="alert">{t((highlightRuleErrorKey(write.error) ?? apiFailureKey(write.error) ?? 'saveFailed') as 'saveFailed')}</p>}
    <AlertDialog open={deleteId !== null} onOpenChange={(value) => { if (!value) setDeleteId(null); }}>
      <AlertDialogContent><AlertDialogHeader><AlertDialogTitle>{t('deleteRuleTitle')}</AlertDialogTitle><AlertDialogDescription>{t('deleteRuleBody')}{usageNames && ` ${t('usageDelete', { names: usageNames })}`}</AlertDialogDescription></AlertDialogHeader>
        <AlertDialogFooter><AlertDialogCancel>{t('cancelRule')}</AlertDialogCancel><AlertDialogAction onClick={() => write.mutate(rules.filter((item) => item.id !== deleteId))}>{t('deleteRule')}</AlertDialogAction></AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </div>;
}
