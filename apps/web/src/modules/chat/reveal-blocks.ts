'use client';

import * as React from 'react';

/**
 * Fades each top-level block of a completed answer in once, when it first scrolls into view.
 * Does nothing without IntersectionObserver, under prefers-reduced-motion, or while `enabled`
 * is false (streaming answers re-render their HTML on every delta, so they are never hidden).
 *
 * The blocks are the direct children of the first child of `ref` (the ChatMarkdown container);
 * the CSS in chat-gaps.css owns the hidden/visible styling.
 *
 * @param ref - Wrapper element around a ChatMarkdown.
 * @param enabled - True only for settled (non-streaming) messages.
 * @param content - Message content; re-runs when the rendered markdown changes.
 */
export function useRevealBlocks(
  ref: React.RefObject<HTMLElement | null>,
  enabled: boolean,
  content: string,
): void {
  React.useEffect(() => {
    const root = ref.current?.firstElementChild;
    if (!enabled || !root || typeof IntersectionObserver === 'undefined') return;
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.remove('reveal-pending');
        observer.unobserve(entry.target);
      });
    }, { threshold: 0.05 });
    // ChatMarkdown sanitizes asynchronously, so blocks may not exist yet: re-scan on mutation.
    const scan = () => {
      Array.from(root.children).forEach((block) => {
        if (block.classList.contains('reveal-block')) return;
        block.classList.add('reveal-block', 'reveal-pending');
        observer.observe(block);
      });
    };
    scan();
    const mutations = new MutationObserver(scan);
    mutations.observe(root, { childList: true });
    return () => {
      observer.disconnect();
      mutations.disconnect();
      // Unhide anything still pending so a stale observer can never leave text invisible.
      Array.from(root.children).forEach((block) => block.classList.remove('reveal-pending'));
    };
  }, [ref, enabled, content]);
}
