'use client';

import { useQuery } from '@tanstack/react-query';
import { usePathname } from 'next/navigation';
import { useEffect, useMemo, useState } from 'react';
import { useTranslations } from 'next-intl';
import type { ApiConnectionStatus } from '@/core/app-shell/connection-footer';
import { formatDateWith } from '@/core/format';
import type { AppLocaleId } from '@/core/i18n';
import { useRealtime } from '@/core/realtime-provider';
import { useAiSettings } from '@/modules/chat';
import { listAllSources, sourceKeys, type Source } from '@/modules/sources';
import './shell-skin.css';

/** Freshness window for a source with no expected cadence (`next_due_at` absent): mirrors dashboard freshness. */
const FRESH_FALLBACK_MS = 2 * 60 * 60 * 1000;

/** Health bucket of one source: `ok` (fresh), `warn` (stale) or `err` (latest error is newer than the last success). */
export type SourceHealth = 'ok' | 'warn' | 'err';

/** Source fields the health rule reads. */
type SourceHealthFields = Pick<Source, 'last_success_at' | 'last_error_at' | 'next_due_at'>;

/** Health counts of the owner's active sources, one bucket per `SourceHealth`. */
export type SourceHealthCounts = Record<SourceHealth, number>;

/** Epoch ms of an optional ISO timestamp; NaN when absent or malformed. */
function timestampOf(value: string | null | undefined): number {
  return value ? Date.parse(value) : NaN;
}

/**
 * Health of one active source, using the same rule as the dashboard freshness tick (`computeFreshnessTick` in
 * `modules/dashboard/freshness-strip.tsx`, which is not exported across modules): failing when the latest error is newer
 * than the latest success (or there is an error and no success); fresh while `next_due_at` has not passed, or within
 * two hours when no cadence is known; anything else is stale.
 *
 * @param source Source fields needed for the health rule.
 * @param now Epoch milliseconds treated as "now".
 * @returns The health bucket; malformed timestamps count as unknown and never throw.
 */
export function sourceHealth(source: SourceHealthFields, now: number): SourceHealth {
  const success = timestampOf(source.last_success_at);
  const error = timestampOf(source.last_error_at);
  const hasSuccess = Number.isFinite(success);
  if (Number.isFinite(error) && (!hasSuccess || error > success)) return 'err';
  const due = timestampOf(source.next_due_at);
  const fresh = hasSuccess && (Number.isFinite(due) ? now <= due : now - success < FRESH_FALLBACK_MS);
  return fresh ? 'ok' : 'warn';
}

/**
 * Counts active sources by health for the statusline. Reads the same `listAllSources` cache entry the dashboard uses
 * (no extra request); realtime source invalidations keep it current. Paused and archived sources are not counted.
 *
 * @returns Counts once the source list has loaded, or `null` while loading or after a failed read (segments hidden).
 */
export function useSourceHealthCounts(): SourceHealthCounts | null {
  const query = useQuery({ queryKey: sourceKeys.listAll, queryFn: listAllSources });
  const items = query.data?.items;
  const asOf = query.dataUpdatedAt;
  return useMemo(() => {
    if (!items) return null;
    const counts: SourceHealthCounts = { ok: 0, warn: 0, err: 0 };
    // Health is a wall-clock judgement, so it is judged as of the fetch that produced the rows (asOf, epoch ms), and is
    // re-derived when they refresh rather than on a timer; reading Date.now() here would make the render impure.
    for (const source of items) {
      if (source.status === 'active') counts[sourceHealth(source, asOf)] += 1;
    }
    return counts;
  }, [items, asOf]);
}

/**
 * Model id of the owner's default chat model, read from the shared `['ai-settings']` query the model picker uses.
 *
 * @returns The default chat model id, or `null` while loading, on error, or when no default is set.
 */
export function useDefaultChatModel(): string | null {
  const query = useAiSettings<{ defaults?: { chat?: { model: string } | null } }>();
  return query.data?.defaults?.chat?.model ?? null;
}

/** Owner preferences the statusline reads: the app locale and IANA timezone. */
export type StatuslinePreferences = { locale: AppLocaleId; timezone: string };

/**
 * Dot tone for one transport. Mirrors the severity mapping in `connection-footer.tsx` (connected = success,
 * connecting/reconnecting = warning, anything else = danger). Kept local because the shell tests mock that module
 * without `statusColor`; keep the two in step when a status is added.
 *
 * @param status - API or realtime status string.
 * @returns The CSS modifier used for the dot colour.
 */
