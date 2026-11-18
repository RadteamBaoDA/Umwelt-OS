'use client';

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import {
  createScheduledTask,
  errorMessageKey,
  getBriefRoleModels,
  getBriefTimezone,
  patchAutomation,
  type Action,
  type Automation,
  type BriefModelRole,
  type TopicWatchSpec,
} from './api';
import { ModelRoleSelect } from './brief-task-editor';
import { MAX_TOPIC_ITEMS, briefCron, estimateTokens, parseBriefCron, tokenCap, type BriefDays } from './scheduled-task-model';

/** Maximum topic / keep-rule length and sites per list (backend limits). */
const MAX_TEXT = 500;
const MAX_SITES = 10;
/** Languages offered as checkboxes; any other language already stored on a watch is preserved untouched. */
const LANGUAGES = ['en', 'vi'] as const;
const LANGUAGE_KEYS = { en: 'scheduledTasks.topic.langEn', vi: 'scheduledTasks.topic.langVi' } as const;

/** Props: `rule` edits an existing Topic watch; omit it to create a new one. */
export interface TopicWatchEditorProps {
  rule?: Automation;
  csrfToken: string;
  /** Called after a successful save, just before `onClose`. */
  onSaved?: () => void;
  onClose: () => void;
}

/** Editable form state; sites are comma lists kept as text until save. */
interface TopicForm {
  name: string;
  topic: string;
  keepRule: string;
  languages: string[];
  include: string;
  exclude: string;
  maxItems: number;
  time: string;
  days: BriefDays;
  modelRole: BriefModelRole;
}

/** Splits a comma list into trimmed, de-duplicated, non-empty entries. */
function parseSites(text: string): string[] {
  return [
    ...new Set(
      text
        .split(',')
        .map((site) => site.trim())
        .filter(Boolean),
    ),
  ];
}

/** Form state read from an existing rule (template defaults when creating). */
function initialForm(rule?: Automation): TopicForm {
  const action = rule?.actions.find((item) => item.type === 'collect_source');
  const watch = action?.watch;
  const parsed = parseBriefCron(rule?.trigger.cron ?? '');
  return {
    ...watchFields(watch),
    name: rule?.name ?? '',
    time: parsed?.time ?? '08:00',
    days: parsed?.days ?? 'daily',
    modelRole: action?.model_role ?? 'background',
  };
}

/** The watch-spec part of the form, with template defaults for a missing spec. */
function watchFields(watch?: TopicWatchSpec) {
  return {
    topic: watch?.topic ?? '',
    keepRule: watch?.keep_rule ?? '',
    languages: watch?.languages?.length ? watch.languages : ['en'],
    include: (watch?.include_sites ?? []).join(', '),
    exclude: (watch?.exclude_sites ?? []).join(', '),
    maxItems: watch?.max_items ?? 5,
  };
}

/** The watch spec sent to the server for a form (a blank keep rule becomes null). */
function toWatch(form: TopicForm): TopicWatchSpec {
  return {
    topic: form.topic.trim(),
    keep_rule: form.keepRule.trim() || null,
    languages: form.languages,
    include_sites: parseSites(form.include),
    exclude_sites: parseSites(form.exclude),
    max_items: form.maxItems,
  };
}

/**
 * Stable key of every field the consent hash covers (watch spec, schedule, model role). Two forms with the same key
 * keep their consent; a different key requires the owner to consent again.
 */
function consentKey(form: TopicForm): string {
  return JSON.stringify([toWatch(form), form.time, form.days, form.modelRole]);
}

