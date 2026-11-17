import { AppHome } from '@/modules/dashboard';
import { SelectedDayProvider } from '@/modules/dashboard';

/** Renders one dashboard at its stable, addressable route.
 * @param params Route parameters containing the dashboard identity.
 */
export default async function DashboardRoute({ params }: { params: Promise<{ dashboardId: string }> }) {
  const { dashboardId } = await params;
  return (
    <SelectedDayProvider>
      <AppHome dashboardId={dashboardId} />
    </SelectedDayProvider>
  );
}
