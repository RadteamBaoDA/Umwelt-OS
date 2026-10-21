import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { AISettingsWorkspace } from '@/modules/settings/ai-settings';
import { VizRouterCard } from '@/modules/settings/viz-router-card';

/** Renders Models (AI provider) settings, including the opt-in visualization router card, within the workspace shell. */
export default function ModelsPage() { return <WorkspaceShell><div className="flex flex-col gap-5"><AISettingsWorkspace /><VizRouterCard /></div></WorkspaceShell>; }
