import { renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useGadgetReveal } from './use-gadget-reveal';

/** Installs a matchMedia stub whose reduce query matches when `reduce` is true. */
function stubReducedMotion(reduce: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: reduce && query.includes('prefers-reduced-motion'),
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

/** Renders the reveal hook against a detached body element and returns that element with the rerender helper. */
function setup(key: string | null, reduce = false) {
  stubReducedMotion(reduce);
  const el = document.createElement('div');
  const ref = { current: el };
  const view = renderHook(({ k }: { k: string | null }) => useGadgetReveal(ref, k), { initialProps: { k: key } });
  return { el, view };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  delete (window as Partial<Window>).matchMedia;
});

describe('useGadgetReveal', () => {
  it('does nothing while the key is null', () => {
    const { el } = setup(null);
    expect(el.hasAttribute('data-reveal')).toBe(false);
  });

  it('does not animate the first data arrival', () => {
    const { el, view } = setup(null);
    view.rerender({ k: '2026-01-01T00:00:00Z' });
    expect(el.hasAttribute('data-reveal')).toBe(false);
  });

  it('marks the body on every later change, then clears the mark', () => {
    const { el, view } = setup('2026-01-01T00:00:00Z');
    view.rerender({ k: '2026-01-01T01:00:00Z' });
    expect(el.getAttribute('data-reveal')).toBe('true');
    vi.advanceTimersByTime(1500);
    expect(el.hasAttribute('data-reveal')).toBe(false);
  });

  it('treats a later arrival after a null key as a change, not a first arrival', () => {
    const { el, view } = setup('2026-01-01T00:00:00Z');
    view.rerender({ k: null });
    view.rerender({ k: '2026-01-01T02:00:00Z' });
    expect(el.getAttribute('data-reveal')).toBe('true');
  });

  it('never marks the body under prefers-reduced-motion', () => {
    const { el, view } = setup(null, true);
    view.rerender({ k: '2026-01-01T00:00:00Z' });
    expect(el.hasAttribute('data-reveal')).toBe(false);
  });

  it('runs without matchMedia (jsdom or older hosts) and still reveals on a later change', () => {
    delete (window as Partial<Window>).matchMedia;
    const el = document.createElement('div');
    const view = renderHook(({ k }: { k: string }) => useGadgetReveal({ current: el }, k), { initialProps: { k: 'a' } });
    view.rerender({ k: 'b' });
    expect(el.getAttribute('data-reveal')).toBe('true');
  });
});
