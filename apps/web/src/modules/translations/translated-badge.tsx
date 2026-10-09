'use client';

import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';

/** "Bản dịch tự động" label with a toggle back to the original; citations stay outside translated text. */
export function TranslatedBadge({ showOriginal, onToggle }: { showOriginal: boolean; onToggle: () => void }) {
  const t = useTranslations('translations');
  return (
    <span className="inline-flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      <span className="inline-flex h-6 items-center rounded-full border border-border bg-background px-2 font-semibold">{t('autoTranslated')}</span>
      <Button type="button" variant="link" size="sm" className="h-auto p-0 text-xs" onClick={onToggle}>
        {showOriginal ? t('viewTranslation') : t('viewOriginal')}
      </Button>
    </span>
  );
}
