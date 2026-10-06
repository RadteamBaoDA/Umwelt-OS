'use client';

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useState, type FormEvent } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ApiError } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { getSource } from '@/modules/sources/api';
import { deleteDocument, documentDeletionKeys, documentKeys, getCitationTarget, getDocument, getVersion, listVersions, updateContent, updateDocument } from './api';
import { DocumentDeletionReceiptPanel } from './document-deletion-receipt';

/**
 * Loads and edits a document or renders its independent deletion receipt. Citation IDs select
 * historical content but grant no access; an accepted deletion suppresses and clears that content.
 */
export function DocumentDetail({ id, citedVersion, citationVersionId, citationChunkId, deletionOperationId }: { id: string; citedVersion: number | null; citationVersionId?: string | null; citationChunkId?: string | null; deletionOperationId?: string | null }) {
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();
  const router = useRouter();
  const [acceptedOperation, setAcceptedOperation] = useState<{ documentId: string; operationId: string } | null>(null);
  const visibleDeletionOperationId = deletionOperationId ?? (acceptedOperation?.documentId === id ? acceptedOperation.operationId : null);
  const document = useQuery({ queryKey: documentKeys.detail(id), queryFn: () => getDocument(id), enabled: !visibleDeletionOperationId });
  const source = useQuery({ queryKey: ['sources', document.data?.source_id], queryFn: () => getSource(document.data!.source_id), enabled: !visibleDeletionOperationId && !!document.data });
  const current = useQuery({ queryKey: [...documentKeys.versions(id), document.data?.current_version], queryFn: () => getVersion(id, document.data!.current_version), enabled: !visibleDeletionOperationId && !!document.data });
  const versions = useInfiniteQuery({ queryKey: documentKeys.versions(id), initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listVersions(id, pageParam), getNextPageParam: (last) => last.next_cursor ?? undefined, enabled: !visibleDeletionOperationId && !!document.data });
  const citationTarget = useQuery({
    queryKey: ['documents', id, 'citation', citationVersionId, citationChunkId],
    queryFn: () => getCitationTarget(id, citationVersionId!, citationChunkId!),
    enabled: !visibleDeletionOperationId && Boolean(citationVersionId && citationChunkId),
  });
  const [selectedVersion, setSelectedVersion] = useState<number | null>(citedVersion);
  useEffect(() => { setSelectedVersion(citedVersion); }, [citedVersion]);
  useEffect(() => {
    if (citationTarget.data) setSelectedVersion(citationTarget.data.version_number);
  }, [citationTarget.data]);
  useEffect(() => {
    if (citationTarget.data) window.document.getElementById('cited-chunk')?.scrollIntoView({ block: 'center' });
  }, [citationTarget.data]);
  const selected = useQuery({ queryKey: [...documentKeys.versions(id), selectedVersion], queryFn: () => getVersion(id, selectedVersion!), enabled: !visibleDeletionOperationId && selectedVersion !== null });
  const [title, setTitle] = useState<string | null>(null);
  const [metadata, setMetadata] = useState<string | null>(null);
  const [content, setContent] = useState<string | null>(null);
  const [expectedVersion, setExpectedVersion] = useState<number | null>(null);
  const [metadataError, setMetadataError] = useState('');

  useEffect(() => {
    if (!visibleDeletionOperationId && document.data && title === null) { setTitle(document.data.title); setMetadata(JSON.stringify(document.data.metadata, null, 2)); }
  }, [document.data, title, visibleDeletionOperationId]);
  useEffect(() => {
    if (!visibleDeletionOperationId && current.data && expectedVersion === null) { setContent(current.data.content); setExpectedVersion(current.data.version_number); }
  }, [current.data, expectedVersion, visibleDeletionOperationId]);

  const saveMetadata = useMutation({
    mutationFn: (value: { title: string; metadata: Record<string, unknown> }) => updateDocument(id, value, csrfToken),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: documentKeys.all }); queryClient.invalidateQueries({ queryKey: documentKeys.detail(id) }); },
  });
  const saveContent = useMutation({
    mutationFn: () => updateContent(id, content ?? '', expectedVersion!, csrfToken),
    onSuccess: (updated) => { setExpectedVersion(updated.current_version); queryClient.invalidateQueries({ queryKey: documentKeys.all }); queryClient.invalidateQueries({ queryKey: documentKeys.detail(id) }); queryClient.invalidateQueries({ queryKey: documentKeys.versions(id) }); },
  });
  const remove = useMutation({
    mutationFn: (documentId: string) => deleteDocument(documentId, csrfToken),
    onSuccess: async (receipt, documentId) => {
      // Gate content queries and clear form state before awaiting cancellation so deleted data cannot refetch meanwhile.
      setAcceptedOperation({ documentId, operationId: receipt.operation_id });
      setTitle(null);
      setMetadata(null);
      setContent(null);
      setExpectedVersion(null);
      setSelectedVersion(null);
      // Keep the operation-only receipt while clearing cached detail, versions, citations, and document-list titles.
      queryClient.setQueryData(documentDeletionKeys.detail(receipt.operation_id), receipt);
      await queryClient.cancelQueries({ queryKey: documentKeys.all });
      queryClient.removeQueries({ queryKey: documentKeys.all });
      router.replace(`/knowledge/documents/${documentId}?deletionOperationId=${encodeURIComponent(receipt.operation_id)}`);
    },
  });

  /** Submits document metadata changes and reports request failures through the form state. */
  function submitMetadata(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      const value: unknown = JSON.parse(metadata ?? '{}');
      if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('Metadata must be a JSON object.');
      if (!title?.trim()) throw new Error('Enter a title.');
      setMetadataError('');
      saveMetadata.mutate({ title: title.trim(), metadata: value as Record<string, unknown> });
    } catch (error) { setMetadataError(error instanceof Error ? error.message : 'Invalid metadata JSON.'); }
  }

  // Render the retained receipt before deleted-document pending or 404 branches can replace this route.
  if (visibleDeletionOperationId) return <DocumentDeletionReceiptPanel operationId={visibleDeletionOperationId} />;
  if (document.isPending) return <div className="content-panel skeleton" aria-label="Loading document" />;
  if (document.isError) return <section className="content-panel"><h1>Document unavailable</h1><p className="error" role="alert">{document.error instanceof ApiError ? document.error.message : 'Could not load document.'}</p><Button className="secondary" onClick={() => document.refetch()}>Retry</Button></section>;

  return <section className="content-panel"><Link href="/knowledge/documents">← Documents</Link><div className="section-heading"><div><span className="brand">Knowledge</span><h1>{document.data.title}</h1><p className="muted">Source: {source.data?.name ?? document.data.source_id} · Version {document.data.current_version} · Updated {new Date(document.data.updated_at).toLocaleString()}</p>{document.data.raw_uri && <a href={`/api/v1/documents/${document.data.id}/raw`}>Inspect original file and provenance</a>}</div><Button className="secondary" disabled={remove.isPending} onClick={() => { if (window.confirm(`Permanently delete ${document.data.title} and its version history?`)) remove.mutate(id); }}>Delete document</Button></div>
    {remove.error && <p className="error" role="alert">{remove.error instanceof ApiError ? remove.error.message : 'Could not delete document.'}</p>}
    {citationTarget.isError && <p className="error" role="alert">Cited evidence is no longer available under the current source access policy.</p>}
    {citationTarget.data && <section className="sub-panel" id="cited-chunk"><h2>Cited chunk · Version {citationTarget.data.version_number}</h2><pre>{citationTarget.data.excerpt}</pre></section>}
    <div className="detail-grid"><div>
      <section className="sub-panel"><h2>Content</h2>{current.isPending && <p className="muted">Loading content…</p>}{current.isError && <p className="error" role="alert">Could not load current version. <Button className="secondary" onClick={() => current.refetch()}>Retry</Button></p>}
        {current.data && <div className="saved-content"><h3>Saved version {current.data.version_number}</h3><pre>{current.data.content}</pre></div>}
        {content !== null && expectedVersion !== null && <form className="form" onSubmit={(event) => { event.preventDefault(); saveContent.mutate(); }}><div className="field"><Label htmlFor="content">Content</Label><textarea id="content" className="input text-area" value={content} onChange={(event) => setContent(event.target.value)} /></div>
          {saveContent.error && <p className="error" role="alert">{saveContent.error instanceof ApiError && saveContent.error.status === 409 ? 'A newer version exists. Your draft is still here; review the latest version before saving again.' : saveContent.error instanceof ApiError ? saveContent.error.message : 'Could not save content.'}</p>}
          {saveContent.error instanceof ApiError && saveContent.error.status === 409 && <Button type="button" className="secondary" onClick={async () => { const latest = await document.refetch(); if (latest.data) setExpectedVersion(latest.data.current_version); }}>Use latest version number</Button>}
          <Button type="submit" disabled={saveContent.isPending}>Save content</Button></form>}
      </section>
      <section className="sub-panel"><h2>Details</h2><form className="form" onSubmit={submitMetadata}><div className="field"><Label htmlFor="title">Title</Label><Input id="title" maxLength={500} value={title ?? ''} onChange={(event) => setTitle(event.target.value)} /></div><div className="field"><Label htmlFor="metadata">Metadata (JSON object)</Label><textarea id="metadata" className="input text-area compact" value={metadata ?? ''} onChange={(event) => setMetadata(event.target.value)} /></div>
        {metadataError && <p className="error" role="alert">{metadataError}</p>}{saveMetadata.error && <p className="error" role="alert">{saveMetadata.error instanceof ApiError ? saveMetadata.error.message : 'Could not save details.'}</p>}<Button type="submit" disabled={saveMetadata.isPending}>Save details</Button></form></section>
    </div><aside className="sub-panel"><h2>Version history</h2>{versions.isPending && <p className="muted">Loading versions…</p>}{versions.isError && <p className="error" role="alert">Could not load versions. <Button className="secondary" onClick={() => versions.refetch()}>Retry</Button></p>}
      {versions.data && <ul className="version-list">{versions.data.pages.flatMap((page) => page.items).map((version) => <li key={version.id}><button type="button" className="text-button" onClick={() => setSelectedVersion(version.version_number)}>Version {version.version_number}</button><span className="muted">{new Date(version.created_at).toLocaleString()}</span></li>)}</ul>}
      {versions.hasNextPage && <Button className="secondary" disabled={versions.isFetchingNextPage} onClick={() => versions.fetchNextPage()}>Load more versions</Button>}
      {selectedVersion !== null && <div className="version-preview" id="cited-revision"><h3>Version {selectedVersion}</h3>{selected.isPending && <p className="muted">Loading version…</p>}{selected.isError && <p className="error" role="alert">Could not load version.</p>}{selected.data && <pre>{selected.data.content}</pre>}</div>}
    </aside></div>
  </section>;
}
