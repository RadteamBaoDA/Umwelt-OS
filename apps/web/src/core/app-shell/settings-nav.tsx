'use client';

import Link from 'next/link';
import type { ReactNode } from 'react';
import { useTranslations } from 'next-intl';
import { useGuardedNavigation } from '@/core/guarded-navigation';
import { sourcesSubNavigation, type NavigationDestination } from '@/core/module-registry';
import '@/modules/settings/settings-gaps.css';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';

/** True when `pathname` is inside the settings group rooted at `href` (sources also owns /settings/sources/*). */
export const groupActive = (pathname: string, href: string) => pathname === href || pathname.startsWith(`${href}/`);

/** Ids of the three design groups rendered above the separator in the settings panel nav. */
const primaryIds = new Set(['data-sources', 'ai-router', 'system-prompts', 'dashboard-gadgets']);

/**
 * Settings panel: a surface card (18px radius, line border) with a 230px inner nav column and the page content on the right.
 * Under 720px the nav column is replaced by the "Settings section" Select; `links` must already be filtered by role and module availability.
 *
 * @param links Visible settings destinations in display order (design groups first, then Members, Translation, Memory, System).
 * @param pathname Current route, used to mark the active entry.
 * @param children Page content (including the Data sources sub-tabs), rendered with 32px padding and 20px gap.
 */
export function SettingsPanel({ links, pathname, children }: { links: NavigationDestination[]; pathname: string; children: ReactNode }) {
  const t = useTranslations('shell');
  const { navigate } = useGuardedNavigation();
  const current = links.find((item) => groupActive(pathname, item.href));
  const primary = links.filter((item) => primaryIds.has(item.id));
  const rest = links.filter((item) => !primaryIds.has(item.id));
  const renderLink = (item: NavigationDestination) => <Link key={item.id} href={item.href} aria-current={current?.id === item.id ? 'page' : undefined}>{t(item.messageKey)}</Link>;
  return <div className="settings-panel">
    <nav className="settings-nav" aria-label={t('settings')}>
      {primary.map(renderLink)}
      {primary.length > 0 && rest.length > 0 && <hr aria-hidden="true" />}
      {rest.map(renderLink)}
    </nav>
    <div className="settings-section-select">
      <span className="label" id="settings-section-label">{t('settingsSection')}</span>
      <Select value={current?.href ?? ''} onValueChange={(href) => navigate(href)}>
        <SelectTrigger aria-labelledby="settings-section-label"><SelectValue placeholder={t('settings')} /></SelectTrigger>
        <SelectContent>{links.map((item) => <SelectItem key={item.id} value={item.href}>{t(item.messageKey)}</SelectItem>)}</SelectContent>
      </Select>
    </div>
    <div className="settings-body">{children}</div>
  </div>;
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
