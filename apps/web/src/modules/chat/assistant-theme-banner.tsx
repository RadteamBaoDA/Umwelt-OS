'use client';

import * as React from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { apiRequest, csrfHeaders } from '@/core/api';
import type { ThemePreference } from '@/core/i18n';
import type { OwnerPreferences } from '@/core/preferences';
import { useDisplayPreferences } from '@/core/query-provider';

/** Props for {@link AssistantThemeBanner}. */
export interface AssistantThemeBannerProps {
  /** Mode the assistant asked for through the `ui.theme` event. */
  mode: ThemePreference;
  /** Session CSRF token authorizing the Keep (preference save) write. */
  csrfToken: string;
  /** Called after Keep succeeded or Revert ran, so the owner state can drop the request. */
  onResolved: () => void;
}

/**
 * Applies an assistant-requested theme as a transient preview (this browser session only, nothing is written to
 * the account) and offers Keep or Revert. Keep persists through the existing owner preference save; Revert drops
 * the preview. The design system's theme preferences stay the single source of truth.
 *
 * @param props - Requested mode, CSRF token and resolution callback.
 * @returns Status banner with Keep and Revert buttons.
 */
export function AssistantThemeBanner({ mode, csrfToken, onResolved }: AssistantThemeBannerProps) {
  const t = useTranslations('chat');
  const client = useQueryClient();
  const { confirmedPreferences, confirmPreferences, setPreview, authGeneration, locale, timezone } = useDisplayPreferences();
  const [failed, setFailed] = React.useState(false);
  const [saving, setSaving] = React.useState(false);

  // Transient preview: same mechanism the preferences dialog uses before Save.
  React.useEffect(() => {
    setPreview({ theme: mode, locale, timezone }, authGeneration);
    // locale/timezone are intentionally not dependencies: a later locale change must not re-apply the theme.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, authGeneration, setPreview]);

  /** Persists the previewed theme with the owner's other preferences unchanged. */
  const keep = async () => {
    if (!confirmedPreferences) return;
    setSaving(true);
    setFailed(false);
    try {
      const saved = await apiRequest<OwnerPreferences>('/api/v1/settings/preferences', {
        method: 'PUT',
        headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expected_revision: confirmedPreferences.configuration_revision,
          theme: mode,
          locale: confirmedPreferences.locale,
          timezone: confirmedPreferences.timezone,
        }),
      });
      client.setQueryData(['owner-preferences'], saved);
      confirmPreferences(saved, authGeneration);
      setPreview(null, authGeneration);
      onResolved();
    } catch {
      setFailed(true); // revision conflict or network: the preview stays so the owner can retry or revert
    } finally {
      setSaving(false);
    }
  };

  /** Drops the preview, restoring the confirmed preference. */
  const revert = () => {
    setPreview(null, authGeneration);
    onResolved();
  };

  return (
    <div role="status" data-testid="assistant-theme-banner" className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-border bg-secondary px-4 py-2 text-xs">
      <span className="text-muted-foreground">
        {t('assistantThemeChanged')}{failed ? ` ${t('assistantThemeKeepFailed')}` : ''}
      </span>
      <span className="flex gap-2">
        <Button type="button" size="sm" disabled={saving || !confirmedPreferences} onClick={() => void keep()}>
          {t('assistantThemeKeep')}
        </Button>
        <Button type="button" size="sm" variant="outline" disabled={saving} onClick={revert}>
          {t('assistantThemeRevert')}
        </Button>
      </span>
    </div>
  );
}
