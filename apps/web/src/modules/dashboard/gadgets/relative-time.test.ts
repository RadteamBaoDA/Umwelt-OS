import { describe, expect, it } from 'vitest';
import { formatCompactAgo } from './relative-time';

const NOW = Date.parse('2026-11-18T12:00:00Z');

describe('formatCompactAgo', () => {
  it('uses unit letters from minutes to months', () => {
    expect(formatCompactAgo('2026-11-18T11:59:50Z', NOW)).toBe('0m');
    expect(formatCompactAgo('2026-11-18T11:55:00Z', NOW)).toBe('5m');
    expect(formatCompactAgo('2026-11-18T10:00:00Z', NOW)).toBe('2h');
    expect(formatCompactAgo('2026-11-15T12:00:00Z', NOW)).toBe('3d');
    expect(formatCompactAgo('2026-09-18T12:00:00Z', NOW)).toBe('2mo');
  });
  it('clamps the future and rejects invalid input', () => {
    expect(formatCompactAgo('2026-11-19T12:00:00Z', NOW)).toBe('0m');
    expect(formatCompactAgo('nope', NOW)).toBe('');
    expect(formatCompactAgo(null, NOW)).toBe('');
  });
});
