import { AppHome } from '@/modules/dashboard';
import { SelectedDayProvider } from '@/modules/dashboard';

/** Resolves the default dashboard at `/dashboard` before navigating to its ID route. */
export default function DashboardHomePage() {
  return (
    <SelectedDayProvider>
      <AppHome />
    </SelectedDayProvider>
  );
}
