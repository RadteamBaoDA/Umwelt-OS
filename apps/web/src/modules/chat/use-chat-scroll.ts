'use client';

import * as React from 'react';
import { useResetOnChange } from '@/modules/shared';

// Follow/scroll-direction behavior adapts AnythingLLM's frontend/src/hooks/useAutoScroll.js;
// app preferences and its imperative ref are omitted because this transcript has no such owner.

/** Options of {@link useChatScroll}. */
interface ChatScrollOptions {
  conversationId: string | null | undefined;
  messageCount: number;
  lastMessageId: string | undefined;
  lastMessageRole: 'user' | 'assistant' | 'system' | undefined;
  isStreaming: boolean;
  /** Reader mode: submitting scrolls the new question to the top and streaming never auto-scrolls. */
  reader?: boolean;
}

/** Tracks the `prefers-reduced-motion: reduce` media query. */
function usePrefersReducedMotion(): boolean {
  const [prefersReducedMotion, setPrefersReducedMotion] = React.useState(false);
  React.useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');
    const update = () => setPrefersReducedMotion(media.matches);
    update();
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);
  return prefersReducedMotion;
}

/** Mutable follow state shared by the scroll hooks (never triggers renders). */
interface FollowState {
  containerRef: React.RefObject<HTMLDivElement | null>;
  /** True while new content should keep the transcript pinned to the bottom. */
  followRef: React.MutableRefObject<boolean>;
}

/**
 * Scroll commands: explicit scroll to the end (motion-preference aware) and bounded frame-by-frame layout pinning that
 * stops when the reader opts out of following. Pending frames are cancelled on unmount.
 *
 * @param state - Container and follow refs.
 * @param prefersReducedMotion - Disables smooth scrolling.
 */
function useScrollCommands({ containerRef, followRef }: FollowState, prefersReducedMotion: boolean) {
  const pinFramesRef = React.useRef(0);
  const pinFrameRef = React.useRef<number | null>(null);

  /** Scrolls the transcript container, using the user's motion preference for explicit movement. */
  const scrollToBottom = React.useCallback((smooth = false) => {
    const element = containerRef.current;
    if (!element) return;
    element.scrollTo({
      top: element.scrollHeight,
      behavior: smooth && !prefersReducedMotion ? 'smooth' : 'auto',
    });
  }, [prefersReducedMotion, containerRef]);

  /** Pins after layout changes for a bounded number of frames, stopping when the reader opts out. */
  const pinToBottom = React.useCallback((frames = 30) => {
    pinFramesRef.current = frames;
    if (pinFrameRef.current !== null) return;
    const tick = () => {
      const element = containerRef.current;
      if (!element || !followRef.current || pinFramesRef.current <= 0) {
        pinFrameRef.current = null;
        pinFramesRef.current = 0;
        return;
      }
      pinFramesRef.current -= 1;
      element.scrollTop = element.scrollHeight;
      pinFrameRef.current = requestAnimationFrame(tick);
    };
    pinFrameRef.current = requestAnimationFrame(tick);
  }, [containerRef, followRef]);

  React.useEffect(() => () => {
    if (pinFrameRef.current !== null) cancelAnimationFrame(pinFrameRef.current);
  }, []);

  return { scrollToBottom, pinToBottom };
}

/**
 * Follows new messages: a new conversation starts at its latest message; reader mode parks the newest user question at
 * the top; otherwise a new user message re-engages follow and the latest message is pinned.
 */
function useFollowNewMessages(
  { containerRef, followRef }: FollowState,
  opts: Pick<ChatScrollOptions, 'conversationId' | 'messageCount' | 'lastMessageId' | 'lastMessageRole'> & { reader: boolean },
  commands: ReturnType<typeof useScrollCommands>,
  prefersReducedMotion: boolean,
  loadedRef: React.MutableRefObject<boolean>,
) {
  const { conversationId, messageCount, lastMessageId, lastMessageRole, reader } = opts;
  const { scrollToBottom, pinToBottom } = commands;
  React.useLayoutEffect(() => {
    if (messageCount === 0) {
      loadedRef.current = false;
      return;
    }
    if (!loadedRef.current) {
      loadedRef.current = true;
      scrollToBottom();
      pinToBottom();
      return;
    }
    if (reader) {
      // Reader mode: park the newest user question at the top so the answer streams below it.
      if (lastMessageRole === 'user' && lastMessageId) {
        const target = containerRef.current?.querySelector(`[data-message-id="${lastMessageId}"]`);
        (target as HTMLElement | null)?.scrollIntoView?.({ block: 'start', behavior: prefersReducedMotion ? 'auto' : 'smooth' });
      }
      return;
    }
    if (lastMessageRole === 'user') followRef.current = true;
    if (lastMessageId && followRef.current) {
      scrollToBottom();
      pinToBottom();
    }
  }, [conversationId, messageCount, lastMessageId, lastMessageRole, prefersReducedMotion, scrollToBottom, pinToBottom, reader, containerRef, followRef, loadedRef]);
}

