import { useQuery } from '@tanstack/react-query';
import { apiRequest } from '@/core/api';
import { WEB_SEARCH_SEND_ENABLED } from './api';

/** Display names for known web-search providers; anything else uses the neutral `webSearchProviderGeneric` copy. */
export const WEB_SEARCH_PROVIDER_LABELS: Record<string, string> = { brave: 'Brave', tavily: 'Tavily', searxng: 'SearXNG', omniroute: 'OmniRoute' };

/** Subset of the AI settings the chat surface needs to decide web-search availability. */
interface WebSearchSettings {
  web_search_provider: string;
  web_search_credential_configured: boolean;
  privacy: { allow_remote_web_search: boolean };
}

/**
 * Derives whether web search can be offered/sent for the chat surface.
 *
 * Uses the same cache key as AI settings; availability needs a provider, a stored credential (SearXNG needs none) and
 * remote-search consent. Specialist-agent sends (non-assistant profile or a pending profile retry) never carry web_search.
 *
 * @param mode - Settings are fetched only in full mode.
 * @param profileBlocked - True when the selected profile/pending retry cannot carry web_search.
 * @returns Availability, whether it is usable for a send, and the configured provider id.
 */
export function useChatWebSearch(mode: 'drawer' | 'full', profileBlocked: boolean): {
  available: boolean;
  usable: boolean;
  provider: string | undefined;
} {
  const aiSettings = useQuery({
    queryKey: ['ai-settings'],
    queryFn: () => apiRequest<WebSearchSettings>('/api/v1/settings/ai'),
    enabled: mode === 'full',
  });
  const data = aiSettings.data;
  const available = Boolean(
    data && data.web_search_provider !== 'none'
    && (data.web_search_provider === 'searxng' || data.web_search_credential_configured)
    && data.privacy?.allow_remote_web_search,
  );
  return { available, usable: WEB_SEARCH_SEND_ENABLED && available && !profileBlocked, provider: data?.web_search_provider };
}
