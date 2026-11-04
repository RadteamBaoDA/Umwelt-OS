'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';
import { LogIn, RotateCw } from 'lucide-react';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import type { RealtimeStatus } from '@/core/realtime-provider';

export type ApiConnectionStatus = 'connecting' | 'reconnecting' | 'unavailable' | 'connected' | 'expired' | 'clientOffline';

/** Milliseconds a non-connected state must persist before the indicator is shown. */
const DEGRADED_DELAY_MS = 5000;

type StatusColor = 'success' | 'warning' | 'danger';
type ShellTranslator = ReturnType<typeof useTranslations<'shell'>>;

/** Help-text message key per API status; any status outside the table (including `unavailable`) falls back to the unavailable text. */
const apiHelpKeys = {
  connected: 'apiConnectedHelp', connecting: 'apiConnectingHelp', reconnecting: 'apiReconnectingHelp',
  clientOffline: 'apiOfflineHelp', expired: 'apiExpiredHelp', unavailable: 'apiUnavailableHelp',
} as const satisfies Record<ApiConnectionStatus, string>;

/** Help-text message key per realtime status. */
const realtimeHelpKeys = {
  connected: 'realtimeConnectedHelp', connecting: 'realtimeConnectingHelp', reconnecting: 'realtimeReconnectingHelp',
  expired: 'realtimeExpiredHelp', unavailable: 'realtimeUnavailableHelp',
} as const satisfies Record<RealtimeStatus, string>;

/** Localized label for a transport status; expiry and unreachability use their dedicated messages. */
function statusLabel(status: ApiConnectionStatus | RealtimeStatus, t: ShellTranslator): string {
  if (status === 'expired') return t('sessionExpired');
  if (status === 'unavailable') return t('serverUnreachable');
  return t(status);
}

/** Severity colour of a transport: connected is success, connecting/reconnecting warning, everything else danger. */
function statusColor(status: ApiConnectionStatus | RealtimeStatus): StatusColor {
  if (status === 'connected') return 'success';
  return status === 'connecting' || status === 'reconnecting' ? 'warning' : 'danger';
}

/** The worst of the two transport colours drives the dot colour and the short label. */
function worstColor(a: StatusColor, b: StatusColor): StatusColor {
  if (a === 'danger' || b === 'danger') return 'danger';
  return a === 'warning' || b === 'warning' ? 'warning' : 'success';
}

/**
 * Renders the 48px top-bar connection indicator only while degraded: nothing visible is shown when both transports are connected,
 * and a non-connected state must persist 5 s before the indicator appears (brief reconnects stay silent); screen-reader status text is always present.
 * The indicator is: a semantic status dot plus a short label on desktop (dot-only on mobile, named by aria-label).
 * Clicking opens a popover with API and realtime details, retry / sign-in-again actions and the source-freshness note.
 * Transport health never implies source freshness. The live-region spans announce status changes to assistive technology.
 */
export function ConnectionFooter({ apiStatus, realtimeStatus, onRetry, retrying = false }: { apiStatus: ApiConnectionStatus; realtimeStatus: RealtimeStatus; onRetry?: () => void; retrying?: boolean }) {
  const t = useTranslations('shell');
  const apiLabel = statusLabel(apiStatus, t);
  const realtimeLabel = statusLabel(realtimeStatus, t);
  const apiColor = statusColor(apiStatus);
  const realtimeColor = statusColor(realtimeStatus);
  const canRetry = apiStatus !== 'connected' && apiStatus !== 'expired' && onRetry;
  // The worst of the two transports drives the dot colour and the short label.
  const overall = worstColor(apiColor, realtimeColor);
  const degraded = overall !== 'success';
  // Debounce: show only after the degraded state has lasted DEGRADED_DELAY_MS; any recovery resets the timer.
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    if (!degraded) return;
    const timer = setTimeout(() => setVisible(true), DEGRADED_DELAY_MS);
    // Reset in cleanup (not in the effect body) so recovery hides the indicator without a synchronous set-state in the effect.
    return () => { clearTimeout(timer); setVisible(false); };
  }, [degraded]);
  const summary = overall === 'success' ? t('allSystemsNormal') : apiColor !== 'success' ? `${t('api')}: ${apiLabel}` : `${t('realtime')}: ${realtimeLabel}`;

  return <div className="connection-footer">
    {visible && degraded && <Popover>
      <PopoverTrigger asChild>
        <button type="button" className="connection-indicator" aria-label={`${t('connectionStatus')}: ${summary}`}>
          <span className={`connection-dot connection-dot-${overall}`} aria-hidden="true" />
          <span className="connection-indicator-label">{summary}</span>
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="connection-tooltip">
        <strong>{t('api')}: {apiLabel}</strong>
        <span>{t('apiStatusHelp')}</span>
        <span>{t(apiHelpKeys[apiStatus])}</span>
        <strong>{t('realtime')}: {realtimeLabel}</strong>
        <span>{t('realtimeStatusHelp')}</span>
        <span>{t(realtimeHelpKeys[realtimeStatus])}</span>
        {canRetry && <button type="button" className="connection-action" aria-label={retrying ? t('reconnecting') : t('retryApi')} disabled={retrying} onClick={onRetry}>
          <RotateCw size={16} aria-hidden="true" /><span>{retrying ? t('reconnecting') : t('retryApi')}</span>
        </button>}
        {apiStatus === 'expired' && <Link className="connection-action" href="/login" aria-label={t('signInAgain')}>
          <LogIn size={17} aria-hidden="true" /><span>{t('signInAgain')}</span>
        </Link>}
        <strong>{t('sourceFreshnessTitle')}</strong>
        <span>{t('sourceFreshnessNote')}</span>
      </PopoverContent>
    </Popover>}
    <span className="sr-only" role="status" aria-live="polite">{t('api')}: {apiLabel}</span>
    <span className="sr-only" role="status" aria-live="polite">{t('realtime')}: {realtimeLabel}</span>
  </div>;
}
