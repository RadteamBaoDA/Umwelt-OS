'use client';

import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import { useItemTranslation, type ItemTranslationController, type TranslationTarget } from '@/modules/translations';
import { eventKind } from '../gadgets/event-kind';
import type { StoredEvent } from './store';

/** Server bounds of the inline `live_event` snapshot (code points); longer text is cut before hashing and sending. */
export const TITLE_MAX = 500;
export const SUMMARY_MAX = 1000;
/**
 * Most NEWS events given a translation target per render. Each target is one react-query observer, so the list is
 * capped; events past the cap simply show no Translate button. ponytail: raise or switch to visible-rows-only if
 * owners hold more than this many NEWS events in range.
 */
export const MAX_TARGETS = 200;

/** Cuts `value` to at most `max` code points (the server counts code points, not UTF-16 units). */
function clip(value: string, max: number): string {
  const points = Array.from(value);
  return points.length <= max ? value : points.slice(0, max).join('');
}

/** Lower-case hex SHA-256 of the UTF-8 bytes of `text`. */
async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('');
}

/** True for events shown with the NEWS kind label; only those can be translated. */
export function isNewsEvent(event: StoredEvent): boolean {
  return eventKind(event).label === 'NEWS';
}

/**
 * Builds the translation target of one live event. The id is the first 32 hex chars of sha256(`live_event:`+key)
 * laid out 8-4-4-4-12 (live event keys are not UUIDs); the revision is sha256(title + newline + (summary ?? "")),
 * matching the server's `live_event_revision`; `source` is the snapshot the server re-hashes and translates.
 * Title and summary are clipped to the server bounds first so hash and body always agree.
 *
 * @param event Stored live event with a non-empty title.
 * @returns Target carrying id, revision and inline source.
 */
export async function liveEventTarget(event: StoredEvent): Promise<TranslationTarget> {
  const title = clip(event.title, TITLE_MAX);
  const summary = event.summary == null ? null : clip(event.summary, SUMMARY_MAX);
  const [idHex, revision] = await Promise.all([sha256Hex(`live_event:${event.key}`), sha256Hex(`${title}\n${summary ?? ''}`)]);
  const h = idHex.slice(0, 32);
  const id = `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20, 32)}`;
  return { id, revision, source: { title, summary } };
}

/** Translation state for the live map: the shared controller and the target of an event key (if translatable). */
export type LiveEventTranslation = {
  controller: ItemTranslationController;
  targetOf: (key: string) => TranslationTarget | undefined;
};

/**
 * Owner-only per-item translation of NEWS live events. Targets are hashed asynchronously (capped at `MAX_TARGETS`);
 * a result from a superseded event list is discarded. Nothing is submitted until a row's Translate button is used.
 *
 * @param events Filtered events currently shown.
 * @param enabled False for non-owners: no targets are built, so no button can render.
 * @returns Controller from `useItemTranslation('live_event', ...)` and a key-to-target lookup.
 */
export function useLiveEventTranslation(events: StoredEvent[], enabled: boolean): LiveEventTranslation {
  const [byKey, setByKey] = useState<ReadonlyMap<string, TranslationTarget>>(() => new Map());
  useEffect(() => {
    if (!enabled) return undefined;
    let stale = false;
    const news = events.filter((event) => isNewsEvent(event) && event.title).slice(0, MAX_TARGETS);
    void Promise.all(news.map(liveEventTarget)).then((targets) => {
      if (!stale) setByKey(new Map(news.map((event, i) => [event.key, targets[i]])));
    });
    return () => {
      stale = true;
    };
  }, [events, enabled]);
  // Disabled (member) view: ignore any targets built earlier so no button can render.
  const active = useMemo(() => (enabled ? byKey : new Map<string, TranslationTarget>()), [enabled, byKey]);
  const targets = useMemo(() => [...active.values()], [active]);
  const controller = useItemTranslation('live_event', targets);
  return { controller, targetOf: (key) => active.get(key) };
}

/** Provides the owner's live-event translation to rows and the popup; null for members (no buttons). */
export const LiveEventTranslationContext = createContext<LiveEventTranslation | null>(null);

/** Reads the live-event translation provided by the live map gadget, or null outside one or for non-owners. */
export function useLiveEventTranslationContext(): LiveEventTranslation | null {
  return useContext(LiveEventTranslationContext);
}

/**
 * Translation view of one event for a row or popup: its target and controller (both null without owner translation
 * or for a non-NEWS event) plus the title and summary to display (translated when a ready translation is shown).
 *
 * @param event Event being rendered.
 */
export function useTranslatedEvent(event: StoredEvent): {
  controller: ItemTranslationController | null;
  target: TranslationTarget | undefined;
  title: string;
  summary: string | null;
} {
  const translation = useLiveEventTranslationContext();
  const target = translation?.targetOf(event.key);
  const shown = target ? translation?.controller.shown(target.id) : undefined;
  return {
    controller: target ? (translation?.controller ?? null) : null,
    target,
    title: shown?.title ?? event.title,
    summary: shown?.excerpt ?? event.summary ?? null,
  };
}
