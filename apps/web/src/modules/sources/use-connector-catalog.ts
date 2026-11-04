'use client';

import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import type { DataShape } from '@/core/data-shapes';
import { connectorKeys, getConnectorCatalog, getProviderTerms, listBuiltinSources, type ConnectorCatalogEntry } from './api';
import type { SourcesTranslator } from './connector-editor-context';
import { isPreset, PROVIDER_GROUP_KEY, PROVIDER_GROUP_ORDER, type ProviderGroup } from './connector-editor-model';
import { buildProviderGroups } from './provider-picker';
import { matchesShapes } from './shape-filter';

/** Message key of the per-provider history note; providers without an entry fall back to the catalog's own description. */
const HISTORY_KEYS = {
  youtube: 'youtubeHistory',
  arxiv: 'arxivHistory',
  huggingface: 'huggingfaceHistory',
  github: 'githubAppHistory',
  github_releases: 'githubReleasesHistory',
  telegram: 'telegramHistory',
} as const;

/** Maps a picker group id to its message key; `nokey` is the leading group of providers needing no credentials. */
function providerGroupTitleKey(group: string) {
  return group === 'nokey' ? 'providerGroupNoKey' : PROVIDER_GROUP_KEY[group as ProviderGroup];
}

/**
 * Catalog data of the connector editor: the provider catalog (with built-ins and shape-filtered entries removed from the add
 * grid), the selected provider's entry, preset terms gating, built-in review state, the grouped picker model and its search.
 */
export function useConnectorCatalog(provider: string, sourceId: string, shapeFilter: DataShape[] | undefined, t: SourcesTranslator) {
  const [providerQuery, setProviderQuery] = useState('');
  const catalogQuery = useQuery({ queryKey: connectorKeys.catalog, queryFn: ({ signal }) => getConnectorCatalog(signal) });
  const currentEntry = (catalogQuery.data ?? []).find((entry) => entry.provider_id === provider);
  const termsGated = isPreset(currentEntry) && Boolean(sourceId);
  const termsQuery = useQuery({
    queryKey: connectorKeys.terms(sourceId),
    queryFn: ({ signal }) => getProviderTerms(sourceId, signal),
    enabled: termsGated,
  });
  const termsBlocked = isPreset(currentEntry) && (!termsGated || termsQuery.data?.eligible !== true);
  // Built-ins are switched on from the Built-in sources section, not created as tiles here.
  // Also loaded in edit mode: a built-in's review-class terms are settled by its Resume action, not this form.
  const builtinQuery = useQuery({ queryKey: connectorKeys.builtins, queryFn: ({ signal }) => listBuiltinSources(signal) });
  const builtinIds = useMemo(() => new Set((builtinQuery.data ?? []).map((item) => item.id)), [builtinQuery.data]);
  // Built-in edit mode: the owner options the server lists for this preset (only once its source exists).
  const builtinEntry = sourceId ? (builtinQuery.data ?? []).find((item) => item.id === provider) : undefined;
  const builtinReview = (builtinQuery.data ?? []).some((item) => item.id === provider && item.eligibility === 'review');
  // One source per built-in: a provider whose built-in already has a source cannot be created again from the catalog.
  const addedBuiltinIds = useMemo(
    () => new Set((builtinQuery.data ?? []).filter((item) => item.source_id !== null).map((item) => item.id)),
    [builtinQuery.data],
  );
  // Built-ins are dropped from the add grid (`catalog`) but edit mode still needs their guide and terms panel.
  const catalog = useMemo(
    () => (catalogQuery.data ?? []).filter((entry) => !builtinIds.has(entry.provider_id) && matchesShapes(entry.data_shapes, shapeFilter)),
    [catalogQuery.data, builtinIds, shapeFilter],
  );
  const historyKey = Object.hasOwn(HISTORY_KEYS, provider) ? HISTORY_KEYS[provider as keyof typeof HISTORY_KEYS] : undefined;
  const historyDescription = historyKey ? t(historyKey) : currentEntry?.history_description;
  const providerGroups = buildProviderGroups(
    catalog.filter((entry) => entry.kind !== 'group'),
    PROVIDER_GROUP_ORDER,
    (category) => t(PROVIDER_GROUP_KEY[category as ProviderGroup] ?? 'providerGroupOther'),
    providerQuery,
  );
  /** Formats provider availability and unsupported-operation information for the connector catalog. */
  const catalogStatus = (entry: ConnectorCatalogEntry) =>
    entry.availability === 'available' || entry.availability === 'implemented'
      ? ''
      : entry.availability === 'requires_credentials'
        ? t('needsCredentials')
        : entry.availability === 'planned'
          ? t('planned')
          : t('unavailable');
  return {
    providerQuery,
    setProviderQuery,
    catalogQuery,
    currentEntry,
    termsBlocked,
    builtinEntry,
    builtinReview,
    addedBuiltinIds,
    catalog,
    historyDescription,
    providerGroups,
    providerGroupTitleKey,
    catalogStatus,
  };
}
