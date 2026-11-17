'use client';

import { useState } from 'react';

/**
 * Decorative item image in a fixed 16:9 box so the layout does not jump while it loads.
 * Renders nothing without a URL or after a load error (no empty placeholder).
 */
export function FeedImage({ src, className = '' }: { src?: string | null; className?: string }) {
  const [failed, setFailed] = useState(false);
  if (!src || failed) return null;
  return (
    <div className={`aspect-video w-full overflow-hidden rounded-md bg-muted ${className}`}>
      {/* eslint-disable-next-line @next/next/no-img-element -- same-origin signed proxy path; next/image optimisation not wanted */}
      <img src={src} alt="" loading="lazy" decoding="async" onError={() => setFailed(true)} className="size-full object-cover" />
    </div>
  );
}
