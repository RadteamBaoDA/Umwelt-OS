import { redirect } from 'next/navigation';

/** Redirects the legacy privacy route to the consolidated Models settings page. */
export default function PrivacyPage() { redirect('/settings/models'); }
