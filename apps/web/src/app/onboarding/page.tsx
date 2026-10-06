import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { OnboardingPage } from '@/modules/settings/onboarding-page';

/** Renders the authenticated, resumable workspace onboarding surface. */
export default function OnboardingRoute() {
  return <WorkspaceShell><OnboardingPage /></WorkspaceShell>;
}
