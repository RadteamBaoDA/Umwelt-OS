import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { MemoryPrivacySettings } from '@/modules/settings/memory-privacy';

/** Renders memory and conversation privacy controls within the workspace shell. */
export default function MemoryPrivacyPage() {
  return (
    <WorkspaceShell>
      <MemoryPrivacySettings />
    </WorkspaceShell>
  );
}
