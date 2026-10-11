import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { SystemPromptsWorkspace } from '@/modules/settings/system-prompts';

/** Renders owner-editable AI system prompts within the workspace shell. */
export default function SystemPromptsPage() { return <WorkspaceShell><SystemPromptsWorkspace /></WorkspaceShell>; }
