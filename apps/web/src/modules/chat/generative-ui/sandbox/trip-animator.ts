/**
 * Multi-step itinerary and diagram animation state controller (`sandbox/trip-animator.ts`).
 * Self-contained controller serialized into sandboxed iframes without host object or network access.
 */

export interface TripFrame {
  progress: number;
  fromIndex: number;
  toIndex: number;
  fraction: number;
  activeStop: number;
  pinIndex: number;
  pinProgress: number;
}

export type TripAnimatorState = 'playing' | 'paused' | 'complete';

export interface TripAnimationOptions {
  stopCount: number;
  durationMs?: number;
  /** Inner marker nodes, never outer positioning wrappers. */
  pinElements?: HTMLElement[];
  onFrame: (frame: TripFrame) => void;
  onState?: (state: TripAnimatorState) => void;
}

export interface TripAnimatorController {
  play: () => void;
  pause: () => void;
  reset: () => void;
  replay: () => void;
  stepTo: (stop: number) => void;
  seek: (stop: number) => void;
  destroy: () => void;
  dispose: () => void;
}

/**
 * Creates a self-contained multi-step animation controller that interpolates across `stopCount` stops,
 * respects `prefers-reduced-motion: reduce`, pauses automatically when the document is hidden,
 * and exposes `play`, `pause`, `reset`, `replay`, `stepTo` (`seek`), and `destroy` (`dispose`).
 *
 * @param options - `TripAnimationOptions` specifying `stopCount` (2..30), `durationMs` (1000..120000), and frame/state callbacks.
 * @returns `TripAnimatorController` instance.
 * @throws Error when `stopCount` is outside `[2, 30]` or `durationMs` is outside `[1000, 120000]`.
 */
export function createTripAnimator(options: TripAnimationOptions): TripAnimatorController {
  const { stopCount, onFrame, onState } = options;
  const duration = options.durationMs ?? 6000;
  if (!Number.isInteger(stopCount) || stopCount < 2 || stopCount > 30) {
    throw new Error('A trip needs 2-30 stops.');
  }
  if (!Number.isFinite(duration) || duration < 1000 || duration > 120000) {
    throw new Error('Trip duration must be between 1 and 120 seconds.');
  }

  options.pinElements?.forEach((pin, index) => {
    if (!pin.textContent?.trim()) {
      pin.textContent = String(index + 1);
    }
  });

  let elapsed = 0;
  let lastTime: number | null = null;
  let frameId: number | null = null;
  let playing = false;
  let disposed = false;

  const reduceMotion = () =>
    typeof window !== 'undefined' &&
    (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false);

  const emit = () => {
    const progress = Math.min(1, elapsed / duration);
    const step = progress * (stopCount - 1);
    const fromIndex = Math.min(stopCount - 2, Math.floor(step));
    // Travel for 80% of each leg, then hold at the destination for 20%.
    const travel = Math.min(1, (step - fromIndex) / 0.8);
    const fraction = travel * travel * (3 - 2 * travel);
    const pinStep = progress * stopCount;
    const pinIndex = Math.min(stopCount - 1, Math.floor(pinStep));
    const pinProgress = progress === 1 ? 1 : Math.min(1, (pinStep - pinIndex) / 0.55);

    options.pinElements?.forEach((pin, index) => {
      const landing = index < pinIndex ? 1 : index === pinIndex ? pinProgress : 0;
      const t = landing - 1;
      const ease = 1 + 2.2 * t * t * t + 1.2 * t * t;
      pin.style.opacity = landing > 0 ? '1' : '0';
      pin.style.transform = `translateY(${-18 * (1 - ease)}px) scale(${0.72 + 0.28 * ease})`;
    });

    onFrame({
      progress,
      fromIndex,
      toIndex: fromIndex + 1,
      fraction,
      activeStop: pinIndex,
      pinIndex,
      pinProgress,
    });
  };

  const pause = () => {
    if (disposed) return;
    playing = false;
    lastTime = null;
    if (frameId !== null) {
      cancelAnimationFrame(frameId);
      frameId = null;
    }
    onState?.(elapsed >= duration ? 'complete' : 'paused');
  };

  const tick = (time: number) => {
    if (!playing || disposed) return;
    if (lastTime !== null) {
      elapsed = Math.min(duration, elapsed + Math.max(0, time - lastTime));
    }
    lastTime = time;
    emit();
    if (elapsed >= duration) {
      pause();
    } else {
      frameId = requestAnimationFrame(tick);
    }
  };

  const play = () => {
    if (disposed || playing) return;
    if (reduceMotion()) {
      elapsed = duration;
      emit();
      onState?.('complete');
      return;
    }
    if (elapsed >= duration) return;
    playing = true;
    lastTime = null;
    onState?.('playing');
    frameId = requestAnimationFrame(tick);
  };

  const seek = (stop: number) => {
    if (disposed) return;
    if (!Number.isInteger(stop) || stop < 0 || stop >= stopCount) {
      throw new Error('Stop index is outside this trip.');
    }
    pause();
    elapsed = stop === stopCount - 1 ? duration : (duration * (stop + 0.6)) / stopCount;
    emit();
    onState?.(elapsed >= duration ? 'complete' : 'paused');
  };

  const stepTo = (stop: number) => {
    seek(stop);
  };

  const reset = () => {
    if (disposed) return;
    pause();
    elapsed = 0;
    emit();
    onState?.('paused');
  };

  const replay = () => {
    if (disposed) return;
    pause();
    elapsed = 0;
    emit();
    play();
  };

  const onVisibility = () => {
    if (typeof document !== 'undefined' && document.hidden) {
      pause();
    }
  };

  const dispose = () => {
    if (disposed) return;
    pause();
    disposed = true;
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisibility);
    }
    if (typeof window !== 'undefined') {
      window.removeEventListener('pagehide', dispose);
    }
  };

  const destroy = () => {
    dispose();
  };

  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVisibility);
  }
  if (typeof window !== 'undefined') {
    window.addEventListener('pagehide', dispose);
  }

  if (reduceMotion()) {
    elapsed = duration;
  }
  emit();

  return {
    play,
    pause,
    reset,
    replay,
    stepTo,
    seek,
    destroy,
    dispose,
  };
}

