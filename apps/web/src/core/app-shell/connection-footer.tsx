'use client';

import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { Info, LogIn, Radio, RotateCw, Server } from 'lucide-react';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import type { RealtimeStatus } from '@/core/realtime-provider';

export type ApiConnectionStatus = 'connecting' | 'reconnecting' | 'unavailable' | 'connected' | 'expired' | 'clientOffline';

/** Renders compact, accessible API and realtime health indicators with recovery actions; transport health never implies source freshness. */
export function ConnectionFooter({ apiStatus, realtimeStatus, onRetry, retrying = false }: { apiStatus: ApiConnectionStatus; realtimeStatus: RealtimeStatus; onRetry?: () => void; retrying?: boolean }) {
  const t = useTranslations('shell');
  const apiLabel = apiStatus === 'expired' ? t('sessionExpired') : apiStatus === 'unavailable' ? t('serverUnreachable') : t(apiStatus);
  const realtimeLabel = realtimeStatus === 'expired' ? t('sessionExpired') : realtimeStatus === 'unavailable' ? t('serverUnreachable') : t(realtimeStatus);
  const apiColor = apiStatus === 'connected' ? 'success' : apiStatus === 'connecting' || apiStatus === 'reconnecting' ? 'warning' : 'danger';
  const realtimeColor = realtimeStatus === 'connected' ? 'success' : realtimeStatus === 'connecting' || realtimeStatus === 'reconnecting' ? 'warning' : 'danger';
  const canRetry = apiStatus !== 'connected' && apiStatus !== 'expired' && onRetry;

  return <footer className="connection-footer" aria-label={t('connectionStatus')}>
    <TooltipProvider delayDuration={300}>
      <Tooltip>
        <TooltipTrigger asChild>
          <button type="button" className={`connection-status connection-status-${apiColor}`} aria-label={`${t('api')}: ${apiLabel}`}>
            <Server size={17} aria-hidden="true" />
          </button>
        </TooltipTrigger>
        <TooltipContent className="connection-tooltip">
          <strong>{t('api')}: {apiLabel}</strong>
          <span>{t('apiStatusHelp')}</span>
          <span>{apiStatus === 'connected' ? t('apiConnectedHelp') : apiStatus === 'connecting' ? t('apiConnectingHelp') : apiStatus === 'reconnecting' ? t('apiReconnectingHelp') : apiStatus === 'clientOffline' ? t('apiOfflineHelp') : apiStatus === 'expired' ? t('apiExpiredHelp') : t('apiUnavailableHelp')}</span>
        </TooltipContent>
      </Tooltip>
      <Tooltip>
        <TooltipTrigger asChild>
          <button type="button" className={`connection-status connection-status-${realtimeColor}`} aria-label={`${t('realtime')}: ${realtimeLabel}`}>
            <Radio size={17} aria-hidden="true" />
          </button>
        </TooltipTrigger>
        <TooltipContent className="connection-tooltip">
          <strong>{t('realtime')}: {realtimeLabel}</strong>
          <span>{t('realtimeStatusHelp')}</span>
          <span>{realtimeStatus === 'connected' ? t('realtimeConnectedHelp') : realtimeStatus === 'connecting' ? t('realtimeConnectingHelp') : realtimeStatus === 'reconnecting' ? t('realtimeReconnectingHelp') : realtimeStatus === 'expired' ? t('realtimeExpiredHelp') : t('realtimeUnavailableHelp')}</span>
        </TooltipContent>
      </Tooltip>
      {canRetry && <Tooltip>
        <TooltipTrigger asChild>
          <button type="button" className="connection-action" aria-label={retrying ? t('reconnecting') : t('retryApi')} disabled={retrying} onClick={onRetry}>
            <RotateCw size={16} aria-hidden="true" />
          </button>
        </TooltipTrigger>
        <TooltipContent>{retrying ? t('reconnecting') : t('retryApi')}</TooltipContent>
      </Tooltip>}
      {apiStatus === 'expired' && <Tooltip>
        <TooltipTrigger asChild>
          <Link className="connection-action" href="/login" aria-label={t('signInAgain')}>
            <LogIn size={17} aria-hidden="true" />
          </Link>
        </TooltipTrigger>
        <TooltipContent>{t('signInAgain')}</TooltipContent>
      </Tooltip>}
      <Tooltip>
        <TooltipTrigger asChild>
          <button type="button" className="connection-action connection-info" aria-label={t('sourceFreshnessTitle')}>
            <Info size={17} aria-hidden="true" />
          </button>
        </TooltipTrigger>
        <TooltipContent className="connection-tooltip connection-tooltip-info">
          <strong>{t('sourceFreshnessTitle')}</strong>
          <span>{t('sourceFreshnessNote')}</span>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
    <span className="sr-only" role="status" aria-live="polite">{t('api')}: {apiLabel}</span>
    <span className="sr-only" role="status" aria-live="polite">{t('realtime')}: {realtimeLabel}</span>
  </footer>;
}
