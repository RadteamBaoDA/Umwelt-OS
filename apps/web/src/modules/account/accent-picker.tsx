'use client';

import { useTranslations } from 'next-intl';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { ACCENTS, previewAccent, type AccentId } from '@/core/accent';

/** Literal catalog key per accent swatch (no runtime key building, so the typed translator checks each one). */
const ACCENT_LABEL_KEYS = {
  ink: 'accent.ink',
  moss: 'accent.moss',
  sea: 'accent.sea',
  earth: 'accent.earth',
  plum: 'accent.plum',
} as const satisfies Record<AccentId, string>;

/**
 * Controlled accent swatch radio group. Choosing a swatch previews it on <html> immediately (not persisted) and reports
 * the value up; the owner of the draft (preferences dialog) persists it with `applyAccent` on Save and reverts the
 * preview on Cancel. Keyboard operation comes from the Radix radio group.
 *
 * @param value Currently selected accent.
 * @param onChange Called with the newly chosen accent after it has been previewed.
 */
export function AccentPicker({ value, onChange }: { value: AccentId; onChange: (id: AccentId) => void }) {
  const t = useTranslations('preferences');
  return (
    <div className="field">
      <Label id="accent-label">{t('accent.label')}</Label>
      <RadioGroup
        aria-labelledby="accent-label"
        value={value}
        onValueChange={(next) => {
          previewAccent(next as AccentId);
          onChange(next as AccentId);
        }}
        className="accent-options"
      >
        {ACCENTS.map((id) => (
          <Label className="theme-option" key={id} htmlFor={`accent-${id}`}>
            <span className={`theme-swatch accent-swatch-${id}`} aria-hidden="true" />
            <span className="inline-flex items-center gap-2">
              <RadioGroupItem id={`accent-${id}`} value={id} />
              <span>{t(ACCENT_LABEL_KEYS[id])}</span>
            </span>
          </Label>
        ))}
      </RadioGroup>
      <p className="text-muted-foreground text-xs">{t('accent.help')}</p>
    </div>
  );
}
