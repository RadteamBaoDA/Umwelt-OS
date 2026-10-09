import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { AppHome } from '@/modules/dashboard/app-home';
import { SelectedDayProvider } from '@/modules/dashboard/selected-day';

/** Renders the authenticated application dashboard route. */
export default function AppPage() {
  return (
    <WorkspaceShell>
      <SelectedDayProvider>
        <AppHome />
      </SelectedDayProvider>
    </WorkspaceShell>
  );
}
