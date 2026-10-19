import type { AppLocaleId } from '@/core/i18n';
import { chatMessages } from '@/core/messages/chat';

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
 * @returns Flat message catalog for the resolved locale.
 */
export function genUiCopy(locale?: AppLocaleId): (typeof chatMessages)[AppLocaleId] {
  return chatMessages[resolveWidgetLocale(locale)];
}

/**
 * Substitutes `{name}` placeholders in a catalog template. Unknown placeholders are left intact.
 *
 * @param template - Catalog message containing `{name}` placeholders.
 * @param values - Placeholder values.
 * @returns Interpolated string.
 */
export function fmt(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (match, key: string) =>
    key in values ? String(values[key]) : match,
  );
}

/** Builds one stable copy object per locale at module load (chatMessages is static). */
function perLocale<T>(build: (c: ReturnType<typeof genUiCopy>) => T): Record<AppLocaleId, T> {
  return { 'en-us': build(chatMessages['en-us']), 'vi-vi': build(chatMessages['vi-vi']) };
}

/** Adapter exposing PlanCard copy from the shared catalog (keeps call sites readable). */
export const PLAN_COPY = perLocale((c) => ({
    progressLabel: (completed: number, total: number) => fmt(c.genUiPlanProgress, { completed, total }),
    statusCompleted: c.genUiPlanStatusCompleted,
    statusInProgress: c.genUiPlanStatusInProgress,
    statusPending: c.genUiPlanStatusPending,
    approvedNotice: c.genUiPlanApproved,
    approachLabel: c.genUiPlanApproach,
    technologyLabel: c.genUiPlanTechnology,
    approvePrompt: (title: string, steps: string[]) =>
      fmt(c.genUiPlanApprovePrompt, { title, steps: steps.join('; ') }),
    refinePrompt: (title: string) => fmt(c.genUiPlanRefinePrompt, { title }),
}));

/** Adapter exposing MeetingTimePicker copy from the shared catalog. */
export const MEETING_COPY = perLocale((c) => ({
    defaultTitle: c.genUiMeetingDefaultTitle,
    availableBadge: c.genUiMeetingAvailable,
    unavailableBadge: c.genUiMeetingUnavailable,
    recommendedBadge: c.genUiMeetingRecommended,
    suggestAlternative: c.genUiMeetingSuggestAlternative,
    confirmedStatus: (date: string, time: string) => fmt(c.genUiMeetingConfirmed, { date, time }),
    declinedStatus: c.genUiMeetingDeclined,
    confirmPrompt: (title: string, date: string, time: string, label?: string) =>
      fmt(c.genUiMeetingConfirmPrompt, { title, date, time, label: label ? ` (${label})` : '' }),
    declinePrompt: (title: string) => fmt(c.genUiMeetingDeclinePrompt, { title }),
}));

/** Adapter exposing the user-input request card copy (HITL `request_user_input`). */
export const INPUT_COPY = perLocale((c) => ({
    submit: c.genUiInputSubmit,
    textPlaceholder: c.genUiInputTextPlaceholder,
    answered: (label: string) => fmt(c.genUiInputAnswered, { label }),
    declined: c.genUiInputDeclined,
    expired: c.genUiInputExpired,
    cancelled: c.genUiInputCancelled,
    failed: c.genUiInputFailed,
}));

/** Adapter exposing TableCatalog copy from the shared catalog. */
export const TABLE_COPY = perLocale((c) => ({
    emptyTitle: c.genUiTableEmpty,
    emptyFiltered: c.genUiTableEmptyFiltered,
    sourcePrefix: c.genUiTableSource,
    actionsHeader: c.genUiTableActions,
    defaultActionLabel: c.genUiTableAnalyze,
    rowCountLabel: (visible: number, total: number) =>
      visible === total
        ? fmt(c.genUiTableRowCountAll, { total })
        : fmt(c.genUiTableRowCountSome, { visible, total }),
    sortByLabel: (label: string) => fmt(c.genUiTableSortBy, { label }),
    rowPrompt: (title: string, summary: string) => fmt(c.genUiTableRowPrompt, { title, summary }),
}));

/** Keys of the `chat` catalog whose value is a plain string (excludes nested objects). */
export type StringCopyKey = {
  [K in keyof (typeof chatMessages)['en-us']]: (typeof chatMessages)['en-us'][K] extends string ? K : never;
}[keyof (typeof chatMessages)['en-us']];

/** Adapter exposing generic Form widget copy from the shared catalog. */
export const FORM_COPY = perLocale((c) => ({
    defaultSubmit: c.genUiFormDefaultSubmit,
    required: (label: string) => fmt(c.genUiFormRequired, { label }),
    maxLength: (label: string, max: number) => fmt(c.genUiFormMaxLength, { label, max }),
    numberInvalid: (label: string) => fmt(c.genUiFormNumberInvalid, { label }),
    min: (label: string, min: number) => fmt(c.genUiFormMin, { label, min }),
    max: (label: string, max: number) => fmt(c.genUiFormMax, { label, max }),
    tooLong: c.genUiFormTooLong,
    selectPlaceholder: c.genUiFormSelectPlaceholder,
    sending: c.genUiFormSending,
    submitted: c.genUiFormSubmitted,
    sendFailed: c.genUiFormSendFailed,
    promptHeader: (title: string) => fmt(c.genUiFormPromptHeader, { title }),
    yes: c.genUiFormYes,
    no: c.genUiFormNo,
    empty: c.genUiFormEmpty,
    optional: c.genUiFormOptional,
}));
