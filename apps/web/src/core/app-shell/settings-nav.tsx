'use client';

import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { useGuardedNavigation } from '@/core/guarded-navigation';
import { sourcesSubNavigation, type NavigationDestination } from '@/core/module-registry';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';

/** True when `pathname` is inside the settings group rooted at `href` (sources also owns /settings/sources/*). */
export const groupActive = (pathname: string, href: string) => pathname === href || pathname.startsWith(`${href}/`);

/** Desktop settings rail (three groups) plus the phone "Settings section" select; the shell shows exactly one by CSS breakpoint. */
export function SettingsNav({ groups, pathname }: { groups: NavigationDestination[]; pathname: string }) {
  const t = useTranslations('shell');
  const { navigate } = useGuardedNavigation();
  const current = groups.find((item) => groupActive(pathname, item.href));
  return <>
    <nav className="settings-nav" aria-label={t('settings')}>
      <h2>{t('settings')}</h2>
      {groups.map((item) => <Link key={item.id} href={item.href} aria-current={current?.id === item.id ? 'page' : undefined}>{t(item.messageKey)}</Link>)}
    </nav>
    <div className="settings-section-select">
      <span className="label" id="settings-section-label">{t('settingsSection')}</span>
      <Select value={current?.href ?? ''} onValueChange={(href) => navigate(href)}>
        <SelectTrigger aria-labelledby="settings-section-label"><SelectValue placeholder={t('settings')} /></SelectTrigger>
        <SelectContent>{groups.map((item) => <SelectItem key={item.id} value={item.href}>{t(item.messageKey)}</SelectItem>)}</SelectContent>
      </Select>
    </div>
  </>;
}

/** Sources | MCP | Personal data permissions tabs, rendered by the shell on every /settings/sources route. */
export function SourcesSubNav({ pathname }: { pathname: string }) {
  const t = useTranslations('shell');
  return <nav className="sub-tabs" aria-label={t('sourcesNavigation')}>
    {sourcesSubNavigation.map((item) => {
      const on = item.exact ? pathname === item.href : groupActive(pathname, item.href);
      return <Link key={item.id} href={item.href} aria-current={on ? 'page' : undefined}>{t(item.messageKey)}</Link>;
    })}
  </nav>;
}
