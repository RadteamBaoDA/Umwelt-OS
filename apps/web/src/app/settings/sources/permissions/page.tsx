import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { PersonalDataPermissionsPage } from '@/modules/settings/personal-data-permissions-page';

/** Renders the Data sources "Personal data permissions" tab (truthful unavailable state until a dedicated backend page exists). */
export default function SettingsPermissionsPage() { return <WorkspaceShell><PersonalDataPermissionsPage /></WorkspaceShell>; }
