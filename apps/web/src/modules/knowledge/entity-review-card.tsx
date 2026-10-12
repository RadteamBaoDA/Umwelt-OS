'use client';

import Link from 'next/link';
import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ApiError } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { assignEntityReview, entityKeys, listEntities, resolveRelationshipReview, type EntityReviewPage } from './api';

type ReviewItem = EntityReviewPage['items'][number];
const schema = z.object({ targetId: z.string(), reason: z.string().trim().min(1).max(300), confirmed: z.boolean() });
type Values = z.infer<typeof schema>;

/** Extracts conflict details suitable for display from a request error. */
function conflictDetails(error: unknown): string[] {
  if (!(error instanceof ApiError) || error.status !== 409 || !error.details || typeof error.details !== 'object') return [];
  const details = error.details as Record<string, unknown>;
  const raw = Array.isArray(details.conflicts) ? details.conflicts : details.conflict ? [details.conflict] : [];
  return raw.slice(0, 5).flatMap((value) => {
    if (!value || typeof value !== 'object') return [];
    const conflict = value as Record<string, unknown>;
    return [
      [conflict.code, conflict.message].filter((part): part is string => typeof part === 'string').join(': '),
      ...(['entity_ids', 'membership_ids', 'relationship_ids'] as const).flatMap((key) => {
        const ids = conflict[key];
        return Array.isArray(ids) && ids.length ? [`${key}: ${ids.slice(0, 5).filter((id): id is string => typeof id === 'string').join(', ')}`] : [];
      }),
    ].filter(Boolean);
  });
}

