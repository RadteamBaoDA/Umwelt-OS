import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { RollingNumber } from './rolling-number';

function setSkin(skin: 'terminal' | null) {
  if (skin) document.documentElement.setAttribute('data-skin', skin);
  else document.documentElement.removeAttribute('data-skin');
}

function stubReducedMotion(matches: boolean) {
  vi.stubGlobal('matchMedia', (query: string) => ({ matches: matches && query.includes('reduce'), media: query }));
}

describe('RollingNumber', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    // Math.random -> 0.5 gives every scrambled digit a 5, so the frame is deterministic.
    vi.spyOn(Math, 'random').mockReturnValue(0.5);
    stubReducedMotion(false);
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    setSkin(null);
  });

  it('shows the value without scrambling on first render', () => {
    setSkin('terminal');
    const { container } = render(<RollingNumber text="1,234" value={1234} />);
    expect(container.textContent).toBe('1,234');
    expect(container.querySelector('[aria-hidden]')).toBeNull();
  });

  it('scrambles digits in the terminal skin, then settles on the final value', () => {
    setSkin('terminal');
    const { container, rerender } = render(<RollingNumber text="1,234" value={1234} />);
    rerender(<RollingNumber text="1,300" value={1300} />);
    expect(container.querySelector('[aria-hidden="true"]')?.textContent).toBe('5,555');
    expect(screen.getByText('1,300', { selector: '.sr-only' })).toBeTruthy();
    act(() => vi.advanceTimersByTime(600));
    expect(container.querySelector('[aria-hidden]')).toBeNull();
    expect(container.textContent).toBe('1,300');
  });

  it('does not scramble in the default skin', () => {
    const { container, rerender } = render(<RollingNumber text="1,234" value={1234} />);
    rerender(<RollingNumber text="1,300" value={1300} />);
    expect(container.querySelector('[aria-hidden]')).toBeNull();
    expect(container.textContent).toBe('1,300');
  });

  it('does not scramble under reduced motion', () => {
    setSkin('terminal');
    stubReducedMotion(true);
    const { container, rerender } = render(<RollingNumber text="1,234" value={1234} />);
    rerender(<RollingNumber text="1,300" value={1300} />);
    expect(container.querySelector('[aria-hidden]')).toBeNull();
    expect(container.textContent).toBe('1,300');
  });

  it('clears its timer on unmount', () => {
    setSkin('terminal');
    const { rerender, unmount } = render(<RollingNumber text="1" value={1} />);
    rerender(<RollingNumber text="2" value={2} />);
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});
