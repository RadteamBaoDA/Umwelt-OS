import { describe, expect, it } from 'vitest';
import { isStepFailed, stepText } from './chat-steps';

/** Fake translator: echoes the message key and its params so the test asserts which copy was chosen. */
const t = ((key: string, params?: Record<string, unknown>) => (params ? `${key}:${JSON.stringify(params)}` : key)) as never;

describe('collected-data step outcome', () => {
  const done = (detail?: Record<string, number>) => ({ tool: 'query_collected_data', state: 'done' as const, detail });

  it('shows row and source counts', () => {
    expect(stepText(t, done({ rows: 12, sources: 3 }))).toBe('stepDataRows:{"rows":12,"sources":3}');
    expect(stepText(t, done({ rows: 4, sources: 0 }))).toBe('stepDataRowsNoSources:{"rows":4}');
  });

  it('shows the empty and failed outcomes, and flags failure for the error icon', () => {
    expect(stepText(t, done({ rows: 0, sources: 0 }))).toBe('stepDataEmpty');
    expect(stepText(t, done({ error: 1 }))).toBe('stepDataFailed');
    expect(isStepFailed(done({ error: 1 }))).toBe(true);
    expect(isStepFailed(done({ rows: 1, sources: 1 }))).toBe(false);
  });

  it('keeps the generic copy without detail and while running', () => {
    expect(stepText(t, done())).toBe('stepDataDone');
    expect(stepText(t, { tool: 'query_collected_data', state: 'running' })).toBe('stepDataRunning');
  });
});
