'use client';

import { useTranslations } from 'next-intl';
import React, { useCallback, useMemo, useState, useSyncExternalStore } from 'react';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useWorkspace } from '@/core/workspace-context';
import { useDisplayPreferences } from '@/core/query-provider';
import type { GadgetInstance } from './api';
import { DashboardCanvas, LayoutEditorBar } from './dashboard-canvas';
import { DashboardCrudDialogs, ManagementDialog, useDashboardCrud } from './dashboard-dialogs';
import { DashboardHeader, formatRelativeAge } from './dashboard-header';
import { GroupTabs, LayoutNotices, StaleSourcesPanel, TimezoneFooter, TodayHeaderGate } from './dashboard-panels';
import { FreshnessStrip } from './freshness-strip';
import { GadgetSettings } from './gadget-settings';
import { PresetPicker } from './preset-picker';
import { useDashboardData } from './use-dashboard-data';
import { useGadgetMembership } from './use-gadget-membership';
import { useLayoutSave } from './use-layout-save';
import { useLayoutSession } from './use-layout-session';

export { packPlacements } from './layout-geometry';

const MOBILE_NOTICE_KEY = 'umwelt.dashboard.mobileNoticeDismissed';
const MOBILE_QUERY = '(max-width: 720px)';
/** Subscribes to the phone-width media query. */
function subscribeMobileMedia(onChange: () => void): () => void {
  const media = window.matchMedia(MOBILE_QUERY);
  media.addEventListener('change', onChange);
  return () => media.removeEventListener('change', onChange);
}
/** Reads the current phone-width match. */
function getMobileMedia(): boolean { return window.matchMedia(MOBILE_QUERY).matches; }
const TABLET_QUERY = '(min-width: 721px) and (max-width: 1099px)';
/** Subscribes to the tablet-width media query. */
function subscribeTabletMedia(onChange: () => void): () => void {
  const media = window.matchMedia(TABLET_QUERY);
  media.addEventListener('change', onChange);
  return () => media.removeEventListener('change', onChange);
}
/** Reads the current tablet-width match. */
function getTabletMedia(): boolean { return window.matchMedia(TABLET_QUERY).matches; }
/** Server render assumes desktop. */
function getServerMobileMedia(): boolean { return false; }

/**
 * Tracks the phone and tablet media queries (server render assumes desktop).
 *
 * @returns Whether the viewport currently matches the phone or tablet query.
 */
function useDashboardMedia(): { isMobile: boolean; isTablet: boolean } {
  const isMobile = useSyncExternalStore(subscribeMobileMedia, getMobileMedia, getServerMobileMedia);
  const isTablet = useSyncExternalStore(subscribeTabletMedia, getTabletMedia, getServerMobileMedia);
  return { isMobile, isTablet };
}

/**
 * Remembers whether the owner dismissed the mobile reading-sizes notice (localStorage, best effort).
 *
 * The initial read is lazy: the notice only renders on the mobile media path, which is client-only, so there is
 * no hydration mismatch. Storage failures only affect persistence across sessions.
 */
function useMobileNoticeDismissal(): [boolean, () => void] {
  const [dismissed, setDismissed] = useState(() => {
    try { return typeof window !== 'undefined' && window.localStorage.getItem(MOBILE_NOTICE_KEY) === '1'; } catch { return false; }
  });
  /** Persists the dismissal of the mobile reading-sizes notice. */
  const dismiss = useCallback(() => {
    setDismissed(true);
    try { window.localStorage.setItem(MOBILE_NOTICE_KEY, '1'); } catch { /* storage unavailable: dismissal lasts this session */ }
  }, []);
  return [dismissed, dismiss];
}

/** Joins the "updated" and "stale sources" fragments of the header summary; null when both are absent. */
function buildHeaderSummary(
  t: ReturnType<typeof useTranslations>,
  lastUpdatedAt: number | null,
  staleCount: number,
  locale: string,
): string | null {
  const parts = [
    lastUpdatedAt !== null ? t('summaryUpdated', { time: formatRelativeAge(lastUpdatedAt, locale) }) : null,
    staleCount > 0 ? t('staleSourcesSummary', { count: staleCount }) : null,
  ].filter(Boolean);
  return parts.length ? parts.join(' · ') : null;
}

type PageData = ReturnType<typeof useDashboardData>;
type PageOps = { session: ReturnType<typeof useLayoutSession>; save: ReturnType<typeof useLayoutSave>; membership: ReturnType<typeof useGadgetMembership> };

