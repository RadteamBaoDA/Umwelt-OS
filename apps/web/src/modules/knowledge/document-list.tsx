'use client';

import { useInfiniteQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { DocumentForm } from './document-form';
import { documentKeys, listDocuments } from './api';
import { Upload } from '@/modules/ingestion/upload';
import { useRealtime } from '@/core/realtime-provider';
import { useTranslations } from 'next-intl';
import { useWorkspace } from '@/core/workspace-context';

/** Lists documents and consumes realtime refresh state for the knowledge document view. */
export function DocumentList() {
  const t = useTranslations('shell');
  const realtime = useRealtime();
  const { isOwner } = useWorkspace();
  const [adding, setAdding] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [refreshingUpdates, setRefreshingUpdates] = useState(false);
  const documents = useInfiniteQuery({ queryKey: documentKeys.all, initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listDocuments(pageParam), getNextPageParam: (last) => last.next_cursor ?? undefined });
  const items = documents.data?.pages.flatMap((page) => page.items) ?? [];
  /** Refreshes documents from realtime updates and clears the local busy indicator after the attempt settles. */
  const consumeUpdates = async () => {
    setRefreshingUpdates(true);
    try { await realtime.consumeDocumentUpdates(); } finally { setRefreshingUpdates(false); }
  };
  return <section className="content-panel"><div className="section-heading"><div><span className="brand">Knowledge</span><h1>Documents</h1><p className="muted">Your private source backed library. Semantic search and AI answers are not available yet.</p></div><div className="form-actions">{(realtime.newDocumentCount > 0 || realtime.documentRefreshRequired) && <Button className="secondary" disabled={refreshingUpdates} onClick={() => void consumeUpdates()}>{refreshingUpdates ? t('reconnecting') : realtime.newDocumentCount > 0 ? t('newDocumentUpdates', { count: realtime.newDocumentCount }) : t('refreshDocumentList')}</Button>}{isOwner && <><Button className="secondary" onClick={() => setUploading((value) => !value)}>Upload file</Button><Button onClick={() => setAdding(true)}>New document</Button></>}</div></div>
    {realtime.documentRefreshRequired && <p className="muted" role="status">{t('documentRefreshAvailable')}</p>}
    {realtime.documentRefreshFailed && <p className="error" role="alert">{t('documentRefreshFailed')}</p>}
    {adding && <DocumentForm onCancel={() => setAdding(false)} />}
    {uploading && <Upload />}
    {documents.isPending && <div className="skeleton" aria-label="Loading documents" />}
    {documents.isError && <p className="error" role="alert">Could not load documents. <Button className="secondary" onClick={() => documents.refetch()}>Retry</Button></p>}
    {documents.isSuccess && items.length === 0 && <p className="empty-state">No documents yet. Create a manual source, add a document, or upload a file. Search and AI answers become available in later phases.</p>}
    {items.length > 0 && <ul className="record-list">{items.map((document) => <li key={document.id} className="record-row"><div><Link href={`/knowledge/documents/${document.id}`}><strong>{document.title}</strong></Link><p className="muted">Version {document.current_version} · Updated {new Date(document.updated_at).toLocaleString()}</p></div></li>)}</ul>}
    {documents.hasNextPage && <Button className="secondary" disabled={documents.isFetchingNextPage} onClick={() => documents.fetchNextPage()}>Load more</Button>}
  </section>;
}
