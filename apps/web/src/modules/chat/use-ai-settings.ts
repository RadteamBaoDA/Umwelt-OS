import { useQuery } from '@tanstack/react-query';
import { apiRequest } from '@/core/api';

/**
 * Shared read of `/api/v1/settings/ai` for the chat surface. Every chat consumer (model picker, provider gate,
 * web-search availability) goes through this one key and fetcher, so they share one cached request and a settings save
 * (which invalidates `['ai-settings']`) refreshes all of them.
 *
 * @param options - Per-consumer `enabled`, `staleTime` and `retry` overrides.
 * @typeParam T - The consumer's view of the response (the cache entry stays the full response).
 * @returns The react-query result.
 */
export function useAiSettings<T>(options: { enabled?: boolean; staleTime?: number; retry?: boolean } = {}) {
  return useQuery({
    queryKey: ['ai-settings'],
    queryFn: () => apiRequest<T>('/api/v1/settings/ai'),
    ...options,
  });
}
