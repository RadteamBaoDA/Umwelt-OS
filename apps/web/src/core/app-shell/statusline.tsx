'use client';

import { usePathname } from 'next/navigation';
import { useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';
import type { ApiConnectionStatus } from '@/core/app-shell/connection-footer';
import { formatDateWith } from '@/core/format';
import type { AppLocaleId } from '@/core/i18n';
import { useRealtime } from '@/core/realtime-provider';
import './shell-skin.css';

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
 * words are aria-hidden for the same reason. The terminal-only mode chip and the `ShellCrumb` prompt are hidden
 * by CSS outside `html[data-skin='terminal']`. The `#statusline-info` slot is filled by a portal from page headers.
 *
 * @param apiStatus - API health from the shell's health query (drives the API dot).
 * @param preferences - Owner locale and timezone for the clock; `en-us` and the browser zone are used until they load.
 */
export function Statusline({ apiStatus, preferences }: { apiStatus: ApiConnectionStatus; preferences?: StatuslinePreferences }) {
  const t = useTranslations('shell');
  const pathname = usePathname();
  const { status: realtimeStatus } = useRealtime();
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
      <span className="statusline-transport" aria-hidden="true">
        <span className={`statusline-dot statusline-dot-${dotTone(apiStatus)}`} />
        {t('api')}
        <span className={`statusline-dot statusline-dot-${dotTone(realtimeStatus)}`} />
        {t('realtime')}
      </span>
      <span className="statusline-locale hide-sm">
        {locale} · {zone}
      </span>
      <StatuslineClock locale={locale} timezone={zone} />
    </footer>
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
 * Decorative terminal prompt `umwelt:~<path> $` placed at the start of the top bar. Hidden from assistive technology
 * and visible only in the terminal skin (CSS); it duplicates the route and carries no information.
 */
export function ShellCrumb() {
  const pathname = usePathname();
  return (
    <span className="shell-crumb hide-sm" aria-hidden="true">
      <b>umwelt</b>:<span className="shell-crumb-path">~{pathname}</span> $
    </span>
  );
}
