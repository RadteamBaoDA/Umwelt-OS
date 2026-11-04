'use client';

import { zodResolver } from '@hookform/resolvers/zod';
import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useRouter } from 'next/navigation';
import { useTranslations } from 'next-intl';
import { Controller, useForm } from 'react-hook-form';
import { z } from 'zod';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ApiError } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { listSources, sourceKeys } from '@/modules/sources/public-api';
import { createDocument, documentKeys } from './api';
import { Textarea } from '@/components/ui/textarea';

const schema = z.object({ source_id: z.string().min(1, 'Select a source.'), title: z.string().trim().min(1).max(500), content: z.string().max(1_048_576) });
type Values = z.infer<typeof schema>;
/** Radix Select forbids an empty item value, so the "no source chosen" option uses this sentinel and maps back to ''. */
const NO_SOURCE = '__none__';

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
      <div className="field"><Label htmlFor="document-source">{ts('docSource')}</Label><Controller control={form.control} name="source_id" render={({ field }) => <Select value={field.value === '' ? NO_SOURCE : field.value} onValueChange={(next) => field.onChange(next === NO_SOURCE ? '' : next)}><SelectTrigger id="document-source" onBlur={field.onBlur} ref={field.ref}><SelectValue /></SelectTrigger><SelectContent><SelectItem value={NO_SOURCE}>{ts('docSelectManual')}</SelectItem>{manualSources.map((source) => <SelectItem key={source.id} value={source.id}>{source.name}</SelectItem>)}</SelectContent></Select>} />{form.formState.errors.source_id && <span className="error">{ts('docSelectSourceError')}</span>}</div>
      <div className="field"><Label htmlFor="document-title">{ts('docTitle')}</Label><Input id="document-title" autoFocus maxLength={500} {...form.register('title')} />{form.formState.errors.title && <span className="error">{ts('docTitleError')}</span>}</div>
      <div className="field"><Label htmlFor="document-content">{ts('docContent')}</Label><Textarea id="document-content" className="input text-area min-h-[220px] field-sizing-fixed resize-y rounded-[6px] border-[color:var(--control)] bg-surface px-3 py-[7px] text-[length:inherit] md:text-[length:inherit] dark:bg-surface focus-visible:border-[color:var(--control)] focus-visible:ring-0 focus-visible:outline-3 focus-visible:outline-solid focus-visible:outline-offset-2 focus-visible:outline-[color-mix(in_srgb,var(--accent)_40%,transparent)]" {...form.register('content')} />{form.formState.errors.content && <span className="error">{ts('docContentError')}</span>}</div>
      {create.error && <p className="error" role="alert">{create.error instanceof ApiError ? create.error.message : ts('docSaveFailed')}</p>}
      <div className="form-actions"><Button type="submit" disabled={create.isPending}>{tc('save')}</Button><Button type="button" className="secondary" onClick={onCancel}>{tc('cancel')}</Button></div>
    </form>}
    {sources.hasNextPage && <Button className="secondary" disabled={sources.isFetchingNextPage} onClick={() => sources.fetchNextPage()}>{ts('docLoadMore')}</Button>}
  </section>;
}
