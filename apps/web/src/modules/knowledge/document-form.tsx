'use client';

import { zodResolver } from '@hookform/resolvers/zod';
import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useRouter } from 'next/navigation';
import { useTranslations } from 'next-intl';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ApiError } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { listSources, sourceKeys } from '@/modules/sources/api';
import { createDocument, documentKeys } from './api';

const schema = z.object({ source_id: z.string().min(1, 'Select a source.'), title: z.string().trim().min(1).max(500), content: z.string().max(1_048_576) });
type Values = z.infer<typeof schema>;

/** Creates a document from the selected source and form values, then invokes the supplied completion callback. */
export function DocumentForm({ onCancel }: { onCancel: () => void }) {
  const { csrfToken } = useWorkspaceSession();
  const ts = useTranslations('uiSweep');
  const tc = useTranslations('common');
  const queryClient = useQueryClient();
  const router = useRouter();
  const sources = useInfiniteQuery({ queryKey: sourceKeys.list, initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listSources(pageParam), getNextPageParam: (last) => last.next_cursor ?? undefined });
  const manualSources = sources.data?.pages.flatMap((page) => page.items).filter((source) => source.type === 'manual' && source.status === 'active') ?? [];
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { source_id: '', title: '', content: '' } });
  const create = useMutation({ mutationFn: (values: Values) => createDocument(values, csrfToken), onSuccess: (document) => { queryClient.invalidateQueries({ queryKey: documentKeys.all }); router.push(`/knowledge/documents/${document.id}`); } });
  return <section className="sub-panel"><h2>{ts('docNew')}</h2>
    {sources.isPending && <p className="muted">{ts('docLoadingSources')}</p>}
    {sources.isError && <p className="error" role="alert">{ts('docSourcesFailed')} <Button className="secondary" onClick={() => sources.refetch()}>{tc('retry')}</Button></p>}
    {sources.isSuccess && manualSources.length === 0 && <p className="empty-state">{ts('docNeedManual')}</p>}
    {manualSources.length > 0 && <form className="form" onSubmit={form.handleSubmit((values) => create.mutate(values))}>
      <div className="field"><Label htmlFor="document-source">{ts('docSource')}</Label><select id="document-source" className="input" {...form.register('source_id')}><option value="">{ts('docSelectManual')}</option>{manualSources.map((source) => <option key={source.id} value={source.id}>{source.name}</option>)}</select>{form.formState.errors.source_id && <span className="error">{ts('docSelectSourceError')}</span>}</div>
      <div className="field"><Label htmlFor="document-title">{ts('docTitle')}</Label><Input id="document-title" autoFocus maxLength={500} {...form.register('title')} />{form.formState.errors.title && <span className="error">{ts('docTitleError')}</span>}</div>
      <div className="field"><Label htmlFor="document-content">{ts('docContent')}</Label><textarea id="document-content" className="input text-area" {...form.register('content')} />{form.formState.errors.content && <span className="error">{ts('docContentError')}</span>}</div>
      {create.error && <p className="error" role="alert">{create.error instanceof ApiError ? create.error.message : ts('docSaveFailed')}</p>}
      <div className="form-actions"><Button type="submit" disabled={create.isPending}>{tc('save')}</Button><Button type="button" className="secondary" onClick={onCancel}>{tc('cancel')}</Button></div>
    </form>}
    {sources.hasNextPage && <Button className="secondary" disabled={sources.isFetchingNextPage} onClick={() => sources.fetchNextPage()}>{ts('docLoadMore')}</Button>}
  </section>;
}
