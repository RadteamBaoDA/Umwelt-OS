'use client';

import dynamic from 'next/dynamic';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { type ComponentProps, type RefObject, useCallback, useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { useTranslations } from 'next-intl';
import { UmweltLogo, UmweltMark } from '@/components/brand/umwelt-mark';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { ApiError } from '@/core/api';
import { CommandPalette } from '@/core/command-palette';
import { ConnectionFooter } from '@/core/app-shell/connection-footer';
import { useChatController } from '@/core/app-shell/chat-controller';
import { ShellNav } from '@/core/app-shell/shell-nav';
import { activeNavigationId, computeShellRedirects, visibleMainNavigationFor } from '@/core/app-shell/shell-redirects';
import { SettingsPanel, SourcesSubNav } from '@/core/app-shell/settings-nav';
import { useShellSession } from '@/core/app-shell/use-shell-session';
import { SessionContext, useWorkspaceSession } from '@/core/app-shell/workspace-session';
import { WorkspaceSwitcher } from '@/core/app-shell/workspace-switcher';
import { useWorkspace } from '@/core/workspace-context';
import { destinationEnabled, destinationForRole, mainNavigation, settingsExtraLinks, settingsGroups } from '@/core/module-registry';
import { useDisplayPreferences } from '@/core/query-provider';
import { useRealtime } from '@/core/realtime-provider';
import { AccountDeletionSection, ChangePasswordDialog, GoogleLink, PreferencesDialog, UserMenu } from '@/modules/account';
import { LegalLinks } from '@/modules/legal';
import { NotificationBell } from '@/modules/notifications';

/** Browser connectivity (navigator.onLine); assumed online during server render. */
function useBrowserOnline() {
  return useSyncExternalStore(
    (notify) => {
      window.addEventListener('online', notify);
      window.addEventListener('offline', notify);
      return () => {
        window.removeEventListener('online', notify);
        window.removeEventListener('offline', notify);
      };
    },
    () => navigator.onLine,
    () => true,
  );
}

/** Per-device rail preference; the rail stays icon-only until the owner expands it. */
const NAV_EXPANDED_KEY = 'umwelt.nav.expanded';
const NAV_EXPANDED_EVENT = 'umwelt:nav-expanded';

/**
 * Expanded/collapsed state of the desktop navigation rail, persisted in localStorage.
 * Server render and blocked storage report collapsed; the setter notifies other subscribers in this tab.
 * @returns The current state and a toggle that flips and persists it.
 */
function useNavExpanded(): [boolean, () => void] {
  const expanded = useSyncExternalStore(
    (notify) => {
      window.addEventListener(NAV_EXPANDED_EVENT, notify);
      window.addEventListener('storage', notify);
      return () => {
        window.removeEventListener(NAV_EXPANDED_EVENT, notify);
        window.removeEventListener('storage', notify);
      };
    },
    () => {
      try {
        return localStorage.getItem(NAV_EXPANDED_KEY) === '1';
      } catch {
        return false;
      }
    },
    () => false,
  );
  const toggle = useCallback(() => {
    try {
      if (expanded) localStorage.removeItem(NAV_EXPANDED_KEY);
      else localStorage.setItem(NAV_EXPANDED_KEY, '1');
    } catch {
      /* storage blocked: keep current state */
    }
    window.dispatchEvent(new Event(NAV_EXPANDED_EVENT));
  }, [expanded]);
  return [expanded, toggle];
}
// Re-exported so existing importers of the shell keep working; the context itself lives in the light module.
export { useWorkspaceSession };

/** Chat drawer chunk (chat session, markdown, generative-UI and chart libraries) fetched only when the drawer is first opened. */
const ChatDrawer = dynamic(() => import('@/modules/chat').then((m) => m.ChatDrawer), { ssr: false });

/**
 * Mounts the lazily loaded chat drawer on first open and keeps it mounted afterwards, so the Sheet's close animation,
 * in-memory drafts and background generation behave as before while closed sessions never download the chat bundle.
 */
function LazyChatDrawer() {
  const { isDrawerOpen } = useChatController();
  const [everOpened, setEverOpened] = useState(isDrawerOpen);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- latches the first open so the drawer stays mounted
    if (isDrawerOpen) setEverOpened(true);
  }, [isDrawerOpen]);
  return everOpened || isDrawerOpen ? <ChatDrawer /> : null;
}

