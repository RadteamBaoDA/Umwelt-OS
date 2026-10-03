'use client';

import Link from 'next/link';
import { useRef, useState } from 'react';
import { useInfiniteQuery } from '@tanstack/react-query';
import { useTheme } from 'next-themes';
import { useTranslations } from 'next-intl';
import { Background, Panel, ReactFlow, useReactFlow, type Edge, type Node, type ReactFlowInstance } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { entityKeys, getEntityNeighbors, listRelationshipHistory } from './api';

const NODE_LIMIT = 100;
const EDGE_LIMIT = 100;
const PAGE_SIZE = 50;

/** Renders keyboard-accessible pan, zoom, reset, fit, and optional expand controls for the graph. */
function GraphActions({ focusId, canExpand, isExpanding, onExpand }: { focusId: string; canExpand: boolean; isExpanding: boolean; onExpand: () => void }) {
  const t = useTranslations('entities');
  const flow = useReactFlow();
  /** Moves the graph viewport by the supplied horizontal and vertical offsets. */
  const pan = (x: number, y: number) => { const viewport = flow.getViewport(); void flow.setViewport({ ...viewport, x: viewport.x + x, y: viewport.y + y }, { duration: 0 }); };
  return <div className="flex flex-wrap gap-2" role="group" aria-label={t('controlsA11yLabel')}>
    <Button type="button" className="secondary" aria-label={t('panLeft')} onClick={() => pan(120, 0)}>← {t('panLeft')}</Button>
    <Button type="button" className="secondary" aria-label={t('panRight')} onClick={() => pan(-120, 0)}>{t('panRight')} →</Button>
    <Button type="button" className="secondary" aria-label={t('panUp')} onClick={() => pan(0, 120)}>↑ {t('panUp')}</Button>
    <Button type="button" className="secondary" aria-label={t('panDown')} onClick={() => pan(0, -120)}>{t('panDown')} ↓</Button>
    <Button type="button" className="secondary" aria-label={t('zoomInA11yLabel')} onClick={() => flow.zoomIn({ duration: 0 })}>{t('zoomIn')}</Button>
    <Button type="button" className="secondary" aria-label={t('zoomOutA11yLabel')} onClick={() => flow.zoomOut({ duration: 0 })}>{t('zoomOut')}</Button>
    <Button type="button" className="secondary" aria-label={t('resetGraph')} onClick={() => flow.fitView({ nodes: [{ id: focusId }], duration: 0 })}>{t('resetGraph')}</Button>
    <Button type="button" className="secondary" aria-label={t('fitViewA11yLabel')} onClick={() => flow.fitView({ padding: 0.2, duration: 0 })}>{t('refocusGraph')}</Button>
    {canExpand && <Button type="button" className="secondary" disabled={isExpanding} onClick={onExpand}>{t('expandGraph')}</Button>}
  </div>;
}

