import { ImageResponse } from 'next/og';
import { SHARE_ALT, SHARE_SIZE, ShareCard } from '@/core/share-card';

export const size = SHARE_SIZE;
export const contentType = 'image/png';
export const alt = SHARE_ALT;

/** Renders the static share card; public by design (see session-gate) and free of user data. */
export default function Image() {
  return new ImageResponse(<ShareCard />, { ...size });
}
