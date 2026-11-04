'use client';

import { useTranslations } from 'next-intl';
import React from 'react';
import type { Dashboard, GadgetInstance } from './api';
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
export function LayoutEditorBar({ ops, activeDashboard, onOpenPresets, onAddGroup }: {
  ops: CanvasOperations;
  activeDashboard: Dashboard | null;
  onOpenPresets: () => void;
  onAddGroup: () => void;
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
      onAddGadgetInstance={membership.handleAddGadgetInstance}
      onBeginAddGadgetInstance={membership.beginAddGadgetInstance}
      onCancelPendingAdd={membership.releaseAddReservation}
      groups={activeDashboard?.groups ?? []}
      activeGroupId={session.activeGroupId === 'all' ? undefined : session.activeGroupId}
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
      instances={activeDashboard.instances}
      placements={session.mobileProjectionUnavailable ? [] : session.draftPlacements}
      visibleInstanceIds={visibleInstances.map((instance) => instance.id)}
      isMobileView={session.currentBreakpoint === 'mobile'}
      isTabletView={props.isTablet}
      onReadableProjectionChange={session.setReadablePlacements}
      onGridMetricsChange={session.setGridMetrics}
      columns={session.layoutColumns}
      breakpoint={session.currentBreakpoint}
      requestedBreakpoint={session.requestedBreakpoint}
      interactionPending={save.layoutOperationPending}
      isEditMode={session.editMode && !save.layoutOperationPending}
      onLayoutChange={session.pushHistory}
      onRemoveInstance={ops.membership.handleRemoveInstance}
      onConfigureInstance={props.onConfigureInstance}
      readingQueues={session.readingQueues}
      onApplyReadingQueue={session.handleApplyReadingQueue}
    />
  );
}