/** Renders an entity’s related graph and emits the selected relationship to its owner. */
export function EntityGraph({ entityId, title, selectedRelationshipId, onSelectRelationship }: {
  entityId: string;
  title: string;
  selectedRelationshipId?: string;
  onSelectRelationship: (relationshipId: string) => void;
}) {
  const t = useTranslations('entities');
  const { resolvedTheme } = useTheme();
  const flowRef = useRef<ReactFlowInstance | null>(null);
  const [validAt, setValidAt] = useState('');
  const [knowledgeAsOf, setKnowledgeAsOf] = useState('');
  const history = useInfiniteQuery({ queryKey: ['relationships', 'history', entityId, validAt, knowledgeAsOf], enabled: !!validAt || !!knowledgeAsOf, initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listRelationshipHistory(entityId, validAt || undefined, knowledgeAsOf || undefined, pageParam), getNextPageParam: (page) => page.next_cursor ?? undefined });
  const graph = useInfiniteQuery({
    queryKey: entityKeys.graphNeighbors(entityId),
    initialPageParam: { cursor: undefined as string | undefined, limit: PAGE_SIZE },
    queryFn: ({ pageParam }) => getEntityNeighbors(entityId, pageParam.cursor, pageParam.limit),
    getNextPageParam: (last, pages) => {
      const rows = pages.flatMap((page) => page.items);
      const nodes = new Set(rows.map((row) => row.entity.id).filter((id) => id !== entityId));
      const edges = new Set(rows.map((row) => row.relationship.id));
      const additionalRows = Math.min(PAGE_SIZE, NODE_LIMIT - 1 - nodes.size, EDGE_LIMIT - edges.size, EDGE_LIMIT - rows.length);
      return last.next_cursor && additionalRows > 0 ? { cursor: last.next_cursor, limit: additionalRows + 1 } : undefined;
    },
  });
  const rows = graph.data?.pages.flatMap((page) => page.items) ?? [];
  const historyRows = history.data?.pages.flatMap((page) => page.items) ?? [];
  const temporalFilter = !!validAt || !!knowledgeAsOf;
  const nodes = new Map<string, { id: string; name: string | null; type: string }>();
  nodes.set(entityId, { id: entityId, name: temporalFilter ? entityId : title, type: temporalFilter ? 'reference' : 'focus' });
  const uniqueEdges = [...new Map(rows.map((row) => [row.relationship.id, row])).values()];
  // Historical snapshots carry endpoint IDs but no historical entity names; use those IDs as reference nodes instead of current names or edges.
  const edgeRows = temporalFilter ? historyRows.map((relationship) => ({ relationship, entity: { id: relationship.target_entity_id, name: relationship.target_entity_id, type: 'reference' } })) : uniqueEdges;
  if (!temporalFilter) for (const row of rows) if (nodes.size < NODE_LIMIT) nodes.set(row.entity.id, row.entity);
  if (temporalFilter) for (const { relationship } of edgeRows) for (const id of [relationship.source_entity_id, relationship.target_entity_id]) {
    if (nodes.size < NODE_LIMIT || nodes.has(id)) nodes.set(id, { id, name: id, type: 'reference' });
  }
  const edges = edgeRows.slice(0, EDGE_LIMIT).filter(({ relationship }) => nodes.has(relationship.source_entity_id) && nodes.has(relationship.target_entity_id));
  const missingSnapshotNodes = temporalFilter ? new Set(edgeRows.flatMap(({ relationship }) => [relationship.source_entity_id, relationship.target_entity_id]).filter((id) => !nodes.has(id))).size : 0;
  const fallbackRows = temporalFilter ? edgeRows : edges;
  const reachedLimit = nodes.size >= NODE_LIMIT || edgeRows.length >= EDGE_LIMIT;
  const lastPage = graph.data?.pages.at(-1);
  const truncated = temporalFilter ? reachedLimit && !!history.hasNextPage : lastPage?.truncated === true || (reachedLimit && !!lastPage?.next_cursor) || (rows.length >= EDGE_LIMIT && !!lastPage?.next_cursor);
  /** Uses actual snapshot identifiers for historical references and localized current entity labels otherwise. */
  const entityLabel = (entity: { id: string; name: string | null; type: string }) => entity.type === 'reference' ? t('historicalEntityReference', { id: entity.id }) : entity.name ?? t(`type_${entity.type}` as 'type_person');
  const flowNodes: Node[] = [...nodes.values()].map((node, index) => { const label = entityLabel(node); return { id: node.id, position: index === 0 ? { x: 0, y: 0 } : { x: 260 * Math.cos((2 * Math.PI * (index - 1)) / Math.max(1, nodes.size - 1)), y: 180 * Math.sin((2 * Math.PI * (index - 1)) / Math.max(1, nodes.size - 1)) }, data: { label }, ariaLabel: `${t('nodeA11yTitle')}: ${label}`, type: 'default' }; });
  const flowEdges: Edge[] = edges.map(({ relationship }) => ({ id: relationship.id, source: relationship.source_entity_id, target: relationship.target_entity_id, label: relationship.type, ariaLabel: `${t('edgeA11yTitle')}: ${relationship.type}`, selectable: !temporalFilter, focusable: true, style: !temporalFilter && relationship.id === selectedRelationshipId ? { stroke: 'var(--primary)', strokeWidth: 3 } : undefined }));
  return <section aria-labelledby="entity-graph-heading"><h2 id="entity-graph-heading">{t('graph')}</h2>
    <div className="search-filters"><div className="field"><Label htmlFor="relationship-valid-at">{t('validAt')}</Label><Input id="relationship-valid-at" type="date" value={validAt} onChange={(event) => setValidAt(event.target.value)} /></div><div className="field"><Label htmlFor="relationship-knowledge-as-of">{t('knowledgeAsOf')}</Label><Input id="relationship-knowledge-as-of" type="date" value={knowledgeAsOf} onChange={(event) => setKnowledgeAsOf(event.target.value)} /></div></div>
    {!!(validAt || knowledgeAsOf) && <p className="muted" role="status">{t('relationshipTimeHelp')} {history.data?.pages[0]?.canonical_history_available === false ? t('canonicalHistoryUnavailable') : ''}</p>}
    {missingSnapshotNodes > 0 && <p className="muted" role="status">{t('snapshotNodeBound', { count: missingSnapshotNodes })}</p>}
    {history.isError && <p className="error" role="alert">{t('relationshipUnavailable')} <Button type="button" className="secondary" onClick={() => history.refetch()}>{t('retry')}</Button></p>}
    {temporalFilter && history.data?.pages.some((page) => !page.canonical_history_available || page.unavailable_relationship_ids.length > 0) && <p className="muted" role="status">{t('canonicalHistoryUnavailable')}: {[...new Set(history.data.pages.flatMap((page) => page.unavailable_relationship_ids))].join(', ') || t('none')}</p>}
    {history.hasNextPage && <Button type="button" className="secondary" disabled={history.isFetchingNextPage} onClick={() => history.fetchNextPage()}>{t('loadRelationships')}</Button>}
    <p className="muted">{nodes.size} {t('selectedSummary')} · {edges.length} {t('relationshipCount')}{truncated ? ` · ${t('graphTruncated')}` : ''}. {t('graphFallback')}</p>
    {!temporalFilter && graph.isError && <p className="error" role="alert">{t('graphRetry')} <Button className="secondary" onClick={() => graph.refetch()}>{t('retry')}</Button></p>}
    <p id="entity-graph-keyboard-help" className="muted">{t('keyboardGraphHelp')}</p>
    <div className="entity-graph" aria-label={t('viewportA11yLabel')}>
      <ReactFlow nodes={flowNodes} edges={flowEdges} fitView colorMode={resolvedTheme === 'dark' ? 'dark' : 'light'} nodesDraggable={false} nodesConnectable={false} elementsSelectable ariaLabelConfig={{ 'node.a11yDescription.default': t('nodeA11yDescription'), 'node.a11yDescription.keyboardDisabled': t('nodeKeyboardDisabled'), 'node.a11yDescription.ariaLiveMessage': ({ direction, x, y }) => t('graphLiveMessage', { direction, x, y }), 'edge.a11yDescription.default': t('edgeA11yDescription'), 'controls.ariaLabel': t('controlsA11yLabel'), 'controls.zoomIn.ariaLabel': t('zoomInA11yLabel'), 'controls.zoomOut.ariaLabel': t('zoomOutA11yLabel'), 'controls.fitView.ariaLabel': t('fitViewA11yLabel'), 'controls.interactive.ariaLabel': t('controlsA11yLabel'), 'minimap.ariaLabel': t('viewportA11yLabel'), 'handle.ariaLabel': t('handleA11yLabel') }} onInit={(instance) => { flowRef.current = instance; }} onKeyDown={(event) => {
        const flow = flowRef.current;
        if (!flow || (event.target instanceof HTMLElement && event.target.closest('.react-flow__panel'))) return;
        const viewport = flow.getViewport();
        /** Adjusts the graph viewport by the supplied pointer movement. */
        const panBy = (x: number, y: number) => { event.preventDefault(); void flow.setViewport({ ...viewport, x: viewport.x + x, y: viewport.y + y }, { duration: 0 }); };
        if (event.key === 'ArrowLeft') panBy(80, 0);
        else if (event.key === 'ArrowRight') panBy(-80, 0);
        else if (event.key === 'ArrowUp') panBy(0, 80);
        else if (event.key === 'ArrowDown') panBy(0, -80);
        else if (event.key === '+' || event.key === '=') { event.preventDefault(); void flow.zoomIn({ duration: 0 }); }
        else if (event.key === '-') { event.preventDefault(); void flow.zoomOut({ duration: 0 }); }
        else if (event.key === 'Home') { event.preventDefault(); void flow.fitView({ padding: 0.2, duration: 0 }); }
      }} onEdgeClick={(_, edge) => { if (!temporalFilter) onSelectRelationship(edge.id); }}><Background color="var(--line)" /><Panel position="top-right"><GraphActions focusId={entityId} canExpand={temporalFilter ? !!history.hasNextPage : !!graph.hasNextPage} isExpanding={temporalFilter ? history.isFetchingNextPage : graph.isFetchingNextPage} onExpand={() => { void (temporalFilter ? history.fetchNextPage() : graph.fetchNextPage()); }} /></Panel></ReactFlow>
    </div>
    <ul className="stack" aria-label={t('graphFallback')}>{fallbackRows.map(({ entity, relationship }) => <li className="card" key={relationship.id}><span aria-hidden="true">↔</span> {temporalFilter ? <><Link href={`/knowledge/entities/${relationship.source_entity_id}`}>{entityLabel({ id: relationship.source_entity_id, name: relationship.source_entity_id, type: 'reference' })}</Link> → <Link href={`/knowledge/entities/${relationship.target_entity_id}`}>{entityLabel({ id: relationship.target_entity_id, name: relationship.target_entity_id, type: 'reference' })}</Link></> : <Link href={`/knowledge/entities/${entity.id}`}>{entityLabel(entity)}</Link>} <small>{relationship.type}</small>{temporalFilter && <><p>{relationship.validity_precision === 'unknown' ? t('unknownValidity') : `${relationship.valid_from ?? t('openStart')} – ${relationship.valid_to ?? t('openEnd')}`}</p>{relationship.evidence.map((evidence) => <div key={evidence.id}><Link href={`/knowledge/documents/${evidence.document_id}?version=${evidence.version_number}#cited-revision`}>{evidence.title} · {t('documentVersion')} {evidence.version_number}</Link><small className="muted">{evidence.metadata_is_version_snapshot ? t('metadataVersionSnapshot') : t('metadataCurrentFallback')}</small><p>{t('observed')} {evidence.observed_at}</p><blockquote>{evidence.excerpt}</blockquote></div>)}</>}{!temporalFilter && <Button type="button" className="secondary" aria-pressed={selectedRelationshipId === relationship.id} onClick={() => onSelectRelationship(relationship.id)}>{selectedRelationshipId === relationship.id ? t('selectedRelationship') : t('selectRelationship')}</Button>}</li>)}</ul>
    {truncated && <p className="muted" role="status">{t('graphAtLimit', { limit: NODE_LIMIT })}</p>}
  </section>;
}
