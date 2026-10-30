import Link from 'next/link';
import { useTranslations } from 'next-intl';

/** Renders the 404 page for unmatched routes with a link back to the app entry. */
export default function NotFound() {
  const t = useTranslations('routeErrors');
  return (
    <main className="page">
      <section className="auth-panel">
        <h1>{t('notFoundTitle')}</h1>
        <p className="muted">{t('notFoundBody')}</p>
        <Link className="text-foreground underline underline-offset-2" href="/">{t('home')}</Link>
      </section>
    </main>
  );
}
