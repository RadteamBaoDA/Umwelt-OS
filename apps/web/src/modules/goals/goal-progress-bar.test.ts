import { describe, expect, it } from 'vitest';
import { asciiProgressBar } from './goal-progress-bar';

describe('asciiProgressBar', () => {
  it('renders ten cells with the rounded percentage', () => {
    expect(asciiProgressBar(83)).toBe('[████████░░] 83%');
    expect(asciiProgressBar(61)).toBe('[██████░░░░] 61%');
    expect(asciiProgressBar(70)).toBe('[███████░░░] 70%');
  });

  it('clamps out-of-range and non-finite progress', () => {
    expect(asciiProgressBar(0)).toBe('[░░░░░░░░░░] 0%');
    expect(asciiProgressBar(150)).toBe('[██████████] 100%');
    expect(asciiProgressBar(-5)).toBe('[░░░░░░░░░░] 0%');
    expect(asciiProgressBar(Number.NaN)).toBe('[░░░░░░░░░░] 0%');
  });
});