/** Presentational fields: name, topic, keep rule, languages, sites, max items, schedule and model role. */
function TopicFields({
  form,
  setForm,
  models,
}: {
  form: TopicForm;
  setForm: (next: TopicForm) => void;
  models: Record<BriefModelRole, string | null> | undefined;
}) {
  const t = useTranslations('automations');
  const toggleLanguage = (language: string, on: boolean) => {
    const rest = form.languages.filter((item) => item !== language);
    setForm({ ...form, languages: on ? [...rest, language] : rest });
  };
  return (
    <>
      <div className="space-y-1">
        <Label htmlFor="topic-name">{t('scheduledTasks.topic.nameLabel')}</Label>
        <Input id="topic-name" value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} />
      </div>
      <div className="space-y-1">
        <Label htmlFor="topic-topic">{t('scheduledTasks.topic.topicLabel')}</Label>
        <Textarea
          id="topic-topic"
          value={form.topic}
          maxLength={MAX_TEXT}
          onChange={(event) => setForm({ ...form, topic: event.target.value })}
        />
      </div>
      <div className="space-y-1">
        <Label htmlFor="topic-keep">{t('scheduledTasks.topic.keepLabel')}</Label>
        <Textarea
          id="topic-keep"
          value={form.keepRule}
          maxLength={MAX_TEXT}
          onChange={(event) => setForm({ ...form, keepRule: event.target.value })}
        />
        <p className="text-xs text-muted-foreground">{t('scheduledTasks.topic.keepHint')}</p>
      </div>
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">{t('scheduledTasks.topic.languagesLabel')}</legend>
        {LANGUAGES.map((language) => (
          <div key={language} className="flex items-center gap-2">
            <Checkbox
              id={`topic-lang-${language}`}
              checked={form.languages.includes(language)}
              onCheckedChange={(value) => toggleLanguage(language, value === true)}
            />
            <Label htmlFor={`topic-lang-${language}`}>{t(LANGUAGE_KEYS[language])}</Label>
          </div>
        ))}
      </fieldset>
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1">
          <Label htmlFor="topic-include">{t('scheduledTasks.topic.includeLabel')}</Label>
          <Input id="topic-include" value={form.include} onChange={(event) => setForm({ ...form, include: event.target.value })} />
        </div>
        <div className="space-y-1">
          <Label htmlFor="topic-exclude">{t('scheduledTasks.topic.excludeLabel')}</Label>
          <Input id="topic-exclude" value={form.exclude} onChange={(event) => setForm({ ...form, exclude: event.target.value })} />
        </div>
      </div>
      <p className="text-xs text-muted-foreground">{t('scheduledTasks.topic.sitesHint', { max: MAX_SITES })}</p>
      <div className="grid grid-cols-3 gap-3">
        <div className="space-y-1">
          <Label htmlFor="topic-max">{t('scheduledTasks.topic.maxItemsLabel')}</Label>
          <Input
            id="topic-max"
            type="number"
            min={1}
            max={MAX_TOPIC_ITEMS}
            value={form.maxItems}
            onChange={(event) => setForm({ ...form, maxItems: Number(event.target.value) })}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="topic-time">{t('scheduledTasks.timeLabel')}</Label>
          <Input id="topic-time" type="time" value={form.time} onChange={(event) => setForm({ ...form, time: event.target.value })} />
        </div>
        <div className="space-y-1">
          <Label htmlFor="topic-days">{t('scheduledTasks.daysLabel')}</Label>
          <Select value={form.days} onValueChange={(value) => setForm({ ...form, days: value as BriefDays })}>
            <SelectTrigger id="topic-days">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="daily">{t('scheduledTasks.days.daily')}</SelectItem>
              <SelectItem value="weekdays">{t('scheduledTasks.days.weekdays')}</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>
      <ModelRoleSelect value={form.modelRole} models={models} onChange={(modelRole) => setForm({ ...form, modelRole })} />
    </>
  );
}

/**
 * Consent block: the cost statement (model, estimate and cap from the shared formula) and the required checkbox.
 * When consent is not required (an edit that changed no watched field) it shows the "already allowed" note instead.
 */
function ConsentBlock({
  required,
  checked,
  onChange,
  maxItems,
  model,
}: {
  required: boolean;
  checked: boolean;
  onChange: (value: boolean) => void;
  maxItems: number;
  model: string;
}) {
  const t = useTranslations('automations');
  if (!required) return <p className="text-sm text-muted-foreground">{t('scheduledTasks.topic.consentKept')}</p>;
  return (
    <fieldset className="space-y-2 rounded-md border border-border bg-surface p-3">
      <legend className="px-1 text-sm font-medium">{t('scheduledTasks.topic.consentTitle')}</legend>
      <p className="text-sm">
        {t('scheduledTasks.topic.consentBody', {
          model,
          tokens: estimateTokens(maxItems),
          cap: tokenCap(maxItems),
        })}
      </p>
      <div className="flex items-center gap-2">
        <Checkbox id="topic-consent" checked={checked} onCheckedChange={(value) => onChange(value === true)} />
        <Label htmlFor="topic-consent">{t('scheduledTasks.topic.consentCheckbox')}</Label>
      </div>
    </fieldset>
  );
}

/** True when the form cannot be saved: empty topic, no language, too many sites, max items outside 1-10 or a bad time. */
function formInvalid(form: TopicForm): boolean {
  const tooManySites = [form.include, form.exclude].some((list) => parseSites(list).length > MAX_SITES);
  const badItems = !Number.isInteger(form.maxItems) || form.maxItems < 1 || form.maxItems > MAX_TOPIC_ITEMS;
  return !form.topic.trim() || form.languages.length === 0 || tooManySites || badItems || !/^\d{2}:\d{2}$/.test(form.time);
}

/** True when saving must carry a fresh consent: a new watch, a stored watch without consent, or a changed consent key. */
function needsConsent(rule: Automation | undefined, form: TopicForm, initialKey: string): boolean {
  const hadConsent = Boolean(rule?.actions.find((item) => item.type === 'collect_source')?.consent);
  return !rule || !hadConsent || consentKey(form) !== initialKey;
}

