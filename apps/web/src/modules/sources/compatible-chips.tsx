'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import Link from 'next/link';
import { Badge } from '@/components/ui/badge';
import { templateName } from '@/core/renderer-labels';
import { apiRequest } from '@/core/api';
import { dashboardKeys } from '@/modules/dashboard';
import { connectorKeys, getConnectorCatalog, listBuiltinSources, type Source } from './api';
import { addTemplateHref, compatibleTemplates, type GadgetTemplate } from './compatible-templates';

const MAX_CHIPS = 3;

/**
 * "This source can power..." line: up to three link chips (one per compatible gadget template) plus a "+N more" count.
 * Templates come from the shared `gadget-templates` query; renders nothing while loading, on error or when none match.
 * Source rows carry no shapes, so they come from the provider's catalog or built-in entry (shared caches); a manual
 * source with no provider is an upload and only feeds `news`. Each chip links to the dashboard with `addTemplate` and `source` query params.
 */
export function CompatibleChips({ source }: { source: Pick<Source, 'id' | 'provider' | 'type'> }) {
  const t = useTranslations('sources');
  const tg = useTranslations('gadgetSettings');
  const templates = useQuery({ queryKey: dashboardKeys.templates, queryFn: ({ signal }) => apiRequest<GadgetTemplate[]>('/api/v1/gadget-templates', { signal }), staleTime: 5 * 60_000 });
  const catalog = useQuery({ queryKey: connectorKeys.catalog, queryFn: ({ signal }) => getConnectorCatalog(signal), staleTime: 5 * 60_000 });
  const builtins = useQuery({ queryKey: connectorKeys.builtins, queryFn: ({ signal }) => listBuiltinSources(signal) });
  const shapes = source.provider
    ? catalog.data?.find((entry) => entry.provider_id === source.provider)?.data_shapes ?? builtins.data?.find((entry) => entry.id === source.provider)?.data_shapes
    : source.type === 'manual' ? ['news' as const] : undefined;
  const matches = compatibleTemplates({ data_shapes: shapes }, templates.data ?? []);
  if (matches.length === 0) return null;
  return <div className="flex flex-wrap items-center gap-1 text-xs text-muted-foreground" data-testid="compatible-chips">
    <span>{t('compatibleTitle')}</span>
    {matches.slice(0, MAX_CHIPS).map((template) => <Badge key={template.id} variant="outline" asChild>
      <Link href={addTemplateHref(template.id, source.id)} aria-label={t('compatibleAdd', { name: templateName(template.renderer, tg) })}>{templateName(template.renderer, tg)}</Link>
    </Badge>)}
    {matches.length > MAX_CHIPS && <span>{t('compatibleMore', { count: matches.length - MAX_CHIPS })}</span>}
  </div>;
}
