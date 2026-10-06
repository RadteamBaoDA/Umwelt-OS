'use client';

import { GlobeIcon } from 'lucide-react';
import { useTheme } from 'next-themes';
import { useTranslations } from 'next-intl';
import { useEffect, useRef } from 'react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import type { AppLocaleId, ThemePreference } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';

/**
 * Pre-sign-in theme and language control. Applies to this browser session only; signed-in owner
 * preferences (User settings) replace it after login. The locale preview is cleared on unmount.
 */
export function AppearanceControl() {
  const t = useTranslations('preferences');
  const login = useTranslations('login');
  const display = useDisplayPreferences();
  const { theme, setTheme } = useTheme();
  const displayRef = useRef(display);
  useEffect(() => { displayRef.current = display; });
  useEffect(() => () => displayRef.current.setPreview(null, displayRef.current.authGeneration), []);
  return <Dialog>
    <DialogTrigger asChild>
      <Button type="button" variant="ghost"><GlobeIcon aria-hidden="true" className="size-4" />{login('appearance')}</Button>
    </DialogTrigger>
    <DialogContent className="max-w-sm">
      <DialogHeader>
        <DialogTitle>{login('appearance')}</DialogTitle>
        <DialogDescription>{login('appearanceHelp')}</DialogDescription>
      </DialogHeader>
      <div className="field">
        <Label id="login-theme-label">{t('theme')}</Label>
        <RadioGroup aria-labelledby="login-theme-label" value={theme ?? 'system'} onValueChange={(value) => setTheme(value as ThemePreference)} className="theme-options">
          {(['light', 'dark', 'system'] as const).map((value) => <Label className="theme-option" key={value} htmlFor={`login-theme-${value}`}>
            <RadioGroupItem id={`login-theme-${value}`} value={value} />
            <span>{t(value)}</span>
          </Label>)}
        </RadioGroup>
      </div>
      <div className="field">
        <Label htmlFor="login-locale">{t('language')}</Label>
        <Select value={display.locale} onValueChange={(value) => display.setPreview({ theme: 'system', locale: value as AppLocaleId, timezone: display.timezone }, display.authGeneration)}>
          <SelectTrigger id="login-locale"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="en-us">{t('englishUs')}</SelectItem>
            <SelectItem value="vi-vi">{t('vietnamese')}</SelectItem>
          </SelectContent>
        </Select>
        <p className="muted text-xs">{t('languageHelp')}</p>
      </div>
    </DialogContent>
  </Dialog>;
}
