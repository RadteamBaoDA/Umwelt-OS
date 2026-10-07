'use client';

import Link from 'next/link';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { ArrowLeft } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { entityKeys, getEntity, getEntityNeighbors, getEntityTimeline, getRelationshipEvidence, listEntityEvidence } from '@/modules/knowledge/api';
import { FollowEntityButton } from './follow-entity';
import { EventDetail } from '@/modules/timeline/event-detail';

const badgeClass = 'inline-flex h-6 items-center rounded-full border border-border bg-background px-2 text-xs font-semibold text-muted-foreground';

/**
 * Read-only entity view for the shared detail dialog. Identity corrections (rename, alias, merge, split,
 * suppress) intentionally stay on the full entity page so their revision-fenced previews and confirmations are untouched.
 */
export function EntityDialogBody({ entityId, canGoBack, onBack, onOpenEntity }: {
  entityId: string;
  canGoBack: boolean;
  onBack: () => void;
  onOpenEntity: (id: string) => void;
}) {
  const t = useTranslations('entities');
  const d = useTranslations('detail');
  const display = useDisplayPreferences();
  const [relationshipId, setRelationshipId] = useState<{ entityId: string; id: string } | null>(null);
  const selectedRelationshipId = relationshipId?.entityId === entityId ? relationshipId.id : null;
  const entity = useQuery({ queryKey: entityKeys.detail(entityId), queryFn: () => getEntity(entityId) });
  const evidence = useInfiniteQuery({ queryKey: entityKeys.evidence(entityId), initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listEntityEvidence(entityId, pageParam), getNextPageParam: (page) => page.next_cursor ?? undefined });
  const timeline = useInfiniteQuery({ queryKey: entityKeys.timeline(entityId, { timezone: display.timezone }), initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => getEntityTimeline(entityId, { timezone: display.timezone }, pageParam), getNextPageParam: (page) => page.timeline.next_cursor ?? undefined });
  const neighbors = useInfiniteQuery({ queryKey: entityKeys.neighbors(entityId), initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => getEntityNeighbors(entityId, pageParam), getNextPageParam: (page) => page.next_cursor ?? undefined });
  const relEvidence = useQuery({ queryKey: ['relationships', selectedRelationshipId, 'evidence', 'dialog'], enabled: !!selectedRelationshipId, queryFn: () => getRelationshipEvidence(selectedRelationshipId!) });

  const value = entity.data;
  const documents = evidence.data?.pages.flatMap((page) => page.items) ?? [];
  const events = timeline.data?.pages.flatMap((page) => page.timeline.items) ?? [];
  const relations = neighbors.data?.pages.flatMap((page) => page.items) ?? [];
  /** Appends "+" when more pages exist so counts never overstate what is loaded. */
  const count = (n: number, more: boolean) => `${n}${more ? '+' : ''}`;
  const typeLabel = (type: string) => t.has(`type_${type}`) ? t(`type_${type}` as 'type_person') : type;
  const nameOf = (name: string | null) => name ?? t('unnamedEntity');

  const back = canGoBack ? <Button type="button" variant="ghost" className="w-fit" onClick={onBack}><ArrowLeft aria-hidden="true" className="mr-2 size-4" />{d('back')}</Button> : null;
  if (entity.isPending) return <section className="grid gap-3 p-4">{back}<div className="skeleton h-24 rounded-lg" role="status" aria-label={t('loadingEntity')} /></section>;
  if (entity.isError || !value) return <section className="grid gap-3 p-4">{back}<p className="error" role="alert">{t('unavailable')} <Button type="button" variant="outline" size="sm" onClick={() => { void entity.refetch(); }}>{t('retry')}</Button></p></section>;

  return <section className="flex h-full min-h-0 flex-col gap-4 overflow-auto p-4" aria-labelledby="entity-dialog-title">
    {back}
    <header className="grid gap-1">
      <span className={`${badgeClass} w-fit`}>{typeLabel(value.type)}</span>
      <h2 id="entity-dialog-title" className="text-xl font-semibold leading-snug">{nameOf(value.name)}</h2>
      <p className="text-xs text-muted-foreground">
        {value.first_seen_at ? `${t('firstSeen')} ${formatDateTime(value.first_seen_at, display.locale, display.timezone)}` : t('ownerManaged')} · {t('revision')} {value.revision} · {display.timezone}
      </p>
    </header>
    <Tabs defaultValue="overview">
      <TabsList>
        <TabsTrigger value="overview">{d('tabOverview')}</TabsTrigger>
        <TabsTrigger value="timeline">{d('tabTimeline')}{timeline.isSuccess ? ` ${count(events.length, !!timeline.hasNextPage)}` : ''}</TabsTrigger>
        <TabsTrigger value="documents">{d('tabDocuments')}{evidence.isSuccess ? ` ${count(documents.length, !!evidence.hasNextPage)}` : ''}</TabsTrigger>
      </TabsList>
      <TabsContent value="overview" className="grid gap-5 pt-3">
        {value.description ? <p className="text-sm leading-relaxed">{value.description}</p> : <p className="muted text-sm">{d('noDescription')}</p>}
        <div className="grid gap-2">
          <h3 className="text-sm font-semibold">{t('aliases')}</h3>
          {value.aliases.length ? <ul className="flex flex-wrap gap-2">{value.aliases.map((item) => <li key={item.id} className={badgeClass}>{item.alias} · {item.confirmed ? t('confirmed') : t('unconfirmed')}</li>)}</ul> : <p className="muted text-sm">{t('noAliases')}</p>}
        </div>
        <div className="grid gap-2">
          <h3 className="text-sm font-semibold">{t('relationships')}</h3>
          {neighbors.isError && <p className="error" role="alert">{t('relationshipUnavailable')} <Button type="button" variant="outline" size="sm" onClick={() => { void neighbors.refetch(); }}>{t('retry')}</Button></p>}
          {neighbors.isSuccess && !relations.length && <p className="muted text-sm">{t('noRelationships')}</p>}
          {!!relations.length && <div className="overflow-x-auto"><table className="w-full border-collapse text-sm">
            <caption className="sr-only">{t('relationships')}</caption>
            <thead className="sr-only"><tr><th scope="col">{d('colTo')}</th><th scope="col">{d('colBasis')}</th><th scope="col">{d('colValid')}</th><th scope="col">{d('colEvidence')}</th></tr></thead>
            <tbody>{relations.map(({ entity: other, relationship }) => <tr key={relationship.id} className="border-t border-border align-top">
              <td className="px-2 py-2"><button type="button" className="min-h-11 text-left font-semibold text-primary underline-offset-4 hover:underline" onClick={() => onOpenEntity(other.id)}>{nameOf(other.name)}</button><span className="block text-xs text-muted-foreground">{relationship.type}</span></td>
              <td className="px-2 py-2"><span className={`${badgeClass} ${relationship.origin === 'derived' ? 'border-dashed' : ''}`}>{relationship.origin === 'derived' ? d('basisInferred') : d('basisConfirmed')}</span></td>
              <td className="px-2 py-2 text-xs text-muted-foreground">{relationship.validity_precision === 'bounded' && (relationship.valid_from || relationship.valid_to) ? `${relationship.valid_from ? formatDateTime(relationship.valid_from, display.locale, display.timezone) : d('openStart')} – ${relationship.valid_to ? formatDateTime(relationship.valid_to, display.locale, display.timezone) : d('openEnd')}` : d('validityUnknown')}</td>
              <td className="px-2 py-2"><Button type="button" variant="ghost" size="sm" aria-pressed={selectedRelationshipId === relationship.id} onClick={() => setRelationshipId({ entityId, id: relationship.id })}>{d('showEvidence')}</Button></td>
            </tr>)}</tbody>
          </table></div>}
          {neighbors.hasNextPage && <Button type="button" variant="outline" className="w-fit" disabled={neighbors.isFetchingNextPage} onClick={() => { void neighbors.fetchNextPage(); }}>{t('loadRelationships')}</Button>}
          <p className="text-xs text-muted-foreground">{d('relationshipLegend')}</p>
          {selectedRelationshipId && <div className="grid gap-2" aria-live="polite">
            <h4 className="text-sm font-semibold">{t('relationshipEvidence')}</h4>
            {relEvidence.isPending && <p role="status" className="text-sm text-muted-foreground">{d('loading')}</p>}
            {relEvidence.isError && <p className="error" role="alert">{t('relationshipUnavailable')} <Button type="button" variant="outline" size="sm" onClick={() => { void relEvidence.refetch(); }}>{t('retry')}</Button></p>}
            {relEvidence.data?.items.map((item) => <article key={item.id} className="rounded-md border border-border p-3 text-sm">
              <Link className="font-semibold text-primary underline-offset-4 hover:underline" href={`/knowledge/documents/${item.document_id}?version=${item.version_number}#cited-revision`}>{item.title} · {t('documentVersion')} {item.version_number}</Link>
              <blockquote className="mt-2 border-l-2 border-border pl-3">{item.excerpt}</blockquote>
              <p className="mt-1 text-xs text-muted-foreground">{t('observed')} {formatDateTime(item.observed_at, display.locale, display.timezone)}</p>
            </article>)}
            {relEvidence.isSuccess && !relEvidence.data.items.length && <p className="muted text-sm">{d('noEvidence')}</p>}
          </div>}
        </div>
      </TabsContent>
      <TabsContent value="timeline" className="grid gap-3 pt-3">
        {timeline.isError && <p className="error" role="alert">{t('timelineUnavailable')} <Button type="button" variant="outline" size="sm" onClick={() => { void timeline.refetch(); }}>{t('retry')}</Button></p>}
        {timeline.isPending && <p role="status" className="text-sm text-muted-foreground">{d('loading')}</p>}
        {timeline.isSuccess && !events.length && <p className="muted text-sm">{d('noEvents')}</p>}
        {events.map((event) => <EventDetail key={event.id} event={event} locale={display.locale} timezone={display.timezone} entityNames={new Map([[value.id, nameOf(value.name)]])} />)}
        {timeline.hasNextPage && <Button type="button" variant="outline" className="w-fit" disabled={timeline.isFetchingNextPage} onClick={() => { void timeline.fetchNextPage(); }}>{t('loadTimeline')}</Button>}
      </TabsContent>
      <TabsContent value="documents" className="grid gap-3 pt-3">
        {evidence.isError && <p className="error" role="alert">{t('evidenceUnavailable')} <Button type="button" variant="outline" size="sm" onClick={() => { void evidence.refetch(); }}>{t('retry')}</Button></p>}
        {evidence.isPending && <p role="status" className="text-sm text-muted-foreground">{d('loading')}</p>}
        {evidence.isSuccess && !documents.length && <p className="muted text-sm">{d('noEvidence')}</p>}
        {documents.map((item) => <article key={item.id} className="rounded-md border border-border p-3 text-sm">
          <Link className="font-semibold text-primary underline-offset-4 hover:underline" href={`/knowledge/documents/${item.document_id}?version=${item.version_number}#cited-revision`}>{item.title}</Link>
          <blockquote className="mt-2 border-l-2 border-border pl-3">{item.excerpt}</blockquote>
          <p className="mt-1 text-xs text-muted-foreground">{t('observed')} {formatDateTime(item.observed_at, display.locale, display.timezone)} · {t('documentVersion')} {item.version_number}</p>
        </article>)}
        {evidence.hasNextPage && <Button type="button" variant="outline" className="w-fit" disabled={evidence.isFetchingNextPage} onClick={() => { void evidence.fetchNextPage(); }}>{t('loadEvidence')}</Button>}
      </TabsContent>
    </Tabs>
    <footer className="mt-auto flex flex-wrap items-center gap-2 border-t border-border pt-3">
      <FollowEntityButton entityId={value.id} name={value.name} />
      <Button asChild variant="outline"><Link href={`/knowledge/entities/${value.id}`}>{d('openFullEntity')}</Link></Button>
      <span className="text-xs text-muted-foreground">{d('correctionsNote')}</span>
    </footer>
  </section>;
}