/** Dashboard header wired to the data, layout and CRUD hooks (summary text, edit toggle, switcher actions). */
function ConnectedHeader({ data, ops, crud, onOpenPresets, onOpenManagement }: {
  data: PageData;
  ops: PageOps;
  crud: ReturnType<typeof useDashboardCrud>;
  onOpenPresets: () => void;
  onOpenManagement: () => void;
}) {
  const t = useTranslations('dashboard');
  const { isAdminViewer, isOwner } = useWorkspace();
  const display = useDisplayPreferences();
  const { session, save } = ops;
  const { activeDashboard, sources } = data;
  return (
    <DashboardHeader
      dashboards={data.dashboards} activeDashboard={activeDashboard} selectedDashboardId={data.selectedDashboardId}
      layoutOperationPending={save.layoutOperationPending} isAdminViewer={isAdminViewer} isOwner={isOwner}
      isEditMode={session.isEditMode} canEditLayout={session.canEditLayout}
      summary={buildHeaderSummary(t, sources.lastUpdatedAt, sources.staleSources.length, display.locale)}
      onSwitch={save.requestSwitchDashboard} onCreate={() => crud.setCreateOpen(true)}
      onRename={() => { crud.setRenameValue(activeDashboard!.name); crud.setRenameOpen(true); }}
      onDelete={() => crud.setDeleteOpen(true)} onOpenPresets={onOpenPresets}
      onToggleEdit={() => { if (session.isEditMode) save.requestExitEdit(); else session.setIsEditMode(true); }}
      onOpenManagement={onOpenManagement}
    />
  );
}

/** Preset picker wired to navigation: applying a preset opens it, leaves edit mode and clears the dirty flag. */
function ConnectedPresetPicker({ open, onOpenChange, data, session }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  data: PageData;
  session: PageOps['session'];
}) {
  const { activeDashboard } = data;
  return (
    <PresetPicker
      open={open}
      onOpenChange={onOpenChange}
      currentDashboardId={data.selectedDashboardId}
      currentDashboardRevision={activeDashboard?.revision}
      currentDashboardName={activeDashboard?.name}
      onPresetApplied={(applied) => {
        data.navigateToDashboard(applied.id, true);
        session.setActiveGroupId('all');
        session.setIsDirty(false);
        session.setIsEditMode(false);
      }}
    />
  );
}

/** Instances of the active dashboard that belong to the selected group tab ("all" shows everything). */
function useVisibleInstances(activeDashboard: PageData['activeDashboard'], activeGroupId: string): GadgetInstance[] {
  return useMemo(() => {
    if (!activeDashboard) return [];
    if (activeGroupId === 'all') return activeDashboard.instances;
    return activeDashboard.instances.filter((inst) => inst.group_id === activeGroupId);
  }, [activeDashboard, activeGroupId]);
}

/** True when the dismissible mobile reading-projection hint applies (read-only mobile view with a readable projection). */
function shouldShowMobileHint(session: PageOps['session'], hasDashboard: boolean, dismissed: boolean): boolean {
  return hasDashboard && session.currentBreakpoint === 'mobile' && !session.editMode
    && !session.mobileProjectionUnavailable && session.readablePlacements !== null && !dismissed;
}

/** Stale-source disclosure (when any source is failing) followed by the timezone footer; both need a loaded dashboard. */
function StaleAndFooter({ activeDashboard, staleSources, open, onToggle }: {
  activeDashboard: PageData['activeDashboard'];
  staleSources: PageData['sources']['staleSources'];
  open: boolean;
  onToggle: () => void;
}) {
  const display = useDisplayPreferences();
  if (!activeDashboard) return null;
  return (
    <>
      {staleSources.length > 0 && (
        <StaleSourcesPanel sources={staleSources} open={open} onToggle={onToggle} locale={display.locale} timezone={display.timezone} />
      )}
      <TimezoneFooter locale={display.locale} timezone={display.timezone} />
    </>
  );
}

/**
 * Main dashboard container view for BBD-OS, keeping dashboard controls visible while its content scrolls.
 * Supports owner-scoped desktop/mobile drafts and administrator read-only views, plus mobile reading,
 * geometry history, dashboard switching, and dirty navigation guarding. The route parameter selects
 * the dashboard; `/dashboard` resolves to the first available dashboard.
 *
 * Composition only: data loading lives in `useDashboardData`, the editing session in `useLayoutSession`,
 * `useLayoutSave` and `useGadgetMembership`, dialogs in `useDashboardCrud`.
 *
 * @param dashboardId Dashboard identity from the canonical dashboard route, when present.
 * @returns Authenticated dashboard view component.
 */
