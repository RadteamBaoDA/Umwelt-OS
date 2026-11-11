'use client';

import { useQuery } from '@tanstack/react-query';
import { ExternalLink, RotateCw, Search, Users } from 'lucide-react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import React, { useMemo, useState } from 'react';
import { Input } from '@/components/ui/input';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { entityKeys, listEntities, type Entity } from '@/modules/knowledge';
import type { GadgetInstance } from '../api';
import { BareButton } from '@/components/bare-button';

/** Props for the EntityGadget component. */
export interface EntityGadgetProps {
  /** Gadget instance configuration projection. */
  instance: GadgetInstance;
  /** Optional pre-loaded entity items for preview. */
  initialEntities?: Entity[];
}

/** One compact entity row: linked name, type with aliases (or revision), optional description and last-seen time. */
function EntityRow({ entity }: { entity: Entity }) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const lastSeen = entity.last_seen_at ? formatDateTime(entity.last_seen_at, display.locale, display.timezone) : null;
  return (
    <li className="flex items-baseline justify-between gap-3 py-2">
      <div className="min-w-0">
        <Link href={`/knowledge/entities/${entity.id}`} className="block truncate text-sm font-semibold text-foreground hover:underline">
          {entity.name || entity.canonical_name || t('entityUnnamed')}
        </Link>
        <p className="truncate text-xs text-muted-foreground">
          {[
            entity.type,
            entity.aliases && entity.aliases.length > 0
              ? t('entityAliases', { aliases: entity.aliases.map((a) => a.alias).join(', ') })
              : t('entityRevision', { revision: entity.revision }),
          ].join(' · ')}
        </p>
        {entity.description && <p className="line-clamp-1 text-xs text-muted-foreground">{entity.description}</p>}
      </div>
      {lastSeen && (
        <span className="shrink-0 text-xs tabular-nums text-muted-foreground" title={t('entitySeen', { time: lastSeen })}>
          {lastSeen}
        </span>
      )}
    </li>
  );
}

/**
 * Standard Entity Spotlight gadget template.
 * Highlights tracked persons, companies, and projects extracted by the Phase 4 Entity Knowledge API,
 * including aliases, provenance metadata, and direct navigation into knowledge graphs.
 * The renderer root is a bounded scroll fallback so compact cards never clip its controls.
 *
 * @param props Gadget instance configuration and optional entity items.
 * @returns Accessible entity spotlight gadget component.
 */
export function EntityGadget({ instance, initialEntities }: EntityGadgetProps) {
  const ts = useTranslations('uiSweep');

  const definition = instance.definition;
  const [filterQuery, setFilterQuery] = useState<string>('');

  // Extract type filter from gadget configuration
  const targetType = useMemo(() => {
    const fromFilters = definition.filters?.keywords?.[0];
    if (fromFilters && ['person', 'company', 'project', 'organization'].includes(fromFilters)) {
      return fromFilters;
    }
    return undefined;
  }, [definition.filters]);

  // Query entities from Phase 4 API
  const entitiesQuery = useQuery({
    queryKey: entityKeys.list(targetType, filterQuery),
    queryFn: async () => {
      const page = await listEntities(undefined, targetType, filterQuery || undefined);
      return page.items.slice(0, definition.filters?.limit ?? 10);
    },
    staleTime: 60_000,
  });

  const entities = entitiesQuery.data ?? initialEntities ?? [];

  return (
    <div className="flex flex-col h-full bg-card text-card-foreground p-3 space-y-3 overflow-y-auto overflow-x-hidden">
      {/* Header with search filter and quick link to all entities */}
      <div className="flex items-center gap-2">
        <div className="relative flex-1">
          <Search className="w-3.5 h-3.5 absolute left-2 top-2 text-muted-foreground" />
          <Input
            type="search"
            value={filterQuery}
            onChange={(e) => setFilterQuery(e.target.value)}
            placeholder={ts('entFilterPlaceholder')}
            className="h-7 text-xs pl-7 bg-muted/20"
          />
        </div>
        <BareButton
          type="button"
          onClick={() => entitiesQuery.refetch()}
          disabled={entitiesQuery.isFetching}
          className="p-1 rounded text-muted-foreground hover:text-foreground transition-colors"
          title={ts('entRefresh')}
          aria-label={ts('entRefresh')}
        >
          <RotateCw className={`w-3.5 h-3.5 ${entitiesQuery.isFetching ? 'animate-spin' : ''}`} />
        </BareButton>
        <Link href="/knowledge/entities" className="p-1 text-muted-foreground hover:text-foreground" title={ts('entBrowse')}>
          <ExternalLink className="w-3.5 h-3.5" />
        </Link>
      </div>

      {/* Loading Skeleton */}
      {entitiesQuery.isLoading && entities.length === 0 && (
        <div className="flex-1 space-y-2.5 p-1 motion-safe:animate-pulse">
          <div className="h-14 bg-rule rounded-lg" />
          <div className="h-14 bg-rule rounded-lg" />
        </div>
      )}

      {/* Empty State */}
      {!entitiesQuery.isLoading && entities.length === 0 && (
        <div className="flex-1 flex flex-col items-center justify-center p-6 text-center text-muted-foreground">
          <Users className="w-8 h-8 mb-2 opacity-50" />
          <p className="text-xs font-semibold text-foreground mb-1">{ts('entNone')}</p>
          <p className="text-[11px] max-w-xs text-muted-foreground">{ts('entNoneHint')}</p>
        </div>
      )}

      {/* Compact rows: name (14px) left, last-seen right and tabular; type and aliases stay muted. */}
      {entities.length > 0 && (
        <ul className="min-h-0 flex-1 divide-y divide-border/60 overflow-y-auto pr-0.5">
          {entities.map((entity) => (
            <EntityRow key={entity.id} entity={entity} />
          ))}
        </ul>
      )}
    </div>
  );
}
