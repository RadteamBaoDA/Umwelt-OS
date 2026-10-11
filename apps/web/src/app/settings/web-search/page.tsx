import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { WebSearchSettingsWorkspace } from '@/modules/settings/web-search-settings';

/** Renders owner web search provider settings within the workspace shell. */
export default function WebSearchSettingsPage() { return <WorkspaceShell><WebSearchSettingsWorkspace /></WorkspaceShell>; }
