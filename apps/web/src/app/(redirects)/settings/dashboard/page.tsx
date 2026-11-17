import { redirect } from 'next/navigation';

/** Sends old Dashboard & Gadget settings links to the new Dashboard owner. */
export default function DashboardSettingsPage() {
  redirect('/dashboard');
}
