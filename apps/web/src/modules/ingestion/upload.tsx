'use client';

import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ApiError, apiRequest, csrfHeaders } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { listSources, sourceKeys, getRun } from '@/modules/sources/api';

/** Renders file selection and upload state, then reports the accepted ingestion run.
 *
 * Optional locked source/mode values let the Notes and Personal finance wizards reuse this
 * owner-scoped upload path without exposing unrelated sources or scheduler controls.
 */
export function Upload({ initialMode, initialSourceId }: {
  initialMode?: 'document' | 'notes' | 'finance';
  initialSourceId?: string;
} = {}) {
  const { csrfToken } = useWorkspaceSession();
  const ts = useTranslations('uiSweep');
  const [sourceId, setSourceId] = useState(initialSourceId ?? '');
  const [mode, setMode] = useState<'document' | 'notes' | 'finance'>(initialMode ?? 'document');
  const [runId, setRunId] = useState('');
  const [error, setError] = useState('');
  const sources = useInfiniteQuery({ queryKey: sourceKeys.list, initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listSources(pageParam), getNextPageParam: (last) => last.next_cursor ?? undefined });
  const run = useQuery({ queryKey: ['ingestion-run', runId], queryFn: () => getRun(runId), enabled: !!runId, refetchInterval: (query) => ['succeeded', 'needs_ocr', 'failed'].includes(query.state.data?.status ?? '') ? false : 1500 });
  const available = sources.data?.pages.flatMap((page) => page.items).filter((source) => source.status === 'active'
    && (source.type === 'file' || source.type === 'manual')
    && (!initialSourceId || source.id === initialSourceId)
    && (mode === 'document' || source.type === 'manual' && source.local_only)) ?? [];

  /** Posts a document or local-only financial export through its durable ingestion receipt and tracks the run. */
  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    const form = new FormData(event.currentTarget);
    form.set('source_id', sourceId);
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
      {!initialMode && <div className="field"><Label htmlFor="upload-mode">{ts('uploadMode')}</Label><Select value={mode} onValueChange={(value) => { setMode(value as typeof mode); setSourceId(''); setRunId(''); setError(''); }}><SelectTrigger id="upload-mode"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="document">{ts('uploadModeDocument')}</SelectItem><SelectItem value="notes">{ts('uploadModeNotes')}</SelectItem><SelectItem value="finance">{ts('uploadModeFinance')}</SelectItem></SelectContent></Select></div>}
      {!initialSourceId && <div className="field"><Label htmlFor="upload-source">{ts('uploadSource')}</Label><Select value={sourceId} onValueChange={setSourceId} required><SelectTrigger id="upload-source"><SelectValue placeholder={ts('uploadChooseSource')} /></SelectTrigger><SelectContent>{available.map((source) => <SelectItem value={source.id} key={source.id}>{source.name}</SelectItem>)}</SelectContent></Select></div>}
      <div className="field"><Label htmlFor="upload-file">{ts('uploadFile')}</Label><input id="upload-file" className="input" name="file" type="file" accept={mode === 'finance' ? '.csv,.ofx' : mode === 'notes' ? '.md,.markdown' : '.txt,.md,.json,.csv,.pdf,.docx'} required /><span className="muted">{mode === 'finance' ? ts('uploadFinanceHint') : mode === 'notes' ? ts('uploadNotesHint') : ts('uploadHint')}</span></div>
      {error && <p className="error" role="alert">{error}</p>}
      {sources.isSuccess && available.length === 0 && <p className="empty-state">{ts('uploadNeedSource')}</p>}
      <Button type="submit" disabled={!sourceId || available.length === 0}>{ts('uploadButton')}</Button>
    </form>
    {runId && run.data && <div aria-live="polite"><h3>{ts('uploadProgress')}</h3><p>{ts('uploadReceived')}</p><p>{ts('uploadParsed', { value: run.data.status === 'needs_ocr' ? ts('uploadNoText') : run.data.stages.find((stage) => stage.stage_key === 'parse_file')?.status ?? ts('uploadQueued') })}</p><p>{ts('uploadChunked', { value: run.data.stages.find((stage) => stage.stage_key === 'parse_file')?.result_count ?? (run.data.status === 'running' ? ts('uploadProcessing') : '0') })}</p><p>{ts('uploadEmbedded')}</p>{run.data.error_code && <p className="error">{ts('uploadError', { code: run.data.error_code })}</p>}</div>}
    {run.isError && <p className="error" role="alert">{ts('uploadProgressFailed')}</p>}
  </section>;
}
