'use client';

import { useTranslations } from 'next-intl';
import React, { useCallback, useMemo } from 'react';
import type { Dashboard, DashboardPlacement, GadgetInstance } from './api';
import { DashboardGrid } from './dashboard-grid';
import { DashboardEmptyState } from './dashboard-panels';
import { FirstRunSetup } from './first-run-setup';
import { LayoutEditor } from './layout-editor';
import type { useGadgetMembership } from './use-gadget-membership';
import type { useLayoutSave } from './use-layout-save';
import type { LayoutSession } from './use-layout-session';

/** Operations produced by the layout hooks that the editor bar and grid consume. */
export interface CanvasOperations {
  session: LayoutSession;
  save: ReturnType<typeof useLayoutSave>;
  membership: ReturnType<typeof useGadgetMembership>;
}

/** Layout editor toolbar (edit mode only): maps session, save and membership operations to its props. */
export function LayoutEditorBar({ ops, activeDashboard, onOpenPresets, onAddGroup, openAddInitially }: {
  ops: CanvasOperations;
  activeDashboard: Dashboard | null;
  onOpenPresets: () => void;
  onAddGroup?: () => void;
  openAddInitially?: boolean;
}) {
  const t = useTranslations('dashboard');
  const { session, save, membership } = ops;
  const { editOrigin, requestedBreakpoint, isDirty, historyIndex, history, currentBreakpoint, readablePlacements } = session;
  const layoutLabel = (breakpoint: 'desktop' | 'mobile') => t(breakpoint === 'mobile' ? 'mobileLayout' : 'desktopLayout');
  return (
    <LayoutEditor
      dashboardName={activeDashboard?.name ?? ''}
      editCount={isDirty ? Math.max(1, historyIndex) : 0}
      baseRevision={editOrigin?.baseRevision ?? activeDashboard?.revision}
      isDirty={isDirty}
      isSaving={save.layoutOperationPending}
      canUndo={historyIndex > 0}
      canRedo={historyIndex < history.length - 1}
      onSave={save.handleSave}
      onCancel={save.requestExitEdit}
      onUndo={session.handleUndo}
      onTidyLayout={session.handleTidyLayout}
      canTidyLayout={session.canTidyLayout}
      onRedo={session.handleRedo}
      onUseReadableMobileSizes={() => {
        if (currentBreakpoint === 'mobile' && readablePlacements) session.pushHistory(readablePlacements);
      }}
      canUseReadableMobileSizes={currentBreakpoint === 'mobile' && readablePlacements !== null}
      onOpenPresetPicker={onOpenPresets}
      onAddGroup={onAddGroup}
      openAddInitially={openAddInitially}
      onAddGadgetInstance={membership.handleAddGadgetInstance}
      onBeginAddGadgetInstance={membership.beginAddGadgetInstance}
      onCancelPendingAdd={membership.releaseAddReservation}
      groups={activeDashboard?.groups.filter((group) => group.id === session.activeGroupId) ?? []}
      activeGroupId={session.activeGroupId}
      dirtyModalOpen={session.dirtyModalOpen}
      dirtyDescription={editOrigin && requestedBreakpoint !== editOrigin.breakpoint
        ? t('mobileBreakpointDirtyWarning', { breakpoint: layoutLabel(editOrigin.breakpoint) }) : undefined}
      onDirtyModalResolution={save.handleDirtyModalResolution}
      saveError={session.saveError}
      onDismissSaveError={() => session.setSaveError(null)}
    />
  );
}

/** Props of the main canvas: first-run panel, empty state or the gadget grid. */
interface CanvasProps {
  ops: CanvasOperations;
  activeDashboard: Dashboard | null;
  visibleInstances: GadgetInstance[];
  showFirstRun: boolean;
  csrfToken: string;
  isTablet: boolean;
  isAdminViewer: boolean;
  onPickTopics: () => void;
  onOpenPresets: () => void;
  onCreateDashboard: () => void;
  onConfigureInstance: (instance: GadgetInstance) => void;
}

/** Main grid canvas, or the first-run / empty state that replaces it. */
export function DashboardCanvas(props: CanvasProps) {
  const { ops, activeDashboard, visibleInstances, showFirstRun, isAdminViewer } = props;
  const { session, save } = ops;
  const visibleIds = useMemo(() => new Set(visibleInstances.map((instance) => instance.id)), [visibleInstances]);
  const panelPlacements = useMemo(() => session.draftPlacements.filter((placement) => visibleIds.has(placement.instance_id)),
    [session.draftPlacements, visibleIds]);
  /** Merges the measured panel projection with untouched placements from the other panels. */
  const setPanelProjection = useCallback((next: DashboardPlacement[] | null) => {
    const byId = new Map(next?.map((placement) => [placement.instance_id, placement]));
    session.setReadablePlacements(next
      ? session.draftPlacements.map((placement) => byId.get(placement.instance_id) ?? placement) : null);
  }, [session.draftPlacements, session.setReadablePlacements]);
  if (showFirstRun && !session.editMode) {
    return <FirstRunSetup sourceCount={0} csrfToken={props.csrfToken} onPickTopics={props.onPickTopics} />;
  }
  if (!activeDashboard || visibleInstances.length === 0) {
    return (
      <DashboardEmptyState
        variant={activeDashboard ? 'empty' : 'none'}
        isAdminViewer={isAdminViewer}
        canEditLayout={session.canEditLayout}
        onOpenPresets={props.onOpenPresets}
        onCreateDashboard={props.onCreateDashboard}
        onStartEmpty={() => { session.setIsEditMode(true); }}
      />
    );
  }
  return (
    <DashboardGrid
      instances={visibleInstances}
      placements={session.mobileProjectionUnavailable ? [] : panelPlacements}
      visibleInstanceIds={visibleInstances.map((instance) => instance.id)}
      isMobileView={session.currentBreakpoint === 'mobile'}
      isTabletView={true}
      onReadableProjectionChange={setPanelProjection}
      onGridMetricsChange={session.setGridMetrics}
      columns={session.layoutColumns}
      breakpoint={session.currentBreakpoint}
      requestedBreakpoint={session.requestedBreakpoint}
      interactionPending={save.layoutOperationPending}
      isEditMode={session.editMode && !save.layoutOperationPending}
      onLayoutChange={(next) => {
        const nextById = new Map(next.map((placement) => [placement.instance_id, placement]));
        session.pushHistory(session.draftPlacements.map((placement) => nextById.get(placement.instance_id) ?? placement));
      }}
      onRemoveInstance={ops.membership.handleRemoveInstance}
      onConfigureInstance={props.onConfigureInstance}
      readingQueues={session.readingQueues}
      onApplyReadingQueue={session.handleApplyReadingQueue}
    />
  );
}
