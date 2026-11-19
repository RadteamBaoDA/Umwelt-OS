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
  BRIEF_MODEL_ROLES,
  getBriefRoleModels,
  getBriefTimezone,
  patchAutomation,
  type Action,
  type Automation,
  type BriefModelRole,
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

/** Literal catalog keys for the model-role options. */
const MODEL_ROLE_KEYS = {
  brief: 'scheduledTasks.modelRole.brief',
  chat: 'scheduledTasks.modelRole.chat',
  background: 'scheduledTasks.modelRole.background',
} as const;

/** Props: `rule` edits an existing task; `convert` converts `legacyRule`; omit both to create a new task. */
export interface BriefTaskEditorProps {
  rule?: Automation;
  /** Existing non-task brief rule; offered as the conflict resolution and used as the convert source. */
  legacyRule?: Automation;
  /** Open as a conversion of `legacyRule` (its settings prefill the form, Save patches it). */
  convert?: boolean;
  /** Pre-fills a NEW task (ignored when editing or converting); used by the chat draft card. */
  initial?: BriefInitial;
  csrfToken: string;
  /** Called after a successful save, just before `onClose`; lets the host move to the view that shows the task. */
  onSaved?: () => void;
  onClose: () => void;
}

/** Form values a host may pre-fill into a new task (the chat draft card). */
export interface BriefInitial {
  time: string;
  days: BriefDays;
  sections: readonly BriefSection[];
  extraInstruction: string | null;
  modelRole: BriefModelRole;
}

/** Form state of a new task pre-filled from `initial` (a chat draft). */
function draftState(initial: BriefInitial) {
  return {
    sections: new Set<BriefSection>(initial.sections.length ? initial.sections : ALL_BRIEF_SECTIONS),
    instruction: initial.extraInstruction ?? '',
    time: initial.time,
    days: initial.days,
    modelRole: initial.modelRole,
  };
}

/**
 * Reads the form defaults from the source rule's brief action and generated cron; without a source, from `initial`
 * (a chat draft) when given, else the template defaults.
 */
function initialState(source?: Automation, initial?: BriefInitial) {
  return !source && initial ? draftState(initial) : ruleState(source);
}

/** Form state read from the source rule's brief action and cron (template defaults when absent). */
function ruleState(source?: Automation) {
  const action = source?.actions.find((item) => item.type === 'generate_brief');
  const parsed = parseBriefCron(source?.trigger.cron ?? '');
  return {
    sections: new Set<BriefSection>(action?.sections?.length ? action.sections : ALL_BRIEF_SECTIONS),
    instruction: action?.extra_instruction ?? '',
    time: parsed?.time ?? '07:00',
    days: (parsed?.days ?? 'daily') as BriefDays,
    modelRole: (action?.model_role ?? 'brief') as BriefModelRole,
  };
}

/** Editable brief state: selected sections, extra instruction, "HH:MM" time, day pattern and model role. */
type BriefForm = ReturnType<typeof initialState>;

/**
 * Select over the three model roles, each labelled with the model Settings > Models resolves for it. A role with no
 * model (or while the settings are still loading) is disabled. Presentational; calls `onChange` with the role.
 */
export function ModelRoleSelect({
  value,
  models,
  onChange,
}: {
  value: BriefModelRole;
  models: Record<BriefModelRole, string | null> | undefined;
  onChange: (role: BriefModelRole) => void;
}) {
  const t = useTranslations('automations');
  return (
    <div className="space-y-1">
      <Label htmlFor="brief-model-role">{t('scheduledTasks.modelLabel')}</Label>
      <Select value={value} onValueChange={(next) => onChange(next as BriefModelRole)}>
        <SelectTrigger id="brief-model-role">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {BRIEF_MODEL_ROLES.map((role) => {
            const name = models?.[role];
            return (
              <SelectItem key={role} value={role} disabled={!name}>
                {t(MODEL_ROLE_KEYS[role])} - {name ?? t('scheduledTasks.modelUnset')}
              </SelectItem>
            );
          })}
        </SelectContent>
      </Select>
    </div>
  );
}

