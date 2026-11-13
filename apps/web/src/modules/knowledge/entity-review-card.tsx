'use client';

import { entityTypeLabel } from '@/core/entity-type-label';
import Link from 'next/link';
import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { useForm, useWatch } from 'react-hook-form';
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
        return Array.isArray(ids) && ids.length
          ? [
              `${key}: ${ids
                .slice(0, 5)
                .filter((id): id is string => typeof id === 'string')
                .join(', ')}`,
            ]
          : [];
      }),
    ].filter(Boolean);
  });
}

type ReviewTarget = { id: string; name: string | null; type: string; revision: number };
type ReviewForm = ReturnType<typeof useForm<Values>>;
type EntitiesTranslator = ReturnType<typeof useTranslations<'entities'>>;

/**
 * Loads merge-target candidates for an entity review item (enabled only for actionable entity reviews once the
 * search text has at least two characters).
 *
 * @param item Review candidate.
 * @param query Raw search text (trimmed before use).
 * @returns The paged query and the de-duplicated option list (first occurrence per id wins).
 */
function useReviewTargets(item: ReviewItem, query: string) {
  const targets = useInfiniteQuery({
    queryKey: ['entities', 'review-target', item.candidate_id, query, item.candidate_type],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => listEntities(pageParam, item.candidate_type ?? undefined, query.trim() || undefined),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: item.actionable && item.kind === 'entity' && query.trim().length >= 2,
  });
  const options = [...new Map(targets.data?.pages.flatMap((page) => page.items).map((entity) => [entity.id, entity]) ?? []).values()];
  return { targets, options };
}

/**
 * Declares the assign (entity) and resolve (relationship) review mutations.
 *
 * Both throw a "reload review" error when the item lacks a snapshot digest or owner generation, and send the
 * caller's CSRF token. On success they invalidate the review and entity caches (resolve also relationships).
 *
 * @param item Review candidate whose generations and digest fence the write.
 * @param selected Chosen merge target (required by assign).
 * @returns The two mutations.
 */
function useReviewMutations(item: ReviewItem, selected: ReviewTarget | undefined) {
  const t = useTranslations('entities');
  const session = useWorkspaceSession();
  const client = useQueryClient();
  /** Invalidates the owning query after a review action so displayed evidence is reloaded. */
  const invalidate = () => {
    void client.invalidateQueries({ queryKey: [...entityKeys.all, 'review'] });
    void client.invalidateQueries({ queryKey: entityKeys.all });
  };
  const assigned = useMutation({
    mutationFn: (values: Values) => {
      if (!selected || !item.candidate_id || !item.snapshot_digest || item.owner_generation === null) throw new Error(t('reloadReview'));
      return assignEntityReview(
        item.candidate_id,
        {
          result_id: item.result_id,
          snapshot_digest: item.snapshot_digest,
          expected_source_generation: item.source_generation,
          expected_owner_generation: item.owner_generation,
          target_entity_id: selected.id,
          expected_target_revision: selected.revision,
          reason: values.reason,
        },
        session.csrfToken,
      );
    },
    onSuccess: invalidate,
  });
  const resolved = useMutation({
    mutationFn: (values: Values) => {
      if (!item.candidate_id || !item.snapshot_digest || item.owner_generation === null) throw new Error(t('reloadReview'));
      return resolveRelationshipReview(
        item.candidate_id,
        {
          result_id: item.result_id,
          snapshot_digest: item.snapshot_digest,
          expected_source_generation: item.source_generation,
          expected_owner_generation: item.owner_generation,
          reason: values.reason,
        },
        session.csrfToken,
      );
    },
    onSuccess: () => {
      invalidate();
      void client.invalidateQueries({ queryKey: ['relationships'] });
    },
  });
  return { assigned, resolved };
}

/**
 * Renders one relationship endpoint (assigned entity link, ambiguous or unassigned label, membership id).
 *
 * @param p.value Endpoint state, or null/undefined to render nothing.
 */
function ReviewEndpoint({ value }: { value: ReviewItem['source_endpoint'] }) {
  const t = useTranslations('entities');
  return (
    value && (
      <span>
        {value.state === 'assigned' && value.entity_id ? (
          <Link href={`/knowledge/entities/${value.entity_id}`}>
            {value.entity_name ?? t('unnamedEntity')} · {value.entity_type ? entityTypeLabel(t, value.entity_type) : t('unknownValue')}
          </Link>
        ) : value.state === 'ambiguous' ? (
          t('endpointAmbiguous')
        ) : (
          t('endpointUnassigned')
        )}
        {value.membership_id && (
          <small>
            {' '}
            · {t('membership')} {value.membership_id}
          </small>
        )}
      </span>
    )
  );
}

/**
 * Evidence block of a review card: the evidence list, else a link to the document revision, else a notice.
 *
 * @param p.item Review candidate.
 */
