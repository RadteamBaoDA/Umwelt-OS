import type { Metadata } from 'next';
import { InviteAccept } from './invite-accept';

// The bearer token travels in the URL: never leak it through Referer and keep indexing off.
export const metadata: Metadata = { referrer: 'no-referrer', robots: { index: false, follow: false } };

/** Renders the invitation acceptance page without the authenticated shell. */
export default function InvitePage() { return <InviteAccept />; }
