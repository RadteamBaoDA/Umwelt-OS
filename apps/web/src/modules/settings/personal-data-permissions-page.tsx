'use client';

import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { SettingsPageHeader } from './settings-layout';

/** Data sources > Personal data permissions tab body. No dedicated backend exists, so it states that and links to the real controls. */
export function PersonalDataPermissionsPage() {
  const t = useTranslations('shell');
  return <div className="flex flex-col gap-5">
    <SettingsPageHeader eyebrow={t('settings')} title={t('permissionsUnavailableTitle')} description={t('permissionsUnavailable')} />
    <p className="flex flex-wrap gap-4">
      <Link className="text-button" href="/settings/memory">{t('openMemoryPrivacy')}</Link>
      <Link className="text-button" href="/settings/sources/mcp">{t('openMcp')}</Link>
    </p>
  </div>;
}
