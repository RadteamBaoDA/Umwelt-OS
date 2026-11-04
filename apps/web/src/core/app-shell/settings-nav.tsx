'use client';

import Link from 'next/link';
import type { ReactNode } from 'react';
import { useTranslations } from 'next-intl';
import { useGuardedNavigation } from '@/core/guarded-navigation';
import { sourcesSubNavigation, type NavigationDestination } from '@/core/module-registry';
import './settings-gaps.css';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';

/** True when `pathname` is inside the settings group rooted at `href` (sources also owns /settings/sources/*). */
export const groupActive = (pathname: string, href: string) => pathname === href || pathname.startsWith(`${href}/`);

/** Ids (in display order) of the "Data & AI" settings group; every other destination falls under "Workspace". */
const dataAiIds = ['data-sources', 'ai-router', 'system-prompts', 'memory-settings', 'web-search-settings', 'translation-settings'];

/**
 * Settings panel: a surface card (18px radius, line border) with a 230px inner nav column and the page content on the right.
 * The nav column is split into labelled "Data & AI" and "Workspace" groups (empty groups are omitted). Under 1024px the nav column is replaced by the "Settings section" Select; `links` must already be filtered by role and module availability.
 *
 * @param links Visible settings destinations in display order (grouped by id; order within "Data & AI" follows `dataAiIds`).
 * @param pathname Current route, used to mark the active entry.
 * @param children Page content (including the Data sources sub-tabs), rendered with 32px padding and 20px gap.
 */
export function SettingsPanel({ links, pathname, children }: { links: NavigationDestination[]; pathname: string; children: ReactNode }) {
  const t = useTranslations('shell');
  const { navigate } = useGuardedNavigation();
  const current = links.find((item) => groupActive(pathname, item.href));
  const dataAi = dataAiIds.flatMap((id) => links.filter((item) => item.id === id));
  const workspace = links.filter((item) => !dataAiIds.includes(item.id));
  /** Nav link for one destination; marks it `aria-current="page"` when it is the active route. */
  const renderLink = (item: NavigationDestination) => <Link key={item.id} href={item.href} aria-current={current?.id === item.id ? 'page' : undefined}>{t(item.messageKey)}</Link>;
  return <div className="settings-panel">
    <nav className="settings-nav" aria-label={t('settings')}>
      {[{ id: 'data-ai', label: t('settingsGroupDataAi'), items: dataAi }, { id: 'workspace', label: t('settingsGroupWorkspace'), items: workspace }]
        .filter((group) => group.items.length > 0)
        .map((group, gi) => <div key={group.id} role="group" aria-labelledby={`settings-group-${group.id}`} className="contents">
          <span id={`settings-group-${group.id}`} className={`px-2.5 pb-1 text-xs font-medium text-muted-foreground ${gi === 0 ? 'pt-0' : 'pt-3'}`}>{group.label}</span>
          {group.items.map(renderLink)}
        </div>)}
    </nav>
    <div className="settings-section-select">
      <span className="label" id="settings-section-label">{t('settingsSection')}</span>
      <Select value={current?.href ?? ''} onValueChange={(href) => navigate(href)}>
        <SelectTrigger aria-labelledby="settings-section-label"><SelectValue placeholder={t('settings')} /></SelectTrigger>
        <SelectContent>{[...dataAi, ...workspace].map((item) => <SelectItem key={item.id} value={item.href}>{t(item.messageKey)}</SelectItem>)}</SelectContent>
      </Select>
    </div>
    <div className="settings-body">{children}</div>
  </div>;
}

/**
 * Sources | MCP | Personal data permissions tabs, rendered by the shell on every /settings/sources route.
 * The long permissions label is swapped for a short visible one below 480px (CSS) so the tabs stay on one row;
 * the link keeps the full text as its accessible name.
 */
export function SourcesSubNav({ pathname }: { pathname: string }) {
  const t = useTranslations('shell');
  return <nav className="sub-tabs" aria-label={t('sourcesNavigation')}>
    {sourcesSubNavigation.map((item) => {
      const on = item.exact ? pathname === item.href : groupActive(pathname, item.href);
      if (item.messageKey === 'permissionsTab') {
        return <Link key={item.id} href={item.href} aria-current={on ? 'page' : undefined} aria-label={t('permissionsTab')}><span className="tab-full">{t('permissionsTab')}</span><span className="tab-short" aria-hidden="true">{t('permissionsTabShort')}</span></Link>;
      }
      return <Link key={item.id} href={item.href} aria-current={on ? 'page' : undefined}>{t(item.messageKey)}</Link>;
    })}
  </nav>;
}
