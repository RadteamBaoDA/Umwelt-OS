'use client';

import { useState, type ReactNode } from 'react';
import { useTranslations } from 'next-intl';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { LiveMapOverview } from './gadgets/live-map-gadget';
import { DashboardPanel } from './dashboard-panel';

/** Fixed desktop map beside an independently scrolling Overview gadget panel; compact screens switch views. */
export function DashboardOverview({ panelId, children, editMode, compact, empty, canAdd, onAdd, scrollPositions }: {
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
  const [compactView, setCompactView] = useState<'map' | 'gadgets'>('map');
  const map = <div className="flex min-h-0 min-w-0 flex-1 overflow-hidden rounded-lg border border-border bg-card">
    <LiveMapOverview />
  </div>;
  const gadgets = <div className={`flex min-h-0 min-w-0 flex-1 flex-col ${editMode ? 'min-[1100px]:col-span-2' : ''}`}>
    <DashboardPanel panelId={panelId} empty={empty} canAdd={canAdd} onAdd={onAdd} scrollPositions={scrollPositions}>
      {children}
    </DashboardPanel>
  </div>;

  if (compact && !editMode) {
    return (
      <Tabs value={compactView} onValueChange={(value) => setCompactView(value as 'map' | 'gadgets')}
        className="flex min-h-0 min-w-0 flex-1 flex-col gap-3">
        <TabsList className="w-full">
          <TabsTrigger value="map" className="flex-1">{t('overviewMap')}</TabsTrigger>
          <TabsTrigger value="gadgets" className="flex-1">{t('overviewGadgets')}</TabsTrigger>
        </TabsList>
        <TabsContent value="map" className="mt-0 flex min-h-0 min-w-0 flex-1">{map}</TabsContent>
        <TabsContent value="gadgets" className="mt-0 flex min-h-0 min-w-0 flex-1">{gadgets}</TabsContent>
      </Tabs>
    );
  }
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <div className="flex min-h-0 min-w-0 flex-1 gap-4 min-[1100px]:grid min-[1100px]:grid-cols-[minmax(0,45fr)_minmax(0,55fr)]">
        {!editMode && map}
        {gadgets}
      </div>
    </div>
  );
}
