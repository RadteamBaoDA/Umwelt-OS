'use client';

import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ApiError, apiRequest, csrfHeaders } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { listSources, sourceKeys, getRun } from '@/modules/sources/public-api';
import { Input } from '@/components/ui/input';

/** Sentinel Select value for "no explicit source": the server files the upload under its default Uploads source. */
const DEFAULT_SOURCE = 'default';

/** Renders file selection and upload state, then reports the accepted ingestion run.
 *
 * The source is optional: when none is chosen `source_id` is omitted and the API stores the
 * file in the server-owned "Uploads" source. Notes and finance modes only offer local-only manual sources.
 */
export function Upload() {
  const { csrfToken } = useWorkspaceSession();
  const ts = useTranslations('uiSweep');
  const [sourceId, setSourceId] = useState(DEFAULT_SOURCE);
  const [mode, setMode] = useState<'document' | 'notes' | 'finance'>('document');
  const [runId, setRunId] = useState('');
  const [error, setError] = useState('');
  const sources = useInfiniteQuery({ queryKey: sourceKeys.list, initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listSources(pageParam), getNextPageParam: (last) => last.next_cursor ?? undefined });
  const run = useQuery({ queryKey: ['ingestion-run', runId], queryFn: () => getRun(runId), enabled: !!runId, refetchInterval: (query) => ['succeeded', 'needs_ocr', 'failed'].includes(query.state.data?.status ?? '') ? false : 1500 });
  const available = sources.data?.pages.flatMap((page) => page.items).filter((source) => source.status === 'active'
    && (source.type === 'file' || source.type === 'manual')
    && (mode === 'document' || source.type === 'manual' && source.local_only)) ?? [];

  /** Posts a document or local-only financial export through its durable ingestion receipt and tracks the run. */
  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    const form = new FormData(event.currentTarget);
    if (sourceId !== DEFAULT_SOURCE) form.set('source_id', sourceId);
    try {
      const endpoint = mode === 'finance' ? '/api/v1/documents/finance-import' : '/api/v1/documents/upload';
      const receipt = await apiRequest<{ run_id: string }>(endpoint, { method: 'POST', headers: csrfHeaders(csrfToken), body: form });
      setRunId(receipt.run_id);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : ts('uploadFailed'));
    }
  }

  return <section className="sub-panel">
    <h2>{ts('uploadTitle')}</h2>
    <form className="form" onSubmit={upload}>
      <div className="field"><Label htmlFor="upload-mode">{ts('uploadMode')}</Label><Select value={mode} onValueChange={(value) => { setMode(value as typeof mode); setSourceId(DEFAULT_SOURCE); setRunId(''); setError(''); }}><SelectTrigger id="upload-mode"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="document">{ts('uploadModeDocument')}</SelectItem><SelectItem value="notes">{ts('uploadModeNotes')}</SelectItem><SelectItem value="finance">{ts('uploadModeFinance')}</SelectItem></SelectContent></Select></div>
      <div className="field"><Label htmlFor="upload-source">{ts('uploadSource')}</Label><Select value={sourceId} onValueChange={setSourceId}><SelectTrigger id="upload-source"><SelectValue /></SelectTrigger><SelectContent><SelectItem value={DEFAULT_SOURCE}>{ts('uploadDefaultSource')}</SelectItem>{available.map((source) => <SelectItem value={source.id} key={source.id}>{source.name}</SelectItem>)}</SelectContent></Select></div>
      <div className="field"><Label htmlFor="upload-file">{ts('uploadFile')}</Label><Input id="upload-file" name="file" type="file" accept={mode === 'finance' ? '.csv,.ofx' : mode === 'notes' ? '.md,.markdown' : '.txt,.md,.json,.csv,.pdf,.docx'} required /><span className="muted">{mode === 'finance' ? ts('uploadFinanceHint') : mode === 'notes' ? ts('uploadNotesHint') : ts('uploadHint')}</span></div>
      {error && <p className="error" role="alert">{error}</p>}
      <Button type="submit">{ts('uploadButton')}</Button>
    </form>
    {runId && run.data && <div aria-live="polite"><h3>{ts('uploadProgress')}</h3><p>{ts('uploadReceived')}</p><p>{ts('uploadParsed', { value: run.data.status === 'needs_ocr' ? ts('uploadNoText') : run.data.stages.find((stage) => stage.stage_key === 'parse_file')?.status ?? ts('uploadQueued') })}</p><p>{ts('uploadChunked', { value: run.data.stages.find((stage) => stage.stage_key === 'parse_file')?.result_count ?? (run.data.status === 'running' ? ts('uploadProcessing') : '0') })}</p><p>{ts('uploadEmbedded')}</p>{run.data.error_code && <p className="error">{ts('uploadError', { code: run.data.error_code })}</p>}</div>}
    {run.isError && <p className="error" role="alert">{ts('uploadProgressFailed')}</p>}
  </section>;
}
