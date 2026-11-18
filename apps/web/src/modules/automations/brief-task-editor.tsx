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
import { ApiError } from '@/core/api';
import {
  createScheduledTask,
  errorMessageKey,
  getBriefModel,
  getBriefTimezone,
  patchAutomation,
  type Action,
  type Automation,
  type BriefSection,
} from './api';
import { ALL_BRIEF_SECTIONS, MAX_EXTRA_INSTRUCTION, briefCron, parseBriefCron, type BriefDays } from './scheduled-task-model';

/** Literal catalog keys for the section checklist (checked against the locale files at compile time). */
const SECTION_KEYS = {
  stories: 'scheduledTasks.section.stories',
  markets: 'scheduledTasks.section.markets',
  weather: 'scheduledTasks.section.weather',
  hazards: 'scheduledTasks.section.hazards',
  agenda: 'scheduledTasks.section.agenda',
  source_health: 'scheduledTasks.section.source_health',
} as const;

/** Props: `rule` edits an existing task; `convert` converts `legacyRule`; omit both to create a new task. */
export interface BriefTaskEditorProps {
  rule?: Automation;
  /** Existing non-task brief rule; offered as the conflict resolution and used as the convert source. */
  legacyRule?: Automation;
  /** Open as a conversion of `legacyRule` (its settings prefill the form, Save patches it). */
  convert?: boolean;
  csrfToken: string;
  onClose: () => void;
}

/** Reads the form defaults from the source rule's brief action and generated cron, falling back to the template defaults. */
function initialState(source?: Automation) {
  const action = source?.actions.find((item) => item.type === 'generate_brief');
  const parsed = parseBriefCron(source?.trigger.cron ?? '');
  return {
    sections: new Set<BriefSection>(action?.sections?.length ? action.sections : ALL_BRIEF_SECTIONS),
    instruction: action?.extra_instruction ?? '',
    time: parsed?.time ?? '07:00',
    days: (parsed?.days ?? 'daily') as BriefDays,
  };
}

/** Editable brief state: selected sections, extra instruction, "HH:MM" time and day pattern. */
type BriefForm = ReturnType<typeof initialState>;

/**
 * Presentational body of the editor: section checklist, extra instruction with counter, time and days inputs, the
 * read-only timezone and model lines and the delivery checkboxes (chat forced on, dashboard informational).
 * Calls `setForm` with a full replacement state; performs no I/O.
 */
function BriefFormFields({
  form,
  setForm,
  timezone,
  model,
}: {
  form: BriefForm;
  setForm: (next: BriefForm) => void;
  timezone: string;
  model: string | null;
}) {
  const t = useTranslations('automations');
  /** Adds or removes one section from the selection. */
  const toggleSection = (section: BriefSection, on: boolean) => {
    const next = new Set(form.sections);
    if (on) next.add(section);
    else next.delete(section);
    setForm({ ...form, sections: next });
  };
  return (
    <>
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">{t('scheduledTasks.sectionsLabel')}</legend>
        {ALL_BRIEF_SECTIONS.map((section) => (
          <div key={section} className="flex items-center gap-2">
            <Checkbox
              id={`brief-section-${section}`}
              checked={form.sections.has(section)}
              onCheckedChange={(value) => toggleSection(section, value === true)}
            />
            <Label htmlFor={`brief-section-${section}`}>{t(SECTION_KEYS[section])}</Label>
          </div>
        ))}
      </fieldset>
      <div className="space-y-1">
        <Label htmlFor="brief-instruction">{t('scheduledTasks.extraLabel')}</Label>
        <Textarea
          id="brief-instruction"
          value={form.instruction}
          maxLength={MAX_EXTRA_INSTRUCTION}
          onChange={(event) => setForm({ ...form, instruction: event.target.value })}
        />
        <p className="text-xs text-muted-foreground">
          {t('scheduledTasks.extraCounter', { count: form.instruction.length, max: MAX_EXTRA_INSTRUCTION })}
        </p>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1">
          <Label htmlFor="brief-time">{t('scheduledTasks.timeLabel')}</Label>
          <Input id="brief-time" type="time" value={form.time} onChange={(event) => setForm({ ...form, time: event.target.value })} />
        </div>
        <div className="space-y-1">
          <Label htmlFor="brief-days">{t('scheduledTasks.daysLabel')}</Label>
          <Select value={form.days} onValueChange={(value) => setForm({ ...form, days: value as BriefDays })}>
            <SelectTrigger id="brief-days">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="daily">{t('scheduledTasks.days.daily')}</SelectItem>
              <SelectItem value="weekdays">{t('scheduledTasks.days.weekdays')}</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>
      <dl className="space-y-1 text-sm">
        <div className="flex gap-2">
          <dt className="text-muted-foreground">{t('scheduledTasks.timezoneLabel')}</dt>
          <dd className="font-mono">{timezone}</dd>
        </div>
        <div className="flex gap-2">
          <dt className="text-muted-foreground">{t('scheduledTasks.modelLabel')}</dt>
          <dd>{model ?? t('scheduledTasks.modelUnset')}</dd>
        </div>
      </dl>
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">{t('scheduledTasks.deliverLabel')}</legend>
        <div className="flex items-center gap-2">
          <Checkbox id="brief-deliver-chat" checked disabled />
          <Label htmlFor="brief-deliver-chat">
            {t('scheduledTasks.deliverChat')} <span className="text-muted-foreground">{t('scheduledTasks.deliverChatNote')}</span>
          </Label>
        </div>
        <div className="flex items-center gap-2">
          <Checkbox id="brief-deliver-dashboard" checked disabled />
          <Label htmlFor="brief-deliver-dashboard">{t('scheduledTasks.deliverDashboard')}</Label>
        </div>
      </fieldset>
    </>
  );
}

