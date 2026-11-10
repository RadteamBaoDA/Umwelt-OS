'use client';

import { useTranslations } from 'next-intl';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';

/**
 * One tab per bound source (plus All) driving the existing source filter. Shown only for 2-6
 * sources (it renders nothing otherwise); Radix Tabs supplies arrow-key navigation. Selecting a tab filters the loaded items client-side.
 *
 * @param props Bound source ids, resolved names, current filter value and change handler.
 */
export function FeedSourceTabs({ sourceIds, sourceNames, value, onChange }: {
  sourceIds: string[];
  sourceNames: { id: string; name: string }[] | undefined;
  value: string;
  onChange: (value: string) => void;
}) {
  const t = useTranslations('dashboard');
  // More than 6 sources keep the filter dropdown only; a single source needs no tabs.
  if (sourceIds.length < 2 || sourceIds.length > 6) return null;
  const trigger = 'min-h-8 pointer-coarse:min-h-11 max-w-40 rounded-none border-b-2 border-transparent text-xs text-muted-foreground data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:text-foreground data-[state=active]:shadow-none';
  return (
    <Tabs value={value} onValueChange={onChange}>
      <TabsList aria-label={t('feedSourceTabs')} className="h-auto max-w-full flex-wrap gap-1 p-0">
        <TabsTrigger value="all" className={trigger}>{t('feedSourceAll')}</TabsTrigger>
        {sourceIds.map((id) => {
          const name = sourceNames?.find((source) => source.id === id)?.name ?? t('feedSourceUnknown');
          return <TabsTrigger key={id} value={id} title={name} className={trigger}><span className="truncate">{name}</span></TabsTrigger>;
        })}
      </TabsList>
    </Tabs>
  );
}
