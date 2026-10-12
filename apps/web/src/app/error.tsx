'use client';

import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';

/** Route-level error boundary: shows a retry action without leaking the error message or digest to the page. */
export default function RouteError({ reset }: Readonly<{ error: Error & { digest?: string }; reset: () => void }>) {
  const t = useTranslations('routeErrors');
  return (
    <main className="page">
      <section className="auth-panel" role="alert">
        <h1>{t('errorTitle')}</h1>
        <p className="muted">{t('errorBody')}</p>
        <Button onClick={reset}>{t('retry')}</Button>
      </section>
    </main>
  );
}
