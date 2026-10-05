import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { DashboardPage } from '@/modules/dashboard/dashboard-page';
import { SelectedDayProvider } from '@/modules/dashboard/selected-day';

/** Renders the authenticated application dashboard route. */
export default function AppPage() {
  return (
    <WorkspaceShell>
      <SelectedDayProvider>
        <DashboardPage />
      </SelectedDayProvider>
    </WorkspaceShell>
  );
}
