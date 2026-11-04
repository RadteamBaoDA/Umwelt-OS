'use client';

import { useTranslations } from 'next-intl';
import { useEffect, useMemo, useState } from 'react';
import type { Source } from '@/modules/sources';
import { BareButton } from '@/components/bare-button';

const DAY_MS = 24 * 60 * 60 * 1000;
/** Active-id sentinel for the single folded marker of sources older than 24 h. */
const OVERDUE_ID = '__overdue__';
const FRESH_FALLBACK_MS = 2 * 60 * 60 * 1000;

/** Freshness class of one source tick. */
export type FreshnessStatus = 'fresh' | 'stale' | 'failing';

/** Computed placement and state of one source on the 24 h axis. */
export interface FreshnessTick {
  id: string;
  name: string;
  /** Percent from the left edge (0 = -24h, 100 = now). */
  position: number;
  status: FreshnessStatus;
  /** True when the last success is older than 24 h or unknown (hollow mark pinned left). */
  clamped: boolean;
  lastSuccessMs: number | null;
  errorCode: string | null;
}

type TickSource = Pick<Source, 'id' | 'name' | 'last_success_at' | 'last_error_at' | 'last_error_code' | 'collection_error_code' | 'next_due_at'>;

/** Epoch ms of an optional ISO timestamp; NaN when absent or malformed. */
function parseTimestamp(value: string | null | undefined): number {
  return value ? Date.parse(value) : NaN;
}

/**
 * Pure placement and status rule for one source: position on the 24 h axis, clamped at the left
 * edge when older; failing when the latest error is newer than the latest success; fresh while
 * inside the expected cadence (`next_due_at` not yet passed) or, without cadence, newer than 2 h.
 *
 * @param source Source fields needed for freshness.
 * @param now Epoch milliseconds treated as "now".
 * @returns Tick geometry and status; malformed timestamps count as unknown, never throw.
 */
export function computeFreshnessTick(source: TickSource, now: number): FreshnessTick {
  const success = parseTimestamp(source.last_success_at);
  const error = parseTimestamp(source.last_error_at);
  const hasSuccess = Number.isFinite(success);
  const age = hasSuccess ? Math.max(0, now - success) : Infinity;
  const failing = Number.isFinite(error) && (!hasSuccess || error > success);
  const due = parseTimestamp(source.next_due_at);
  const fresh = hasSuccess && (Number.isFinite(due) ? now <= due : age < FRESH_FALLBACK_MS);
  return {
    id: source.id,
    name: source.name,
    position: age >= DAY_MS ? 0 : 100 - (age / DAY_MS) * 100,
    status: failing ? 'failing' : fresh ? 'fresh' : 'stale',
    clamped: age >= DAY_MS,
    lastSuccessMs: hasSuccess ? success : null,
    errorCode: failing ? source.last_error_code ?? source.collection_error_code ?? null : null,
  };
}

const STATUS_COLOR: Record<FreshnessStatus, string> = {
  fresh: 'var(--accent)',
  stale: 'var(--status-warning)',
  failing: 'var(--negative)',
};

/**
 * Horizontal jitter so ticks sharing (nearly) the same axis position stay individually visible.
 *
 * @param ticks All ticks in render order.
 * @param index Index of the tick being placed.
 * @returns Pixel offset of 0, 2 or -2 by rank among earlier ticks within 0.5% of the same position.
 */
export function jitterPx(ticks: readonly FreshnessTick[], index: number): number {
  let rank = 0;
  for (let i = 0; i < index; i += 1) {
    if (Math.abs(ticks[i].position - ticks[index].position) < 0.5) rank += 1;
  }
  return [0, 2, -2][rank % 3];
}

/**
 * Splits ticks for rendering: ticks inside the 24 h window stay individual, every older or
 * never-collected tick folds into one overdue marker so a row of identical clamped brackets
 * does not pile up at the left edge.
 *
 * @param ticks All ticks in render order.
 * @returns Individual in-window ticks, the folded overdue ticks, and per-status counts.
 */
export function groupFreshnessTicks(ticks: readonly FreshnessTick[]): {
  inWindow: FreshnessTick[];
  overdue: FreshnessTick[];
  counts: Record<FreshnessStatus, number>;
} {
  const counts: Record<FreshnessStatus, number> = { fresh: 0, stale: 0, failing: 0 };
  for (const tick of ticks) counts[tick.status] += 1;
  return { inWindow: ticks.filter((tick) => !tick.clamped), overdue: ticks.filter((tick) => tick.clamped), counts };
}

/**
 * Formats a past instant as a localized relative phrase.
 *
 * @param ms Epoch milliseconds of the event.
 * @param locale Display locale id.
 * @returns Phrase such as "12 minutes ago".
 */
function formatAgo(ms: number, locale: string): string {
  const minutes = Math.round((Date.now() - ms) / 60_000);
  const rtf = new Intl.RelativeTimeFormat(locale.toLowerCase().startsWith('vi') ? 'vi' : 'en', { numeric: 'auto' });
  if (minutes < 60) return rtf.format(-Math.max(0, minutes), 'minute');
  if (minutes < 1440) return rtf.format(-Math.round(minutes / 60), 'hour');
  return rtf.format(-Math.round(minutes / 1440), 'day');
}

/**
 * Thin 24-hour axis showing one focusable tick per source at its last successful collection.
 * Presentational: reuses the dashboard's already-loaded sources and performs no requests.
 * Ticks are keyboard-focusable buttons; the tooltip shows on hover or focus and the group label
 * summarises fresh/stale/failing counts.
 *
 * @param props.sources Sources the dashboard reads; empty renders nothing.
 * @param props.locale Display locale id used for relative times.
 * @returns The strip, or null when there are no sources.
 */
