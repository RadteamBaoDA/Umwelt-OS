'use client';

import { entityTypeLabel, type EntitiesT } from '@/core/entity-type-label';
import Link from 'next/link';
import { useRef, useState, type KeyboardEvent } from 'react';
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
function GraphActions({
  focusId,
  canExpand,
  isExpanding,
  onExpand,
}: {
  focusId: string;
  canExpand: boolean;
  isExpanding: boolean;
  onExpand: () => void;
}) {
  const t = useTranslations('entities');
  const flow = useReactFlow();
  /** Moves the graph viewport by the supplied horizontal and vertical offsets. */
  const pan = (x: number, y: number) => {
    const viewport = flow.getViewport();
    void flow.setViewport({ ...viewport, x: viewport.x + x, y: viewport.y + y }, { duration: 0 });
  };
  return (
    <div className="flex flex-wrap gap-2" role="group" aria-label={t('controlsA11yLabel')}>
      <Button type="button" variant="outline" aria-label={t('pan.left')} onClick={() => pan(120, 0)}>
        ← {t('pan.left')}
      </Button>
      <Button type="button" variant="outline" aria-label={t('pan.right')} onClick={() => pan(-120, 0)}>
        {t('pan.right')} →
      </Button>
      <Button type="button" variant="outline" aria-label={t('pan.up')} onClick={() => pan(0, 120)}>
        ↑ {t('pan.up')}
      </Button>
      <Button type="button" variant="outline" aria-label={t('pan.down')} onClick={() => pan(0, -120)}>
        {t('pan.down')} ↓
      </Button>
      <Button type="button" variant="outline" aria-label={t('zoom.inA11yLabel')} onClick={() => flow.zoomIn({ duration: 0 })}>
        {t('zoom.in')}
      </Button>
      <Button type="button" variant="outline" aria-label={t('zoom.outA11yLabel')} onClick={() => flow.zoomOut({ duration: 0 })}>
        {t('zoom.out')}
      </Button>
      <Button
        type="button"
        variant="outline"
        aria-label={t('resetGraph')}
        onClick={() => flow.fitView({ nodes: [{ id: focusId }], duration: 0 })}
      >
        {t('resetGraph')}
      </Button>
      <Button
        type="button"
        variant="outline"
        aria-label={t('fitViewA11yLabel')}
        onClick={() => flow.fitView({ padding: 0.2, duration: 0 })}
      >
        {t('refocusGraph')}
      </Button>
      {canExpand && (
        <Button type="button" variant="outline" disabled={isExpanding} onClick={onExpand}>
          {t('expandGraph')}
        </Button>
      )}
    </div>
  );
}

type GraphEntity = { id: string; name: string | null; type: string };
type Translator = EntitiesT;

/**
 * Loads the temporal relationship history (enabled only while a date filter is set) and the paged neighbor graph.
 *
 * @param entityId Focus entity id.
 * @param validAt Valid-at date filter or empty string.
 * @param knowledgeAsOf Knowledge-as-of date filter or empty string.
 * @returns The two infinite queries. Graph paging stops at the node/edge limits.
 */
