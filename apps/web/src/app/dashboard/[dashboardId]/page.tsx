import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { AppHome } from '@/modules/dashboard/app-home';
import { SelectedDayProvider } from '@/modules/dashboard/selected-day';

/** Renders one dashboard at its stable, addressable route.
 * @param params Route parameters containing the dashboard identity.
 */
export default async function DashboardRoute({ params }: { params: Promise<{ dashboardId: string }> }) {
  const { dashboardId } = await params;
  return (
    <WorkspaceShell>
      <SelectedDayProvider>
        <AppHome dashboardId={dashboardId} />
      </SelectedDayProvider>
    </WorkspaceShell>
  );
}
