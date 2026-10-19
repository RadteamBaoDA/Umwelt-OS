import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createTripAnimator, TripAnimator } from '../sandbox/trip-animator';

let tick: FrameRequestCallback;
let reducedMotion = false;

beforeEach(() => {
  vi.stubGlobal(
    'requestAnimationFrame',
    vi.fn((callback: FrameRequestCallback) => {
      tick = callback;
      return 1;
    }),
  );
  vi.stubGlobal('cancelAnimationFrame', vi.fn());
  vi.stubGlobal('matchMedia', () => ({ matches: reducedMotion }));
});

afterEach(() => {
  reducedMotion = false;
  vi.unstubAllGlobals();
});

describe('TripAnimator & createTripAnimator', () => {
  it('advances sequentially across stops, pauses without a time jump, and completes at the final stop', () => {
    const onFrame = vi.fn();
    const animator = createTripAnimator({
      stopCount: 4,
      durationMs: 24000,
      onFrame,
    });

    animator.play();
    tick(0);
    tick(6000);
    expect(onFrame.mock.lastCall?.[0].progress).toBe(0.25);

    animator.pause();
    animator.play();
    tick(100000);
    tick(106000);
    expect(onFrame.mock.lastCall?.[0].progress).toBe(0.5);

    tick(118000);
    expect(onFrame.mock.lastCall?.[0]).toMatchObject({
      progress: 1,
      activeStop: 3,
      fraction: 1,
    });
    animator.destroy();
  });

  it('supports stepTo, reset, replay, and destroy on the TripAnimator class', () => {
    const onFrame = vi.fn();
    const onState = vi.fn();
    const pin1 = document.createElement('div');
    const pin2 = document.createElement('div');
    const pin3 = document.createElement('div');

    const animator = new TripAnimator({
      stopCount: 3,
      durationMs: 9000,
      pinElements: [pin1, pin2, pin3],
      onFrame,
      onState,
    });

    expect(pin1.textContent).toBe('1');
    expect(pin2.textContent).toBe('2');
    expect(pin3.textContent).toBe('3');

    animator.stepTo(2);
    expect(onFrame.mock.lastCall?.[0]).toMatchObject({
      progress: 1,
      activeStop: 2,
    });
    expect(onState).toHaveBeenLastCalledWith('complete');

    animator.reset();
    expect(onFrame.mock.lastCall?.[0].progress).toBe(0);
    expect(onState).toHaveBeenLastCalledWith('paused');

    animator.replay();
    expect(onState).toHaveBeenLastCalledWith('playing');

    animator.destroy();
  });

  it('jumps immediately to completion when prefers-reduced-motion is enabled', () => {
    reducedMotion = true;
    const onFrame = vi.fn();
    const onState = vi.fn();
    const animator = createTripAnimator({
      stopCount: 3,
      durationMs: 6000,
      onFrame,
      onState,
    });

    expect(onFrame.mock.lastCall?.[0].progress).toBe(1);
    animator.play();
    expect(onState).toHaveBeenLastCalledWith('complete');
    animator.dispose();
  });

  it('validates stopCount and durationMs bounds', () => {
    expect(() =>
      createTripAnimator({ stopCount: 1, onFrame: () => undefined }),
    ).toThrow(/2-30 stops/);
    expect(() =>
      createTripAnimator({ stopCount: 3, durationMs: 200, onFrame: () => undefined }),
    ).toThrow(/between 1 and 120 seconds/);
  });
});
