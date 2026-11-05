import type { ReactNode } from 'react';
import { WorkspaceShell } from '@/core/app-shell/workspace-shell';

/**
 * Layout for every authenticated workspace route. Rendering the shell here (not in each page) keeps the
 * navigation, realtime connection and header mounted across client-side navigations instead of remounting per page.
 * The route group adds no URL segment.
 * @param children The current workspace page, rendered inside the shell's scrollable main region.
 */
export default function WorkspaceLayout({ children }: Readonly<{ children: ReactNode }>) {
  return <WorkspaceShell>{children}</WorkspaceShell>;
}
