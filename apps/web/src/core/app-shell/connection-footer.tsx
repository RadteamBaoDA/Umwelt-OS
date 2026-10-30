'use client';

import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { LogIn, RotateCw } from 'lucide-react';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import type { RealtimeStatus } from '@/core/realtime-provider';

export type ApiConnectionStatus = 'connecting' | 'reconnecting' | 'unavailable' | 'connected' | 'expired' | 'clientOffline';

/**
 * Renders the 48px top-bar connection indicator: a semantic status dot plus a short label on desktop (dot-only on mobile, named by aria-label).
 * Clicking opens a popover with API and realtime details, retry / sign-in-again actions and the source-freshness note.
 * Transport health never implies source freshness. The live-region spans announce status changes to assistive technology.
 */
export function ConnectionFooter({ apiStatus, realtimeStatus, onRetry, retrying = false }: { apiStatus: ApiConnectionStatus; realtimeStatus: RealtimeStatus; onRetry?: () => void; retrying?: boolean }) {
  const t = useTranslations('shell');
  const apiLabel = apiStatus === 'expired' ? t('sessionExpired') : apiStatus === 'unavailable' ? t('serverUnreachable') : t(apiStatus);
  const realtimeLabel = realtimeStatus === 'expired' ? t('sessionExpired') : realtimeStatus === 'unavailable' ? t('serverUnreachable') : t(realtimeStatus);
  const apiColor = apiStatus === 'connected' ? 'success' : apiStatus === 'connecting' || apiStatus === 'reconnecting' ? 'warning' : 'danger';
  const realtimeColor = realtimeStatus === 'connected' ? 'success' : realtimeStatus === 'connecting' || realtimeStatus === 'reconnecting' ? 'warning' : 'danger';
  const canRetry = apiStatus !== 'connected' && apiStatus !== 'expired' && onRetry;
  // The worst of the two transports drives the dot colour and the short label.
  const overall = apiColor === 'danger' || realtimeColor === 'danger' ? 'danger' : apiColor === 'warning' || realtimeColor === 'warning' ? 'warning' : 'success';
  const summary = overall === 'success' ? t('allSystemsNormal') : apiColor !== 'success' ? `${t('api')}: ${apiLabel}` : `${t('realtime')}: ${realtimeLabel}`;

  return <div className="connection-footer">
    <Popover>
      <PopoverTrigger asChild>
        <button type="button" className="connection-indicator" aria-label={`${t('connectionStatus')}: ${summary}`}>
          <span className={`connection-dot connection-dot-${overall}`} aria-hidden="true" />
          <span className="connection-indicator-label">{summary}</span>
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="connection-tooltip">
        <strong>{t('api')}: {apiLabel}</strong>
        <span>{t('apiStatusHelp')}</span>
        <span>{apiStatus === 'connected' ? t('apiConnectedHelp') : apiStatus === 'connecting' ? t('apiConnectingHelp') : apiStatus === 'reconnecting' ? t('apiReconnectingHelp') : apiStatus === 'clientOffline' ? t('apiOfflineHelp') : apiStatus === 'expired' ? t('apiExpiredHelp') : t('apiUnavailableHelp')}</span>
        <strong>{t('realtime')}: {realtimeLabel}</strong>
        <span>{t('realtimeStatusHelp')}</span>
        <span>{realtimeStatus === 'connected' ? t('realtimeConnectedHelp') : realtimeStatus === 'connecting' ? t('realtimeConnectingHelp') : realtimeStatus === 'reconnecting' ? t('realtimeReconnectingHelp') : realtimeStatus === 'expired' ? t('realtimeExpiredHelp') : t('realtimeUnavailableHelp')}</span>
        {canRetry && <button type="button" className="connection-action" aria-label={retrying ? t('reconnecting') : t('retryApi')} disabled={retrying} onClick={onRetry}>
          <RotateCw size={16} aria-hidden="true" /><span>{retrying ? t('reconnecting') : t('retryApi')}</span>
        </button>}
        {apiStatus === 'expired' && <Link className="connection-action" href="/login" aria-label={t('signInAgain')}>
          <LogIn size={17} aria-hidden="true" /><span>{t('signInAgain')}</span>
        </Link>}
        <strong>{t('sourceFreshnessTitle')}</strong>
        <span>{t('sourceFreshnessNote')}</span>
      </PopoverContent>
    </Popover>
    <span className="sr-only" role="status" aria-live="polite">{t('api')}: {apiLabel}</span>
    <span className="sr-only" role="status" aria-live="polite">{t('realtime')}: {realtimeLabel}</span>
  </div>;
}