/** Cancel and Save buttons; Save shows the saving label while the request is in flight. */
function TopicFooter({
  pending,
  disabled,
  onCancel,
  onSave,
}: {
  pending: boolean;
  disabled: boolean;
  onCancel: () => void;
  onSave: () => void;
}) {
  const t = useTranslations('automations');
  return (
    <DialogFooter>
      <Button type="button" variant="outline" disabled={pending} onClick={onCancel}>
        {t('scheduledTasks.cancel')}
      </Button>
      <Button type="button" disabled={disabled || pending} onClick={onSave}>
        {pending ? t('scheduledTasks.saving') : t('scheduledTasks.save')}
      </Button>
    </DialogFooter>
  );
}

/**
 * Save mutation of the editor: POST (new) or revision-fenced PATCH (edit) of one `collect_source` action. The consent
 * grant is attached only when `consentRequired`. On success it invalidates the rule list and calls `onDone`; on
 * failure it reports a localized error key through `setErrorKey` and reloads the list (the revision may be stale).
 */
function useTopicSave({
  rule,
  form,
  timezone,
  consentRequired,
  csrfToken,
  onDone,
  setErrorKey,
}: {
  rule?: Automation;
  form: TopicForm;
  timezone: string;
  consentRequired: boolean;
  csrfToken: string;
  onDone: () => void;
  setErrorKey: (key: ReturnType<typeof errorMessageKey>) => void;
}) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => {
      const action: Action = {
        type: 'collect_source',
        watch: toWatch(form),
        model_role: form.modelRole,
        ...(consentRequired ? { grant_consent_est_tokens: estimateTokens(form.maxItems) } : {}),
      };
      const trigger = { type: 'schedule' as const, cron: briefCron(form.time, form.days), timezone };
      const name = form.name.trim() || form.topic.trim().slice(0, 60);
      if (rule) return patchAutomation(rule.id, rule.revision, { name, trigger, actions: [action] }, csrfToken);
      return createScheduledTask(name, { trigger, conditions: [], actions: [action] }, csrfToken);
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['automations'] });
      onDone();
    },
    onError: (error) => {
      setErrorKey(errorMessageKey(error, 'saveFailed'));
      void client.invalidateQueries({ queryKey: ['automations'] });
    },
  });
}

/**
 * Topic watch editor dialog. Creates an enabled schedule rule with one `collect_source` action (POST), or edits an
 * existing watch (revision-fenced PATCH). Consent is explicit and one-time: it is required on create, whenever the
 * stored watch has no consent, and whenever a field the consent hash covers (topic, keep rule, languages, sites,
 * max items, schedule, model role) changed; only then is `grant_consent_est_tokens` sent. Saving is disabled until the
 * topic is non-empty, a language is chosen, site lists fit their bound, max items is 1-10 and, when required, consent
 * is ticked. Side effects: one request, list invalidation, then `onSaved` and `onClose`. A rejected estimate shows a
 * localized error (server text is never rendered).
 */
export function TopicWatchEditor({ rule, csrfToken, onSaved, onClose }: TopicWatchEditorProps) {
  const t = useTranslations('automations');
  const [form, setForm] = useState(() => initialForm(rule));
  const [initialKey] = useState(() => consentKey(initialForm(rule)));
  const [consent, setConsent] = useState(false);
  const [errorKey, setErrorKey] = useState<ReturnType<typeof errorMessageKey> | null>(null);
  const zone = useQuery({ queryKey: ['brief-timezone'], queryFn: getBriefTimezone, retry: false });
  const models = useQuery({ queryKey: ['brief-models'], queryFn: getBriefRoleModels, retry: false });
  const timezone = rule?.trigger.timezone ?? zone.data ?? Intl.DateTimeFormat().resolvedOptions().timeZone;
  const consentRequired = needsConsent(rule, form, initialKey);

  const save = useTopicSave({
    rule,
    form,
    timezone,
    consentRequired,
    csrfToken,
    onDone: () => {
      onSaved?.();
      onClose();
    },
    setErrorKey,
  });

  const invalid = formInvalid(form) || (consentRequired && !consent) || (!rule && zone.isPending);
  const model = models.data?.[form.modelRole] ?? t('scheduledTasks.modelUnset');

  return (
    <Dialog open onOpenChange={(open) => !open && !save.isPending && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{t(rule ? 'scheduledTasks.topic.editorTitle' : 'scheduledTasks.topic.editorNewTitle')}</DialogTitle>
          <DialogDescription>{t('scheduledTasks.topic.templateBody')}</DialogDescription>
        </DialogHeader>
        <TopicFields form={form} setForm={setForm} models={models.data} />
        <ConsentBlock required={consentRequired} checked={consent} onChange={setConsent} maxItems={form.maxItems} model={model} />
        {errorKey && (
          <p role="alert" className="text-sm text-destructive">
            {t(errorKey)}
          </p>
        )}
        <TopicFooter pending={save.isPending} disabled={invalid} onCancel={onClose} onSave={() => save.mutate()} />
      </DialogContent>
    </Dialog>
  );
}
