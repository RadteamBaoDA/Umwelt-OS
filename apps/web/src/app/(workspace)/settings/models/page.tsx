import { AISettingsWorkspace } from '@/modules/settings';
import { VizRouterCard } from '@/modules/settings';

/** Renders Models (AI provider) settings, including the opt-in visualization router card, within the workspace shell. */
export default function ModelsPage() { return <div className="flex flex-col gap-5"><AISettingsWorkspace /><VizRouterCard /></div>; }
