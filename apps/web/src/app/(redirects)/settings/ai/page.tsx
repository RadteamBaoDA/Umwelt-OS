import { redirect } from 'next/navigation';

/** Redirects the legacy AI settings route to Models settings, keeping old bookmarks working. */
export default function AISettingsPage() { redirect('/settings/models'); }
