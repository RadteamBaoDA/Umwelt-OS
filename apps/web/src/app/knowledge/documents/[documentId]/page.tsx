import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { DocumentDetail } from '@/modules/knowledge/document-detail';

/** Renders document detail or an operation-ID-only deletion receipt inside the shared shell. */
export default async function DocumentPage({ params, searchParams }: { params: Promise<{ documentId: string }>; searchParams: Promise<{ version?: string; versionId?: string; chunkId?: string; deletionOperationId?: string }> }) {
  const { documentId } = await params;
  const { version, versionId, chunkId, deletionOperationId } = await searchParams;
  const revision = Number(version);
  return <WorkspaceShell><DocumentDetail id={documentId} citedVersion={Number.isSafeInteger(revision) && revision > 0 ? revision : null} citationVersionId={versionId ?? null} citationChunkId={chunkId ?? null} deletionOperationId={deletionOperationId ?? null} /></WorkspaceShell>;
}