function useGraphQueries(entityId: string, validAt: string, knowledgeAsOf: string) {
  const history = useInfiniteQuery({
    queryKey: ['relationships', 'history', entityId, validAt, knowledgeAsOf],
    enabled: !!validAt || !!knowledgeAsOf,
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => listRelationshipHistory(entityId, validAt || undefined, knowledgeAsOf || undefined, pageParam),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
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
  return { history, graph };
}

type GraphQueries = ReturnType<typeof useGraphQueries>;

/**
 * Collects the capped node map: the focus node first, then neighbor entities (current mode) or edge endpoints
 * as reference nodes (temporal mode, where only IDs are known).
 *
 * @param entityId Focus entity id.
 * @param title Display title of the focus entity (current mode).
 * @param temporalFilter True when a date filter is active.
 * @param rows Neighbor rows from the current graph query.
 * @param edgeRows Edge rows chosen for the current mode.
 * @returns Insertion-ordered node map holding at most NODE_LIMIT nodes (existing ids may still be refreshed). Pure.
 */
function collectGraphNodes(
  entityId: string,
  title: string,
  temporalFilter: boolean,
  rows: { entity: GraphEntity }[],
  edgeRows: { relationship: { source_entity_id: string; target_entity_id: string } }[],
) {
  const nodes = new Map<string, GraphEntity>();
  nodes.set(entityId, { id: entityId, name: temporalFilter ? entityId : title, type: temporalFilter ? 'reference' : 'focus' });
  if (!temporalFilter) for (const row of rows) if (nodes.size < NODE_LIMIT) nodes.set(row.entity.id, row.entity);
  if (temporalFilter)
    for (const { relationship } of edgeRows)
      for (const id of [relationship.source_entity_id, relationship.target_entity_id]) {
        if (nodes.size < NODE_LIMIT || nodes.has(id)) nodes.set(id, { id, name: id, type: 'reference' });
      }
  return nodes;
}

/**
 * Whether the current-mode graph was cut off by the server or by the local node/edge limits.
 *
 * @param lastPage Last loaded neighbor page, if any.
 * @param reachedLimit True when node or edge counts hit their limits.
 * @param rowCount Number of loaded neighbor rows.
 * @returns True when more data exists beyond what is shown. Pure.
 */
function isCurrentGraphTruncated(
  lastPage: { truncated?: boolean; next_cursor?: string | null } | undefined,
  reachedLimit: boolean,
  rowCount: number,
): boolean {
  return lastPage?.truncated === true || (reachedLimit && !!lastPage?.next_cursor) || (rowCount >= EDGE_LIMIT && !!lastPage?.next_cursor);
}

/**
 * Derives the bounded node/edge sets shown by the graph and its text fallback.
 *
 * Historical snapshots carry endpoint IDs but no historical entity names, so in temporal mode those IDs become
 * reference nodes instead of current names. Nodes are capped at NODE_LIMIT and edges at EDGE_LIMIT.
 *
 * @param p.entityId Focus entity id.
 * @param p.title Display title of the focus entity (current mode only).
 * @param p.temporalFilter True when a date filter is active.
 * @param p.graph Neighbor graph query.
 * @param p.history History query.
 * @returns Nodes map, drawable edges, fallback rows, count of endpoints missing from the capped snapshot, and
 * whether the result was truncated by limits. Pure.
 */
function buildGraphModel(p: {
  entityId: string;
  title: string;
  temporalFilter: boolean;
  graph: GraphQueries['graph'];
  history: GraphQueries['history'];
}) {
  const { entityId, title, temporalFilter, graph, history } = p;
  const rows = graph.data?.pages.flatMap((page) => page.items) ?? [];
  const historyRows = history.data?.pages.flatMap((page) => page.items) ?? [];
  const edgeRows = temporalFilter
    ? historyRows.map((relationship) => ({
        relationship,
        entity: { id: relationship.target_entity_id, name: relationship.target_entity_id, type: 'reference' },
      }))
    : [...new Map(rows.map((row) => [row.relationship.id, row])).values()];
  const nodes = collectGraphNodes(entityId, title, temporalFilter, rows, edgeRows);
  const edges = edgeRows
    .slice(0, EDGE_LIMIT)
    .filter(({ relationship }) => nodes.has(relationship.source_entity_id) && nodes.has(relationship.target_entity_id));
  const missingSnapshotNodes = temporalFilter
    ? new Set(
        edgeRows
          .flatMap(({ relationship }) => [relationship.source_entity_id, relationship.target_entity_id])
          .filter((id) => !nodes.has(id)),
      ).size
    : 0;
  const fallbackRows = temporalFilter ? edgeRows : edges;
  const reachedLimit = nodes.size >= NODE_LIMIT || edgeRows.length >= EDGE_LIMIT;
  const lastPage = graph.data?.pages.at(-1);
  const truncated = temporalFilter ? reachedLimit && !!history.hasNextPage : isCurrentGraphTruncated(lastPage, reachedLimit, rows.length);
  return { nodes, edges, fallbackRows, missingSnapshotNodes, truncated };
}

type GraphModel = ReturnType<typeof buildGraphModel>;

/**
 * Converts the graph model into React Flow nodes (focus at the origin, others on an ellipse) and edges.
 *
 * @param model Graph model from {@link buildGraphModel}.
 * @param entityLabel Label formatter for nodes.
 * @param t Entities translator (accessibility labels).
 * @param temporalFilter True when historical edges are shown (not selectable).
 * @param selectedRelationshipId Currently selected relationship, highlighted in current mode.
 * @returns React Flow nodes and edges. Pure.
 */
function toFlowElements(
  model: GraphModel,
  entityLabel: (entity: GraphEntity) => string,
  t: Translator,
  temporalFilter: boolean,
  selectedRelationshipId: string | undefined,
) {
  const { nodes, edges } = model;
  const flowNodes: Node[] = [...nodes.values()].map((node, index) => {
    const label = entityLabel(node);
    return {
      id: node.id,
      position:
        index === 0
          ? { x: 0, y: 0 }
          : {
              x: 260 * Math.cos((2 * Math.PI * (index - 1)) / Math.max(1, nodes.size - 1)),
              y: 180 * Math.sin((2 * Math.PI * (index - 1)) / Math.max(1, nodes.size - 1)),
            },
      data: { label },
      ariaLabel: `${t('node.a11yTitle')}: ${label}`,
      type: 'default',
    };
  });
  const flowEdges: Edge[] = edges.map(({ relationship }) => ({
    id: relationship.id,
    source: relationship.source_entity_id,
    target: relationship.target_entity_id,
    label: relationship.type,
    ariaLabel: `${t('edgeA11yTitle')}: ${relationship.type}`,
    selectable: !temporalFilter,
    focusable: true,
    style: !temporalFilter && relationship.id === selectedRelationshipId ? { stroke: 'var(--primary)', strokeWidth: 3 } : undefined,
  }));
  return { flowNodes, flowEdges };
}

/**
 * Keyboard handling for the graph viewport: arrows pan, +/= and - zoom, Home fits the view.
 *
 * @param event Keyboard event from the React Flow wrapper.
 * @param flow React Flow instance, or null before init. Events from the controls panel are ignored.
 * @returns Nothing; moves the viewport and prevents default for handled keys.
 */
function handleGraphKeyDown(event: KeyboardEvent<HTMLDivElement>, flow: ReactFlowInstance | null) {
  if (!flow || (event.target instanceof HTMLElement && event.target.closest('.react-flow__panel'))) return;
  const viewport = flow.getViewport();
  /** Adjusts the graph viewport by the supplied pointer movement. */
  const panBy = (x: number, y: number) => {
    event.preventDefault();
    void flow.setViewport({ ...viewport, x: viewport.x + x, y: viewport.y + y }, { duration: 0 });
  };
  if (event.key === 'ArrowLeft') panBy(80, 0);
  else if (event.key === 'ArrowRight') panBy(-80, 0);
  else if (event.key === 'ArrowUp') panBy(0, 80);
  else if (event.key === 'ArrowDown') panBy(0, -80);
  else if (event.key === '+' || event.key === '=') {
    event.preventDefault();
    void flow.zoomIn({ duration: 0 });
  } else if (event.key === '-') {
    event.preventDefault();
    void flow.zoomOut({ duration: 0 });
  } else if (event.key === 'Home') {
    event.preventDefault();
    void flow.fitView({ padding: 0.2, duration: 0 });
  }
}

/**
 * Valid-at and knowledge-as-of date filter fields.
 *
 * @param p Current values and their setters.
 */
function GraphFilters(p: {
  validAt: string;
  setValidAt: (v: string) => void;
  knowledgeAsOf: string;
  setKnowledgeAsOf: (v: string) => void;
}) {
  const t = useTranslations('entities');
  return (
    <div className="search-filters">
      <div className="field">
        <Label htmlFor="relationship-valid-at">{t('validAt')}</Label>
        <Input id="relationship-valid-at" type="date" value={p.validAt} onChange={(event) => p.setValidAt(event.target.value)} />
      </div>
      <div className="field">
        <Label htmlFor="relationship-knowledge-as-of">{t('knowledgeAsOf')}</Label>
        <Input
          id="relationship-knowledge-as-of"
          type="date"
          value={p.knowledgeAsOf}
          onChange={(event) => p.setKnowledgeAsOf(event.target.value)}
        />
      </div>
    </div>
  );
}

/**
 * Status, error and load-more notices between the filters and the graph canvas.
 *
 * @param p.history History query (temporal mode notices and paging).
 * @param p.graph Neighbor graph query (retry notice in current mode).
 * @param p.hasFilter True when either date filter is set.
 * @param p.temporalFilter True when historical edges are shown.
 * @param p.model Graph model (counts, snapshot bound, truncation).
 */
function GraphNotices(p: {
  history: GraphQueries['history'];
  graph: GraphQueries['graph'];
  hasFilter: boolean;
  temporalFilter: boolean;
  model: GraphModel;
}) {
  const t = useTranslations('entities');
  const { history, graph, hasFilter, temporalFilter, model } = p;
  const { nodes, edges, missingSnapshotNodes, truncated } = model;
  return (
    <>
      {hasFilter && (
        <p className="text-muted-foreground" role="status">
          {t('relationship.timeHelp')}{' '}
          {history.data?.pages[0]?.canonical_history_available === false ? t('canonicalHistoryUnavailable') : ''}
        </p>
      )}
      {missingSnapshotNodes > 0 && (
        <p className="text-muted-foreground" role="status">
          {t('snapshotNodeBound', { count: missingSnapshotNodes })}
        </p>
      )}
      {history.isError && (
        <p className="error" role="alert">
          {t('relationship.unavailable')}{' '}
          <Button type="button" variant="outline" onClick={() => history.refetch()}>
            {t('retry')}
          </Button>
        </p>
      )}
      {temporalFilter &&
        history.data?.pages.some((page) => !page.canonical_history_available || page.unavailable_relationship_ids.length > 0) && (
          <p className="text-muted-foreground" role="status">
            {t('canonicalHistoryUnavailable')}:{' '}
            {[...new Set(history.data.pages.flatMap((page) => page.unavailable_relationship_ids))].join(', ') || t('none')}
          </p>
        )}
      {history.hasNextPage && (
        <Button type="button" variant="outline" disabled={history.isFetchingNextPage} onClick={() => history.fetchNextPage()}>
          {t('load.relationships')}
        </Button>
      )}
      <p className="text-muted-foreground">
        {nodes.size} {t('selected.summary')} · {edges.length} {t('relationship.count')}
        {truncated ? ` · ${t('graph.truncated')}` : ''}. {t('graph.fallback')}
      </p>
      {!temporalFilter && graph.isError && (
        <p className="error" role="alert">
          {t('graph.retry')}{' '}
          <Button variant="outline" onClick={() => graph.refetch()}>
            {t('retry')}
          </Button>
        </p>
      )}
    </>
  );
}

/**
 * Accessible text alternative to the canvas: one list item per relationship with links, validity and evidence
 * (temporal mode) or a select button (current mode).
 *
 * @param p.rows Fallback rows from the graph model.
 * @param p.temporalFilter True when historical rows are shown.
 * @param p.entityLabel Label formatter.
 * @param p.selectedRelationshipId Selected relationship id.
 * @param p.onSelectRelationship Selection callback (current mode only).
 */
function GraphFallbackList(p: {
  rows: GraphModel['fallbackRows'];
  temporalFilter: boolean;
  entityLabel: (entity: GraphEntity) => string;
  selectedRelationshipId?: string;
  onSelectRelationship: (relationshipId: string) => void;
}) {
  const t = useTranslations('entities');
  const { rows, temporalFilter, entityLabel, selectedRelationshipId, onSelectRelationship } = p;
  return (
    <ul className="flex flex-col" aria-label={t('graph.fallback')}>
      {rows.map(({ entity, relationship }) => (
        <li className="border-b border-border py-4 last:border-b-0" key={relationship.id}>
          <span aria-hidden="true">↔</span>{' '}
          {temporalFilter ? (
            <>
              <Link href={`/knowledge/entities/${relationship.source_entity_id}`}>
                {entityLabel({ id: relationship.source_entity_id, name: relationship.source_entity_id, type: 'reference' })}
              </Link>{' '}
              →{' '}
              <Link href={`/knowledge/entities/${relationship.target_entity_id}`}>
                {entityLabel({ id: relationship.target_entity_id, name: relationship.target_entity_id, type: 'reference' })}
              </Link>
            </>
          ) : (
            <Link href={`/knowledge/entities/${entity.id}`}>{entityLabel(entity)}</Link>
          )}{' '}
          <small>{relationship.type}</small>
          {temporalFilter && (
            <>
              <p>
                {relationship.validity_precision === 'unknown'
                  ? t('unknownValidity')
                  : `${relationship.valid_from ?? t('openStart')} – ${relationship.valid_to ?? t('openEnd')}`}
              </p>
              {relationship.evidence.map((evidence) => (
                <div key={evidence.id}>
                  <Link href={`/knowledge/documents/${evidence.document_id}?version=${evidence.version_number}#cited-revision`}>
                    {evidence.title} · {t('documentVersion')} {evidence.version_number}
                  </Link>
                  <small className="text-muted-foreground">
                    {evidence.metadata_is_version_snapshot ? t('metadataVersionSnapshot') : t('metadataCurrentFallback')}
                  </small>
                  <p>
                    {t('observed')} {evidence.observed_at}
                  </p>
                  <blockquote>{evidence.excerpt}</blockquote>
                </div>
              ))}
            </>
          )}
          {!temporalFilter && (
            <Button
              type="button"
              variant="outline"
              aria-pressed={selectedRelationshipId === relationship.id}
              onClick={() => onSelectRelationship(relationship.id)}
            >
              {selectedRelationshipId === relationship.id ? t('selected.relationship') : t('selectRelationship')}
            </Button>
          )}
        </li>
      ))}
    </ul>
  );
}

/**
 * Localized accessibility strings for React Flow.
 *
 * @param t Entities translator.
 * @returns The `ariaLabelConfig` object. Pure.
 */
function graphAriaLabels(t: Translator) {
  return {
    'node.a11yDescription.default': t('node.a11yDescription'),
    'node.a11yDescription.keyboardDisabled': t('node.keyboardDisabled'),
    'node.a11yDescription.ariaLiveMessage': ({ direction, x, y }: { direction: string; x: number; y: number }) =>
      t('graph.liveMessage', { direction, x, y }),
    'edge.a11yDescription.default': t('edgeA11yDescription'),
    'controls.ariaLabel': t('controlsA11yLabel'),
    'controls.zoomIn.ariaLabel': t('zoom.inA11yLabel'),
    'controls.zoomOut.ariaLabel': t('zoom.outA11yLabel'),
    'controls.fitView.ariaLabel': t('fitViewA11yLabel'),
    'controls.interactive.ariaLabel': t('controlsA11yLabel'),
    'minimap.ariaLabel': t('viewportA11yLabel'),
    'handle.ariaLabel': t('handleA11yLabel'),
  };
}

/**
 * Renders an entity’s related graph and emits the selected relationship to its owner.
 *
 * Loads neighbors page by page (bounded by node/edge limits) or, when a valid-at / knowledge-as-of date is set,
 * the relationship history; every relationship is also listed as accessible text below the canvas.
 *
 * @param props.entityId Focus entity id.
 * @param props.title Display title of the focus entity.
 * @param props.selectedRelationshipId Currently selected relationship (highlighted).
 * @param props.onSelectRelationship Called with a relationship id on edge click or select button (current mode only).
 */
export function EntityGraph({
  entityId,
  title,
  selectedRelationshipId,
  onSelectRelationship,
}: {
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
  const { history, graph } = useGraphQueries(entityId, validAt, knowledgeAsOf);
  const temporalFilter = !!validAt || !!knowledgeAsOf;
  const model = buildGraphModel({ entityId, title, temporalFilter, graph, history });
  /** Uses actual snapshot identifiers for historical references and localized current entity labels otherwise. */
  const entityLabel = (entity: GraphEntity) =>
    entity.type === 'reference' ? t('historicalEntityReference', { id: entity.id }) : (entity.name ?? entityTypeLabel(t, entity.type));
  const { flowNodes, flowEdges } = toFlowElements(model, entityLabel, t, temporalFilter, selectedRelationshipId);
  return (
    <section aria-labelledby="entity-graph-heading">
      <h2 id="entity-graph-heading">{t('graph.label')}</h2>
      <GraphFilters validAt={validAt} setValidAt={setValidAt} knowledgeAsOf={knowledgeAsOf} setKnowledgeAsOf={setKnowledgeAsOf} />
      <GraphNotices history={history} graph={graph} hasFilter={temporalFilter} temporalFilter={temporalFilter} model={model} />
      <p id="entity-graph-keyboard-help" className="text-muted-foreground">
        {t('keyboardGraphHelp')}
      </p>
      <div className="entity-graph" aria-label={t('viewportA11yLabel')}>
        <ReactFlow
          nodes={flowNodes}
          edges={flowEdges}
          fitView
          colorMode={resolvedTheme === 'dark' ? 'dark' : 'light'}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable
          ariaLabelConfig={graphAriaLabels(t)}
          onInit={(instance) => {
            flowRef.current = instance;
          }}
          onKeyDown={(event) => handleGraphKeyDown(event, flowRef.current)}
          onEdgeClick={(_, edge) => {
            if (!temporalFilter) onSelectRelationship(edge.id);
          }}
        >
          <Background color="var(--line)" />
          <Panel position="top-right">
            <GraphActions
              focusId={entityId}
              canExpand={temporalFilter ? !!history.hasNextPage : !!graph.hasNextPage}
              isExpanding={temporalFilter ? history.isFetchingNextPage : graph.isFetchingNextPage}
              onExpand={() => {
                void (temporalFilter ? history.fetchNextPage() : graph.fetchNextPage());
              }}
            />
          </Panel>
        </ReactFlow>
      </div>
      <GraphFallbackList
        rows={model.fallbackRows}
        temporalFilter={temporalFilter}
        entityLabel={entityLabel}
        selectedRelationshipId={selectedRelationshipId}
        onSelectRelationship={onSelectRelationship}
      />
      {model.truncated && (
        <p className="text-muted-foreground" role="status">
          {t('graph.atLimit', { limit: NODE_LIMIT })}
        </p>
      )}
    </section>
  );
}
