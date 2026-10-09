import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { WorkspaceMembers } from '@/modules/settings/workspace-members';

/** Renders workspace member and invitation management within the workspace shell. */
export default function WorkspaceSettingsPage() { return <WorkspaceShell><WorkspaceMembers /></WorkspaceShell>; }
