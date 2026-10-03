import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { MemoryList } from '@/modules/knowledge/memory-list';

/** Renders the selective memory management route inside the workspace shell. */
export default function MemoryPage() {
  return (
    <WorkspaceShell>
      <MemoryList />
    </WorkspaceShell>
  );
}
