import type { ReactNode } from 'react';
import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { messages } from '@/core/messages';
import { WorkspaceProviders } from '@/core/workspace-providers';

/**
 * Layout for every authenticated workspace route. Rendering the shell here (not in each page) keeps the
 * navigation, realtime connection and header mounted across client-side navigations instead of remounting per page.
 * The workspace-only providers mount here too, so public pages never load them.
 * The route group adds no URL segment.
 * @param children The current workspace page, rendered inside the shell's scrollable main region.
 */
export default function WorkspaceLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <WorkspaceProviders english={messages['en-us']}>
      <WorkspaceShell>{children}</WorkspaceShell>
    </WorkspaceProviders>
  );
}
