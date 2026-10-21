'use client';

import { RouteSpinner } from '@/core/app-shell/route-loader';

/** Root Suspense fallback shown while a route segment streams in: the same blocking spinner as the route-change overlay. */
export default function Loading() {
  return <RouteSpinner />;
}
