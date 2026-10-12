'use client';

import { useTranslations } from 'next-intl';

/** Root Suspense fallback shown while a route segment streams in. */
export default function Loading() {
  const t = useTranslations('routeErrors');
  return <main className="page"><div className="skeleton" style={{ width: 'min(100%, 480px)' }} aria-label={t('loading')} /></main>;
}
