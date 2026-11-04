import { describe, expect, it } from 'vitest';
import type { Capabilities } from './api';
import { toDefinition, type Draft } from './rule-editor';

const caps = { triggers: [{ type: 'task_due', module: 'tasks', available: true, reason: null, requires_approval: false, fields: { priority: 'number', done: 'boolean', title: 'string' } }, { type: 'schedule', module: null, available: true, reason: null, requires_approval: false, fields: {} }, { type: 'webhook', module: null, available: true, reason: null, requires_approval: false, fields: {} }], actions: [], webhook_aliases: [] } as unknown as Capabilities;

describe('toDefinition', () => {
  it('reports missing trigger parameters, then missing actions, de-duplicated and in order', () => {
    const draft: Draft = { name: 'r', trigger: { type: 'schedule', cron: '  ' }, conditions: [], actions: [] };
    expect(toDefinition(draft, caps).errors).toEqual(['errCron', 'errNoActions']);
    expect(toDefinition({ ...draft, trigger: { type: 'webhook' } }, caps).errors).toEqual(['errWebhookAlias', 'errNoActions']);
  });

  it('flags incomplete actions once regardless of how many are incomplete', () => {
    const draft: Draft = { name: 'r', trigger: { type: 'task_due', lead_minutes: 5 }, conditions: [], actions: [{ type: 'run_agent', profile_id: 'knowledge', instruction: ' ' }, { type: 'create_task', title: '' }, { type: 'call_webhook', alias: '', event: 'x' }, { type: 'generate_brief', scope: 'daily' }] };
    expect(toDefinition(draft, caps).errors).toEqual(['errAction']);
  });

  it('parses typed condition values and list operands, rejecting unparsable ones', () => {
    const base = { name: 'r', trigger: { type: 'task_due', lead_minutes: 5 } as Draft['trigger'], actions: [{ type: 'generate_brief', scope: 'daily' }] as Draft['actions'] };
    const ok = toDefinition({ ...base, conditions: [{ field: 'priority', operator: 'gt', value: '3' }, { field: 'done', operator: 'eq', value: 'true' }, { field: 'title', operator: 'in', value: 'a, b' }] }, caps);
    expect(ok.errors).toEqual([]);
    expect(ok.definition?.conditions).toEqual([
      { field: 'priority', operator: 'gt', value: 3 },
      { field: 'done', operator: 'eq', value: true },
      { field: 'title', operator: 'in', value: ['a', 'b'] },
    ]);
    expect(toDefinition({ ...base, conditions: [{ field: 'priority', operator: 'gt', value: 'abc' }] }, caps).errors).toEqual(['errCondition']);
  });

  it('trims the schedule cron in the produced definition', () => {
    const result = toDefinition({ name: 'r', trigger: { type: 'schedule', cron: ' 0 8 * * * ' }, conditions: [], actions: [{ type: 'generate_brief', scope: 'daily' }] }, caps);
    expect(result.definition?.trigger).toMatchObject({ cron: '0 8 * * *' });
  });
});
