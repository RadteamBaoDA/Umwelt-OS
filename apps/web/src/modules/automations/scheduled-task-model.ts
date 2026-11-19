import type { Automation, BriefModelRole, BriefSection } from './api';

/** Brief sections in display order; mirrors the backend `ALL_BRIEF_SECTIONS`. */
export const ALL_BRIEF_SECTIONS: readonly BriefSection[] = ['stories', 'markets', 'weather', 'hazards', 'agenda', 'source_health'];

/** Maximum length of the owner's extra brief instruction (backend limit). */
export const MAX_EXTRA_INSTRUCTION = 500;

/** Day pattern of a generated brief schedule. */
export type BriefDays = 'daily' | 'weekdays';

/** A starting point offered on the Templates tab. */
export interface ScheduledTaskTemplate {
  id: string;
  kind: 'daily_brief' | 'topic_watch';
  icon: 'sun' | 'telescope';
  defaultTime: string;
  defaultDays: BriefDays;
}

/** Templates offered on the Templates tab: the single Daily brief and any number of Topic watches. */
export const TEMPLATES: readonly ScheduledTaskTemplate[] = [
  { id: 'daily-brief', kind: 'daily_brief', icon: 'sun', defaultTime: '07:00', defaultDays: 'daily' },
  { id: 'topic-watch', kind: 'topic_watch', icon: 'telescope', defaultTime: '08:00', defaultDays: 'daily' },
];

/** Per-run token estimate constants; mirror the backend `topic_watch_rules` (a unit test pins `estimateTokens(5)`). */
const PLAN_TOKENS = 600;
const PAGE_TOKENS = 1500;

/** Largest `max_items` a Topic watch accepts (backend limit). */
export const MAX_TOPIC_ITEMS = 10;

/**
 * Per-run token estimate for a Topic watch that keeps at most `maxItems` items: planning plus two fetched pages per
 * item. The server recomputes this and refuses a different grant (`consent_estimate_mismatch`), so the value shown to
 * the owner is exactly the value they consent to.
 */
export function estimateTokens(maxItems: number): number {
  return PLAN_TOKENS + maxItems * 2 * PAGE_TOKENS;
}

/** Hard per-run token cap the server enforces: 1.5x the estimate, rounded up. */
export function tokenCap(maxItems: number): number {
  return Math.ceil(1.5 * estimateTokens(maxItems));
}

/**
 * Classifies a rule as a scheduled task. Mirrors the Python `scheduled_task_kind`: a schedule trigger and exactly one
 * `generate_brief` action with `deliver_chat === true` is a daily brief; anything else is not a task.
 */
export function scheduledTaskKind(rule: Pick<Automation, 'trigger' | 'actions'>): 'daily_brief' | 'topic_watch' | null {
  const [only] = rule.actions;
  const single = rule.trigger.type === 'schedule' && rule.actions.length === 1;
  if (single && only.type === 'collect_source') return 'topic_watch';
  return single && only.type === 'generate_brief' && only.deliver_chat === true ? 'daily_brief' : null;
}

/** Outcome of the newest Topic watch run, derived from its action's reason code and result reference. */
export type TopicOutcome = 'collecting' | 'settled' | 'no_new_data' | 'failed' | 'consent_required';

/**
 * Classifies a `collect_source` run action: `consent_required` (skipped), `no_new_data`, any other error code (failed or
 * rejected), `settled:` (items posted), `collection:` (queued, not yet settled); null when none applies.
 */
export function topicOutcome(action: { error_code: string | null; result_reference: string | null }): TopicOutcome | null {
  if (action.error_code === 'consent_required') return 'consent_required';
  if (action.error_code === 'no_new_data') return 'no_new_data';
  // Any other reason code (collection_*, stale_revision, ...) is a failure; it must win over the references.
  if (action.error_code) return 'failed';
  if (action.result_reference?.startsWith('settled:')) return 'settled';
  if (action.result_reference?.startsWith('collection:')) return 'collecting';
  return null;
}

