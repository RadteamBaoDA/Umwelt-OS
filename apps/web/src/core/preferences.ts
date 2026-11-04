import type { AccentId } from '@/core/accent';
import { apiRequest } from '@/core/api';
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

/** Shared TanStack Query cache key for the owner's preferences. */
export const OWNER_PREFERENCES_KEY = ['owner-preferences'] as const;

/**
 * Loads the owner's stored preferences. Every `useQuery` on {@link OWNER_PREFERENCES_KEY} passes this as `queryFn`,
 * so a refetch never lands on a subscriber without one (TanStack may adopt any observer's options for the query).
 * @param signal Abort signal forwarded from TanStack Query.
 * @throws ApiError when the request fails.
 */
export function fetchOwnerPreferences(signal?: AbortSignal): Promise<OwnerPreferences> {
  return apiRequest<OwnerPreferences>('/api/v1/settings/preferences', { signal });
}
