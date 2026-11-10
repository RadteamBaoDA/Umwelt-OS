'use client';

import { useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import React, { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useWorkspace } from '@/core/workspace-context';
import { useDisplayPreferences } from '@/core/query-provider';
import { Tabs, TabsContent } from '@/components/ui/tabs';
import { useResetOnChange } from '@/modules/shared';
import { sourceKeys, StarterPackWizard } from '@/modules/sources';
import { dashboardKeys, type GadgetInstance } from './api';
import { DashboardCanvas, LayoutEditorBar } from './dashboard-canvas';
import { DashboardOverview } from './dashboard-overview';
import { DashboardPanel } from './dashboard-panel';
import { DashboardCrudDialogs, ManagementDialog, useDashboardCrud } from './dashboard-dialogs';
import { DashboardHeader, formatRelativeAge } from './dashboard-header';
import { GroupTabs, LayoutNotices, StaleSourcesPanel, TimezoneFooter, TodayHeaderGate } from './dashboard-panels';
import { FreshnessStrip } from './freshness-strip';
import { GadgetSettings } from './gadget-settings';
import { useUpdatedGroups } from './group-updates';
import { PresetPicker } from './preset-picker';
import { useDashboardData } from './use-dashboard-data';
import { useDashboardDeepLink } from './use-dashboard-deep-link';
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
function getMobileMedia(): boolean {
  return window.matchMedia(MOBILE_QUERY).matches;
}
const TABLET_QUERY = '(min-width: 721px) and (max-width: 1099px)';
/** Subscribes to the tablet-width media query. */
function subscribeTabletMedia(onChange: () => void): () => void {
  const media = window.matchMedia(TABLET_QUERY);
  media.addEventListener('change', onChange);
  return () => media.removeEventListener('change', onChange);
}
/** Reads the current tablet-width match. */
function getTabletMedia(): boolean {
  return window.matchMedia(TABLET_QUERY).matches;
}
/** Server render assumes desktop. */
function getServerMobileMedia(): boolean {
  return false;
}

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
    try {
      return typeof window !== 'undefined' && window.localStorage.getItem(MOBILE_NOTICE_KEY) === '1';
    } catch {
      return false;
    }
  });
  /** Persists the dismissal of the mobile reading-sizes notice. */
  const dismiss = useCallback(() => {
    setDismissed(true);
    try {
      window.localStorage.setItem(MOBILE_NOTICE_KEY, '1');
    } catch {
      /* storage unavailable: dismissal lasts this session */
    }
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
type PageOps = {
  session: ReturnType<typeof useLayoutSession>;
  save: ReturnType<typeof useLayoutSave>;
  membership: ReturnType<typeof useGadgetMembership>;
};

/** Dashboard header wired to the data, layout and CRUD hooks (summary text, edit toggle, switcher actions). */
function ConnectedHeader({
  data,
  ops,
  crud,
  onOpenPresets,
  onOpenManagement,
}: {
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
      dashboards={data.dashboards}
      activeDashboard={activeDashboard}
      selectedDashboardId={data.selectedDashboardId}
      layoutOperationPending={save.layoutOperationPending}
      isAdminViewer={isAdminViewer}
      isOwner={isOwner}
      isEditMode={session.isEditMode}
      canEditLayout={session.canEditLayout}
      summary={buildHeaderSummary(t, sources.lastUpdatedAt, sources.staleSources.length, display.locale)}
      onSwitch={save.requestSwitchDashboard}
      onCreate={() => crud.setCreateOpen(true)}
      onRename={() => {
        crud.setRenameValue(activeDashboard!.name);
        crud.setRenameOpen(true);
      }}
      onDelete={() => crud.setDeleteOpen(true)}
      onOpenPresets={onOpenPresets}
      onToggleEdit={() => {
        if (session.isEditMode) save.requestExitEdit();
        else session.setIsEditMode(true);
      }}
      onOpenManagement={onOpenManagement}
    />
  );
}

/** Preset picker wired to navigation: applying a preset opens it, leaves edit mode and clears the dirty flag. */
function ConnectedPresetPicker({
  open,
  onOpenChange,
  data,
  session,
  initialPresetId,
  initialApplyMode,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  data: PageData;
  session: PageOps['session'];
  initialPresetId?: string;
  initialApplyMode?: 'append';
}) {
  const { activeDashboard } = data;
  return (
    <PresetPicker
      open={open}
      onOpenChange={onOpenChange}
      initialPresetId={initialPresetId}
      initialApplyMode={initialApplyMode}
      currentDashboardId={data.selectedDashboardId}
      currentDashboardRevision={activeDashboard?.revision}
      currentDashboardName={activeDashboard?.name}
      onPresetApplied={(applied) => {
        data.navigateToDashboard(applied.id, true);
        session.setActiveGroupId('');
        session.setIsDirty(false);
        session.setIsEditMode(false);
      }}
    />
  );
}

/** Instances belonging only to the selected panel. */
function useVisibleInstances(activeDashboard: PageData['activeDashboard'], activeGroupId: string): GadgetInstance[] {
  return useMemo(() => {
    if (!activeDashboard) return [];
    return activeDashboard.instances.filter((inst) => inst.group_id === activeGroupId);
  }, [activeDashboard, activeGroupId]);
}

/** True when the dismissible mobile reading-projection hint applies (read-only mobile view with a readable projection). */
function shouldShowMobileHint(session: PageOps['session'], hasDashboard: boolean, dismissed: boolean): boolean {
  return (
    hasDashboard &&
    session.currentBreakpoint === 'mobile' &&
    !session.editMode &&
    !session.mobileProjectionUnavailable &&
    session.readablePlacements !== null &&
    !dismissed
  );
}

/** Stale-source disclosure (when any source is failing) followed by the timezone footer; both need a loaded dashboard. */
function StaleAndFooter({
  activeDashboard,
  staleSources,
  open,
  onToggle,
}: {
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
 * Owns the preset-picker and Add-gadget intents: deep links (`preset`, `addTemplate`) open them, and leaving
 * edit mode drops any pending Add-gadget intent. Add deep links apply only to owners on an editable layout.
 */
function useDashboardIntents(session: PageOps['session'], activeDashboard: PageData['activeDashboard'], isOwner: boolean) {
  const [presetPickerOpen, setPresetPickerOpen] = useState(false);
  const [openAddInitially, setOpenAddInitially] = useState(false);
  // Template/source from an `addTemplate` deep link; handed to the Add gadget editor, cleared when edit mode ends.
  const [addDeepLink, setAddDeepLink] = useState<{ templateId: string; sourceId?: string } | undefined>(undefined);
  // Set only when the picker is opened from a starter-pack run, so header/empty-state opens keep the plain catalog.
  const [presetInitialId, setPresetInitialId] = useState<string | undefined>(undefined);
  const [presetInitialMode, setPresetInitialMode] = useState<'append' | undefined>(undefined);
  // Render-phase reset instead of a state-in-effect; only the transition out of edit mode clears the intent.
  useResetOnChange([session.editMode], () => {
    if (!session.editMode) {
      setOpenAddInitially(false);
      setAddDeepLink(undefined);
    }
  });
  useDashboardDeepLink({
    ready: Boolean(activeDashboard),
    onPreset: (id) => {
      setPresetInitialId(id);
      setPresetPickerOpen(true);
    },
    onAddTemplate: (target) => {
      if (!isOwner || !session.canEditLayout) return;
      setAddDeepLink(target);
      setOpenAddInitially(true);
      session.setIsEditMode(true);
    },
  });
  return {
    presetPickerOpen,
    setPresetPickerOpen,
    openAddInitially,
    setOpenAddInitially,
    addDeepLink,
    presetInitialId,
    setPresetInitialId,
    presetInitialMode,
    setPresetInitialMode,
  };
}

/** Gadget instance and definition settings modal bound to the selected dashboard and its current revision. */
function ConnectedGadgetSettings({
  open,
  onOpenChange,
  instance,
  selectedDashboardId,
  activeDashboard,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  instance: GadgetInstance | null;
  selectedDashboardId: string | null;
  activeDashboard: PageData['activeDashboard'];
}) {
  return (
    <GadgetSettings
      open={open}
      onOpenChange={onOpenChange}
      instance={instance}
      dashboardId={selectedDashboardId ?? ''}
      dashboardRevision={activeDashboard?.revision ?? 1}
    />
  );
}

/** After a group is created, selects it once it appears in the active dashboard and clears the mutation result. */
function useActivateCreatedGroup(
  crud: ReturnType<typeof useDashboardCrud>,
  activeDashboard: PageData['activeDashboard'],
  session: PageOps['session'],
): void {
  const { groupMutation } = crud;
  const { setActiveGroupId } = session;
  useEffect(() => {
    const created = groupMutation.data;
    if (created && activeDashboard?.groups.some((group) => group.id === created.id)) {
      setActiveGroupId(created.id);
      groupMutation.reset();
    }
  }, [activeDashboard, groupMutation, setActiveGroupId]);
}

/** Starter-pack wizard whose runs refresh every source-backed query so first-run state and gadgets update. */
function ConnectedStarterWizard({ onClose, onBuildDashboard }: { onClose: () => void; onBuildDashboard: () => void }) {
  const queryClient = useQueryClient();
  return (
    <StarterPackWizard
      onClose={onClose}
      onChanged={() => {
        void queryClient.invalidateQueries({ queryKey: sourceKeys.list });
        void queryClient.invalidateQueries({ queryKey: dashboardKeys.sources });
      }}
      onBuildDashboard={onBuildDashboard}
    />
  );
}

/** Props for {@link DashboardGroupBody}; callbacks are owned by the page so dialog state stays in one place. */
interface GroupBodyProps {
  ops: PageOps;
  data: PageData;
  activeGroup: NonNullable<PageData['activeDashboard']>['groups'][number] | undefined;
  visibleInstances: GadgetInstance[];
  csrfToken: string;
  isTablet: boolean;
  isMobile: boolean;
  scrollPositions: Map<string, number>;
  staleDetailsOpen: boolean;
  onToggleStale: () => void;
  onPickTopics: () => void;
  onOpenStarterPacks: () => void;
  onOpenPresets: () => void;
  onCreateDashboard: () => void;
  onConfigureInstance: (instance: GadgetInstance) => void;
  onAdd: () => void;
}

/** Canvas plus the Overview-only strips (today header, freshness, stale sources, timezone footer) of one group. */
function DashboardGroupContent(props: GroupBodyProps) {
  const { ops, data, activeGroup, visibleInstances, isTablet } = props;
  const { activeDashboard, sources, showFirstRun } = data;
  const { isAdminViewer, isOwner } = useWorkspace();
  const display = useDisplayPreferences();
  return (
    <div className="flex flex-col gap-4 pb-4">
      {activeGroup?.is_overview && (
        <>
          <TodayHeaderGate visible={isOwner && !showFirstRun} sources={sources.allSources.items ?? []} />
          <FreshnessStrip sources={sources.dashboardSources} locale={display.locale} />
        </>
      )}
      <DashboardCanvas
        ops={ops}
        activeDashboard={activeDashboard}
        visibleInstances={visibleInstances}
        showFirstRun={showFirstRun && Boolean(activeGroup?.is_overview)}
        csrfToken={props.csrfToken}
        isTablet={isTablet}
        isAdminViewer={isAdminViewer}
        onPickTopics={props.onPickTopics}
        onOpenStarterPacks={props.onOpenStarterPacks}
        onOpenPresets={props.onOpenPresets}
        onCreateDashboard={props.onCreateDashboard}
        onConfigureInstance={props.onConfigureInstance}
      />
      {activeGroup?.is_overview && (
        <StaleAndFooter
          activeDashboard={activeDashboard}
          staleSources={sources.staleSources}
          open={props.staleDetailsOpen}
          onToggle={props.onToggleStale}
        />
      )}
    </div>
  );
}

/**
 * Body of the active group tab: the gadget canvas (plus freshness and stale-source strips on Overview)
 * inside the Overview map split or the plain scrolling panel. `scrollPositions` is the per-dashboard map
 * that keeps each panel's scroll offset across tab switches.
 */
function DashboardGroupBody(props: GroupBodyProps) {
  const { ops, activeGroup, visibleInstances, isTablet, isMobile, scrollPositions } = props;
  const { session } = ops;
  const { isOwner } = useWorkspace();
  const content = <DashboardGroupContent {...props} />;
  const empty = Boolean(activeGroup && !activeGroup.is_overview && visibleInstances.length === 0 && !session.editMode);
  const panelProps = {
    panelId: activeGroup?.id ?? 'none',
    empty,
    canAdd: isOwner && session.canEditLayout,
    onAdd: props.onAdd,
    scrollPositions,
  };
  return activeGroup?.is_overview ? (
    <DashboardOverview {...panelProps} editMode={session.editMode} compact={isMobile || isTablet}>
      {content}
    </DashboardOverview>
  ) : (
    <DashboardPanel {...panelProps}>{content}</DashboardPanel>
  );
}

/** Page-local UI state: gadget settings target, stale-source disclosure and the starter-pack wizard. */
function useDashboardUi() {
  const [configuringInstance, setConfiguringInstance] = useState<GadgetInstance | null>(null);
  const [gadgetSettingsOpen, setGadgetSettingsOpen] = useState<boolean>(false);
  // Stale-source detail list stays collapsed until the owner asks for it.
  const [staleDetailsOpen, setStaleDetailsOpen] = useState(false);
  const [starterOpen, setStarterOpen] = useState(false);
  return {
    configuringInstance,
    setConfiguringInstance,
    gadgetSettingsOpen,
    setGadgetSettingsOpen,
    staleDetailsOpen,
    setStaleDetailsOpen,
    starterOpen,
    setStarterOpen,
  };
}

type PageCrud = ReturnType<typeof useDashboardCrud>;
type PageIntents = ReturnType<typeof useDashboardIntents>;
type PageUi = ReturnType<typeof useDashboardUi>;

/** Props of {@link DashboardTabsArea}. */
interface TabsAreaProps {
  data: PageData;
  ops: PageOps;
  crud: PageCrud;
  intents: PageIntents;
  ui: PageUi;
  isMobile: boolean;
  isTablet: boolean;
}

/**
 * Group tab strip, layout notices, edit toolbar and the active group's body. Owns the per-dashboard scroll
 * memory and the dismissible mobile hint so the page component only wires hooks together.
 */
function DashboardTabsArea({ data, ops, crud, intents, ui, isMobile, isTablet }: TabsAreaProps) {
  const workspaceSession = useWorkspaceSession();
  const { isOwner } = useWorkspace();
  const { session, save } = ops;
  const { selectedDashboardId, activeDashboard, management, sources } = data;
  const [mobileNoticeDismissed, dismissMobileNotice] = useMobileNoticeDismissal();
  const [scrollPositions] = useState(() => new Map<string, number>());
  // Remembered scroll offsets belong to one dashboard; drop them when another is selected.
  useEffect(() => {
    scrollPositions.clear();
  }, [scrollPositions, selectedDashboardId]);
  const activeGroup = activeDashboard?.groups.find((group) => group.id === session.activeGroupId);
  const updatedGroupIds = useUpdatedGroups(activeDashboard?.instances, sources.dashboardSources, session.activeGroupId);
  const visibleInstances = useVisibleInstances(activeDashboard, session.activeGroupId);
  const showMobileHint = shouldShowMobileHint(session, Boolean(activeDashboard), mobileNoticeDismissed);
  const { setPresetPickerOpen, setOpenAddInitially, openAddInitially, addDeepLink } = intents;
  const { setStaleDetailsOpen, setStarterOpen, setConfiguringInstance, setGadgetSettingsOpen } = ui;
  return (
    <Tabs
      value={session.activeGroupId}
      onValueChange={save.requestSwitchGroup}
      className="flex min-h-0 min-w-0 flex-1 flex-col gap-3 overflow-hidden"
    >
      {activeDashboard && activeDashboard.groups.length > 0 && (
        <GroupTabs
          groups={activeDashboard.groups}
          updatedGroupIds={updatedGroupIds}
          onAdd={() => crud.setGroupOpen(true)}
          canAdd={isOwner && !save.layoutOperationPending && !session.isDirty}
        />
      )}

      <TabsContent value={session.activeGroupId} className="mt-0 flex min-h-0 min-w-0 flex-1 flex-col gap-3 overflow-hidden">
        <LayoutNotices
          editOrigin={session.editOrigin}
          selectedDashboardId={selectedDashboardId}
          requestedBreakpoint={session.requestedBreakpoint}
          mobileProjectionUnavailable={session.mobileProjectionUnavailable}
          showMobileHint={showMobileHint}
          onDismissMobileHint={dismissMobileNotice}
        />
        {/* Layout Editor Toolbar (visible in Edit Mode) */}
        {session.editMode && (
          <LayoutEditorBar
            key={session.activeGroupId}
            ops={ops}
            activeDashboard={activeDashboard}
            openAddInitially={openAddInitially}
            initialTemplateId={addDeepLink?.templateId}
            initialSourceId={addDeepLink?.sourceId}
            onOpenPresets={() => setPresetPickerOpen(true)}
            onAddGroup={session.isDirty ? undefined : () => crud.setGroupOpen(true)}
          />
        )}
        <DashboardGroupBody
          ops={ops}
          data={data}
          activeGroup={activeGroup}
          visibleInstances={visibleInstances}
          csrfToken={workspaceSession.csrfToken}
          isTablet={isTablet}
          isMobile={isMobile}
          scrollPositions={scrollPositions}
          staleDetailsOpen={ui.staleDetailsOpen}
          onToggleStale={() => setStaleDetailsOpen((open) => !open)}
          onPickTopics={() => {
            management.setTab('topics');
            management.setOpen(true);
          }}
          onOpenStarterPacks={() => setStarterOpen(true)}
          onOpenPresets={() => setPresetPickerOpen(true)}
          onCreateDashboard={() => crud.setCreateOpen(true)}
          onConfigureInstance={(inst) => {
            setConfiguringInstance(inst);
            setGadgetSettingsOpen(true);
          }}
          onAdd={() => {
            setOpenAddInitially(true);
            session.setIsEditMode(true);
          }}
        />
      </TabsContent>
    </Tabs>
  );
}

/** Props of {@link DashboardOverlays}. */
interface OverlaysProps {
  data: PageData;
  session: PageOps['session'];
  crud: PageCrud;
  intents: PageIntents;
  ui: PageUi;
  layoutOperationPending: boolean;
}

/** Preset picker, starter wizard, CRUD dialogs, gadget settings and the management dialog. */
function DashboardOverlays({ data, session, crud, intents, ui, layoutOperationPending }: OverlaysProps) {
  const { selectedDashboardId, activeDashboard, management } = data;
  const { presetPickerOpen, setPresetPickerOpen, presetInitialId, setPresetInitialId, presetInitialMode, setPresetInitialMode } = intents;
  const { starterOpen, setStarterOpen, gadgetSettingsOpen, setGadgetSettingsOpen, configuringInstance } = ui;
  return (
    <>
      {/* Preset Picker Modal */}
      <ConnectedPresetPicker
        open={presetPickerOpen}
        initialPresetId={presetInitialId}
        initialApplyMode={presetInitialMode}
        data={data}
        session={session}
        onOpenChange={(next) => {
          setPresetPickerOpen(next);
          if (!next) {
            setPresetInitialId(undefined);
            setPresetInitialMode(undefined);
          }
        }}
      />
      {starterOpen && (
        <ConnectedStarterWizard
          onClose={() => setStarterOpen(false)}
          onBuildDashboard={() => {
            setStarterOpen(false);
            setPresetInitialId('monitor');
            setPresetPickerOpen(true);
          }}
        />
      )}
      <DashboardCrudDialogs
        crud={crud}
        activeDashboardName={activeDashboard?.name ?? ''}
        layoutOperationPending={layoutOperationPending}
        // The template is appended to the active dashboard through the preset picker, not into the new page.
        onFromTemplate={(id) => {
          crud.setGroupOpen(false);
          setPresetInitialId(id);
          setPresetInitialMode('append');
          setPresetPickerOpen(true);
        }}
      />
      <ConnectedGadgetSettings
        open={gadgetSettingsOpen}
        onOpenChange={setGadgetSettingsOpen}
        instance={configuringInstance}
        selectedDashboardId={selectedDashboardId}
        activeDashboard={activeDashboard}
      />
      <ManagementDialog management={management} />
    </>
  );
}

/**
 * Main Umwelt-OS dashboard view with a fixed header, persistent panel tabs, and panel-owned scrolling.
 * Overview owns the map split while other tabs fill the viewport. Layout history and guarded switching
 * remain dashboard-scoped but edits are confined to the selected group and breakpoint.
 *
 * Composition only: data loading lives in `useDashboardData`, the editing session in `useLayoutSession`,
 * `useLayoutSave` and `useGadgetMembership`, dialogs in `useDashboardCrud`.
 *
 * @param dashboardId Dashboard identity from the canonical dashboard route, when present.
 * @returns Authenticated dashboard view component.
 */
export function DashboardPage({ dashboardId }: { dashboardId?: string }) {
  const { isOwner } = useWorkspace();
  const { isMobile, isTablet } = useDashboardMedia();
  const data = useDashboardData(dashboardId);
  const { dashboards, selectedDashboardId, activeDashboard, management } = data;
  const session = useLayoutSession({ selectedDashboardId, activeDashboard, isMobile });
  const save = useLayoutSave(session, data.navigateToDashboard, selectedDashboardId);
  const membership = useGadgetMembership(session, save.layoutOperationPending);
  const crud = useDashboardCrud({
    activeDashboard,
    dashboards,
    layoutOperationPending: save.layoutOperationPending,
    navigateToDashboard: data.navigateToDashboard,
  });
  const ops = { session, save, membership };
  const ui = useDashboardUi();
  useActivateCreatedGroup(crud, activeDashboard, session);
  const intents = useDashboardIntents(session, activeDashboard, isOwner);

  return (
    <section
      className="dashboard-page flex h-full min-h-0 w-full flex-1 flex-col gap-3 overflow-hidden"
      data-dashboard-ready={data.dashboardRouteReady}
    >
      <ConnectedHeader
        data={data}
        ops={ops}
        crud={crud}
        onOpenPresets={() => intents.setPresetPickerOpen(true)}
        onOpenManagement={() => management.setOpen(true)}
      />
      <DashboardTabsArea data={data} ops={ops} crud={crud} intents={intents} ui={ui} isMobile={isMobile} isTablet={isTablet} />
      <DashboardOverlays
        data={data}
        session={session}
        crud={crud}
        intents={intents}
        ui={ui}
        layoutOperationPending={save.layoutOperationPending}
      />
    </section>
  );
}
