'use client';

import Link from 'next/link';
import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { useTranslations } from 'next-intl';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { createEntity, entityKeys, listEntities, listEntityReview } from './api';
import { EntityReviewCard } from './entity-review-card';

const entityTypes = ['person', 'organization', 'company', 'project', 'repository', 'place', 'country', 'product', 'topic', 'technology', 'asset', 'device', 'website', 'event_subject', 'other'] as const;
const createSchema = z.object({ type: z.enum(entityTypes), name: z.string().trim().min(1).max(300), reason: z.string().trim().min(1).max(300) });
type CreateValues = z.infer<typeof createSchema>;

/** Lists and filters entities and review candidates for the knowledge workspace. */
export function EntityList() {
  const t = useTranslations('entities');
  const [query, setQuery] = useState('');
  // Debounce the filter so each keystroke does not trigger a list request.
  const [debouncedQuery, setDebouncedQuery] = useState('');
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query), 300);
    return () => clearTimeout(timer);
  }, [query]);
  const [type, setType] = useState<string | undefined>();
  const [creating, setCreating] = useState(false);
  const form = useForm<CreateValues>({ resolver: zodResolver(createSchema), mode: 'onChange', defaultValues: { type: 'person', name: '', reason: 'owner_create' } });
  const session = useWorkspaceSession();
  const client = useQueryClient();
  const create = useMutation({ mutationFn: (values: CreateValues) => createEntity(values, session.csrfToken), onSuccess: (entity) => { client.setQueryData(entityKeys.detail(entity.id), entity); void client.invalidateQueries({ queryKey: entityKeys.all }); setCreating(false); form.reset(); } });
  const entities = useInfiniteQuery({ queryKey: entityKeys.list(type, debouncedQuery), initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listEntities(pageParam, type, debouncedQuery || undefined), getNextPageParam: (page) => page.next_cursor ?? undefined });
  const review = useInfiniteQuery({ queryKey: [...entityKeys.all, 'review'], initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listEntityReview(pageParam), getNextPageParam: (page) => page.next_cursor ?? undefined });
  const items = entities.data?.pages.flatMap((page) => page.items) ?? [];
  return <section className="flex flex-col gap-4 rounded-lg border bg-card p-6"><div className="section-heading"><div><h1>{t('title')}</h1><p className="text-muted-foreground">{t('intro')}</p></div><Button onClick={() => setCreating((value) => !value)}>{t('create')}</Button></div>
    {creating && <form className="form gap-5 rounded-lg border bg-card p-4 text-card-foreground" onSubmit={form.handleSubmit((values) => create.mutate(values))}><div className="field"><Label htmlFor="new-entity-type">{t('type')}</Label><Select value={form.watch('type')} onValueChange={(value) => form.setValue('type', value as CreateValues['type'], { shouldDirty: true })}><SelectTrigger id="new-entity-type"><SelectValue /></SelectTrigger><SelectContent>{entityTypes.map((item) => <SelectItem key={item} value={item}>{t(`type_${item}` as 'type_person')}</SelectItem>)}</SelectContent></Select></div><div className="field"><Label htmlFor="new-entity-name">{t('name')}</Label><Input id="new-entity-name" maxLength={300} aria-invalid={!!form.formState.errors.name} {...form.register('name')} />{form.formState.errors.name && <p className="error">{t('entityNameRequired')}</p>}</div><div className="field"><Label htmlFor="new-entity-reason">{t('reason')}</Label><Input id="new-entity-reason" maxLength={300} aria-invalid={!!form.formState.errors.reason} {...form.register('reason')} />{form.formState.errors.reason && <p className="error">{t('entityReasonRequired')}</p>}</div><Button disabled={!form.formState.isValid || create.isPending}>{t('saveEntity')}</Button>{create.isError && <p className="error" role="alert">{create.error.message}</p>}</form>}
    <div className="flex flex-wrap items-end gap-4">
    <div className="field w-full sm:max-w-xs"><Label htmlFor="entity-type-filter">{t('type')}</Label><Select value={type ?? 'all'} onValueChange={(value) => setType(value === 'all' ? undefined : value)}><SelectTrigger id="entity-type-filter"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">{t('allTypes')}</SelectItem>{entityTypes.map((item) => <SelectItem key={item} value={item}>{t(`type_${item}` as 'type_person')}</SelectItem>)}</SelectContent></Select></div>
    <div className="field w-full sm:max-w-xs"><Label htmlFor="entity-filter">{t('filter')}</Label><Input id="entity-filter" type="search" value={query} onChange={(event) => setQuery(event.target.value)} /></div>
    </div>
    {entities.isError && <p className="error" role="alert">{t('unavailable')} <Button variant="outline" onClick={() => entities.refetch()}>{t('retry')}</Button></p>}
    {entities.isPending && <div className="skeleton" role="status" aria-label={t('loadingEntities')} />}
    {items.length > 0 ? <ul className="flex flex-col">{items.map((entity) => <li className="border-b border-border py-4 last:border-b-0 knowledge-row" key={entity.id}><div className="min-w-0"><Link href={`/knowledge/entities/${entity.id}`}><strong>{entity.name ?? t('name')}</strong></Link><p className="meta text-muted-foreground m-0">{t('revision')} {entity.revision} · {entity.aliases.length} {t('aliasesCount')}</p></div><Badge variant="outline">{t(`type_${entity.type}` as 'type_person')}</Badge></li>)}</ul> : !entities.isPending && <p className="empty-state">{t('none')}</p>}
    {entities.hasNextPage && <Button variant="outline" disabled={entities.isFetchingNextPage} onClick={() => entities.fetchNextPage()}>{t('loadMore')}</Button>}
    <h2 className="mt-4 text-base font-semibold">{t('review')}</h2>{review.isError && <p className="error" role="alert">{t('reviewUnavailable')} <Button variant="outline" onClick={() => review.refetch()}>{t('retry')}</Button></p>}
    {review.data?.pages.some((page) => page.items.length) ? <ul className="flex flex-col gap-3">{review.data.pages.flatMap((page) => page.items).map((item) => <EntityReviewCard key={`${item.result_id}-${item.candidate_id ?? item.candidate_name}`} item={item} />)}</ul> : !review.isPending && <p className="empty-state">{t('noReview')}</p>}{review.hasNextPage && <Button variant="outline" disabled={review.isFetchingNextPage} onClick={() => review.fetchNextPage()}>{t('loadReview')}</Button>}{review.isFetchNextPageError && <p className="error" role="alert">{t('loadReviewFailed')} <Button variant="outline" onClick={() => review.fetchNextPage()}>{t('retry')}</Button></p>}
  </section>;
}