/**
 * Registers the Cmd+J / Ctrl+J shortcut that toggles the quick-chat Sheet drawer. Renders nothing: the header
 * button was removed (Quick chat stays reachable here and from the command palette).
 *
 * @returns null.
 */
function QuickChatShortcut() {
  const { toggleDrawer } = useChatController();
  const pathname = usePathname();

  useEffect(() => {
    /** Global shortcut Ctrl+J or Cmd+J to toggle chat drawer without rebinding on draft changes. */
    const handleKeyDown = (e: KeyboardEvent) => {
      // The full Chat page is already the chat surface: opening a drawer over it would duplicate the session.
      if (pathname === '/chat' || pathname.startsWith('/chat/')) return;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'j') {
        e.preventDefault();
        toggleDrawer();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [toggleDrawer, pathname]);

  return null;
}

/**
 * Registers Ctrl/Cmd+Shift+O on the full Chat page to start a new chat. Renders nothing; elsewhere the key keeps its
 * browser meaning. The composer focuses itself when the conversation resets.
 *
 * @returns null.
 */
export function NewChatShortcut() {
  const { resetConversation } = useChatController();
  const pathname = usePathname();
  const onChatPage = pathname === '/chat' || pathname.startsWith('/chat/');

  useEffect(() => {
    if (!onChatPage) return;
    /** Starts a clean conversation on Ctrl/Cmd+Shift+O (Alt excluded so AltGr layouts are untouched). */
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && !e.altKey && e.key.toLowerCase() === 'o') {
        e.preventDefault();
        resetConversation();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onChatPage, resetConversation]);

  return null;
}

type ApiHealth = { isFetching: boolean; isError: boolean; isPending: boolean; refetch: () => unknown };

/** Maps the health query and browser connectivity to the top-bar API status. */
function apiStatusOf(online: boolean, health: ApiHealth) {
  if (!online) return 'clientOffline';
  if (health.isFetching && health.isError) return 'reconnecting';
  if (health.isPending) return 'connecting';
  return health.isError ? 'unavailable' : 'connected';
}

/** Minimal shell frame used while the session loads or is unavailable: brand, connection status and main content. */
function ShellFrame({
  apiStatus,
  health,
  children,
}: {
  apiStatus: Parameters<typeof ConnectionFooter>[0]['apiStatus'];
  health: ApiHealth;
  children: ReactNode;
}) {
  const realtime = useRealtime();
  return (
    <div className="shell">
      <header className="topbar">
        <Link href="/login" className="brand-name" aria-label="Umwelt-OS">
          <UmweltLogo />
        </Link>
        <ConnectionFooter
          apiStatus={apiStatus}
          realtimeStatus={realtime.status}
          onRetry={() => health.refetch()}
          retrying={health.isFetching}
        />
      </header>
      {children}
    </div>
  );
}

/** Full-page error state shown when the session cannot be read; a 401 reads as an expired session. */
function SessionUnavailable({
  error,
  online,
  health,
  onRetry,
}: {
  error: unknown;
  online: boolean;
  health: ApiHealth;
  onRetry: () => void;
}) {
  const t = useTranslations('shell');
  const expired = error instanceof ApiError && error.status === 401;
  return (
    <ShellFrame apiStatus={expired ? 'expired' : online ? 'unavailable' : 'clientOffline'} health={health}>
      <main id="main-content" tabIndex={-1} className="page">
        <section className="status-panel">
          <h1>{expired ? t('expiredTitle') : t('workspaceUnavailable')}</h1>
          <p className="text-muted-foreground">{expired ? t('expiredHelp') : t('workspaceUnavailableHelp')}</p>
          <Button variant="outline" onClick={onRetry}>
            {t('retry')}
          </Button>
        </section>
      </main>
    </ShellFrame>
  );
}

/** Top bar: brand mark (phones), connection status and the action cluster (search, notifications, workspace, user menu). */
function ShellTopbar({
  apiStatus,
  health,
  quickChat,
  menuTriggerRef,
  onOpenPreferences,
  onOpenAccount,
  onOpenChangePassword,
  onSignOut,
  signOutPending,
}: {
  apiStatus: Parameters<typeof ConnectionFooter>[0]['apiStatus'];
  health: ApiHealth;
  /** Registers the Ctrl/Cmd+J drawer shortcut (dashboard detail routes when chat is available). */
  quickChat: boolean;
  menuTriggerRef: RefObject<HTMLButtonElement | null>;
  onOpenPreferences: () => void;
  onOpenAccount: () => void;
  onOpenChangePassword: () => void;
  onSignOut: () => void;
  signOutPending: boolean;
}) {
  const realtime = useRealtime();
  return (
    <header className="topbar">
      <Link href="/dashboard" className="brand-name sm:hidden" aria-label="Umwelt-OS">
        <UmweltMark />
      </Link>
      <ConnectionFooter
        apiStatus={apiStatus}
        realtimeStatus={realtime.status}
        onRetry={() => health.refetch()}
        retrying={health.isFetching}
      />
      <div className="top-actions">
        {quickChat && <QuickChatShortcut />}
        <NewChatShortcut />
        <CommandPalette />
        {/* Phones (<640px) get these inside the avatar menu to keep the header to logo, search and avatar. */}
        <div className="hidden sm:contents">
          <NotificationBell />
        </div>
        <div className="hidden sm:contents">
          <WorkspaceSwitcher />
        </div>
        <UserMenu
          triggerRef={menuTriggerRef}
          onOpenPreferences={onOpenPreferences}
          onOpenAccount={onOpenAccount}
          onOpenChangePassword={onOpenChangePassword}
          onSignOut={onSignOut}
          signOutPending={signOutPending}
        />
      </div>
    </header>
  );
}

/** Replaces the current route with `target` once it is defined (role-based confinement); no-op otherwise. */
function useReplaceRoute(target: string | undefined): void {
  const router = useRouter();
  useEffect(() => {
    if (target) router.replace(target);
  }, [router, target]);
}

/**
 * Computes the settings groups visible to `role` and applies the role-based route confinement (replaces the route when one applies).
 * @returns The visible groups plus the member/admin-viewer settings redirects the shell uses to hide flashes of content.
 */
function useShellRedirects(
  role: Parameters<typeof destinationForRole>[1],
  pathname: string,
  availability: Parameters<typeof destinationEnabled>[1],
  isInstanceAdmin: boolean | undefined,
) {
  const visibleSettingsGroups = settingsGroups.filter((item) => destinationEnabled(item, availability) && destinationForRole(item, role));
  const { memberSettingsRedirect, adminViewerSettingsRedirect, adminViewerPrivateRedirect } = computeShellRedirects(
    role,
    pathname,
    visibleSettingsGroups,
    isInstanceAdmin,
  );
  useReplaceRoute(memberSettingsRedirect ?? adminViewerSettingsRedirect ?? adminViewerPrivateRedirect);
  return { visibleSettingsGroups, memberSettingsRedirect, adminViewerSettingsRedirect };
}

/** Settings page body: the sources sub-navigation (only under /settings/sources) followed by the route content. */
function SettingsBody({ pathname, children }: { pathname: string; children: ReactNode }) {
  return (
    <>
      {pathname.startsWith('/settings/sources') && <SourcesSubNav pathname={pathname} />}
      {children}
    </>
  );
}

/**
 * Scrolling main region. Renders nothing while a role redirect is pending (avoids flashing a forbidden page),
 * wraps /settings routes in the settings panel (extra links filtered by admin/role) and passes other routes through.
 */
function ShellMain({
  role,
  pathname,
  redirecting,
  settingsGroups: groups,
  isInstanceAdmin,
  children,
}: {
  role: Parameters<typeof destinationForRole>[1];
  pathname: string;
  redirecting: boolean;
  settingsGroups: typeof settingsGroups;
  isInstanceAdmin: boolean;
  children: ReactNode;
}) {
  let content = children;
  if (redirecting) content = null;
  else if (pathname.startsWith('/settings')) {
    const extra = settingsExtraLinks.filter((item) => (item.adminOnly ? isInstanceAdmin : destinationForRole(item, role)));
    content = (
      <SettingsPanel links={[...groups, ...extra]} pathname={pathname}>
        <SettingsBody pathname={pathname}>{children}</SettingsBody>
      </SettingsPanel>
    );
  }
  return (
    <main id="main-content" tabIndex={-1} className="workspace-main" data-workspace-readonly={role === 'admin_viewer' || undefined}>
      {content}
    </main>
  );
}

type ShellPreferencesProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  query: ReturnType<typeof useShellSession>['preferences'];
  onRetry: ComponentProps<typeof PreferencesDialog>['onRetry'];
  csrfToken: string;
  authGeneration: number;
  onCloseAutoFocus: (event: Event) => void;
};

/**
 * Preferences dialog wired to the owner-preferences query. Shows the saved value (query data, else the last confirmed
 * preferences, else null); loading/error states apply only while no data exists, and saving is disabled until the
 * query has loaded so a stale confirmed value can never be overwritten blindly.
 */
function ShellPreferences({ open, onOpenChange, query, onRetry, csrfToken, authGeneration, onCloseAutoFocus }: ShellPreferencesProps) {
  const display = useDisplayPreferences();
  return (
    <PreferencesDialog
      open={open}
      onOpenChange={onOpenChange}
      preferences={query.data ?? display.confirmedPreferences ?? null}
      loading={query.isPending && !query.data}
      loadError={query.isError && !query.data}
      retrying={query.isFetching}
      onRetry={onRetry}
      csrfToken={csrfToken}
      savingDisabled={!query.data}
      authGeneration={authGeneration}
      onCloseAutoFocus={onCloseAutoFocus}
    />
  );
}

/** Account-settings dialog: Google link, account deletion and legal links. */
function AccountDialog({
  open,
  onOpenChange,
  csrfToken,
  onCloseAutoFocus,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  csrfToken: string;
  onCloseAutoFocus: (event: Event) => void;
}) {
  const t = useTranslations('shell');
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent closeLabel={t('close')} onCloseAutoFocus={onCloseAutoFocus}>
        <DialogHeader>
          <DialogTitle>{t('accountSettings')}</DialogTitle>
          <DialogDescription>{t('googleAccountDescription')}</DialogDescription>
        </DialogHeader>
        <GoogleLink />
        <AccountDeletionSection csrfToken={csrfToken} />
        <LegalLinks />
      </DialogContent>
    </Dialog>
  );
}

