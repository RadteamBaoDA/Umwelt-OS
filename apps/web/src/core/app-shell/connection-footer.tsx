'use client';

import Link from 'next/link';
import { useTranslations } from 'next-intl';
import type { RealtimeStatus } from '@/core/realtime-provider';

export type ApiConnectionStatus = 'connecting' | 'reconnecting' | 'unavailable' | 'connected' | 'expired' | 'clientOffline';

/** Renders separate API and realtime health states; offers Retry (or Sign in again when the session expired). Never implies source freshness. */
export function ConnectionFooter({ apiStatus, realtimeStatus, onRetry, retrying = false }: { apiStatus: ApiConnectionStatus; realtimeStatus: RealtimeStatus; onRetry?: () => void; retrying?: boolean }) {
  const t = useTranslations('shell');
  const apiLabel = apiStatus === 'expired' ? t('sessionExpired') : apiStatus === 'unavailable' ? t('serverUnreachable') : t(apiStatus);
  const canRetry = apiStatus !== 'connected' && apiStatus !== 'expired' && onRetry;
  return <footer className="connection-footer" aria-label={t('connectionStatus')}>
    <span role="status" aria-live="polite">{t('api')}: {apiLabel}</span>
    {canRetry && <button type="button" className="text-button" disabled={retrying} onClick={onRetry}>{retrying ? t('reconnecting') : t('retryApi')}</button>}
    {apiStatus === 'expired' && <Link className="text-button" href="/login">{t('signInAgain')}</Link>}
    <span role="status" aria-live="polite">{t('realtime')}: {t(realtimeStatus === 'expired' ? 'sessionExpired' : realtimeStatus === 'unavailable' ? 'serverUnreachable' : realtimeStatus)}</span>
    <small>{t('sourceFreshnessNote')}</small>
  </footer>;
}
