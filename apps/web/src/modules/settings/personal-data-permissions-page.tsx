'use client';

import Link from 'next/link';
import { useTranslations } from 'next-intl';

/** Data sources > Personal data permissions tab body. No dedicated backend exists, so it states that and links to the real controls. */
export function PersonalDataPermissionsPage() {
  const t = useTranslations('shell');
  return <section className="content-panel">
    <header className="section-heading"><div><h1>{t('permissionsUnavailableTitle')}</h1><p className="muted">{t('permissionsUnavailable')}</p></div></header>
    <p className="flex flex-wrap gap-4">
      <Link className="text-button" href="/settings/memory">{t('openMemoryPrivacy')}</Link>
      <Link className="text-button" href="/settings/sources/mcp">{t('openMcp')}</Link>
    </p>
  </section>;
}
