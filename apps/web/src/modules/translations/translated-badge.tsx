'use client';

import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import type { TranslationLanguage } from './types';

/**
 * Compact translation indicator with a one-click toggle between the translation and original text.
 * Citations and publisher attributions stay outside translated text.
 *
 * @param props.showOriginal True when the original text is currently displayed.
 * @param props.onToggle Toggles between original and translated content.
 * @param props.targetLanguage Optional target language code shown in the pill (for example `VI` or `EN`).
 */
export function TranslatedBadge({
  showOriginal,
  onToggle,
  targetLanguage,
}: {
  showOriginal: boolean;
  onToggle: () => void;
  targetLanguage?: TranslationLanguage;
}) {
  const t = useTranslations('translations');
  const label = targetLanguage ? `${t('translated')} (${targetLanguage.toUpperCase()})` : t('translated');
  return (
    <span className="inline-flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      <span className="inline-flex h-6 items-center rounded-full border border-border bg-background px-2 font-semibold">{label}</span>
      <Button type="button" variant="link" size="sm" className="h-auto p-0 text-xs" onClick={onToggle}>
        {showOriginal ? t('viewTranslation') : t('viewOriginal')}
      </Button>
    </span>
  );
}
