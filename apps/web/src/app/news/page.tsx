import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { NewsPage } from '@/modules/news/news-page';

/** Renders the News stories page in the workspace shell. */
export default function Page() { return <WorkspaceShell><NewsPage /></WorkspaceShell>; }
