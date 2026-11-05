import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useLayoutSave } from './use-layout-save';
import type { LayoutSession } from './use-layout-session';

const mocks = vi.hoisted(() => ({ replaceDashboardLayout: vi.fn(), continueNavigation: vi.fn(), isValid: vi.fn(() => true) }));
const translate = (key: string) => key;
const workspace = { csrfToken: 'csrf-1' };
const guarded = { continueNavigation: mocks.continueNavigation };
vi.mock('next-intl', () => ({ useTranslations: () => translate }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => workspace }));
vi.mock('@/core/guarded-navigation', () => ({ useGuardedNavigation: () => guarded }));
vi.mock('./api', async (original) => ({
  ...(await original<typeof import('./api')>()),
  replaceDashboardLayout: mocks.replaceDashboardLayout,
}));
vi.mock('./layout-geometry', async (original) => ({
  ...(await original<typeof import('./layout-geometry')>()),
  isValidLayoutSnapshot: mocks.isValid,
}));

const placement = (id: string, x = 0) => ({ instance_id: id, x, y: 0, w: 4, h: 4 });

/** Fake LayoutSession: plain refs and spied setters, so the hook's orchestration can be asserted directly. */
function makeSession(overrides: Record<string, unknown> = {}) {
  const session = {
    activeDashboard: { id: 'd1', revision: 7, instances: [] },
    editOrigin: null,
    currentBreakpoint: 'desktop',
    layoutColumns: 20,
    savedPlacements: [placement('a')],
    draftPlacements: [placement('a', 4)],
    mobileProjectionUnavailable: false,
    isDirty: true,
    membershipMutationsPending: 0,
    saveOperationPendingRef: { current: false },
    membershipOperationCountRef: { current: 0 },
    pendingExitEditRef: { current: false },
    pendingDashboardSwitchRef: { current: null as string | null },
    pendingRouteNavigationRef: { current: null as unknown },
    pendingBreakpointRef: { current: 'mobile' as string | null },
    dismissedBreakpointRef: { current: null as string | null },
    setSaveError: vi.fn(),
    setDirtyModalOpen: vi.fn(),
    setIsEditMode: vi.fn(),
    setEditOrigin: vi.fn(),
    setActiveGroupId: vi.fn(),
    setIsDirty: vi.fn(),
    setDraftPlacements: vi.fn(),
    setHistory: vi.fn(),
    setHistoryIndex: vi.fn(),
    ...overrides,
  };
  return session;
}

function setup(session: ReturnType<typeof makeSession>, selected: string | null = 'd1') {
  const navigate = vi.fn();
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const hook = renderHook(() => useLayoutSave(session as unknown as LayoutSession, navigate, selected), { wrapper });
  return { navigate, client, ...hook };
}

