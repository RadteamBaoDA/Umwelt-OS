import type { AppLocaleId } from '@/core/i18n';
import { getWidgetCatalog } from '../../../../locales/widget-catalogs';

/** Nested `chat` catalog tree for one locale (the shape of messages/<locale>/chat.json). */
export type ChatCatalog = ReturnType<typeof getWidgetCatalog<'chat'>>;

/**
 * Resolves the effective widget locale from an explicit prop or the document `lang` attribute,
 * so standalone widgets and unit tests work without a NextIntlClientProvider wrapper.
 *
 * @param locale - Optional explicit locale identifier.
 * @returns Normalized application locale ('en-us' | 'vi-vi').
 */
export function resolveWidgetLocale(locale?: AppLocaleId): AppLocaleId {
  if (locale === 'vi-vi' || locale === 'en-us') {
    return locale;
  }
  if (typeof document !== 'undefined' && document.documentElement.lang.toLowerCase().startsWith('vi')) {
    return 'vi-vi';
  }
  return 'en-us';
}

/**
 * Returns the `chat` message catalog for a widget locale (single source of truth for all
 * `genUi*` copy; widgets must not keep local en-us/vi-vi dictionaries).
 *
 * @param locale - Optional explicit locale identifier (falls back to document lang).
 * @returns Nested `chat` catalog for the resolved locale (genUi copy lives under `genUi.<feature>.<key>`).
 */
export function genUiCopy(locale?: AppLocaleId): ChatCatalog {
  return getWidgetCatalog(resolveWidgetLocale(locale), 'chat');
}

/**
 * Substitutes `{name}` placeholders in a catalog template. Unknown placeholders are left intact.
 *
 * @param template - Catalog message containing `{name}` placeholders.
 * @param values - Placeholder values.
 * @returns Interpolated string.
 */
export function fmt(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (match, key: string) => (key in values ? String(values[key]) : match));
}

/** Builds one stable copy object per locale at module load (the catalogs are static JSON imports). */
function perLocale<T>(build: (c: ReturnType<typeof genUiCopy>) => T): Record<AppLocaleId, T> {
  return { 'en-us': build(getWidgetCatalog('en-us', 'chat')), 'vi-vi': build(getWidgetCatalog('vi-vi', 'chat')) };
}

/** Adapter exposing PlanCard copy from the shared catalog (keeps call sites readable). */
export const PLAN_COPY = perLocale((c) => ({
  progressLabel: (completed: number, total: number) => fmt(c.genUi.plan.progress, { completed, total }),
  statusCompleted: c.genUi.plan.statusCompleted,
  statusInProgress: c.genUi.plan.statusInProgress,
  statusPending: c.genUi.plan.statusPending,
  approvedNotice: c.genUi.plan.approved,
  approachLabel: c.genUi.plan.approach,
  technologyLabel: c.genUi.plan.technology,
  approvePrompt: (title: string, steps: string[]) => fmt(c.genUi.plan.approvePrompt, { title, steps: steps.join('; ') }),
  refinePrompt: (title: string) => fmt(c.genUi.plan.refinePrompt, { title }),
}));

/** Adapter exposing MeetingTimePicker copy from the shared catalog. */
export const MEETING_COPY = perLocale((c) => ({
  defaultTitle: c.genUi.meeting.defaultTitle,
  availableBadge: c.genUi.meeting.available,
  unavailableBadge: c.genUi.meeting.unavailable,
  recommendedBadge: c.genUi.meeting.recommended,
  suggestAlternative: c.genUi.meeting.suggestAlternative,
  confirmedStatus: (date: string, time: string) => fmt(c.genUi.meeting.confirmed, { date, time }),
  declinedStatus: c.genUi.meeting.declined,
  confirmPrompt: (title: string, date: string, time: string, label?: string) =>
    fmt(c.genUi.meeting.confirmPrompt, { title, date, time, label: label ? ` (${label})` : '' }),
  declinePrompt: (title: string) => fmt(c.genUi.meeting.declinePrompt, { title }),
}));

/** Adapter exposing the user-input request card copy (HITL `request_user_input`). */
export const INPUT_COPY = perLocale((c) => ({
  submit: c.genUi.input.submit,
  textPlaceholder: c.genUi.input.textPlaceholder,
  answered: (label: string) => fmt(c.genUi.input.answered, { label }),
  declined: c.genUi.input.declined,
  expired: c.genUi.input.expired,
  cancelled: c.genUi.input.cancelled,
  failed: c.genUi.input.failed,
}));

/** Adapter exposing TableCatalog copy from the shared catalog. */
export const TABLE_COPY = perLocale((c) => ({
  emptyTitle: c.genUi.table.empty,
  emptyFiltered: c.genUi.table.emptyFiltered,
  sourcePrefix: c.genUi.table.source,
  actionsHeader: c.genUi.table.actions,
  defaultActionLabel: c.genUi.table.analyze,
  rowCountLabel: (visible: number, total: number) =>
    visible === total ? fmt(c.genUi.table.rowCountAll, { total }) : fmt(c.genUi.table.rowCountSome, { visible, total }),
  sortByLabel: (label: string) => fmt(c.genUi.table.sortBy, { label }),
  rowPrompt: (title: string, summary: string) => fmt(c.genUi.table.rowPrompt, { title, summary }),
}));

/** Adapter exposing generic Form widget copy from the shared catalog. */
export const FORM_COPY = perLocale((c) => ({
  defaultSubmit: c.genUi.form.defaultSubmit,
  required: (label: string) => fmt(c.genUi.form.required, { label }),
  maxLength: (label: string, max: number) => fmt(c.genUi.form.maxLength, { label, max }),
  numberInvalid: (label: string) => fmt(c.genUi.form.numberInvalid, { label }),
  min: (label: string, min: number) => fmt(c.genUi.form.min, { label, min }),
  max: (label: string, max: number) => fmt(c.genUi.form.max, { label, max }),
  tooLong: c.genUi.form.tooLong,
  selectPlaceholder: c.genUi.form.selectPlaceholder,
  sending: c.genUi.form.sending,
  submitted: c.genUi.form.submitted,
  sendFailed: c.genUi.form.sendFailed,
  promptHeader: (title: string) => fmt(c.genUi.form.promptHeader, { title }),
  yes: c.genUi.form.yes,
  no: c.genUi.form.no,
  empty: c.genUi.form.empty,
  optional: c.genUi.form.optional,
}));
