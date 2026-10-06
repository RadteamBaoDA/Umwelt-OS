'use client';

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { MonitorIcon, MoonIcon, SunIcon } from 'lucide-react';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ApiError, apiRequest, csrfHeaders } from '@/core/api';
import { AppLocaleId, ThemePreference } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import type { PreferenceValues } from '@/core/preferences';

export type OwnerPreferences = {
  configuration_revision: number;
  persisted: boolean;
  theme: ThemePreference;
  locale: AppLocaleId;
  timezone: string;
};

type PreferencesDialogProps = {
  open: boolean;
  preferences: OwnerPreferences | null;
  csrfToken: string;
  loading?: boolean;
  loadError?: boolean;
  retrying?: boolean;
  onRetry?: (signal: AbortSignal) => Promise<OwnerPreferences | null>;
  authGeneration?: number;
  onCloseAutoFocus?: (event: Event) => void;
  savingDisabled?: boolean;
  onOpenChange: (open: boolean) => void;
};

/** Checks a timezone identifier with the platform Intl implementation. */
function isValidTimezone(value: string): boolean {
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: value });
    return true;
  } catch {
    return false;
  }
}

/** Edits a draft preference pair, previews changes, and applies save, cancel, reload, and auth-generation behavior. */
export function PreferencesDialog({ open, preferences, csrfToken, loading = false, loadError = false, retrying = false, onRetry, authGeneration = 0, onCloseAutoFocus, savingDisabled = false, onOpenChange }: PreferencesDialogProps) {
  const t = useTranslations('preferences');
  const client = useQueryClient();
  const { locale, timezone, setPreview, isCurrentGeneration } = useDisplayPreferences();
  const [draft, setDraft] = useState<OwnerPreferences | null>(null);
  const [reloadPending, setReloadPending] = useState(false);
  const [reloadError, setReloadError] = useState(false);
  const baseline = useRef<PreferenceValues>({ theme: 'system', locale, timezone });
  const [baselineValues, setBaselineValues] = useState<PreferenceValues>({ theme: 'system', locale, timezone });
  const mounted = useRef(true);
  const openRef = useRef(open);
  const generationRef = useRef(authGeneration);
  useLayoutEffect(() => { openRef.current = open; generationRef.current = authGeneration; });
  const editSessionRef = useRef(0);
  const reloadRequestRef = useRef(0);
  const reloadControllerRef = useRef<AbortController | null>(null);
  const previousOpenRef = useRef(false);
  const previousGenerationRef = useRef(authGeneration);

  const save = useMutation({
    mutationFn: (value: OwnerPreferences) => apiRequest<OwnerPreferences>('/api/v1/settings/preferences', {
      method: 'PUT',
      headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' },
      body: JSON.stringify({
        expected_revision: value.configuration_revision,
        theme: value.theme,
        locale: value.locale,
        timezone: value.timezone,
      }),
    }),
    onSuccess: (value) => {
      if (!mounted.current || generationRef.current !== authGeneration || !isCurrentGeneration(authGeneration)) return;
      client.setQueryData(['owner-preferences'], value);
      setPreview(null, authGeneration);
      setDraft(null);
      onOpenChange(false);
    },
  });

  useLayoutEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      editSessionRef.current += 1;
      reloadRequestRef.current += 1;
      reloadControllerRef.current?.abort();
      reloadControllerRef.current = null;
      setPreview(null, generationRef.current);
    };
  }, [setPreview]);

  useLayoutEffect(() => {
    if (previousOpenRef.current !== open) {
      previousOpenRef.current = open;
      editSessionRef.current += 1;
      reloadRequestRef.current += 1;
      reloadControllerRef.current?.abort();
      reloadControllerRef.current = null;
      setReloadPending(false);
      setReloadError(false);
      if (!open) setPreview(null, authGeneration);
      else {
        // eslint-disable-next-line react-hooks/set-state-in-effect -- draft resets when the dialog or auth generation changes; keeps the existing stale-result fences
        setDraft(null);
        save.reset();
      }
    }
    if (previousGenerationRef.current !== authGeneration) {
      previousGenerationRef.current = authGeneration;
      editSessionRef.current += 1;
      reloadRequestRef.current += 1;
      reloadControllerRef.current?.abort();
      reloadControllerRef.current = null;
      setReloadPending(false);
      setReloadError(false);
      setDraft(null);
    }
  }, [open, authGeneration, save.reset, setPreview]);

  useEffect(() => {
    if (!open) {
      setPreview(null, authGeneration);
      // eslint-disable-next-line react-hooks/set-state-in-effect -- draft resets when the dialog or auth generation changes; keeps the existing stale-result fences
      setDraft(null);
      return;
    }
    if (draft || !preferences) return;
    const values = { theme: preferences.theme || 'system', locale: preferences.persisted ? preferences.locale : locale, timezone: preferences.timezone };
    baseline.current = values;
    setBaselineValues(values);
    save.reset();
    setDraft({ ...preferences, ...values });
    setPreview(values, authGeneration);
  }, [open, preferences, authGeneration, draft, setPreview, save.reset]);

  useEffect(() => {
    if (!open || !draft) return;
    setPreview({ theme: draft.theme, locale: draft.locale, timezone: isValidTimezone(draft.timezone) ? draft.timezone : baseline.current.timezone }, authGeneration);
  }, [open, draft, authGeneration, setPreview]);

  /** Restores committed preferences and clears the temporary preview when the dialog is dismissed. */
  const restore = () => {
    // Closing without Save clears the live theme or locale preview, restoring the committed values.
    setPreview(null, authGeneration);
    setDraft(null);
  };

  /** Invalidates the current reload attempt so a superseded response cannot update the dialog. */
  const invalidateReload = () => {
    editSessionRef.current += 1;
    reloadRequestRef.current += 1;
    reloadControllerRef.current?.abort();
    reloadControllerRef.current = null;
    openRef.current = false;
    setReloadPending(false);
    setReloadError(false);
    save.reset();
  };

  /** Closes the dialog after restoring the committed preference values. */
  const dismiss = () => {
    if (save.isPending) return;
    invalidateReload();
    restore();
    onOpenChange(false);
  };

  const dirty = Boolean(draft && (!draft.persisted ||
    draft.theme !== baselineValues.theme || draft.locale !== baselineValues.locale || draft.timezone !== baselineValues.timezone
  ));
  const hasConflict = save.error instanceof ApiError && save.error.status === 409;
  /** Aborts the previous request and starts a reload attempt bound to this edit session and auth generation. */
  const startReload = () => {
    reloadControllerRef.current?.abort();
    const controller = new AbortController();
    reloadControllerRef.current = controller;
    const request = ++reloadRequestRef.current;
    setReloadPending(true);
    setReloadError(false);
    return { controller, request, session: editSessionRef.current, generation: authGeneration };
  };
  /** Accepts a reload result only while the dialog is mounted and open and its request, edit session, and auth generation remain current. */
  const isReloadCurrent = (attempt: ReturnType<typeof startReload>) => Boolean(
    attempt && mounted.current && openRef.current &&
    attempt.session === editSessionRef.current && attempt.request === reloadRequestRef.current &&
    attempt.generation === generationRef.current && isCurrentGeneration(attempt.generation)
  );
  /** Clears the stored controller when it matches this attempt and resets pending state; callers must check attempt freshness first. */
  const finishReload = (attempt: ReturnType<typeof startReload>) => {
    if (reloadControllerRef.current === attempt.controller) reloadControllerRef.current = null;
    setReloadPending(false);
  };
  /** Reloads through the optional callback while retaining the draft; keeps it on failure or stale results and replaces it only after a non-null result passes the freshness check. */
  const discardAndReload = async () => {
    const attempt = startReload();
    let latest: OwnerPreferences | null = null;
    try {
      latest = await onRetry?.(attempt.controller.signal) ?? null;
    } catch {
      latest = null;
    }
    if (!isReloadCurrent(attempt)) return;
    finishReload(attempt);
    if (!latest) {
      setReloadError(true);
      return;
    }
    const values = { theme: latest.theme, locale: latest.persisted ? latest.locale : locale, timezone: latest.timezone };
    client.setQueryData(['owner-preferences'], latest);
    baseline.current = values;
    setBaselineValues(values);
    save.reset();
    setDraft({ ...latest, ...values });
    setPreview(values, authGeneration);
  };
  /** Retries the optional preference reload and applies its result only while this dialog attempt remains current. */
  const retryLoad = async () => {
    const attempt = startReload();
    let latest: OwnerPreferences | null = null;
    try {
      latest = await onRetry?.(attempt.controller.signal) ?? null;
    } catch {
      latest = null;
    }
    if (!isReloadCurrent(attempt)) return;
    finishReload(attempt);
    if (!latest) {
      setReloadError(true);
      return;
    }
    client.setQueryData(['owner-preferences'], latest);
  };

  return <Dialog open={open} onOpenChange={(nextOpen) => { if (nextOpen) onOpenChange(true); else dismiss(); }}>
    <DialogContent showCloseButton={false} className="preferences-dialog" onCloseAutoFocus={onCloseAutoFocus}>
      <DialogHeader>
        <DialogTitle>{t('title')}</DialogTitle>
        <DialogDescription>{t('description')}</DialogDescription>
      </DialogHeader>
      {loading && <><p role="status" className="muted">{t('loading')}</p><DialogFooter><Button type="button" className="secondary" onClick={dismiss}>{t('cancel')}</Button></DialogFooter></>}
      {loadError && <div className="status-panel" role="alert"><p>{reloadError ? t('reloadFailed') : t('loadFailed')}</p><DialogFooter><Button type="button" className="secondary" onClick={dismiss}>{t('cancel')}</Button><Button type="button" disabled={reloadPending || retrying} onClick={retryLoad}>{reloadPending ? t('loading') : t('retry')}</Button></DialogFooter></div>}
      {hasConflict && <div className="status-panel" role="alert"><p>{t('conflict')}</p>{reloadError && <p>{t('conflictReloadFailed')}</p>}<DialogFooter><Button type="button" className="secondary" disabled={save.isPending} onClick={dismiss}>{t('cancel')}</Button><Button type="button" disabled={reloadPending || retrying} onClick={discardAndReload}>{reloadPending ? t('loading') : reloadError ? t('retry') : t('reloadDiscard')}</Button></DialogFooter></div>}
      {draft && !hasConflict && <form className="preferences-form" onSubmit={(event) => { event.preventDefault(); save.mutate(draft); }}>
        <fieldset disabled={save.isPending || savingDisabled} className="preferences-fields">
          <legend>{t('appearance')}</legend>
          <div className="field">
            <Label>{t('theme')}</Label>
            <RadioGroup aria-label={t('theme')} value={draft.theme} onValueChange={(value) => {
              const next = value as ThemePreference;
              setDraft({ ...draft, theme: next });
            }} className="theme-options">
              {(['light', 'dark', 'system'] as const).map((value) => <Label className="theme-option" key={value} htmlFor={`theme-${value}`}>
                <RadioGroupItem id={`theme-${value}`} value={value} />
                {value === 'light' ? <SunIcon aria-hidden="true" className="size-4" /> : value === 'dark' ? <MoonIcon aria-hidden="true" className="size-4" /> : <MonitorIcon aria-hidden="true" className="size-4" />}<span>{t(value)}</span>
              </Label>)}
            </RadioGroup>
          </div>
          <div className="field">
            <Label htmlFor="preference-locale">{t('language')}</Label>
            <Select value={draft.locale} onValueChange={(value) => {
              const next = value as AppLocaleId;
              setDraft({ ...draft, locale: next });
            }}>
              <SelectTrigger id="preference-locale" aria-describedby="preference-locale-help"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="en-us">{t('englishUs')}</SelectItem>
                <SelectItem value="vi-vi">{t('vietnamese')}</SelectItem>
              </SelectContent>
            </Select>
            <p id="preference-locale-help" className="muted text-xs">{t('languageHelp')}</p>
          </div>
          <div className="field">
            <Label htmlFor="preference-timezone">{t('timezone')}</Label>
            <Input id="preference-timezone" value={draft.timezone} maxLength={100} aria-describedby="preference-timezone-help" onChange={(event) => {
              const next = event.target.value;
              setDraft({ ...draft, timezone: next });
            }} />
            <p id="preference-timezone-help" className="muted text-xs">{t('timezoneHelp')} {t('scheduleTimezoneNote')}</p>
          </div>
        </fieldset>
        <p className="muted" role="status">{save.isPending ? t('saving') : dirty ? t('unsaved') : t('saved')}</p>
        {save.error && <p className="error" role="alert">{save.error instanceof ApiError && save.error.status === 409 ? t('conflict') : t('saveFailed')}</p>}
        <DialogFooter>
          <Button type="button" className="secondary" disabled={save.isPending} onClick={dismiss}>{t('cancel')}</Button>
          <Button type="submit" disabled={savingDisabled || save.isPending || !dirty || !isValidTimezone(draft.timezone)}>{save.isPending ? t('saving') : t('save')}</Button>
        </DialogFooter>
      </form>}
    </DialogContent>
  </Dialog>;
}