/**
 * Presentational body of the editor: section checklist, extra instruction with counter, time and days inputs, the
 * read-only timezone line, the model-role select and the delivery checkboxes (chat forced on, dashboard informational).
 * Calls `setForm` with a full replacement state; performs no I/O.
 */
function BriefFormFields({
  form,
  setForm,
  timezone,
  models,
}: {
  form: BriefForm;
  setForm: (next: BriefForm) => void;
  timezone: string;
  models: Record<BriefModelRole, string | null> | undefined;
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
      </dl>
      <ModelRoleSelect value={form.modelRole} models={models} onChange={(modelRole) => setForm({ ...form, modelRole })} />
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

/** An edited task already carries its timezone, so a new or converted task must wait for the workspace brief timezone. */
function zoneBlocksSave(edited: Automation | undefined, zonePending: boolean): boolean {
  return !edited && zonePending;
}

/**
 * Daily brief editor dialog. `initial` pre-fills a new task only (no I/O until Save). Creates an enabled schedule-triggered rule with one `generate_brief` action
 * (`deliver_chat: true`), edits an existing task with its `expected_revision`, or converts a legacy brief rule.
 * Sections are sent in canonical order; an edited task keeps its own timezone (the workspace brief timezone applies to new and converted tasks); `model_role` carries the chosen model role; a blank instruction is sent as null. A 409 `brief_slot_owned` response shows an
 * inline error with a Convert action when `legacyRule` is known. Side effects: one POST or PATCH, then list invalidation and `onSaved` before `onClose`.
 */
export function BriefTaskEditor({ rule, legacyRule, convert = false, initial, csrfToken, onSaved, onClose }: BriefTaskEditorProps) {
  const t = useTranslations('automations');
  const client = useQueryClient();
  const target = rule ?? (convert ? legacyRule : undefined);
  const [form, setForm] = useState(() => initialState(target, initial));
  const [errorKey, setErrorKey] = useState<ReturnType<typeof errorMessageKey> | null>(null);
  const [conflict, setConflict] = useState(false);
  const zone = useQuery({ queryKey: ['brief-timezone'], queryFn: getBriefTimezone, retry: false });
  const models = useQuery({ queryKey: ['brief-models'], queryFn: getBriefRoleModels, retry: false });
  const timezone = rule?.trigger.timezone ?? zone.data ?? Intl.DateTimeFormat().resolvedOptions().timeZone;

  /** Builds the generate_brief action from the form, keeping unrelated fields of the action being converted. */
  const buildAction = (previous?: Action): Action => ({
    ...previous,
    type: 'generate_brief',
    scope: 'daily',
    sections: ALL_BRIEF_SECTIONS.filter((section) => form.sections.has(section)),
    extra_instruction: form.instruction.trim() || null,
    deliver_chat: true,
    model_role: form.modelRole,
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
      onSaved?.();
      onClose();
    },
    onError: (error) => {
      setConflict(error instanceof ApiError && error.status === 409 && error.code === 'brief_slot_owned');
      setErrorKey(errorMessageKey(error, 'saveFailed'));
      void client.invalidateQueries({ queryKey: ['automations'] });
    },
  });

  const invalid = form.sections.size === 0 || !/^\d{2}:\d{2}$/.test(form.time) || zoneBlocksSave(rule, zone.isPending);

  return (
    <Dialog open onOpenChange={(open) => !open && !save.isPending && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{t(rule ? 'scheduledTasks.editorTitle' : 'scheduledTasks.editorNewTitle')}</DialogTitle>
          <DialogDescription>{t('scheduledTasks.description')}</DialogDescription>
        </DialogHeader>
        <BriefFormFields form={form} setForm={setForm} timezone={timezone} models={models.data} />
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
