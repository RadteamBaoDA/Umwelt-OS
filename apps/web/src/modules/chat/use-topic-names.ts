'use client';

import { useQuery } from '@tanstack/react-query';
import { fetchTopics } from '@/modules/news';

/**
 * Loads up to `limit` active owner topic names for chat suggestion chips, cached by react-query so remounts do not
 * refetch. Any failure (offline, unauthorised, member role) yields an empty list (no retry) and the caller shows
 * its fixed fallback chips.
 * @param limit Maximum names returned.
 */
export function useTopicNames(limit = 3): string[] {
  const { data } = useQuery({
    queryKey: ['chat-topic-names', limit],
    queryFn: ({ signal }) => fetchTopics({ isActive: true, limit, signal }).then((page) => page.items.map((topic) => topic.name)),
    retry: false,
    staleTime: 60_000,
  });
  return data ?? [];
}