describe('useLayoutSave', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.isValid.mockReturnValue(true);
  });

  it('handleSave sends the draft with the base revision and csrf token, then resets the draft state', async () => {
    mocks.replaceDashboardLayout.mockResolvedValue({ id: 'd1' });
    const session = makeSession();
    const { result } = setup(session);
    act(() => result.current.handleSave());
    await waitFor(() => expect(session.setIsDirty).toHaveBeenCalledWith(false));
    expect(mocks.replaceDashboardLayout).toHaveBeenCalledWith(
      'd1',
      { expected_revision: 7, breakpoint: 'desktop', columns: 20, items: [placement('a', 4)] },
      'csrf-1',
    );
    expect(session.setHistory).toHaveBeenCalledWith([[placement('a', 4)]]);
    expect(session.setHistoryIndex).toHaveBeenCalledWith(0);
    expect(session.pendingBreakpointRef.current).toBeNull();
    await waitFor(() => expect(session.saveOperationPendingRef.current).toBe(false));
  });

  it('surfaces a failed save, keeps the draft dirty and releases the save guard', async () => {
    mocks.replaceDashboardLayout.mockRejectedValue(new Error('revision conflict'));
    const session = makeSession();
    const { result } = setup(session);
    act(() => result.current.handleSave());
    await waitFor(() => expect(session.setSaveError).toHaveBeenCalledWith('revision conflict'));
    expect(session.setIsDirty).not.toHaveBeenCalled();
    await waitFor(() => expect(session.saveOperationPendingRef.current).toBe(false));
  });

  it('refuses to save an invalid snapshot or an unavailable mobile projection', () => {
    mocks.isValid.mockReturnValue(false);
    const invalid = makeSession();
    {
      const h = setup(invalid);
      act(() => h.result.current.handleSave());
    }
    expect(invalid.setSaveError).toHaveBeenCalledWith('layoutActionUnavailable');
    const unavailable = makeSession({ mobileProjectionUnavailable: true });
    {
      const h = setup(unavailable);
      act(() => h.result.current.handleSave());
    }
    expect(unavailable.setSaveError).toHaveBeenCalledWith('layoutActionUnavailable');
    expect(mocks.replaceDashboardLayout).not.toHaveBeenCalled();
  });

  it('does not start a second save while one is pending or a membership change is running', () => {
    const pending = makeSession({ saveOperationPendingRef: { current: true } });
    {
      const h = setup(pending);
      act(() => h.result.current.handleSave());
    }
    const membership = makeSession({ membershipOperationCountRef: { current: 1 } });
    {
      const h = setup(membership);
      act(() => h.result.current.handleSave());
    }
    expect(mocks.replaceDashboardLayout).not.toHaveBeenCalled();
  });

  it('requestExitEdit leaves edit mode when clean and asks for confirmation when dirty', () => {
    const clean = makeSession({ isDirty: false });
    {
      const h = setup(clean);
      act(() => h.result.current.requestExitEdit());
    }
    expect(clean.setIsEditMode).toHaveBeenCalledWith(false);
    expect(clean.setEditOrigin).toHaveBeenCalledWith(null);
    const dirty = makeSession();
    {
      const h = setup(dirty);
      act(() => h.result.current.requestExitEdit());
    }
    expect(dirty.pendingExitEditRef.current).toBe(true);
    expect(dirty.setDirtyModalOpen).toHaveBeenCalledWith(true);
    expect(dirty.setIsEditMode).not.toHaveBeenCalled();
  });

  it('requestSwitchDashboard ignores the current dashboard, navigates when clean and defers when dirty', () => {
    const clean = makeSession({ isDirty: false });
    const cleanHook = setup(clean);
    act(() => cleanHook.result.current.requestSwitchDashboard('d1'));
    expect(cleanHook.navigate).not.toHaveBeenCalled();
    act(() => cleanHook.result.current.requestSwitchDashboard('d2'));
    expect(cleanHook.navigate).toHaveBeenCalledWith('d2');
    expect(clean.setActiveGroupId).toHaveBeenCalledWith('all');
    const dirty = makeSession();
    const dirtyHook = setup(dirty);
    act(() => dirtyHook.result.current.requestSwitchDashboard('d2'));
    expect(dirty.pendingDashboardSwitchRef.current).toBe('d2');
    expect(dirty.setDirtyModalOpen).toHaveBeenCalledWith(true);
    expect(dirtyHook.navigate).not.toHaveBeenCalled();
  });

  it('blocks exit and switch while a layout operation is pending', () => {
    const session = makeSession({ membershipMutationsPending: 1, isDirty: false });
    const { result, navigate } = setup(session);
    expect(result.current.layoutOperationPending).toBe(true);
    act(() => {
      result.current.requestExitEdit();
      result.current.requestSwitchDashboard('d2');
    });
    expect(session.setIsEditMode).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  describe('handleDirtyModalResolution', () => {
    it('stay clears every pending intent and remembers the dismissed breakpoint', async () => {
      const session = makeSession();
      session.pendingDashboardSwitchRef.current = 'd2';
      session.pendingExitEditRef.current = true;
      const { result } = setup(session);
      await act(() => result.current.handleDirtyModalResolution('stay'));
      expect(session.setDirtyModalOpen).toHaveBeenCalledWith(false);
      expect(session.pendingDashboardSwitchRef.current).toBeNull();
      expect(session.pendingExitEditRef.current).toBe(false);
      expect(session.dismissedBreakpointRef.current).toBe('mobile');
      expect(session.pendingBreakpointRef.current).toBeNull();
    });

    it('discard restores the saved layout and then performs the deferred dashboard switch', async () => {
      const session = makeSession();
      session.pendingDashboardSwitchRef.current = 'd2';
      const { result, navigate } = setup(session);
      await act(() => result.current.handleDirtyModalResolution('discard'));
      expect(session.setDraftPlacements).toHaveBeenCalledWith(session.savedPlacements);
      expect(session.setIsDirty).toHaveBeenCalledWith(false);
      expect(navigate).toHaveBeenCalledWith('d2');
      expect(session.setActiveGroupId).toHaveBeenCalledWith('all');
      expect(mocks.replaceDashboardLayout).not.toHaveBeenCalled();
    });

    it('discard continues a pending route navigation instead of the local intents', async () => {
      const session = makeSession();
      const intent = { href: '/chat' };
      session.pendingRouteNavigationRef.current = intent;
      session.pendingExitEditRef.current = true;
      const { result, navigate } = setup(session);
      await act(() => result.current.handleDirtyModalResolution('discard'));
      expect(mocks.continueNavigation).toHaveBeenCalledWith(intent);
      expect(session.pendingExitEditRef.current).toBe(false);
      expect(session.setIsEditMode).not.toHaveBeenCalled();
      expect(navigate).not.toHaveBeenCalled();
    });

    it('save persists first, then leaves edit mode', async () => {
      mocks.replaceDashboardLayout.mockResolvedValue({ id: 'd1' });
      const session = makeSession();
      session.pendingExitEditRef.current = true;
      const { result } = setup(session);
      await act(() => result.current.handleDirtyModalResolution('save'));
      expect(mocks.replaceDashboardLayout).toHaveBeenCalledTimes(1);
      expect(session.setIsEditMode).toHaveBeenCalledWith(false);
      expect(session.saveOperationPendingRef.current).toBe(false);
    });

    it('a failed save reopens the modal for a pending route navigation and drops local intents otherwise', async () => {
      mocks.replaceDashboardLayout.mockRejectedValue(new Error('conflict'));
      const route = makeSession();
      route.pendingRouteNavigationRef.current = { href: '/chat' };
      {
        const h = setup(route);
        await act(() => h.result.current.handleDirtyModalResolution('save'));
      }
      expect(route.setDirtyModalOpen).toHaveBeenLastCalledWith(true);
      expect(mocks.continueNavigation).not.toHaveBeenCalled();

      const local = makeSession();
      local.pendingDashboardSwitchRef.current = 'd2';
      local.pendingExitEditRef.current = true;
      {
        const h = setup(local);
        await act(() => h.result.current.handleDirtyModalResolution('save'));
      }
      expect(local.pendingDashboardSwitchRef.current).toBeNull();
      expect(local.pendingExitEditRef.current).toBe(false);
      expect(local.setIsEditMode).not.toHaveBeenCalled();
    });
  });
});
