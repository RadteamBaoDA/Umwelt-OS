'use client';

import * as React from 'react';

// Follow/scroll-direction behavior adapts AnythingLLM's frontend/src/hooks/useAutoScroll.js;
// app preferences and its imperative ref are omitted because this transcript has no such owner.

/**
 * Owns the transcript's follow, scroll-direction and bounded layout-pinning behavior.
 * A new conversation starts at its latest message; a reader who scrolls upward is left
 * in place until they return to the bottom or a new user message starts another turn.
 */
export function useChatScroll({
  conversationId,
  messageCount,
  lastMessageId,
  lastMessageRole,
  isStreaming,
}: {
  conversationId: string | null | undefined;
  messageCount: number;
  lastMessageId: string | undefined;
  lastMessageRole: 'user' | 'assistant' | 'system' | undefined;
  isStreaming: boolean;
}) {
  const containerRef = React.useRef<HTMLDivElement>(null);
  const followRef = React.useRef(true);
  const loadedRef = React.useRef(false);
  const lastScrollTopRef = React.useRef(0);
  const touchStartYRef = React.useRef<number | null>(null);
  const pinFramesRef = React.useRef(0);
  const pinFrameRef = React.useRef<number | null>(null);
  const streamFrameRef = React.useRef<number | null>(null);
  const [isAtBottom, setIsAtBottom] = React.useState(true);
  const [prefersReducedMotion, setPrefersReducedMotion] = React.useState(false);

  React.useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');
    const update = () => setPrefersReducedMotion(media.matches);
    update();
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);

  /** Scrolls the transcript container, using the user's motion preference for explicit movement. */
  const scrollToBottom = React.useCallback((smooth = false) => {
    const element = containerRef.current;
    if (!element) return;
    element.scrollTo({
      top: element.scrollHeight,
      behavior: smooth && !prefersReducedMotion ? 'smooth' : 'auto',
    });
  }, [prefersReducedMotion]);

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
  }, []);

  // Resetting follow state when the conversation changes must happen before paint.
  /* eslint-disable react-hooks/set-state-in-effect */
  React.useLayoutEffect(() => {
    loadedRef.current = false;
    followRef.current = true;
    lastScrollTopRef.current = 0;
    setIsAtBottom(true);
  }, [conversationId]);
  /* eslint-enable react-hooks/set-state-in-effect */

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
    if (lastMessageRole === 'user') followRef.current = true;
    if (lastMessageId && followRef.current) {
      scrollToBottom();
      pinToBottom();
    }
  }, [conversationId, messageCount, lastMessageId, scrollToBottom, pinToBottom]);

  React.useEffect(() => {
    if (!isStreaming) return;
    const tick = () => {
      const element = containerRef.current;
      if (element && followRef.current) element.scrollTop = element.scrollHeight;
      streamFrameRef.current = requestAnimationFrame(tick);
    };
    streamFrameRef.current = requestAnimationFrame(tick);
    return () => {
      if (streamFrameRef.current !== null) cancelAnimationFrame(streamFrameRef.current);
      streamFrameRef.current = null;
    };
  }, [isStreaming, conversationId]);

  React.useEffect(() => () => {
    if (pinFrameRef.current !== null) cancelAnimationFrame(pinFrameRef.current);
    if (streamFrameRef.current !== null) cancelAnimationFrame(streamFrameRef.current);
  }, []);

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
  }, []);

  /** Stops following immediately when a wheel gesture moves the reader upward. */
  const handleWheel = React.useCallback((event: React.WheelEvent<HTMLDivElement>) => {
    if (event.deltaY < 0) followRef.current = false;
  }, []);

  /** Captures a touch gesture's starting point for scroll-direction detection. */
  const handleTouchStart = React.useCallback((event: React.TouchEvent<HTMLDivElement>) => {
    touchStartYRef.current = event.touches[0]?.clientY ?? null;
  }, []);

  /** Stops following when a touch gesture moves the reader toward earlier messages. */
  const handleTouchMove = React.useCallback((event: React.TouchEvent<HTMLDivElement>) => {
    const startY = touchStartYRef.current;
    if (startY !== null && event.touches[0]?.clientY > startY) followRef.current = false;
  }, []);

  return {
    containerRef,
    isAtBottom,
    scrollHandlers: {
      onScroll: handleScroll,
      onWheel: handleWheel,
      onTouchStart: handleTouchStart,
      onTouchMove: handleTouchMove,
    },
  };
}
