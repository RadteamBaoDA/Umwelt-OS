'use client';

import type { AppLocaleId } from '@/core/i18n';
import { genUiCopy, resolveWidgetLocale } from '../copy';
import { GALLERY_DEMOS, STARTER_DEMO_IDS } from './gallery-demos';
import { TemplateChip } from './template-chip';

/**
 * Four starter suggestion chips (plane, road trip, Monty Hall, bill splitter) for an empty
 * conversation; selecting one sends its prompt.
 *
 * @param props - Send callback and optional locale.
 * @returns Labelled chip row.
 */
export function StarterSuggestions({ onSendPrompt, locale }: { onSendPrompt: (prompt: string) => void; locale?: AppLocaleId }) {
  const copy = genUiCopy(resolveWidgetLocale(locale));
  return (
    <div data-testid="starter-suggestions" role="group" aria-label={copy.shellSuggestionsLabel} className="flex flex-wrap justify-center gap-2">
      {STARTER_DEMO_IDS.map((id) => {
        const demo = GALLERY_DEMOS.find((d) => d.id === id)!;
        return <TemplateChip key={id} label={copy[demo.titleKey]} prompt={copy[demo.promptKey]} locale={locale} onSelectPrompt={onSendPrompt} />;
      })}
    </div>
  );
}
