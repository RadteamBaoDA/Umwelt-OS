import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { TranslationSettings } from '@/modules/settings/translation-settings';

/** Renders translation settings (read only for invited members) within the workspace shell. */
export default function TranslationSettingsPage() { return <WorkspaceShell><TranslationSettings /></WorkspaceShell>; }
