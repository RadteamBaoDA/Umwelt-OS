import type { Automation, BriefSection } from './api';

/** Brief sections in display order; mirrors the backend `ALL_BRIEF_SECTIONS`. */
export const ALL_BRIEF_SECTIONS: readonly BriefSection[] = ['stories', 'markets', 'weather', 'hazards', 'agenda', 'source_health'];

/** Maximum length of the owner's extra brief instruction (backend limit). */
export const MAX_EXTRA_INSTRUCTION = 500;

/** Day pattern of a generated brief schedule. */
export type BriefDays = 'daily' | 'weekdays';

/** A starting point offered on the Templates tab. */
export interface ScheduledTaskTemplate {
  id: string;
  kind: 'daily_brief';
  icon: 'sun';
  defaultTime: string;
  defaultDays: BriefDays;
}

/** Phase 1 ships only the Daily brief template; further kinds arrive in later phases. */
export const TEMPLATES: readonly ScheduledTaskTemplate[] = [
  { id: 'daily-brief', kind: 'daily_brief', icon: 'sun', defaultTime: '07:00', defaultDays: 'daily' },
];

/**
 * Classifies a rule as a scheduled task. Mirrors the Python `scheduled_task_kind`: a schedule trigger and exactly one
 * `generate_brief` action with `deliver_chat === true` is a daily brief; anything else is not a task.
 */
export function scheduledTaskKind(rule: Pick<Automation, 'trigger' | 'actions'>): 'daily_brief' | null {
  const [only] = rule.actions;
  const isBrief = rule.trigger.type === 'schedule' && rule.actions.length === 1 && only.type === 'generate_brief';
  return isBrief && only.deliver_chat === true ? 'daily_brief' : null;
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

/** Plain-language sentence for the two generated cron patterns; any other cron is returned unchanged. */
export function describeSchedule(cron: string, locale: 'en-US' | 'vi-VN'): string {
  const parsed = parseBriefCron(cron);
  if (!parsed) return cron;
  const vi = locale === 'vi-VN';
  const label = parsed.days === 'daily' ? (vi ? 'Hằng ngày' : 'Every day') : vi ? 'Các ngày trong tuần' : 'Weekdays';
  return `${label} ${vi ? 'lúc' : 'at'} ${parsed.time}`;
}