/** Renders an entity or relationship review candidate and submits its supported resolution actions. */
export function EntityReviewCard({ item }: { item: ReviewItem }) {
  const t = useTranslations('entities');
  const display = useDisplayPreferences();
  const session = useWorkspaceSession();
  const client = useQueryClient();
  const [query, setQuery] = useState('');
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { targetId: '', reason: 'owner_review', confirmed: false } });
  const targetId = form.watch('targetId');
  const reason = form.watch('reason');
  const confirmed = form.watch('confirmed');
  const targets = useInfiniteQuery({
    queryKey: ['entities', 'review-target', item.candidate_id, query, item.candidate_type],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => listEntities(pageParam, item.candidate_type ?? undefined, query.trim() || undefined),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: item.actionable && item.kind === 'entity' && query.trim().length >= 2,
  });
  const options = [...new Map(targets.data?.pages.flatMap((page) => page.items).map((entity) => [entity.id, entity]) ?? []).values()];
  const selected = options.find((entity) => entity.id === targetId);
  useEffect(() => { form.setValue('confirmed', false); }, [form, query, targetId, reason, selected?.id, selected?.name, selected?.revision]);
  /** Invalidates the owning query after a review action so displayed evidence is reloaded. */
  const invalidate = () => { void client.invalidateQueries({ queryKey: [...entityKeys.all, 'review'] }); void client.invalidateQueries({ queryKey: entityKeys.all }); };
  const assigned = useMutation({
    mutationFn: (values: Values) => {
      if (!selected || !item.candidate_id || !item.snapshot_digest || item.owner_generation === null) throw new Error(t('reloadReview'));
      return assignEntityReview(item.candidate_id, {
        result_id: item.result_id, snapshot_digest: item.snapshot_digest,
        expected_source_generation: item.source_generation,
        expected_owner_generation: item.owner_generation,
        target_entity_id: selected.id, expected_target_revision: selected.revision,
        reason: values.reason,
      }, session.csrfToken);
    },
    onSuccess: invalidate,
  });
  const resolved = useMutation({
    mutationFn: (values: Values) => {
      if (!item.candidate_id || !item.snapshot_digest || item.owner_generation === null) throw new Error(t('reloadReview'));
      return resolveRelationshipReview(item.candidate_id, {
        result_id: item.result_id, snapshot_digest: item.snapshot_digest,
        expected_source_generation: item.source_generation,
        expected_owner_generation: item.owner_generation, reason: values.reason,
      }, session.csrfToken);
    },
    onSuccess: () => { invalidate(); void client.invalidateQueries({ queryKey: ['relationships'] }); },
  });
  const error = item.kind === 'entity' ? assigned.error : resolved.error;
  const errorText = error instanceof ApiError ? `${error.message}${error.code ? ` (${error.code})` : ''}` : error?.message;
  const statuses: Record<string, string> = { pending: t('pending'), running: t('running'), succeeded: t('succeeded'), blocked: t('blocked'), failed: t('failed') };
  /** Renders the review candidate endpoint label from the supplied endpoint state. */
  const endpoint = (value: ReviewItem['source_endpoint']) => value && <span>{value.state === 'assigned' && value.entity_id ? <Link href={`/knowledge/entities/${value.entity_id}`}>{value.entity_name ?? t('unnamedEntity')} · {value.entity_type ? t(`type_${value.entity_type}` as 'type_person') : t('unknownValue')}</Link> : value.state === 'ambiguous' ? t('endpointAmbiguous') : t('endpointUnassigned')}{value.membership_id && <small> · {t('membership')} {value.membership_id}</small>}</span>;

  return <li className="rounded-xl border bg-card p-4 text-card-foreground">
    <strong>{item.candidate_name}</strong><p>{item.reason}</p>
    <small className="text-muted-foreground">{item.kind === 'entity' ? t('entityCandidate') : t('relationshipCandidate')} · {statuses[item.status] ?? item.status}{!item.actionable ? ` · ${t('displayOnly')}` : ''}</small>
    {item.kind === 'relationship' && item.relationship_type && <p>{t('relationshipType')}: {item.relationship_type}</p>}
    {item.kind === 'relationship' && <p>{t('sourceEndpoint')}: {endpoint(item.source_endpoint)}<br />{t('targetEndpoint')}: {endpoint(item.target_endpoint)}</p>}
    {item.possible_entity_ids.length > 0 && <p>{item.possible_entity_ids.map((id) => <Link key={id} href={`/knowledge/entities/${id}`}>{id.slice(0, 8)} </Link>)}</p>}
    {item.evidence.length > 0 ? <ul className="flex flex-col gap-3" aria-label={t('evidence')}>
      {item.evidence.map((evidence) => <li key={`${evidence.document_version_id}:${evidence.chunk_id}`}>
        <small className="text-muted-foreground">{evidence.source_name} · {t('observed')} {formatDateTime(evidence.observed_at, display.locale, display.timezone)}</small>
        <p><Link href={`/knowledge/documents/${evidence.document_id}?version=${evidence.version_number}#cited-revision`}>{evidence.title} · {t('documentVersion')} {evidence.version_number}</Link></p>
        <small className="text-muted-foreground">{evidence.metadata_is_version_snapshot ? t('metadataVersionSnapshot') : t('metadataCurrentFallback')}</small>
        <blockquote>{evidence.excerpt}</blockquote>
      </li>)}
    </ul> : item.document_id && item.version_number ? <p><Link href={`/knowledge/documents/${item.document_id}?version=${item.version_number}#cited-revision`}>{t('openDocumentRevision')} · {item.source_name}</Link> · {t('evidenceUnavailable')}</p> : <p className="text-muted-foreground">{t('evidenceUnavailable')}</p>}
    {item.actionable && item.candidate_id && item.snapshot_digest && item.kind === 'entity' && <form className="form" onSubmit={form.handleSubmit((values) => { if (values.confirmed && selected) assigned.mutate(values); })}>
      <div className="field"><Label htmlFor={`target-query-${item.candidate_id}`}>{t('findTarget')}</Label><Input id={`target-query-${item.candidate_id}`} value={query} onChange={(event) => { setQuery(event.target.value); form.setValue('targetId', ''); form.setValue('confirmed', false); }} /></div>
      {targets.isError && <p className="error" role="alert">{targets.error.message} <Button type="button" variant="outline" onClick={() => targets.refetch()}>{t('retry')}</Button></p>}
      {query.trim().length >= 2 && <div className="field"><Label htmlFor={`target-${item.candidate_id}`}>{t('targetEntity')}</Label><Select value={targetId} onValueChange={(value) => form.setValue('targetId', value, { shouldDirty: true })}><SelectTrigger id={`target-${item.candidate_id}`}><SelectValue placeholder={t('chooseTarget')} /></SelectTrigger><SelectContent>{options.map((entity) => <SelectItem key={entity.id} value={entity.id}>{entity.name ?? t('name')} · {t(`type_${entity.type}` as 'type_person')} · {t('revision')} {entity.revision}</SelectItem>)}</SelectContent></Select></div>}
      {targets.hasNextPage && <Button type="button" variant="outline" disabled={targets.isFetchingNextPage} onClick={() => targets.fetchNextPage()}>{t('loadMore')}</Button>}
      <div className="field"><Label htmlFor={`reason-${item.candidate_id}`}>{t('reason')}</Label><Input id={`reason-${item.candidate_id}`} required maxLength={300} {...form.register('reason')} /></div>
      <label className="field"><span>{t('confirmAssign')} {selected && `${selected.name ?? t('unnamedEntity')} · ${t('revision')} ${selected.revision}`}</span><Checkbox checked={confirmed} onCheckedChange={(checked) => form.setValue('confirmed', checked === true)} /></label>
      <Button type="submit" disabled={!selected || !confirmed || assigned.isPending}>{t('assignEvidence')}</Button>
    </form>}
    {item.actionable && item.candidate_id && item.snapshot_digest && item.kind === 'relationship' && <form className="form" onSubmit={form.handleSubmit((values) => { if (values.confirmed) resolved.mutate(values); })}>
      <div className="field"><Label htmlFor={`relationship-reason-${item.candidate_id}`}>{t('reason')}</Label><Input id={`relationship-reason-${item.candidate_id}`} required maxLength={300} {...form.register('reason')} /></div>
      <label className="field"><span>{t('confirmRelationship')}</span><Checkbox checked={confirmed} onCheckedChange={(checked) => form.setValue('confirmed', checked === true)} /></label>
      <Button type="submit" disabled={!confirmed || resolved.isPending}>{t('resolveRelationship')}</Button>
    </form>}
    {errorText && <p className="error" role="alert">{errorText}</p>}
    {conflictDetails(error).map((detail, index) => <p key={index} className="error">{detail}</p>)}
    {error instanceof ApiError && error.status === 409 && <p className="text-muted-foreground">{t('conflictReload')}</p>}
    <small className="text-muted-foreground">{t('documentVersion')} {item.version_number ?? item.document_version_id}</small>
  </li>;
}
