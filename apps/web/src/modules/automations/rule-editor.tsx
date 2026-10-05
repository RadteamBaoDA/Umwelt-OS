'use client';

import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import { ApiError } from '@/core/api';
import {
  createAutomation, errorMessageKey, getCapabilities, patchAutomation, previewAutomation,
  type Action, type ActionType, type Automation, type Capabilities, type Condition, type Definition,
  type Operator, type PreviewResult, type Scalar, type Trigger, type TriggerType,
} from './api';

const operators: Operator[] = ['eq', 'ne', 'in', 'gt', 'gte', 'lt', 'lte'];
const agentProfiles = ['knowledge', 'research', 'personal', 'project', 'news', 'planning', 'automation'];
const maxRunsPerHour = 30; // mirrors the server per-rule cap

type CondForm = { field: string; operator: Operator; value: string };
type Draft = { name: string; trigger: Trigger; conditions: CondForm[]; actions: Action[] };

/** Returns the default parameters for a newly selected trigger type. */
function defaultTrigger(type: TriggerType): Trigger {
  if (type === 'schedule') return { type, cron: '0 8 * * *', timezone: 'UTC' };
  if (type === 'task_due') return { type, lead_minutes: 60 };
  if (type === 'goal_deadline') return { type, lead_days: 1 };
  return { type };
}

/** Returns the default parameters for a newly added action type. */
function defaultAction(type: ActionType): Action {
  if (type === 'run_agent') return { type, profile_id: 'knowledge', instruction: '' };
  if (type === 'create_task') return { type, title: '', description: null, due_in_days: null };
  if (type === 'create_notification') return { type, message: '', link: null };
  if (type === 'generate_brief') return { type, scope: 'daily' };
  return { type, alias: '', event: '' };
}

/** Parses a scalar from form text according to the declared field type; null means invalid. */
function parseScalar(text: string, kind: string): Scalar | null {
  const value = text.trim();
  if (kind === 'number') { const n = Number(value); return value !== '' && Number.isFinite(n) ? n : null; }
  if (kind === 'boolean') return value === 'true' ? true : value === 'false' ? false : null;
  return value === '' ? null : value;
}

/** Converts the form draft into the API definition; returns message keys instead when invalid. */
function toDefinition(draft: Draft, caps: Capabilities): { definition?: Definition; errors: string[] } {
  const errors: string[] = [];
  const fields = caps.triggers.find((item) => item.type === draft.trigger.type)?.fields ?? {};
  if (draft.trigger.type === 'schedule' && !(draft.trigger.cron ?? '').trim()) errors.push('errCron');
  const conditions: Condition[] = draft.conditions.map((item) => {
    const kind = fields[item.field] ?? 'string';
    const parts = item.operator === 'in' ? item.value.split(',').map((part) => parseScalar(part, kind)) : [parseScalar(item.value, kind)];
    if (parts.some((part) => part === null)) errors.push('errCondition');
    return { field: item.field, operator: item.operator, value: item.operator === 'in' ? parts as Scalar[] : (parts[0] ?? '') as Scalar };
  });
  if (draft.actions.length === 0) errors.push('errNoActions');
  for (const action of draft.actions) {
    const missing = (action.type === 'run_agent' && !action.instruction?.trim()) || (action.type === 'create_task' && !action.title?.trim())
      || (action.type === 'create_notification' && !action.message?.trim()) || (action.type === 'call_webhook' && (!action.alias || !action.event?.trim()));
    if (missing) errors.push('errAction');
  }
  const trigger = { ...draft.trigger, ...(draft.trigger.type === 'schedule' ? { cron: draft.trigger.cron?.trim() } : {}) };
  return errors.length ? { errors: [...new Set(errors)] } : { definition: { trigger, conditions, actions: draft.actions }, errors };
}

/** Builds the initial form draft from an existing rule or an empty schedule rule. */
function initialDraft(rule?: Automation): Draft {
  if (!rule) return { name: '', trigger: defaultTrigger('schedule'), conditions: [], actions: [] };
  return {
    name: rule.name, trigger: rule.trigger,
    conditions: rule.conditions.map((c) => ({ field: c.field, operator: c.operator, value: Array.isArray(c.value) ? c.value.join(',') : String(c.value) })),
    actions: rule.actions,
  };
}