function dotTone(status: string): 'success' | 'warning' | 'danger' {
  if (status === 'connected') return 'success';
  return status === 'connecting' || status === 'reconnecting' ? 'warning' : 'danger';
}

/**
 * Bottom status line (24px) shared by both skins. It is decorative chrome, not a status announcement: it has no
 * role=status or live region, so the realtime status stays the single announced indicator. The transport dots and
 * words are aria-hidden for the same reason. The terminal-only mode chip, the source-health counts, the model segment
 * and the `ShellCrumb` prompt are hidden by CSS outside `html[data-skin='terminal']`. The `#statusline-info` slot is filled by a portal from page headers.
 *
 * @param apiStatus - API health from the shell's health query (drives the API dot).
 * @param preferences - Owner locale and timezone for the clock; `en-us` and the browser zone are used until they load.
 */
export function Statusline({ apiStatus, preferences }: { apiStatus: ApiConnectionStatus; preferences?: StatuslinePreferences }) {
  const t = useTranslations('shell');
  const pathname = usePathname();
  const { status: realtimeStatus } = useRealtime();
  const counts = useSourceHealthCounts();
  const model = useDefaultChatModel();
  const locale = preferences?.locale ?? 'en-us';
  const zone = preferences?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone;
  // Chat routes use INSERT, every other route NORMAL; the chip only shows in the terminal skin.
  const mode = pathname.startsWith('/chat') ? t('statusline.modeInsert') : t('statusline.modeNormal');

  return (
    <footer className="statusline" aria-label={t('statusline.label')}>
      <span className="statusline-mode" aria-hidden="true">
        {mode}
      </span>
      <span className="statusline-route hide-sm">{pathname}</span>
      <span id="statusline-info" className="statusline-info" />
      {counts && <SourceCounts counts={counts} />}
      <span className="statusline-transport" aria-hidden="true">
        <span className={`statusline-dot rounded-full statusline-dot-${dotTone(apiStatus)}`} />
        {t('api')}
        <span className={`statusline-dot rounded-full statusline-dot-${dotTone(realtimeStatus)}`} />
        {t('realtime')}
      </span>
      {model && (
        <span className="statusline-model hide-sm" aria-hidden="true">
          {t('statusline.model', { model })}
        </span>
      )}
      <span className="statusline-locale hide-sm">
        {locale} · {zone}
      </span>
      <StatuslineClock locale={locale} timezone={zone} />
    </footer>
  );
}

/**
 * Source health segments: `ok` once loaded, `warn` and `err` only when non-zero. Decorative like the rest of the footer
 * (aria-hidden); the sources page and dashboard carry the accessible status.
 *
 * @param counts - Health counts from `useSourceHealthCounts`.
 */
function SourceCounts({ counts }: { counts: SourceHealthCounts }) {
  const t = useTranslations('shell');
  return (
    <>
      <span className="statusline-sources" aria-hidden="true">
        <span className="statusline-dot rounded-full statusline-dot-success" />
        {t('statusline.sourcesOk', { count: counts.ok })}
      </span>
      {counts.warn > 0 && (
        <span className="statusline-sources" aria-hidden="true">
          <span className="statusline-dot rounded-full statusline-dot-warning" />
          {t('statusline.warn', { count: counts.warn })}
        </span>
      )}
      {counts.err > 0 && (
        <span className="statusline-sources" aria-hidden="true">
          <span className="statusline-dot rounded-full statusline-dot-danger" />
          {t('statusline.err', { count: counts.err })}
        </span>
      )}
    </>
  );
}

/**
 * Wall-clock segment that re-renders once per second in the given zone. Owns its own interval so the
 * rest of the shell does not re-render every tick; the interval is cleared on unmount.
 *
 * @param locale - App locale used for the time format.
 * @param timezone - IANA zone the time is shown in.
 */
function StatuslineClock({ locale, timezone }: { locale: AppLocaleId; timezone: string }) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(timer);
  }, []);
  return (
    <span className="statusline-clock" aria-hidden="true">
      {formatDateWith(now, locale, { hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: timezone })}
    </span>
  );
}

/**
 * Decorative terminal prompt `owner@umwelt:~<path> $` placed at the start of the top bar. Hidden from assistive technology
 * and visible only in the terminal skin (CSS); it duplicates the route and carries no information.
 */
export function ShellCrumb() {
  const pathname = usePathname();
  return (
    <span className="shell-crumb hide-sm" aria-hidden="true">
      <b>owner@umwelt</b>:<span className="shell-crumb-path">~{pathname}</span> $
    </span>
  );
}
