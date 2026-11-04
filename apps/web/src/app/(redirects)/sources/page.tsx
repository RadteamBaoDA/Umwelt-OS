import { redirect } from 'next/navigation';

/** Redirects the legacy sources route to its current settings owner. */
export default function SourcesPage() { redirect('/settings/sources'); }
