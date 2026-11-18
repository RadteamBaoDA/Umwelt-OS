'use client';

import { useEffect, useState } from 'react';
import { readSkin } from '@/core/skin';

/** Number of scrambled frames before the figure settles (9 x 55 ms, about 500 ms). */
const ROLL_TICKS = 9;
const ROLL_INTERVAL_MS = 55;

/** An in-progress roll: `id` changes per roll so the interval restarts only then; `frame` is the scrambled text shown now. */
type Roll = { id: number; frame: string; pulse: 'up' | 'down' | null };

/**
 * Replaces every digit in a formatted figure with a random digit, keeping separators and width.
 *
 * @param text Formatted figure, e.g. `1,234.5`.
 * @param random Source of randomness in [0, 1); injectable for deterministic tests.
 * @returns The scrambled text. Pure apart from calling `random`.
 */
export function scrambleDigits(text: string, random: () => number = Math.random): string {
  return text.replace(/\d/g, () => String(Math.floor(random() * 10)));
}

/**
 * Whether a roll may animate now: terminal skin and no reduced-motion request. Missing `matchMedia`
 * (SSR, some test environments) reads as no reduced motion. Read at the moment of change, so a later skin switch
 * applies from the next change.
 *
 * @returns True when the scramble and pulse should run.
 */
function canRoll(): boolean {
  if (readSkin() !== 'terminal') return false;
  const reduce =
    typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
      : false;
  return !reduce;
}

/**
 * Shows a headline figure. When its text changes (never on first render) the terminal skin scrambles its digits
 * for about 500 ms, then settles on the new value with an up/down pulse on the wrapper. Screen readers hear only
 * the final value: the scrambled frame is `aria-hidden` and the settled text is in `sr-only` during the roll.
 * The default skin and reduced motion show the new value immediately with no animation.
 *
 * A text change is handled during render (React's derived-state pattern) so no effect body sets state; the effect
 * only owns the interval, which is cleared when the roll ends, when a newer roll replaces it, and on unmount.
 *
 * @param props.text Formatted figure to display.
 * @param props.value Numeric value behind `text`; only its sign of change (up or down) drives the pulse. Null when unknown.
 * @param props.className Typography and colour classes applied to the figure span.
 * @returns A span holding the figure. Side effects: one `setInterval` while rolling.
 */
export function RollingNumber({ text, value = null, className }: { text: string; value?: number | null; className?: string }) {
  const [seen, setSeen] = useState({ text, value });
  const [roll, setRoll] = useState<Roll | null>(null);

  if (seen.text !== text) {
    const delta = typeof value === 'number' && typeof seen.value === 'number' ? Math.sign(value - seen.value) : 0;
    setSeen({ text, value });
    setRoll(
      canRoll() ? { id: (roll?.id ?? 0) + 1, frame: scrambleDigits(text), pulse: delta > 0 ? 'up' : delta < 0 ? 'down' : null } : null,
    );
  }

  const rollId = roll?.id;
  useEffect(() => {
    if (rollId === undefined) return;
    let ticks = 0;
    const id = window.setInterval(() => {
      ticks += 1;
      if (ticks >= ROLL_TICKS) {
        window.clearInterval(id);
        setRoll((current) => (current?.id === rollId ? null : current));
        return;
      }
      setRoll((current) => (current?.id === rollId ? { ...current, frame: scrambleDigits(text) } : current));
    }, ROLL_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [rollId, text]);

  // The outer span is the figure element, so callers' typography classes stay on the element that holds the text.
  if (roll === null) {
    return <span className={className}>{text}</span>;
  }
  return (
    <span className={className} data-roll={roll.pulse ?? undefined}>
      <span aria-hidden="true">{roll.frame}</span>
      <span className="sr-only">{text}</span>
    </span>
  );
}
