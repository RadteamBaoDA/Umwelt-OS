'use client';

import { useEffect, useState } from 'react';
import { fetchTopics } from '@/modules/news';

/**
 * Loads up to `limit` active owner topic names for chat suggestion chips.
 * Plain effect instead of react-query so the transcript still renders outside a QueryClientProvider; any failure
 * (offline, unauthorised, member role) yields an empty list and the caller shows its fixed fallback chips.
 * @param limit Maximum names returned.
 */
export function useTopicNames(limit = 3): string[] {
  const [names, setNames] = useState<string[]>([]);
  useEffect(() => {
    const controller = new AbortController();
    fetchTopics({ isActive: true, limit, signal: controller.signal })
      .then((page) => setNames(page.items.map((topic) => topic.name)))
      .catch(() => setNames([]));
    return () => controller.abort();
  }, [limit]);
  return names;
}