/** Schema-backed Trigger / Conditions / Actions form with scope estimate, preview and validation errors. */
export function RuleEditor({ rule, csrfToken, onDone }: { rule?: Automation; csrfToken: string; onDone: () => void }) {
  const t = useTranslations('automations');
  const client = useQueryClient();
  const caps = useQuery({ queryKey: ['automation-capabilities'], queryFn: getCapabilities });
  const [draft, setDraft] = useState<Draft>(() => initialDraft(rule));
  const [sample, setSample] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [formErrors, setFormErrors] = useState<string[]>([]);
  const [cleared, setCleared] = useState(0);

  // Actions whose owning module is disabled are cleared, never silently kept in a saveable draft.
  useEffect(() => {
    if (!caps.data) return;
    const usable = new Set(caps.data.actions.filter((item) => item.available).map((item) => item.type));
    setDraft((current) => {
      const kept = current.actions.filter((item) => usable.has(item.type));
      if (kept.length === current.actions.length) return current;
      setCleared(current.actions.length - kept.length);
      return { ...current, actions: kept };
    });
  }, [caps.data]);

  const triggerCap = caps.data?.triggers.find((item) => item.type === draft.trigger.type);
  const fields = triggerCap?.fields ?? {};
  const defined = useMemo(() => (caps.data ? toDefinition(draft, caps.data) : { errors: ['errCondition'] }), [draft, caps.data]);
  const targets = draft.actions.filter((item) => item.type === 'call_webhook' && item.alias).map((item) => item.alias as string);
  const approvals = draft.actions.filter((item) => item.type === 'call_webhook' || item.type === 'run_agent').length;

  const save = useMutation({
    mutationFn: async (definition: Definition) => (rule
      ? patchAutomation(rule.id, rule.revision, { name: draft.name.trim(), ...definition }, csrfToken)
      : createAutomation(draft.name.trim(), definition, csrfToken)),
    onSuccess: () => { void client.invalidateQueries({ queryKey: ['automations'] }); onDone(); },
  });
  const dry = useMutation({
    mutationFn: (definition: Definition) => {
      const values: Record<string, Scalar> = {};
      for (const [key, text] of Object.entries(sample)) {
        const parsed = fields[key] && text.trim() !== '' ? parseScalar(text, fields[key]) : null;
        if (parsed !== null) values[key] = parsed;
      }
      return previewAutomation(definition, values, csrfToken);
    },
    onSuccess: setPreview,
  });

  /** Validates locally, then runs a server dry preview (no queue, webhook, task or model call). */
  const runPreview = () => {
    const result = caps.data ? toDefinition(draft, caps.data) : { errors: ['errCondition'] as string[], definition: undefined };
    setFormErrors(result.errors);
    if (result.definition) dry.mutate(result.definition);
  };
  /** Validates locally and saves; server-side validation errors are shown beneath the form. */
  const submit = () => {
    const result = caps.data ? toDefinition(draft, caps.data) : { errors: ['errCondition'] as string[], definition: undefined };
    const errors = draft.name.trim() ? result.errors : [...result.errors, 'errName'];
    setFormErrors(errors);
    if (!errors.length && result.definition) save.mutate(result.definition);
  };
  /** Updates one action in place by index. */
  const setAction = (index: number, patch: Partial<Action>) => setDraft((d) => ({ ...d, actions: d.actions.map((a, i) => (i === index ? { ...a, ...patch } : a)) }));
  /** Updates one condition row in place by index. */
  const setCondition = (index: number, patch: Partial<CondForm>) => setDraft((d) => ({ ...d, conditions: d.conditions.map((c, i) => (i === index ? { ...c, ...patch } : c)) }));
  /** Moves an action up or down; actions run in listed order. */
  const move = (index: number, by: number) => setDraft((d) => {
    const next = [...d.actions];
    const [item] = next.splice(index, 1);
    next.splice(index + by, 0, item);
    return { ...d, actions: next };
  });

  if (caps.isPending) return <p role="status" className="text-sm text-muted-foreground">{t('loading')}</p>;
  if (caps.isError) return <div role="alert" className="space-y-2 text-sm"><p className="text-destructive">{t('loadFailed')}</p><Button variant="outline" onClick={() => void caps.refetch()}>{t('retry')}</Button></div>;
  const serverError = save.error instanceof ApiError ? save.error : null;

  return <form className="space-y-6" onSubmit={(event) => { event.preventDefault(); submit(); }}>
    <label className="block space-y-1 text-sm"><span className="font-medium">{t('name')}</span>
      <Input value={draft.name} maxLength={200} onChange={(e) => setDraft({ ...draft, name: e.target.value })} /></label>

    <fieldset className="space-y-3 rounded-lg border border-border bg-surface p-4">
      <legend className="px-1 text-sm font-semibold">{t('trigger')}</legend>
      <Select value={draft.trigger.type} onValueChange={(value) => setDraft({ ...draft, trigger: defaultTrigger(value as TriggerType), conditions: [] })}>
        <SelectTrigger aria-label={t('trigger')}><SelectValue /></SelectTrigger>
        <SelectContent>{caps.data.triggers.map((item) => <SelectItem key={item.type} value={item.type} disabled={!item.available}>
          {t(`trigger_${item.type}`)}{item.available ? '' : ` · ${t('unavailable')}`}</SelectItem>)}</SelectContent>
      </Select>
      {triggerCap && !triggerCap.available && <p role="status" className="text-sm text-destructive">{t(triggerCap.type === 'webhook' ? 'webhookUnavailable' : 'moduleDisabled')}</p>}
      {draft.trigger.type === 'schedule' && <div className="grid gap-3 sm:grid-cols-2">
        <label className="block space-y-1 text-sm"><span>{t('cron')}</span><Input value={draft.trigger.cron ?? ''} onChange={(e) => setDraft({ ...draft, trigger: { ...draft.trigger, cron: e.target.value } })} /></label>
        <label className="block space-y-1 text-sm"><span>{t('timezone')}</span><Input value={draft.trigger.timezone ?? 'UTC'} onChange={(e) => setDraft({ ...draft, trigger: { ...draft.trigger, timezone: e.target.value } })} /></label>
        <p className="text-xs text-muted-foreground sm:col-span-2">{t('scheduleHelp')}</p></div>}
      {draft.trigger.type === 'task_due' && <label className="block space-y-1 text-sm"><span>{t('leadMinutes')}</span><Input type="number" min={0} max={10080} value={draft.trigger.lead_minutes ?? 0} onChange={(e) => setDraft({ ...draft, trigger: { ...draft.trigger, lead_minutes: Number(e.target.value) } })} /></label>}
      {draft.trigger.type === 'goal_deadline' && <label className="block space-y-1 text-sm"><span>{t('leadDays')}</span><Input type="number" min={0} max={365} value={draft.trigger.lead_days ?? 0} onChange={(e) => setDraft({ ...draft, trigger: { ...draft.trigger, lead_days: Number(e.target.value) } })} /></label>}
    </fieldset>

    <fieldset className="space-y-3 rounded-lg border border-border bg-surface p-4">
      <legend className="px-1 text-sm font-semibold">{t('conditions')}</legend>
      <p className="text-xs text-muted-foreground">{t('conditionsHelp')}</p>
      {draft.conditions.map((cond, index) => <div key={index} className="grid gap-2 sm:grid-cols-[1fr_8rem_1fr_auto]">
        <Select value={cond.field} onValueChange={(value) => setCondition(index, { field: value, value: '' })}>
          <SelectTrigger aria-label={t('field')}><SelectValue /></SelectTrigger>
          <SelectContent>{Object.keys(fields).map((name) => <SelectItem key={name} value={name}>{name}</SelectItem>)}</SelectContent></Select>
        <Select value={cond.operator} onValueChange={(value) => setCondition(index, { operator: value as Operator })}>
          <SelectTrigger aria-label={t('operator')}><SelectValue /></SelectTrigger>
          <SelectContent>{operators.filter((op) => fields[cond.field] === 'number' || !['gt', 'gte', 'lt', 'lte'].includes(op)).map((op) => <SelectItem key={op} value={op}>{t(`op_${op}`)}</SelectItem>)}</SelectContent></Select>
        {fields[cond.field] === 'boolean' && cond.operator !== 'in'
          ? <Select value={cond.value || undefined} onValueChange={(value) => setCondition(index, { value })}>
            <SelectTrigger aria-label={t('value')}><SelectValue placeholder={t('value')} /></SelectTrigger>
            <SelectContent><SelectItem value="true">{t('yes')}</SelectItem><SelectItem value="false">{t('no')}</SelectItem></SelectContent></Select>
          : <Input aria-label={t('value')} placeholder={cond.operator === 'in' ? t('listHint') : t('value')} value={cond.value} onChange={(e) => setCondition(index, { value: e.target.value })} />}
        <Button type="button" variant="outline" onClick={() => setDraft({ ...draft, conditions: draft.conditions.filter((_, i) => i !== index) })}>{t('remove')}</Button>
      </div>)}
      <Button type="button" variant="outline" disabled={Object.keys(fields).length === 0 || draft.conditions.length >= 20}
        onClick={() => setDraft({ ...draft, conditions: [...draft.conditions, { field: Object.keys(fields)[0], operator: 'eq', value: '' }] })}>{t('addCondition')}</Button>
    </fieldset>

    <fieldset className="space-y-3 rounded-lg border border-border bg-surface p-4">
      <legend className="px-1 text-sm font-semibold">{t('actions')}</legend>
      <p className="text-xs text-muted-foreground">{t('actionsHelp')}</p>
      {cleared > 0 && <p role="status" className="text-sm text-destructive">{t('actionsCleared', { count: cleared })}</p>}
      {draft.actions.map((action, index) => <div key={index} className="space-y-2 rounded-md border border-border p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <strong className="text-sm">{index + 1}. {t(`action_${action.type}`)}</strong>
          <div className="flex gap-2">
            <Button type="button" size="sm" variant="outline" disabled={index === 0} onClick={() => move(index, -1)}>{t('moveUp')}</Button>
            <Button type="button" size="sm" variant="outline" disabled={index === draft.actions.length - 1} onClick={() => move(index, 1)}>{t('moveDown')}</Button>
            <Button type="button" size="sm" variant="outline" onClick={() => setDraft({ ...draft, actions: draft.actions.filter((_, i) => i !== index) })}>{t('remove')}</Button>
          </div></div>
        {(action.type === 'run_agent' || action.type === 'call_webhook') && <p className="text-xs text-muted-foreground">{t('needsApproval')}</p>}
        {action.type === 'run_agent' && <>
          <Select value={action.profile_id} onValueChange={(value) => setAction(index, { profile_id: value })}>
            <SelectTrigger aria-label={t('agentProfile')}><SelectValue /></SelectTrigger>
            <SelectContent>{agentProfiles.map((id) => <SelectItem key={id} value={id}>{id}</SelectItem>)}</SelectContent></Select>
          <Textarea aria-label={t('instruction')} placeholder={t('instruction')} maxLength={2000} value={action.instruction ?? ''} onChange={(e) => setAction(index, { instruction: e.target.value })} /></>}
        {action.type === 'create_task' && <div className="grid gap-2 sm:grid-cols-2">
          <Input aria-label={t('taskTitle')} placeholder={t('taskTitle')} maxLength={500} value={action.title ?? ''} onChange={(e) => setAction(index, { title: e.target.value })} />
          <Input aria-label={t('dueInDays')} type="number" min={0} max={365} placeholder={t('dueInDays')} value={action.due_in_days ?? ''} onChange={(e) => setAction(index, { due_in_days: e.target.value === '' ? null : Number(e.target.value) })} />
          <Textarea className="sm:col-span-2" aria-label={t('taskDescription')} placeholder={t('taskDescription')} value={action.description ?? ''} onChange={(e) => setAction(index, { description: e.target.value || null })} /></div>}
        {action.type === 'create_notification' && <div className="grid gap-2 sm:grid-cols-2">
          <Input aria-label={t('message')} placeholder={t('message')} maxLength={300} value={action.message ?? ''} onChange={(e) => setAction(index, { message: e.target.value })} />
          <Input aria-label={t('link')} placeholder={t('linkHint')} maxLength={300} value={action.link ?? ''} onChange={(e) => setAction(index, { link: e.target.value || null })} /></div>}
        {action.type === 'generate_brief' && <p className="text-xs text-muted-foreground">{t('briefHelp')}</p>}
        {action.type === 'call_webhook' && <div className="grid gap-2 sm:grid-cols-2">
          <Select value={action.alias || undefined} onValueChange={(value) => setAction(index, { alias: value })}>
            <SelectTrigger aria-label={t('webhookAlias')}><SelectValue placeholder={caps.data.webhook_aliases.length ? t('webhookAlias') : t('noAliases')} /></SelectTrigger>
            <SelectContent>{caps.data.webhook_aliases.map((alias) => <SelectItem key={alias} value={alias}>{alias}</SelectItem>)}</SelectContent></Select>
          <Input aria-label={t('eventName')} placeholder={t('eventName')} maxLength={100} value={action.event ?? ''} onChange={(e) => setAction(index, { event: e.target.value })} />
          <p className="text-xs text-muted-foreground sm:col-span-2">{t('webhookHelp')}</p></div>}
      </div>)}
      <Select value="" onValueChange={(value) => setDraft({ ...draft, actions: [...draft.actions, defaultAction(value as ActionType)] })}>
        <SelectTrigger aria-label={t('addAction')}><SelectValue placeholder={t('addAction')} /></SelectTrigger>
        <SelectContent>{caps.data.actions.map((item) => <SelectItem key={item.type} value={item.type} disabled={!item.available || draft.actions.length >= 10}>
          {t(`action_${item.type}`)}{item.available ? '' : ` · ${t('moduleDisabled')}`}</SelectItem>)}</SelectContent>
      </Select>
    </fieldset>

    <section aria-label={t('scope')} className="space-y-1 rounded-lg border border-border bg-surface p-4 text-sm">
      <h3 className="font-semibold">{t('scope')}</h3>
      <p>{t('scopeSummary', { actions: draft.actions.length, approvals, limit: maxRunsPerHour })}</p>
      <p>{targets.length ? t('scopeTargets', { targets: [...new Set(targets)].join(', ') }) : t('scopeNoTargets')}</p>
      {draft.actions.some((a) => a.type === 'run_agent') && <p>{t('scopeModelSpend')}</p>}
      <p className="text-muted-foreground">{t('startsDisabled')}</p>
    </section>

    {Object.keys(fields).length > 0 && <section className="space-y-2 rounded-lg border border-border bg-surface p-4">
      <h3 className="text-sm font-semibold">{t('preview')}</h3>
      <p className="text-xs text-muted-foreground">{t('previewHelp')}</p>
      <div className="grid gap-2 sm:grid-cols-2">{Object.entries(fields).map(([name, kind]) => <label key={name} className="block space-y-1 text-sm"><span>{name} · {kind}</span>
        <Input value={sample[name] ?? ''} onChange={(e) => setSample({ ...sample, [name]: e.target.value })} /></label>)}</div>
      <Button type="button" variant="outline" disabled={dry.isPending || defined.errors.length > 0} onClick={runPreview}>{t('runPreview')}</Button>
      {dry.error && <p role="alert" className="text-sm text-destructive">{t('previewFailed')}</p>}
      {preview && <div role="status" className="space-y-1 text-sm">
        <p className="font-medium">{preview.matched ? t('previewMatched') : t('previewNotMatched')}</p>
        <ul className="list-disc pl-5">{preview.reasons.map((r) => <li key={r.index}>{r.field} {t(`op_${r.operator}`)} · {t(`outcome_${r.outcome}`)}</li>)}</ul>
        {preview.planned_actions.length > 0 && <ul className="list-disc pl-5">{preview.planned_actions.map((a, i) => <li key={i}>{t(`action_${a.type}`)} · {a.module}{a.requires_approval ? ` · ${t('needsApprovalShort')}` : ''}</li>)}</ul>}
      </div>}
    </section>}

    {formErrors.length > 0 && <ul role="alert" className="list-disc pl-5 text-sm text-destructive">{formErrors.map((key) => <li key={key}>{t(key)}</li>)}</ul>}
    {serverError && <p role="alert" className="text-sm text-destructive">{t(errorMessageKey(serverError, 'saveFailed'))}</p>}
    <div className="flex flex-wrap gap-2">
      <Button type="submit" disabled={save.isPending}>{save.isPending ? t('saving') : t('save')}</Button>
      <Button type="button" variant="outline" disabled={save.isPending} onClick={onDone}>{t('cancel')}</Button>
    </div>
  </form>;
}
