import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { AppHome } from '@/modules/dashboard/app-home';
import { SelectedDayProvider } from '@/modules/dashboard/selected-day';

/** Resolves the default dashboard at `/dashboard` before navigating to its ID route. */
export default function DashboardHomePage() {
  return (
    <WorkspaceShell>
      <SelectedDayProvider>
        <AppHome />
      </SelectedDayProvider>
    </WorkspaceShell>
  );
}