/** True for a rule that generates a brief but is not a scheduled task (legacy rule that still owns the brief slot). */
export function isLegacyBrief(rule: Pick<Automation, 'trigger' | 'actions'>): boolean {
  return scheduledTaskKind(rule) === null && rule.actions.some((action) => action.type === 'generate_brief');
}

/** Builds the cron for "HH:MM" and a day pattern (`M H * * *` or `M H * * 1-5`); assumes a valid time. */
export function briefCron(time: string, days: BriefDays): string {
  const [hour, minute] = time.split(':').map(Number);
  return `${minute} ${hour} * * ${days === 'daily' ? '*' : '1-5'}`;
}

/** Parses a cron produced by {@link briefCron}; returns null for any other expression. */
export function parseBriefCron(cron: string): { time: string; days: BriefDays } | null {
  const match = /^(\d{1,2}) (\d{1,2}) \* \* (\*|1-5)$/.exec(cron.trim());
  if (!match || Number(match[1]) > 59 || Number(match[2]) > 23) return null;
  const time = `${match[2].padStart(2, '0')}:${match[1].padStart(2, '0')}`;
  return { time, days: match[3] === '*' ? 'daily' : 'weekdays' };
}

/**
 * Structured description of the two generated cron patterns (`days` and "HH:MM" `time`), or null for any other cron.
 * Wording is left to the caller so every string stays in the locale catalogs.
 */
export function describeSchedule(cron: string): { days: BriefDays; time: string } | null {
  const parsed = parseBriefCron(cron);
  return parsed && { days: parsed.days, time: parsed.time };
}

/** A Daily brief proposed in chat, handed to the Scheduled tasks page to pre-fill the editor (never saved by itself). */
export interface BriefDraft {
  time: string;
  days: BriefDays;
  sections: BriefSection[];
  extraInstruction: string | null;
  modelRole: BriefModelRole;
}

// sessionStorage keeps the owner's instruction out of the URL (history, logs, referrers); it lives for this tab only.
const BRIEF_DRAFT_KEY = 'umwelt:brief-draft';
const ROLES: readonly BriefModelRole[] = ['brief', 'chat', 'background'];

/**
 * Stores a chat draft for the Scheduled tasks page in this tab's sessionStorage. Storage failures (private mode, quota)
 * are ignored: the page then opens without a pre-filled editor.
 */
export function stashBriefDraft(draft: BriefDraft): void {
  try {
    window.sessionStorage.setItem(BRIEF_DRAFT_KEY, JSON.stringify(draft));
  } catch {
    // Best effort only; nothing is saved without the owner's Save anyway.
  }
}

/**
 * Reads and removes the stashed chat draft, so a reload never reopens it. Returns null when absent or when the stored
 * value does not have the exact draft shape (it is page-writable storage, so it is validated like any input).
 */
export function takeBriefDraft(): BriefDraft | null {
  let raw: string | null = null;
  try {
    raw = window.sessionStorage.getItem(BRIEF_DRAFT_KEY);
    window.sessionStorage.removeItem(BRIEF_DRAFT_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    return isBriefDraft(value) ? value : null;
  } catch {
    return null;
  }
}

/** Exact-shape check of a stored draft: the same bounds the server applies to `ScheduledTaskDraft`. */
function isBriefDraft(input: unknown): input is BriefDraft {
  const value = (input ?? {}) as Partial<BriefDraft>;
  const sections = Array.isArray(value.sections) ? value.sections : null;
  const instruction = value.extraInstruction;
  return (
    typeof value.time === 'string' &&
    /^([01]\d|2[0-3]):[0-5]\d$/.test(value.time) &&
    (value.days === 'daily' || value.days === 'weekdays') &&
    sections !== null &&
    sections.every((section) => ALL_BRIEF_SECTIONS.includes(section)) &&
    new Set(sections).size === sections.length &&
    (instruction === null || (typeof instruction === 'string' && instruction.length <= 500)) &&
    ROLES.includes(value.modelRole as BriefModelRole)
  );
}