function ReviewEvidence({ item }: { item: ReviewItem }) {
  const t = useTranslations('entities');
  const display = useDisplayPreferences();
  if (item.evidence.length > 0)
    return (
      <ul className="flex flex-col gap-3" aria-label={t('evidence')}>
        {item.evidence.map((evidence) => (
          <li key={`${evidence.document_version_id}:${evidence.chunk_id}`}>
            <small className="text-muted-foreground">
              {evidence.source_name} · {t('observed')} {formatDateTime(evidence.observed_at, display.locale, display.timezone)}
            </small>
            <p>
              <Link href={`/knowledge/documents/${evidence.document_id}?version=${evidence.version_number}#cited-revision`}>
                {evidence.title} · {t('documentVersion')} {evidence.version_number}
              </Link>
            </p>
            <small className="text-muted-foreground">
              {evidence.metadata_is_version_snapshot ? t('metadataVersionSnapshot') : t('metadataCurrentFallback')}
            </small>
            <blockquote>{evidence.excerpt}</blockquote>
          </li>
        ))}
      </ul>
    );
  if (item.document_id && item.version_number)
    return (
      <p>
        <Link href={`/knowledge/documents/${item.document_id}?version=${item.version_number}#cited-revision`}>
          {t('open.documentRevision')} · {item.source_name}
        </Link>{' '}
        · {t('evidenceUnavailable')}
      </p>
    );
  return <p className="text-muted-foreground">{t('evidenceUnavailable')}</p>;
}

/**
 * Entity-assignment form: target search, target select, reason and explicit confirmation.
 *
 * Submitting only assigns when the confirmation box is ticked and a target is selected; changing the search text
 * clears the chosen target and the confirmation.
 *
 * @param p Review item, shared form, search state, targets query/options, selected target, confirmation flag and
 * the assign mutation.
 */
