'use client';

import Link from 'next/link';
import { Button } from '@/components/ui/button';
import { buildEntityViewModel } from './entity-detail-model';
import { EntityDetailView } from './entity-detail-view';
import { useEntityDetailController } from './use-entity-detail';

/** Loads entity evidence and history; administrator viewers can inspect it without identity or evidence writes. */
export function EntityDetail() {
  const c = useEntityDetailController();
  const { t, entityId, router, entity, documentReadyId } = c;
  if (documentReadyId !== entityId || entity.isPending) return <section className="rounded-lg border bg-card p-6"><div className="skeleton" aria-label={t('loadingEntity')} /></section>;
  if (entity.isError || !entity.data) return <section className="rounded-lg border bg-card p-6"><p className="error" role="alert">{t('unavailable')} <Button variant="outline" onClick={() => entity.refetch()}>{t('retry')}</Button></p></section>;
  const value = entity.data;
  if (value.id !== entityId) return <section className="rounded-lg border bg-card p-6"><h1>{t('canonicalEntityChanged')}</h1><p role="status">{t('canonicalEntityDetails', { id: value.id, revision: value.revision })}</p><Button onClick={() => router.replace(`/knowledge/entities/${value.id}`)}>{t('reviewCanonicalEntity')}</Button><Link href="/knowledge/entities">{t('cancel')}</Link></section>;
  return <EntityDetailView vm={buildEntityViewModel(c, value)} />;
}
