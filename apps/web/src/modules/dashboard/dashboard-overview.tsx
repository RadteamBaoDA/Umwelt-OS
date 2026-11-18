'use client';

import { useState, type ReactNode } from 'react';
import { PanelLeftClose, PanelLeftOpen } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useLocalPref } from '@/core/use-local-pref';
import { useSkin } from '@/core/use-skin';
import { useTranslations } from 'next-intl';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { LiveMapOverview } from './gadgets/live-map-gadget';
import { DashboardPanel } from './dashboard-panel';

const MAP_COLLAPSED_KEY = 'umwelt.dashboard.mapCollapsed';
const MAP_PANEL_ID = 'dashboard-overview-map';
const COLLAPSE_VALUES = ['0', '1'] as const;

/**
 * Collapse/expand control of the Overview map; disclosure semantics (aria-expanded) with a stable label per state.
 * `iconOnly` (terminal skin, expanded) shows just the icon inside the map title bar; the label stays as the accessible name.
 */
function MapToggle({ collapsed, iconOnly, onToggle }: { collapsed: boolean; iconOnly: boolean; onToggle: () => void }) {
  const t = useTranslations('dashboard');
  return (
    <Button
      type="button"
      variant={iconOnly ? 'ghost' : 'outline'}
      size={iconOnly ? 'icon' : 'sm'}
      className={iconOnly ? undefined : 'h-8 gap-1 bg-card px-2 text-xs'}
      aria-label={iconOnly ? t('collapseMap') : undefined}
      aria-expanded={!collapsed}
      aria-controls={collapsed ? undefined : MAP_PANEL_ID}
      onClick={onToggle}
    >
      {collapsed ? (
        <PanelLeftOpen className="h-3.5 w-3.5" aria-hidden="true" />
      ) : (
        <PanelLeftClose className="h-3.5 w-3.5" aria-hidden="true" />
      )}
      {!iconOnly && (collapsed ? t('expandMap') : t('collapseMap'))}
    </Button>
  );
}

/** Fixed desktop map beside an independently scrolling Overview gadget panel; compact screens switch views. */
export function DashboardOverview({
  panelId,
  children,
  editMode,
  compact,
  empty,
  canAdd,
  onAdd,
  scrollPositions,
}: {
  panelId: string;
  children: ReactNode;
  editMode: boolean;
  compact: boolean;
  empty: boolean;
  canAdd: boolean;
  onAdd: () => void;
  scrollPositions: Map<string, number>;
}) {
  const t = useTranslations('dashboard');
  // Desktop-only: the compact Map | Gadgets tab switch already lets small screens hide the map.
  const [collapsedFlag, setCollapsedFlag] = useLocalPref<'0' | '1'>(MAP_COLLAPSED_KEY, '0', COLLAPSE_VALUES);
  const collapsed = collapsedFlag === '1';
  const terminal = useSkin() === 'terminal';
  const [compactView, setCompactView] = useState<'map' | 'gadgets'>('map');
  const toggle = (
    <MapToggle collapsed={collapsed} iconOnly={terminal && !collapsed} onToggle={() => setCollapsedFlag(collapsed ? '0' : '1')} />
  );
  const map = (
    <div id={MAP_PANEL_ID} className="relative flex min-h-0 min-w-0 flex-1 overflow-hidden rounded-lg border border-border bg-card">
      {/* Toggle sits in the map toolbar row (not absolutely positioned) so it cannot cover the filters. */}
      <LiveMapOverview toolbarStart={!compact && !editMode ? toggle : undefined} />
    </div>
  );
  const gadgets = (
    <div className={`flex min-h-0 min-w-0 flex-1 flex-col ${editMode ? 'min-[1100px]:col-span-2' : ''}`}>
      <DashboardPanel panelId={panelId} empty={empty} canAdd={canAdd} onAdd={onAdd} scrollPositions={scrollPositions}>
        {children}
      </DashboardPanel>
    </div>
  );

  if (compact && !editMode) {
    return (
      <Tabs
        value={compactView}
        onValueChange={(value) => setCompactView(value as 'map' | 'gadgets')}
        className="flex min-h-0 min-w-0 flex-1 flex-col gap-3"
      >
        <TabsList className="w-full">
          <TabsTrigger value="map" className="flex-1">
            {t('overview.map')}
          </TabsTrigger>
          <TabsTrigger value="gadgets" className="flex-1">
            {t('overview.gadgets')}
          </TabsTrigger>
        </TabsList>
        <TabsContent value="map" className="mt-0 flex min-h-0 min-w-0 flex-1">
          {map}
        </TabsContent>
        <TabsContent value="gadgets" className="mt-0 flex min-h-0 min-w-0 flex-1">
          {gadgets}
        </TabsContent>
      </Tabs>
    );
  }
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      {collapsed && !editMode && <div className="mb-2 flex justify-end">{toggle}</div>}
      <div
        className={`flex min-h-0 min-w-0 flex-1 gap-4 ${collapsed ? '' : 'min-[1100px]:grid min-[1100px]:grid-cols-[minmax(0,45fr)_minmax(0,55fr)]'}`}
      >
        {!editMode && !collapsed && map}
        {gadgets}
      </div>
    </div>
  );
}