/** Preferences, account and change-password dialogs owned by the shell; open state stays in the shell so it can reset on sign-out. */
function ShellDialogs({
  signOutFailed,
  csrfToken,
  open,
  setOpen,
  preferences,
  onRetry,
  authGeneration,
  onCloseAutoFocus,
}: {
  signOutFailed: boolean;
  csrfToken: string;
  open: { preferencesOpen: boolean; accountOpen: boolean; passwordOpen: boolean };
  setOpen: { setPreferencesOpen: (v: boolean) => void; setAccountOpen: (v: boolean) => void; setPasswordOpen: (v: boolean) => void };
  preferences: ComponentProps<typeof ShellPreferences>['query'];
  onRetry: ComponentProps<typeof ShellPreferences>['onRetry'];
  authGeneration: number;
  onCloseAutoFocus: (event: Event) => void;
}) {
  const t = useTranslations('shell');
  return (
    <>
      {signOutFailed && (
        <p className="error" role="alert">
          {t('signOutFailed')}
        </p>
      )}
      <ShellPreferences
        open={open.preferencesOpen}
        onOpenChange={setOpen.setPreferencesOpen}
        query={preferences}
        onRetry={onRetry}
        csrfToken={csrfToken}
        authGeneration={authGeneration}
        onCloseAutoFocus={onCloseAutoFocus}
      />
      <AccountDialog
        open={open.accountOpen}
        onOpenChange={setOpen.setAccountOpen}
        csrfToken={csrfToken}
        onCloseAutoFocus={onCloseAutoFocus}
      />
      <ChangePasswordDialog
        open={open.passwordOpen}
        onOpenChange={setOpen.setPasswordOpen}
        csrfToken={csrfToken}
        closeLabel={t('close')}
        onCloseAutoFocus={onCloseAutoFocus}
      />
    </>
  );
}

