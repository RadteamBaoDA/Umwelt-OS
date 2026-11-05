'use client';

import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { LayoutDashboard, MessageSquare, PanelLeftClose, PanelLeftOpen, Settings, type LucideIcon } from 'lucide-react';
import { UmweltLogo, UmweltMark } from '@/components/brand/umwelt-mark';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import type { NavigationDestination, destinationForRole } from '@/core/module-registry';

const navigationIcons: Record<string, LucideIcon> = {
  dashboard: LayoutDashboard,
  chat: MessageSquare,
  settings: Settings,
};

type NavItem = NavigationDestination;
type Role = Parameters<typeof destinationForRole>[1];

/**
 * Resolves the link target of a main-navigation item: the dashboard keeps the open dashboard path, and Settings
 * opens the first group the role may use (global for admin viewers, first visible group for members).
 */
export function navHref(item: NavItem, pathname: string, role: Role, firstSettingsHref: string | undefined): string {
  if (item.id === 'dashboard' && pathname.startsWith('/dashboard/')) return pathname;
  if (item.id === 'settings' && role === 'admin_viewer') return '/settings/global';
  // A member who is also an instance admin can see Settings with zero visible groups; fall back to the item's own
  // href instead of passing an undefined target to Link.
  if (item.id === 'settings' && role === 'member') return firstSettingsHref ?? item.href;
  return item.href;
}

/** One destination link; the collapsed rail wraps it in a tooltip because only the icon is visible. */
function NavLink({ item, href, active, expanded }: { item: NavItem; href: string; active: boolean; expanded: boolean }) {
  const t = useTranslations('shell');
  const Icon = navigationIcons[item.id];
  const link = <Link
    key={item.id}
    href={href}
    aria-current={active ? 'page' : undefined}
    className="workspace-nav-link"
  >
    <Icon className="size-[18px] shrink-0" aria-hidden="true" />
    <span className="workspace-nav-label">{t(item.messageKey)}</span>
  </Link>;
  // Expanded rail shows the label itself, so the tooltip would only repeat it.
  return expanded ? link : <Tooltip>
    <TooltipTrigger asChild>{link}</TooltipTrigger>
    <TooltipContent className="workspace-nav-tooltip" side="right">{t(item.messageKey)}</TooltipContent>
  </Tooltip>;
}

/**
 * Desktop navigation rail (bottom bar on phones): logo, one link per visible destination and the expand/collapse toggle.
 * @param items Destinations already filtered by module availability and role.
 * @param activeId Id of the current destination, exposed as aria-current.
 * @param firstSettingsHref Target of the Settings entry for members.
 * @param onToggle Flips the persisted expanded state.
 */
export function ShellNav({ items, activeId, pathname, role, firstSettingsHref, expanded, onToggle }: {
  items: ReadonlyArray<NavItem>;
  activeId: string;
  pathname: string;
  role: Role;
  firstSettingsHref: string | undefined;
  expanded: boolean;
  onToggle: () => void;
}) {
  const t = useTranslations('shell');
  return <TooltipProvider delayDuration={300}>
    <nav id="workspace-nav" className="workspace-nav" data-expanded={expanded || undefined} aria-label={t('mainNavigation')}>
      <div className="workspace-nav-header">
        <Link href="/dashboard" className="brand-name workspace-brand" aria-label="Umwelt-OS">{expanded ? <UmweltLogo /> : <UmweltMark />}</Link>
      </div>
      <div className="workspace-nav-links">
        {items.map((item) => (
          <NavLink
            key={item.id}
            item={item}
            href={navHref(item, pathname, role, firstSettingsHref)}
            active={activeId === item.id}
            expanded={expanded}
          />
        ))}
      </div>
      <Tooltip>
        <TooltipTrigger asChild>
          <button type="button" className="workspace-nav-link workspace-nav-toggle" onClick={onToggle} aria-expanded={expanded} aria-controls="workspace-nav">
            {expanded ? <PanelLeftClose className="size-[18px] shrink-0" aria-hidden="true" /> : <PanelLeftOpen className="size-[18px] shrink-0" aria-hidden="true" />}
            <span className="workspace-nav-label">{t(expanded ? 'collapseSidebar' : 'expandSidebar')}</span>
          </button>
        </TooltipTrigger>
        {!expanded && <TooltipContent className="workspace-nav-tooltip" side="right">{t('expandSidebar')}</TooltipContent>}
      </Tooltip>
    </nav>
  </TooltipProvider>;
}
