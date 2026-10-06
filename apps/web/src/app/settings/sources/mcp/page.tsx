import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { McpSettingsPage } from '@/modules/sources/mcp-settings-page';

/** Renders MCP connection and grant management as the Data sources MCP tab. */
export default function SettingsMcpPage() { return <WorkspaceShell><McpSettingsPage /></WorkspaceShell>; }