/**
 * Inline save error. A slot-owned conflict replaces the generic message with the convert explanation and, when
 * `onConvert` is provided (a legacy brief rule is known), shows the Convert action.
 */
function BriefError({
  errorKey,
  conflict,
  pending,
  onConvert,
}: {
  errorKey: ReturnType<typeof errorMessageKey>;
  conflict: boolean;
  pending: boolean;
  onConvert?: () => void;
}) {
  const t = useTranslations('automations');
  return (
    <div role="alert" className="space-y-2 text-sm text-destructive">
      <p>{conflict ? t('scheduledTasks.slotConflict') : t(errorKey)}</p>
      {conflict && onConvert && (
        <Button type="button" variant="outline" size="sm" disabled={pending} onClick={onConvert}>
          {pending ? t('scheduledTasks.converting') : t('scheduledTasks.convert')}
        </Button>
      )}
    </div>
  );
}

/**
 * Daily brief editor dialog. Creates an enabled schedule-triggered rule with one `generate_brief` action
 * (`deliver_chat: true`), edits an existing task with its `expected_revision`, or converts a legacy brief rule.
 * Sections are sent in canonical order; a blank instruction is sent as null. A 409 `brief_slot_owned` response shows an
 * inline error with a Convert action when `legacyRule` is known. Side effects: one POST or PATCH, then list invalidation.
 */
export function BriefTaskEditor({ rule, legacyRule, convert = false, csrfToken, onClose }: BriefTaskEditorProps) {
  const t = useTranslations('automations');
  const client = useQueryClient();
  const target = rule ?? (convert ? legacyRule : undefined);
  const [form, setForm] = useState(() => initialState(target));
  const [errorKey, setErrorKey] = useState<ReturnType<typeof errorMessageKey> | null>(null);
  const [conflict, setConflict] = useState(false);
  const zone = useQuery({ queryKey: ['brief-timezone'], queryFn: getBriefTimezone, retry: false });
  const model = useQuery({ queryKey: ['brief-model'], queryFn: getBriefModel, retry: false });
  const timezone = zone.data ?? target?.trigger.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone;

  /** Builds the generate_brief action from the form, keeping unrelated fields of the action being converted. */
  const buildAction = (previous?: Action): Action => ({
    ...previous,
    type: 'generate_brief',
    scope: 'daily',
    sections: ALL_BRIEF_SECTIONS.filter((section) => form.sections.has(section)),
    extra_instruction: form.instruction.trim() || null,
    deliver_chat: true,
  });
  const trigger = { type: 'schedule' as const, cron: briefCron(form.time, form.days), timezone };

  const save = useMutation({
    mutationFn: (source: Automation | undefined) => {
      const previous = source?.actions.find((item) => item.type === 'generate_brief');
      if (source) return patchAutomation(source.id, source.revision, { trigger, actions: [buildAction(previous)] }, csrfToken);
      return createScheduledTask(t('scheduledTasks.briefName'), { trigger, conditions: [], actions: [buildAction()] }, csrfToken);
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['automations'] });
      onClose();
    },
    onError: (error) => {
      setConflict(error instanceof ApiError && error.status === 409 && error.code === 'brief_slot_owned');
      setErrorKey(errorMessageKey(error, 'saveFailed'));
      void client.invalidateQueries({ queryKey: ['automations'] });
    },
  });

  const invalid = form.sections.size === 0 || !/^\d{2}:\d{2}$/.test(form.time) || zone.isPending;

  return (
    <Dialog open onOpenChange={(open) => !open && !save.isPending && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{t(rule ? 'scheduledTasks.editorTitle' : 'scheduledTasks.editorNewTitle')}</DialogTitle>
          <DialogDescription>{t('scheduledTasks.description')}</DialogDescription>
        </DialogHeader>
        <BriefFormFields form={form} setForm={setForm} timezone={timezone} model={model.data ?? null} />
        {errorKey && (
          <BriefError
            errorKey={errorKey}
            conflict={conflict}
            pending={save.isPending}
            onConvert={legacyRule && (() => save.mutate(legacyRule))}
          />
        )}
        <DialogFooter>
          <Button type="button" variant="outline" disabled={save.isPending} onClick={onClose}>
            {t('scheduledTasks.cancel')}
          </Button>
          <Button type="button" disabled={invalid || save.isPending} onClick={() => save.mutate(target)}>
            {save.isPending ? t('scheduledTasks.saving') : t('scheduledTasks.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
