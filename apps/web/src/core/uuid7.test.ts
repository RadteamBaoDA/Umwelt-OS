import { describe, expect, it } from 'vitest';
import { uuid7 } from './uuid7';

describe('uuid7', () => {
  it('has the version 7 / RFC variant format', () => {
    expect(uuid7()).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  });

  it('is strictly increasing across a burst and unique', () => {
    const ids = Array.from({ length: 10_000 }, () => uuid7());
    for (let i = 1; i < ids.length; i += 1) expect(ids[i] > ids[i - 1]).toBe(true);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('orders by timestamp across milliseconds', () => {
    const first = uuid7();
    const realNow = Date.now;
    Date.now = () => realNow() + 5000;
    try {
      expect(uuid7() > first).toBe(true);
    } finally {
      Date.now = realNow;
    }
  });
});
