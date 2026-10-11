import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { AISettingsWorkspace } from '@/modules/settings/ai-settings';

/** Renders Models (AI provider) settings within the workspace shell. */
export default function AISettingsPage() { return <WorkspaceShell><AISettingsWorkspace /></WorkspaceShell>; }
