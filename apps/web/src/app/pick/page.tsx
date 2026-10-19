import type { Metadata } from 'next';
import { PickPhone } from '@/modules/pick/pick-phone';

// The session id travels in the URL: never leak it through Referer and keep indexing off.
export const metadata: Metadata = { referrer: 'no-referrer', robots: { index: false, follow: false } };

/** Renders the anonymous phone prompt picker outside the authenticated shell. */
export default function PickPage() { return <PickPhone />; }