/** While streaming (non-reader), pins the transcript to the bottom on animation frames only when scrollHeight changes. */
function useStreamPinning({ containerRef, followRef }: FollowState, isStreaming: boolean, reader: boolean, conversationId: string | null | undefined) {
  const streamFrameRef = React.useRef<number | null>(null);
  React.useEffect(() => {
    if (!isStreaming || reader) return;
    let lastScrollHeight = -1;
    /** Pins transcript to bottom on animation frames only when streaming deltas change scrollHeight. */
    const tick = () => {
      const element = containerRef.current;
      if (element && followRef.current) {
        const currentHeight = element.scrollHeight;
        if (currentHeight !== lastScrollHeight) {
          lastScrollHeight = currentHeight;
          element.scrollTop = currentHeight;
        }
      }
      streamFrameRef.current = requestAnimationFrame(tick);
    };
    streamFrameRef.current = requestAnimationFrame(tick);
    return () => {
      if (streamFrameRef.current !== null) cancelAnimationFrame(streamFrameRef.current);
      streamFrameRef.current = null;
    };
  }, [isStreaming, conversationId, reader, containerRef, followRef]);

  React.useEffect(() => () => {
    if (streamFrameRef.current !== null) cancelAnimationFrame(streamFrameRef.current);
  }, []);
}

/**
 * User scroll gestures: scrolling down to the bottom zone re-engages follow; wheel-up and touch-toward-history stop it.
 *
 * @returns Bottom flag, the DOM handlers and the shared last scrollTop ref (reset on conversation change).
 */
function useScrollGestures({ containerRef, followRef }: FollowState, setIsAtBottom: (atBottom: boolean) => void, lastScrollTopRef: React.MutableRefObject<number>) {
  const touchStartYRef = React.useRef<number | null>(null);

  /** Re-engages follow only when downward movement reaches the bottom zone. */
  const handleScroll = React.useCallback(() => {
    const element = containerRef.current;
    if (!element) return;
    const { scrollTop, scrollHeight, clientHeight } = element;
    const atBottom = scrollHeight - scrollTop - clientHeight < 40;
    const scrolledDown = scrollTop > lastScrollTopRef.current;
    lastScrollTopRef.current = scrollTop;
    setIsAtBottom(atBottom);
    if (atBottom && scrolledDown) followRef.current = true;
  }, [containerRef, followRef, lastScrollTopRef, setIsAtBottom]);

  /** Stops following immediately when a wheel gesture moves the reader upward. */
  const handleWheel = React.useCallback((event: React.WheelEvent<HTMLDivElement>) => {
    if (event.deltaY < 0) followRef.current = false;
  }, [followRef]);

  /** Captures a touch gesture's starting point for scroll-direction detection. */
  const handleTouchStart = React.useCallback((event: React.TouchEvent<HTMLDivElement>) => {
    touchStartYRef.current = event.touches[0]?.clientY ?? null;
  }, []);

  /** Stops following when a touch gesture moves the reader toward earlier messages. */
  const handleTouchMove = React.useCallback((event: React.TouchEvent<HTMLDivElement>) => {
    const startY = touchStartYRef.current;
    if (startY !== null && event.touches[0]?.clientY > startY) followRef.current = false;
  }, [followRef]);

  return { onScroll: handleScroll, onWheel: handleWheel, onTouchStart: handleTouchStart, onTouchMove: handleTouchMove };
}

/**
 * Owns the transcript's follow, scroll-direction and bounded layout-pinning behavior.
 * A new conversation starts at its latest message; a reader who scrolls upward is left
 * in place until they return to the bottom or a new user message starts another turn.
 *
 * @param options - See {@link ChatScrollOptions}.
 * @returns The container ref, bottom flag, `jumpToLatest` and the scroll/wheel/touch handlers to spread on the container.
 */
export function useChatScroll({
  conversationId,
  messageCount,
  lastMessageId,
  lastMessageRole,
  isStreaming,
  reader = false,
}: ChatScrollOptions) {
  const containerRef = React.useRef<HTMLDivElement>(null);
  const followRef = React.useRef(true);
  const loadedRef = React.useRef(false);
  const lastScrollTopRef = React.useRef(0);
  const [isAtBottom, setIsAtBottom] = React.useState(true);
  const prefersReducedMotion = usePrefersReducedMotion();
  const state: FollowState = { containerRef, followRef };
  const commands = useScrollCommands(state, prefersReducedMotion);

  // Resetting follow state when the conversation changes must happen before paint.
  useResetOnChange([conversationId], () => { setIsAtBottom(true); });
  React.useLayoutEffect(() => {
    loadedRef.current = false;
    followRef.current = true;
    lastScrollTopRef.current = 0;
  }, [conversationId]);

  useFollowNewMessages(state, { conversationId, messageCount, lastMessageId, lastMessageRole, reader }, commands, prefersReducedMotion, loadedRef);
  useStreamPinning(state, isStreaming, reader, conversationId);
  const scrollHandlers = useScrollGestures(state, setIsAtBottom, lastScrollTopRef);

  /** Explicit "jump to latest": re-engages follow and scrolls to the end. */
  const { scrollToBottom } = commands;
  const jumpToLatest = React.useCallback(() => {
    followRef.current = true;
    scrollToBottom(true);
  }, [scrollToBottom]);

  return { containerRef, isAtBottom, jumpToLatest, scrollHandlers };
}