export function FreshnessStrip({ sources, locale }: { sources: readonly TickSource[]; locale: string }) {
  const t = useTranslations('dashboard');
  const [activeId, setActiveId] = useState<string | null>(null);
  // "now" advances once a minute so ticks drift left and ages stay current without refetching.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, []);
  const ticks = useMemo(() => sources.map((s) => computeFreshnessTick(s, now)), [sources, now]);
  if (ticks.length === 0) return null;
  const { inWindow, overdue, counts } = groupFreshnessTicks(ticks);
  /** Returns how many sources are in the given freshness status. */
  const count = (status: FreshnessStatus) => counts[status];
  // The overdue marker takes the worst status among its members so a failing source is never hidden.
  /** Formats the time since a source last succeeded, or the never-synced label. */
  const ago = (tick: FreshnessTick) => (tick.lastSuccessMs !== null ? formatAgo(tick.lastSuccessMs, locale) : t('staleSourceNever'));
  const overdueActive = activeId === OVERDUE_ID;
  const active = overdueActive ? null : ticks.find((tick) => tick.id === activeId) ?? null;
  const activePosition = overdueActive ? 0 : active?.position ?? 0;
  const tooltip = overdueActive
    ? overdue.map((tick) => `${tick.name} · ${ago(tick)}`).join(' - ')
    : active
    ? [
      t('freshnessTooltip', { name: active.name, time: ago(active) }),
      active.status === 'failing' ? t('staleSourceLine', { name: active.name, code: active.errorCode ?? '-' }) : null,
    ].filter(Boolean).join(' - ')
    : null;
  const legend = (['fresh', 'stale', 'failing'] as const)
    .filter((status) => count(status) > 0)
    .map((status) => ({
      status,
      label: t(status === 'fresh' ? 'freshnessLegendFresh' : status === 'stale' ? 'freshnessLegendStale' : 'freshnessLegendFailing', { count: count(status) }),
    }));

  return (
    <div className="flex w-full flex-col items-stretch gap-1 text-xs text-muted-foreground min-[721px]:flex-row min-[721px]:items-start min-[721px]:gap-3">
    <div
      role="group"
      aria-label={t('freshnessSummary', { total: ticks.length, fresh: count('fresh'), stale: count('stale'), failing: count('failing') })}
      data-testid="freshness-strip"
      className="relative h-9 min-w-0 min-[721px]:flex-1"
    >
      {/* 1px baseline at y=16px; ticks sit above it and labels in their own row below so they never overlap. */}
      <div className="absolute inset-x-0 top-4 h-px" style={{ background: 'var(--control)' }} aria-hidden="true" />
      {inWindow.map((tick, index) => (
        <BareButton
          key={tick.id}
          type="button"
          aria-label={`${tick.name} · ${ago(tick)}`}
          onMouseEnter={() => setActiveId(tick.id)}
          onMouseLeave={() => setActiveId(null)}
          onFocus={() => setActiveId(tick.id)}
          onBlur={() => setActiveId(null)}
          className="absolute top-0 flex h-4 w-4 -translate-x-1/2 items-end justify-center rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          style={{ left: `calc(0.5rem + (100% - 1rem) * ${tick.position / 100} + ${jitterPx(inWindow, index)}px)` }}
        >
          <span aria-hidden="true" className="block" style={{ width: 3, height: 12, borderRadius: 1, background: STATUS_COLOR[tick.status] }} />
        </BareButton>
      ))}
      <span aria-hidden="true" className="absolute bottom-0 left-0">{t('freshnessAxisHours', { hours: 24 })}</span>
      <span aria-hidden="true" className="absolute bottom-0 left-1/2 hidden -translate-x-1/2 sm:block">{t('freshnessAxisHours', { hours: 12 })}</span>
      <span aria-hidden="true" className="absolute bottom-0 right-0">{t('freshnessAxisNow')}</span>
      {(active || overdueActive) && tooltip && (
        <span
          role="tooltip"
          className="pointer-events-none absolute top-7 z-20 max-w-xs -translate-x-1/2 rounded-md border border-border bg-popover px-2 py-1 text-xs text-popover-foreground"
          style={{ left: `clamp(8rem, calc(0.5rem + (100% - 1rem) * ${activePosition / 100}), calc(100% - 8rem))` }}
        >
          {tooltip}
        </span>
      )}
    </div>
    <p data-testid="freshness-legend" className="m-0 flex shrink-0 flex-wrap items-center justify-start gap-x-2.5 max-[720px]:order-first min-[721px]:justify-end gap-y-0.5 pt-0.5 text-xs tabular-nums">
      {overdue.length > 0 && (
        <BareButton
          type="button"
          aria-label={`${t('freshnessOverdue', { count: overdue.length })}: ${overdue.map((tick) => tick.name).join(', ')}`}
          onMouseEnter={() => setActiveId(OVERDUE_ID)}
          onMouseLeave={() => setActiveId(null)}
          onFocus={() => setActiveId(OVERDUE_ID)}
          onBlur={() => setActiveId(null)}
          className="inline-flex items-center gap-1 whitespace-nowrap rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <span aria-hidden="true" className="block" style={{ width: 8, height: 8, borderRadius: '50%', border: `1.5px solid ${STATUS_COLOR.stale}`, background: 'transparent' }} />
          <span aria-hidden="true" className="text-xs tabular-nums leading-none text-muted-foreground">{t('freshnessOverdue', { count: overdue.length })}</span>
        </BareButton>
      )}
      {legend.map((entry) => (
        <span key={entry.status} className="inline-flex items-center gap-1 whitespace-nowrap">
          <span aria-hidden="true" className="block h-2 w-2 rounded-full" style={{ background: STATUS_COLOR[entry.status] }} />
          {entry.label}
        </span>
      ))}
    </p>
    </div>
  );
}