export function DashboardPage({ dashboardId }: { dashboardId?: string }) {
  const workspaceSession = useWorkspaceSession();
  const { isAdminViewer, isOwner } = useWorkspace();
  const display = useDisplayPreferences();
  const { isMobile, isTablet } = useDashboardMedia();
  const [presetPickerOpen, setPresetPickerOpen] = useState<boolean>(false);
  const [configuringInstance, setConfiguringInstance] = useState<GadgetInstance | null>(null);
  const [gadgetSettingsOpen, setGadgetSettingsOpen] = useState<boolean>(false);
  // Stale-source detail list stays collapsed until the owner asks for it.
  const [staleDetailsOpen, setStaleDetailsOpen] = useState(false);
  const [mobileNoticeDismissed, dismissMobileNotice] = useMobileNoticeDismissal();

  const data = useDashboardData(dashboardId);
  const { dashboards, selectedDashboardId, activeDashboard, management, sources, showFirstRun } = data;
  const session = useLayoutSession({ selectedDashboardId, activeDashboard, isMobile });
  const save = useLayoutSave(session, data.navigateToDashboard, selectedDashboardId);
  const membership = useGadgetMembership(session, save.layoutOperationPending);
  const crud = useDashboardCrud({
    activeDashboard, dashboards, layoutOperationPending: save.layoutOperationPending, navigateToDashboard: data.navigateToDashboard,
  });
  const ops = { session, save, membership };
  const { layoutOperationPending } = save;

  const visibleInstances = useVisibleInstances(activeDashboard, session.activeGroupId);
  const showMobileHint = shouldShowMobileHint(session, Boolean(activeDashboard), mobileNoticeDismissed);

  return (
    <section className="flex w-full flex-col gap-6" data-dashboard-ready={data.dashboardRouteReady}>
      <ConnectedHeader
        data={data} ops={ops} crud={crud}
        onOpenPresets={() => setPresetPickerOpen(true)} onOpenManagement={() => management.setOpen(true)}
      />
      <TodayHeaderGate visible={isOwner && !showFirstRun} sources={sources.allSources.items ?? []} />
      <FreshnessStrip sources={sources.dashboardSources} locale={display.locale} />
      {/* Group tabs: rendered only when the dashboard has groups */}
      {activeDashboard && activeDashboard.groups.length > 1 && (
        <GroupTabs groups={activeDashboard.groups} activeGroupId={session.activeGroupId} onChange={session.setActiveGroupId} />
      )}

      <div className="flex flex-col gap-6">
        <LayoutNotices
          editOrigin={session.editOrigin} selectedDashboardId={selectedDashboardId}
          requestedBreakpoint={session.requestedBreakpoint} mobileProjectionUnavailable={session.mobileProjectionUnavailable}
          showMobileHint={showMobileHint} onDismissMobileHint={dismissMobileNotice}
        />
        {/* Layout Editor Toolbar (visible in Edit Mode) */}
        {session.editMode && (
          <LayoutEditorBar
            ops={ops} activeDashboard={activeDashboard}
            onOpenPresets={() => setPresetPickerOpen(true)} onAddGroup={() => crud.setGroupOpen(true)}
          />
        )}
        {/* Main Grid Canvas or Empty State */}
        <DashboardCanvas
          ops={ops} activeDashboard={activeDashboard} visibleInstances={visibleInstances} showFirstRun={showFirstRun}
          csrfToken={workspaceSession.csrfToken} isTablet={isTablet} isAdminViewer={isAdminViewer}
          onPickTopics={() => { management.setTab('topics'); management.setOpen(true); }}
          onOpenPresets={() => setPresetPickerOpen(true)} onCreateDashboard={() => crud.setCreateOpen(true)}
          onConfigureInstance={(inst) => { setConfiguringInstance(inst); setGadgetSettingsOpen(true); }}
        />
        <StaleAndFooter
          activeDashboard={activeDashboard} staleSources={sources.staleSources}
          open={staleDetailsOpen} onToggle={() => setStaleDetailsOpen((open) => !open)}
        />
      </div>

      {/* Preset Picker Modal */}
      <ConnectedPresetPicker
        open={presetPickerOpen} onOpenChange={setPresetPickerOpen} data={data} session={session}
      />
      <DashboardCrudDialogs crud={crud} activeDashboardName={activeDashboard?.name ?? ''} layoutOperationPending={layoutOperationPending} />
      {/* Gadget Instance & Definition Settings Modal */}
      <GadgetSettings
        open={gadgetSettingsOpen}
        onOpenChange={setGadgetSettingsOpen}
        instance={configuringInstance}
        dashboardId={selectedDashboardId ?? ''}
        dashboardRevision={activeDashboard?.revision ?? 1}
      />
      <ManagementDialog management={management} />
    </section>
  );
}
