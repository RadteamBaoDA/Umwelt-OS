import type { AccentId } from '@/core/accent';
import type { AppLocaleId, ThemePreference } from '@/core/i18n';

export type PreferenceValues = {
  theme: ThemePreference;
  locale: AppLocaleId;
  timezone: string;
};

export type OwnerPreferences = PreferenceValues & {
  configuration_revision: number;
  persisted: boolean;
  /** Optional greeting name; null when unset. */
  display_name?: string | null;
  /** Never-empty name: the stored one, else derived from the owner's email (server-resolved). */
  resolved_display_name?: string;
  /** Server-stored accent; null = default ink. */
  accent?: AccentId | null;
};