function EntityAssignForm(p: {
  item: ReviewItem;
  form: ReviewForm;
  query: string;
  setQuery: (value: string) => void;
  targets: ReturnType<typeof useReviewTargets>['targets'];
  options: ReviewTarget[];
  selected: ReviewTarget | undefined;
  targetId: string;
  confirmed: boolean;
  assigned: ReturnType<typeof useReviewMutations>['assigned'];
}) {
  const t: EntitiesTranslator = useTranslations('entities');
  const { item, form, query, setQuery, targets, options, selected, targetId, confirmed, assigned } = p;
  return (
    <form
      className="form"
      onSubmit={form.handleSubmit((values) => {
        if (values.confirmed && selected) assigned.mutate(values);
      })}
    >
      <div className="field">
        <Label htmlFor={`target-query-${item.candidate_id}`}>{t('findTarget')}</Label>
        <Input
          id={`target-query-${item.candidate_id}`}
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            form.setValue('targetId', '');
            form.setValue('confirmed', false);
          }}
        />
      </div>
      {targets.isError && (
        <p className="error" role="alert">
          {targets.error.message}{' '}
          <Button type="button" variant="outline" onClick={() => targets.refetch()}>
            {t('retry')}
          </Button>
        </p>
      )}
      {query.trim().length >= 2 && (
        <div className="field">
          <Label htmlFor={`target-${item.candidate_id}`}>{t('targetEntity')}</Label>
          <Select value={targetId} onValueChange={(value) => form.setValue('targetId', value, { shouldDirty: true })}>
            <SelectTrigger id={`target-${item.candidate_id}`}>
              <SelectValue placeholder={t('chooseTarget')} />
            </SelectTrigger>
            <SelectContent>
              {options.map((entity) => (
                <SelectItem key={entity.id} value={entity.id}>
                  {entity.name ?? t('name')} · {entityTypeLabel(t, entity.type)} · {t('revision')} {entity.revision}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}
      {targets.hasNextPage && (
        <Button type="button" variant="outline" disabled={targets.isFetchingNextPage} onClick={() => targets.fetchNextPage()}>
          {t('load.more')}
        </Button>
      )}
      <div className="field">
        <Label htmlFor={`reason-${item.candidate_id}`}>{t('reason')}</Label>
        <Input id={`reason-${item.candidate_id}`} required maxLength={300} {...form.register('reason')} />
      </div>
      <label className="field">
        <span>
          {t('confirm.assign')} {selected && `${selected.name ?? t('unnamedEntity')} · ${t('revision')} ${selected.revision}`}
        </span>
        <Checkbox checked={confirmed} onCheckedChange={(checked) => form.setValue('confirmed', checked === true)} />
      </label>
      <Button type="submit" disabled={!selected || !confirmed || assigned.isPending}>
        {t('assignEvidence')}
      </Button>
    </form>
  );
}

/**
 * Relationship-resolution form: reason plus explicit confirmation; submits only when confirmed.
 *
 * @param p.item Review candidate (its id keys the field ids).
 * @param p.form Shared review form.
 * @param p.confirmed Current confirmation flag.
 * @param p.resolved Resolve mutation.
 */
function RelationshipResolveForm(p: {
  item: ReviewItem;
  form: ReviewForm;
  confirmed: boolean;
  resolved: ReturnType<typeof useReviewMutations>['resolved'];
}) {
  const t = useTranslations('entities');
  const { item, form, confirmed, resolved } = p;
  return (
    <form
      className="form"
      onSubmit={form.handleSubmit((values) => {
        if (values.confirmed) resolved.mutate(values);
      })}
    >
      <div className="field">
        <Label htmlFor={`relationship-reason-${item.candidate_id}`}>{t('reason')}</Label>
        <Input id={`relationship-reason-${item.candidate_id}`} required maxLength={300} {...form.register('reason')} />
      </div>
      <label className="field">
        <span>{t('confirm.relationship')}</span>
        <Checkbox checked={confirmed} onCheckedChange={(checked) => form.setValue('confirmed', checked === true)} />
      </label>
      <Button type="submit" disabled={!confirmed || resolved.isPending}>
        {t('resolveRelationship')}
      </Button>
    </form>
  );
}

/**
 * Header of a review card: candidate name, reason, kind/status line, relationship type and endpoints, and
 * possible matching entities.
 *
 * @param p.item Review candidate.
 */
function ReviewSummary({ item }: { item: ReviewItem }) {
  const t = useTranslations('entities');
  const statuses: Record<string, string> = {
    pending: t('pending'),
    running: t('running'),
    succeeded: t('succeeded'),
    blocked: t('blocked'),
    failed: t('failed'),
  };
  return (
    <>
      <strong>{item.candidate_name}</strong>
      <p>{item.reason}</p>
      <small className="text-muted-foreground">
        {item.kind === 'entity' ? t('entity.candidate') : t('relationship.candidate')} · {statuses[item.status] ?? item.status}
        {!item.actionable ? ` · ${t('displayOnly')}` : ''}
      </small>
      {item.kind === 'relationship' && item.relationship_type && (
        <p>
          {t('relationship.type')}: {item.relationship_type}
        </p>
      )}
      {item.kind === 'relationship' && (
        <p>
          {t('sourceEndpoint')}: <ReviewEndpoint value={item.source_endpoint} />
          <br />
          {t('targetEndpoint')}: <ReviewEndpoint value={item.target_endpoint} />
        </p>
      )}
      {item.possible_entity_ids.length > 0 && (
        <p>
          {item.possible_entity_ids.map((id) => (
            <Link key={id} href={`/knowledge/entities/${id}`}>
              {id.slice(0, 8)}{' '}
            </Link>
          ))}
        </p>
      )}
    </>
  );
}

/**
 * Error block of a review card: message, up to five 409 conflict detail lines and a reload hint on conflicts.
 *
 * @param p.error Mutation error of the active action, if any.
 */
function ReviewErrors({ error }: { error: unknown }) {
  const t = useTranslations('entities');
  const errorText =
    error instanceof ApiError ? `${error.message}${error.code ? ` (${error.code})` : ''}` : (error as Error | null)?.message;
  return (
    <>
      {errorText && (
        <p className="error" role="alert">
          {errorText}
        </p>
      )}
      {conflictDetails(error).map((detail, index) => (
        <p key={index} className="error">
          {detail}
        </p>
      ))}
      {error instanceof ApiError && error.status === 409 && <p className="text-muted-foreground">{t('conflictReload')}</p>}
    </>
  );
}

/**
 * Renders an entity or relationship review candidate and submits its supported resolution actions.
 *
 * The confirmation checkbox resets whenever the search text, target, reason or selected target revision changes,
 * so a confirmation always refers to exactly what is displayed. Write errors (including 409 conflict details) are
 * shown below the forms.
 *
 * @param props.item Review candidate; only actionable items with a snapshot digest show a form.
 */
export function EntityReviewCard({ item }: { item: ReviewItem }) {
  const t = useTranslations('entities');
  const [query, setQuery] = useState('');
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { targetId: '', reason: 'owner_review', confirmed: false },
  });
  const targetId = useWatch({ control: form.control, name: 'targetId' });
  const reason = useWatch({ control: form.control, name: 'reason' });
  const confirmed = useWatch({ control: form.control, name: 'confirmed' });
  const { targets, options } = useReviewTargets(item, query);
  const selected = options.find((entity) => entity.id === targetId);
  useEffect(() => {
    form.setValue('confirmed', false);
  }, [form, query, targetId, reason, selected?.id, selected?.name, selected?.revision]);
  const { assigned, resolved } = useReviewMutations(item, selected);
  const error = item.kind === 'entity' ? assigned.error : resolved.error;
  const formReady = item.actionable && item.candidate_id && item.snapshot_digest;

  return (
    <li className="rounded-lg border bg-card p-4 text-card-foreground">
      <ReviewSummary item={item} />
      <ReviewEvidence item={item} />
      {formReady && item.kind === 'entity' && (
        <EntityAssignForm
          item={item}
          form={form}
          query={query}
          setQuery={setQuery}
          targets={targets}
          options={options}
          selected={selected}
          targetId={targetId}
          confirmed={confirmed}
          assigned={assigned}
        />
      )}
      {formReady && item.kind === 'relationship' && (
        <RelationshipResolveForm item={item} form={form} confirmed={confirmed} resolved={resolved} />
      )}
      <ReviewErrors error={error} />
      <small className="text-muted-foreground">
        {t('documentVersion')} {item.version_number ?? item.document_version_id}
      </small>
    </li>
  );
}
