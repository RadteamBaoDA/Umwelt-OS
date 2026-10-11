import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { N8nSettingsWorkspace } from '@/modules/settings/n8n-settings';

/** Renders owner n8n connection settings within the workspace shell. */
export default function N8nSettingsPage() { return <WorkspaceShell><N8nSettingsWorkspace /></WorkspaceShell>; }
