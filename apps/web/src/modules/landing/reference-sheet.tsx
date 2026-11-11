'use client';

import { useTranslations } from 'next-intl';
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import type { LandingSourceId } from './source-canvas';

/** Side sheet showing the sample reference text for the opened citation; closed when `reference` is null. */
export function DemoReferenceSheet({ reference, onClose }: { reference: LandingSourceId | null; onClose: () => void }) {
  const t = useTranslations('landing');
  const activeReference = reference ? t(`sourceNames.${reference}`) : '';
  return (
    <Sheet
      open={reference !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <SheetContent side="right" className="max-w-xl" closeLabel={t('close')}>
        <SheetHeader>
          <SheetTitle>{reference ? t('referenceTitle', { source: activeReference }) : t('citationsTitle')}</SheetTitle>
          <SheetDescription>{t('citationsTitle')}</SheetDescription>
        </SheetHeader>
        {reference && <div className="p-5 text-sm leading-7 text-foreground">{t(`referenceText.${reference}`)}</div>}
      </SheetContent>
    </Sheet>
  );
}