/**
 * Object-oriented wrapper around `createTripAnimator` providing `play`, `pause`, `reset`,
 * `replay`, `stepTo`, `seek`, `destroy`, and `dispose` methods.
 */
export class TripAnimator implements TripAnimatorController {
  private readonly controller: TripAnimatorController;

  /**
   * Initializes a `TripAnimator` state controller for multi-step animations.
   *
   * @param options - Animation configuration and frame/state listeners.
   */
  constructor(options: TripAnimationOptions) {
    this.controller = createTripAnimator(options);
  }

  /** Starts or resumes animation playback. */
  play(): void {
    this.controller.play();
  }

  /** Pauses animation playback at the current elapsed position. */
  pause(): void {
    this.controller.pause();
  }

  /** Resets animation playback to the initial stop (`progress = 0`) in a paused state. */
  reset(): void {
    this.controller.reset();
  }

  /** Resets animation playback to the initial stop and immediately starts playing. */
  replay(): void {
    this.controller.replay();
  }

  /** Jumps to a specific stop index (`0..stopCount - 1`) and pauses playback. */
  stepTo(stop: number): void {
    this.controller.stepTo(stop);
  }

  /** Alias for `stepTo(stop)` matching OpenIntelligentUI's `seek` method. */
  seek(stop: number): void {
    this.controller.seek(stop);
  }

  /** Cancels active animation frames and detaches global visibility/pagehide listeners. */
  destroy(): void {
    this.controller.destroy();
  }

  /** Alias for `destroy()` matching OpenIntelligentUI's `dispose` method. */
  dispose(): void {
    this.controller.dispose();
  }
}

/** Inline `<script>` tag registering `window.createTripAnimator` inside sandboxed iframes. */
export const TRIP_ANIMATOR_SCRIPT = `<script>window.createTripAnimator = ${createTripAnimator.toString()};</script>`;
