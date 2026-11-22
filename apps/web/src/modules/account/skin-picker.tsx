'use client';

import { useId, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { applySkin, readSkin, type SkinId } from '@/core/skin';
import './skin-picker.css';

/**
 * Per-device skin radio group (Default / Terminal). Unlike the theme and accent fields it is outside the preferences
 * draft: choosing a skin applies and persists it immediately through `applySkin`, and Cancel does not revert it.
 * Each option is a card: radio, title, one-line hint and a decorative (aria-hidden) preview. Keyboard operation
 * comes from the Radix radio group.
 *
 * @returns The skin field markup; no props, no server calls.
 */
export function SkinPicker() {
  const t = useTranslations('preferences');
  const [value, setValue] = useState<SkinId>(readSkin);
  // Per-instance id so the label reference stays unique if the picker is rendered more than once.
  const labelId = useId();
  return (
    <div className="field">
      <Label id={labelId}>{t('skin.label')}</Label>
      <RadioGroup
        aria-labelledby={labelId}
        value={value}
        onValueChange={(next) => {
          const id = next as SkinId;
          applySkin(id);
          setValue(id);
        }}
        className="skin-options"
      >
        {(['default', 'terminal'] as const).map((id) => (
          <Label className="skin-option" key={id} htmlFor={`preference-skin-${id}`}>
            <RadioGroupItem id={`preference-skin-${id}`} value={id} />
            <span className="skin-option-text">
              <span className="text-sm font-medium">{t(id === 'default' ? 'skin.default' : 'skin.terminal')}</span>
              <span className="text-muted-foreground text-xs">{t(id === 'default' ? 'skin.defaultHint' : 'skin.terminalHint')}</span>
            </span>
            {id === 'default' ? (
              <span className="skin-preview skin-preview-default" aria-hidden="true">
                <span className="block">Overview</span>
                <span className="block">Updated 9 minutes ago</span>
              </span>
            ) : (
              <span className="skin-preview skin-preview-terminal" aria-hidden="true">
                <span className="block"># Overview</span>
                <span className="block">
                  13:11:58 INFO feed ok <span className="skin-cursor" />
                </span>
              </span>
            )}
          </Label>
        ))}
      </RadioGroup>
      <p className="text-muted-foreground text-xs">{t('skin.help')}</p>
      {value === 'terminal' && <p className="text-muted-foreground text-xs">{t('skin.terminalNote')}</p>}
    </div>
  );
}
