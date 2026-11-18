import { describe, expect, it } from 'vitest';
import {
  briefCron,
  describeSchedule,
  estimateTokens,
  parseBriefCron,
  scheduledTaskKind,
  tokenCap,
  topicOutcome,
  TEMPLATES,
} from './scheduled-task-model';

const brief = (deliver_chat: boolean | undefined) => ({ type: 'generate_brief' as const, scope: 'daily' as const, deliver_chat });
const schedule = { type: 'schedule' as const, cron: '0 7 * * *' };

describe('scheduled task model', () => {
  it('builds generated crons', () => {
    expect(briefCron('07:05', 'daily')).toBe('5 7 * * *');
    expect(briefCron('18:00', 'weekdays')).toBe('0 18 * * 1-5');
  });

  it('round-trips generated crons and rejects others', () => {
    expect(parseBriefCron('5 7 * * *')).toEqual({ time: '07:05', days: 'daily' });
    expect(parseBriefCron('0 18 * * 1-5')).toEqual({ time: '18:00', days: 'weekdays' });
    expect(parseBriefCron('*/5 * * * *')).toBeNull();
    expect(parseBriefCron('0 25 * * *')).toBeNull();
  });

  it('describes generated crons and echoes the rest', () => {
    expect(describeSchedule('0 7 * * *')).toEqual({ days: 'daily', time: '07:00' });
    expect(describeSchedule('0 7 * * 1-5')).toEqual({ days: 'weekdays', time: '07:00' });
    expect(describeSchedule('*/5 * * * *')).toBeNull();
  });

  it('classifies like the Python scheduled_task_kind', () => {
    expect(scheduledTaskKind({ trigger: schedule, actions: [brief(true)] })).toBe('daily_brief');
    expect(scheduledTaskKind({ trigger: schedule, actions: [brief(false)] })).toBeNull();
    expect(scheduledTaskKind({ trigger: { type: 'webhook' }, actions: [brief(true)] })).toBeNull();
    expect(scheduledTaskKind({ trigger: schedule, actions: [brief(true), brief(true)] })).toBeNull();
  });

  it('ships the daily brief and topic watch templates', () => {
    expect(TEMPLATES.map((item) => item.id)).toEqual(['daily-brief', 'topic-watch']);
  });

  it('classifies a topic watch like the Python scheduled_task_kind', () => {
    const watch = { type: 'collect_source' as const };
    expect(scheduledTaskKind({ trigger: schedule, actions: [watch] })).toBe('topic_watch');
    expect(scheduledTaskKind({ trigger: { type: 'webhook' }, actions: [watch] })).toBeNull();
    expect(scheduledTaskKind({ trigger: schedule, actions: [watch, watch] })).toBeNull();
    expect(scheduledTaskKind({ trigger: schedule, actions: [watch, brief(true)] })).toBeNull();
  });

  it('pins the token estimate to the backend value', () => {
    expect(estimateTokens(5)).toBe(15600);
    expect(tokenCap(5)).toBe(23400);
  });

  it('pins the estimate and cap for every item count (mirrors the backend table)', () => {
    const pinned: Record<number, [number, number]> = {
      1: [3600, 5400],
      2: [6600, 9900],
      3: [9600, 14400],
      4: [12600, 18900],
      5: [15600, 23400],
      6: [18600, 27900],
      7: [21600, 32400],
      8: [24600, 36900],
      9: [27600, 41400],
      10: [30600, 45900],
    };
    for (const [n, [estimate, cap]] of Object.entries(pinned)) {
      expect([estimateTokens(Number(n)), tokenCap(Number(n))]).toEqual([estimate, cap]);
    }
  });

  it('treats any other error code as failed, before the reference checks', () => {
    expect(topicOutcome({ error_code: 'stale_revision', result_reference: 'collection:r' })).toBe('failed');
    expect(topicOutcome({ error_code: 'document_evidence_unavailable', result_reference: 'settled:r' })).toBe('failed');
  });

  it('derives the topic outcome from the reason code and result reference', () => {
    expect(topicOutcome({ error_code: 'consent_required', result_reference: null })).toBe('consent_required');
    expect(topicOutcome({ error_code: 'no_new_data', result_reference: 'settled:r' })).toBe('no_new_data');
    expect(topicOutcome({ error_code: 'collection_failed', result_reference: 'collection:r' })).toBe('failed');
    expect(topicOutcome({ error_code: null, result_reference: 'settled:r' })).toBe('settled');
    expect(topicOutcome({ error_code: null, result_reference: 'collection:r' })).toBe('collecting');
    expect(topicOutcome({ error_code: null, result_reference: null })).toBeNull();
  });
});
