'use client';

import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { ApiError, apiRequest, csrfHeaders } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { listSources, sourceKeys, getRun } from '@/modules/sources/api';

/** Renders file selection and upload state, then reports the accepted ingestion run. */
export function Upload() {
  const { csrfToken } = useWorkspaceSession();
  const ts = useTranslations('uiSweep');
  const [sourceId, setSourceId] = useState('');
  const [runId, setRunId] = useState('');
  const [error, setError] = useState('');
  const sources = useInfiniteQuery({ queryKey: sourceKeys.list, initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listSources(pageParam), getNextPageParam: (last) => last.next_cursor ?? undefined });
  const run = useQuery({ queryKey: ['ingestion-run', runId], queryFn: () => getRun(runId), enabled: !!runId, refetchInterval: (query) => ['succeeded', 'needs_ocr', 'failed'].includes(query.state.data?.status ?? '') ? false : 1500 });
  const available = sources.data?.pages.flatMap((page) => page.items).filter((source) => source.status === 'active' && (source.type === 'file' || source.type === 'manual')) ?? [];

  /** Posts the selected file with its source ID and CSRF token, then tracks the returned run or shows the request error. */
  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    const form = new FormData(event.currentTarget);
    form.set('source_id', sourceId);
    try {
      const receipt = await apiRequest<{ run_id: string }>('/api/v1/documents/upload', { method: 'POST', headers: csrfHeaders(csrfToken), body: form });
      setRunId(receipt.run_id);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : ts('uploadFailed'));
    }
  }

  return <section className="sub-panel">
    <h2>{ts('uploadTitle')}</h2>
    <form className="form" onSubmit={upload}>
      <div className="field"><Label htmlFor="upload-source">{ts('uploadSource')}</Label><select id="upload-source" className="input" value={sourceId} onChange={(event) => setSourceId(event.target.value)} required><option value="">{ts('uploadChooseSource')}</option>{available.map((source) => <option value={source.id} key={source.id}>{source.name}</option>)}</select></div>
      <div className="field"><Label htmlFor="upload-file">{ts('uploadFile')}</Label><input id="upload-file" className="input" name="file" type="file" accept=".txt,.md,.json,.csv,.pdf,.docx" required /><span className="muted">{ts('uploadHint')}</span></div>
      {error && <p className="error" role="alert">{error}</p>}
      {sources.isSuccess && available.length === 0 && <p className="empty-state">{ts('uploadNeedSource')}</p>}
      <Button type="submit" disabled={!sourceId || available.length === 0}>{ts('uploadButton')}</Button>
    </form>
    {runId && run.data && <div aria-live="polite"><h3>{ts('uploadProgress')}</h3><p>{ts('uploadReceived')}</p><p>{ts('uploadParsed', { value: run.data.status === 'needs_ocr' ? ts('uploadNoText') : run.data.stages.find((stage) => stage.stage_key === 'parse_file')?.status ?? ts('uploadQueued') })}</p><p>{ts('uploadChunked', { value: run.data.stages.find((stage) => stage.stage_key === 'parse_file')?.result_count ?? (run.data.status === 'running' ? ts('uploadProcessing') : '0') })}</p><p>{ts('uploadEmbedded')}</p>{run.data.error_code && <p className="error">{ts('uploadError', { code: run.data.error_code })}</p>}</div>}
    {run.isError && <p className="error" role="alert">{ts('uploadProgressFailed')}</p>}
  </section>;
}