/**
 * Render the authenticated app shell with session, route, and responsive main-navigation state.
 * Navigation is a 56px icon rail (logo at its top, tooltip plus accessible name per destination) beside a 48px top bar;
 * a toggle at the rail's foot expands it to a 200px labelled column (per-device preference).
 * at 720px and below the rail becomes the labelled bottom bar. The shell keeps the header,
 * navigation, and top-bar connection status fixed while `.workspace-main` owns page scrolling.
 * @param children The current route content rendered inside the main scroll region.
 */
export function WorkspaceShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const t = useTranslations('shell');
  const role = useWorkspace().selection?.role;
  const online = useBrowserOnline();
  const [navExpanded, toggleNav] = useNavExpanded();
  const [accountOpen, setAccountOpen] = useState(false);
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [preferencesOpen, setPreferencesOpen] = useState(false);
  const menuTriggerRef = useRef<HTMLButtonElement>(null);
  /** Closes the dialogs owned by the shell when the session ends (setters are stable). */
  const resetOverlays = useCallback(() => {
    setPreferencesOpen(false);
    setAccountOpen(false);
  }, []);
  const { session, moduleAvailability, preferences, apiHealth, logout, reloadPreferences, generation } = useShellSession(resetOverlays);

  const { visibleSettingsGroups, memberSettingsRedirect, adminViewerSettingsRedirect } = useShellRedirects(
    role,
    pathname,
    moduleAvailability.data,
    session.data?.isInstanceAdmin,
  );

  if (session.isPending)
    return (
      <ShellFrame apiStatus="connecting" health={apiHealth}>
        <main id="main-content" tabIndex={-1} className="page">
          <div className="status-panel skeleton" aria-label={t('loadingWorkspace')} />
        </main>
      </ShellFrame>
    );
  if (session.isError || !session.data) {
    return <SessionUnavailable error={session.error} online={online} health={apiHealth} onRetry={() => session.refetch()} />;
  }

  // Members see only the read-only translation settings group; their Settings entry opens it directly.
  const visibleMainNavigation = visibleMainNavigationFor(
    mainNavigation,
    moduleAvailability.data,
    role,
    visibleSettingsGroups.length,
    session.data.isInstanceAdmin,
  );
  const active = activeNavigationId(visibleMainNavigation, pathname);
  /** Returns focus to the triggering menu control after a dialog closes. */
  const restoreMenuFocus = (event: Event) => {
    event.preventDefault();
    menuTriggerRef.current?.focus();
  };

  return (
    <SessionContext.Provider value={session.data}>
      <div className="shell">
        <a href="#main-content" className="skip-link">
          {t('skipToContent')}
        </a>
        <div className="workspace">
          <ShellNav
            items={visibleMainNavigation}
            activeId={active}
            pathname={pathname}
            role={role}
            firstSettingsHref={visibleSettingsGroups[0]?.href}
            expanded={navExpanded}
            onToggle={toggleNav}
          />
          <div className="workspace-column">
            <ShellTopbar
              apiStatus={apiStatusOf(online, apiHealth)}
              health={apiHealth}
              quickChat={pathname.startsWith('/dashboard/') && visibleMainNavigation.some((item) => item.id === 'chat')}
              menuTriggerRef={menuTriggerRef}
              onOpenPreferences={() => setPreferencesOpen(true)}
              onOpenAccount={() => setAccountOpen(true)}
              onOpenChangePassword={() => setPasswordOpen(true)}
              onSignOut={() => logout.mutate()}
              signOutPending={logout.isPending}
            />
            <ShellMain
              role={role}
              pathname={pathname}
              redirecting={Boolean(memberSettingsRedirect || adminViewerSettingsRedirect)}
              settingsGroups={visibleSettingsGroups}
              isInstanceAdmin={session.data.isInstanceAdmin}
            >
              {children}
            </ShellMain>
          </div>
        </div>
        <ShellDialogs
          signOutFailed={Boolean(logout.error)}
          csrfToken={session.data.csrfToken}
          open={{ preferencesOpen, accountOpen, passwordOpen }}
          setOpen={{ setPreferencesOpen, setAccountOpen, setPasswordOpen }}
          preferences={preferences}
          onRetry={reloadPreferences}
          authGeneration={generation}
          onCloseAutoFocus={restoreMenuFocus}
        />
        {role !== 'admin_viewer' && <LazyChatDrawer />}
      </div>
    </SessionContext.Provider>
  );
}
