'use client';

import { useQuery } from '@tanstack/react-query';
import {
  Building2,
  ExternalLink,
  FolderGit2,
  RotateCw,
  Search,
  Sparkles,
  Tag,
  User,
  Users,
} from 'lucide-react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import React, { useMemo, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { entityKeys, listEntities, type Entity } from '@/modules/knowledge/api';
import type { GadgetInstance } from '../api';

/** Props for the EntityGadget component. */
export interface EntityGadgetProps {
  /** Gadget instance configuration projection. */
  instance: GadgetInstance;
  /** Optional pre-loaded entity items for preview. */
  initialEntities?: Entity[];
}

/**
 * Maps an entity type to an appropriate semantic Lucide icon.
 *
 * @param type Entity classification string.
 * @returns React icon element.
 */
export function getEntityIcon(type: string): React.ReactElement {
  switch (type.toLowerCase()) {
    case 'person':
      return <User className="w-3.5 h-3.5" />;
    case 'company':
    case 'organization':
      return <Building2 className="w-3.5 h-3.5" />;
    case 'project':
    case 'repository':
      return <FolderGit2 className="w-3.5 h-3.5" />;
    default:
      return <Users className="w-3.5 h-3.5" />;
  }
}

/**
 * Standard Entity Spotlight gadget template.
 * Highlights tracked persons, companies, and projects extracted by the Phase 4 Entity Knowledge API,
 * including aliases, provenance metadata, and direct navigation into knowledge graphs.
 *
 * @param props Gadget instance configuration and optional entity items.
 * @returns Accessible entity spotlight gadget component.
 */
export function EntityGadget({ instance, initialEntities }: EntityGadgetProps) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();

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
    <div className="flex flex-col h-full bg-card text-card-foreground p-3 space-y-3 overflow-hidden">
      {/* Header with search filter and quick link to all entities */}
      <div className="flex items-center gap-2 border-b border-border pb-2">
        <div className="relative flex-1">
          <Search className="w-3.5 h-3.5 absolute left-2 top-2 text-muted-foreground" />
          <Input
            type="search"
            value={filterQuery}
            onChange={(e) => setFilterQuery(e.target.value)}
            placeholder="Filter entities..."
            className="h-7 text-xs pl-7 bg-muted/20"
          />
        </div>
        <button
          type="button"
          onClick={() => entitiesQuery.refetch()}
          disabled={entitiesQuery.isFetching}
          className="p-1 rounded text-muted-foreground hover:text-foreground transition-colors"
          title="Refresh entities"
          aria-label="Refresh entities"
        >
          <RotateCw className={`w-3.5 h-3.5 ${entitiesQuery.isFetching ? 'animate-spin' : ''}`} />
        </button>
        <Link
          href="/knowledge/entities"
          className="p-1 text-muted-foreground hover:text-foreground"
          title="Browse all entities"
        >
          <ExternalLink className="w-3.5 h-3.5" />
        </Link>
      </div>

      {/* Loading Skeleton */}
      {entitiesQuery.isLoading && entities.length === 0 && (
        <div className="flex-1 space-y-2.5 p-1 animate-pulse">
          <div className="h-14 bg-muted/20 rounded-lg" />
          <div className="h-14 bg-muted/20 rounded-lg" />
        </div>
      )}

      {/* Empty State */}
      {!entitiesQuery.isLoading && entities.length === 0 && (
        <div className="flex-1 flex flex-col items-center justify-center p-6 text-center text-muted-foreground">
          <Users className="w-8 h-8 mb-2 opacity-50" />
          <p className="text-xs font-semibold text-foreground mb-1">No entities found</p>
          <p className="text-[11px] max-w-xs text-muted-foreground">
            Extracted people, companies, and projects will be spotlit here as documents are ingested.
          </p>
        </div>
      )}

      {/* Entities List / Cards */}
      {entities.length > 0 && (
        <div className="flex-1 overflow-y-auto space-y-2 min-h-0 pr-0.5">
          {entities.map((entity) => {
            const lastSeen = entity.last_seen_at
              ? formatDateTime(entity.last_seen_at, display.locale, display.timezone)
              : null;

            return (
              <article
                key={entity.id}
                className="p-2.5 rounded-lg border border-border/80 bg-card hover:bg-muted/15 transition-colors space-y-1.5"
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-center gap-1.5 min-w-0">
                    <span className="p-1 rounded bg-muted/30 text-primary">
                      {getEntityIcon(entity.type)}
                    </span>
                    <Link
                      href={`/knowledge/entities/${entity.id}`}
                      className="font-semibold text-xs text-foreground hover:underline truncate"
                    >
                      {entity.name || entity.canonical_name || 'Unnamed Entity'}
                    </Link>
                  </div>

                  <span className="px-1.5 py-0.2 rounded text-[10px] font-bold uppercase tracking-wider bg-muted/40 text-muted-foreground shrink-0">
                    {entity.type}
                  </span>
                </div>

                {entity.description && (
                  <p className="text-[11px] text-muted-foreground line-clamp-2 leading-relaxed">
                    {entity.description}
                  </p>
                )}

                {/* Aliases & Metadata bar */}
                <div className="flex items-center justify-between text-[10px] text-muted-foreground pt-1 border-t border-border/50">
                  <div className="flex items-center gap-1 overflow-hidden">
                    {entity.aliases && entity.aliases.length > 0 ? (
                      <span className="truncate">
                        Aliases: {entity.aliases.map((a) => a.alias).join(', ')}
                      </span>
                    ) : (
                      <span>Revision {entity.revision}</span>
                    )}
                  </div>
                  {lastSeen && <span className="font-mono shrink-0">Seen {lastSeen}</span>}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
