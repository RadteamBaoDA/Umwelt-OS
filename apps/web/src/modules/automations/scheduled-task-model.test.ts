import { describe, expect, it } from 'vitest';
import { briefCron, describeSchedule, parseBriefCron, scheduledTaskKind, TEMPLATES } from './scheduled-task-model';

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
    expect(describeSchedule('0 7 * * *', 'en-US')).toBe('Every day at 07:00');
    expect(describeSchedule('0 7 * * 1-5', 'vi-VN')).toBe('Các ngày trong tuần lúc 07:00');
    expect(describeSchedule('*/5 * * * *', 'en-US')).toBe('*/5 * * * *');
  });

  it('classifies like the Python scheduled_task_kind', () => {
    expect(scheduledTaskKind({ trigger: schedule, actions: [brief(true)] })).toBe('daily_brief');
    expect(scheduledTaskKind({ trigger: schedule, actions: [brief(false)] })).toBeNull();
    expect(scheduledTaskKind({ trigger: { type: 'webhook' }, actions: [brief(true)] })).toBeNull();
    expect(scheduledTaskKind({ trigger: schedule, actions: [brief(true), brief(true)] })).toBeNull();
  });

  it('ships exactly the daily brief template', () => {
    expect(TEMPLATES.map((item) => item.id)).toEqual(['daily-brief']);
  });
});
