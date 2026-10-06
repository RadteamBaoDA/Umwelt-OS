'use client';

import { createContext, useCallback, useContext, useEffect, useRef, type ReactNode } from 'react';
import { usePathname, useRouter } from 'next/navigation';

export type NavigationMode = 'push' | 'replace';
export interface GuardedNavigationIntent {
  href: string;
  mode: NavigationMode;
}
export type LeaveGuard = {
  hasUnsavedChanges: () => boolean;
  confirmDiscard: (intent?: GuardedNavigationIntent) => boolean;
  acceptLeave: () => void;
};
type GuardedNavigation = {
  registerLeaveGuard: (guard: LeaveGuard) => () => void;
  navigate: (href: string, mode?: NavigationMode) => boolean;
  continueNavigation: (intent: GuardedNavigationIntent) => void;
  ensureSourcesDocument: () => boolean;
  ensureEntityDocument: () => boolean;
};

const GuardedNavigationContext = createContext<GuardedNavigation | null>(null);
const sourcePaths = new Set(['/sources', '/settings/sources']);

/** Checks whether a pathname is one of the registered data-source routes. */
function isSourcesPath(pathname: string) {
  return sourcePaths.has(pathname);
}

/** Checks whether a pathname identifies an entity detail route. */
function isEntityDetailPath(pathname: string) {
  return /^\/knowledge\/entities\/[^/]+\/?$/.test(pathname);
}

/** Provides navigation that checks registered unsaved-change guards and handles full-page source-route transitions. */
export function GuardedNavigationProvider({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const guardRef = useRef<LeaveGuard | null>(null);
  const documentPath = useRef(pathname);
  const entityDocumentPath = useRef(isEntityDetailPath(pathname) ? pathname : null);
  const entityReloadRequested = useRef<string | null>(null);
  const observedPath = useRef(pathname);

  useEffect(() => {
    if (pathname === observedPath.current) return;
    if (isEntityDetailPath(observedPath.current)) entityDocumentPath.current = null;
    observedPath.current = pathname;
  }, [pathname]);

  /** Registers the active editor leave guard and returns a cleanup that removes only that same guard. */
  const registerLeaveGuard = useCallback((guard: LeaveGuard) => {
    guardRef.current = guard;
    return () => {
      if (guardRef.current === guard) guardRef.current = null;
    };
  }, []);

  /** Reloads the source route when client navigation would leave it with stale route-owned state. */
  const ensureSourcesDocument = useCallback(() => {
    if (!isSourcesPath(pathname) || isSourcesPath(documentPath.current)) return true;
    window.location.replace(window.location.href);
    return false;
  }, [pathname]);

  /** Reloads an entity detail route once per destination to refresh route-owned entity data. */
  const ensureEntityDocument = useCallback(() => {
    if (!isEntityDetailPath(pathname) || entityDocumentPath.current === pathname) return true;
    if (entityReloadRequested.current !== pathname) {
      entityReloadRequested.current = pathname;
      window.location.replace(window.location.href);
    }
    return false;
  }, [pathname]);

  /** Performs the accepted route transition, preserving full-page source-route behavior. */
  const performNavigation = useCallback((href: string, mode: NavigationMode, acceptGuard: boolean) => {
    const destination = new URL(href, window.location.href);
    const leavesPage = destination.origin !== window.location.origin || destination.pathname !== pathname;
    const guard = guardRef.current;
    if (acceptGuard && leavesPage && guard?.hasUnsavedChanges()) {
      if (!guard.confirmDiscard({ href: destination.href, mode })) return false;
      guard.acceptLeave();
    }
    if (!acceptGuard) guard?.acceptLeave();
    if (destination.origin !== window.location.origin) {
      guard?.acceptLeave();
      if (mode === 'replace') window.location.replace(destination.href);
      else window.location.assign(destination.href);
      return true;
    }

    const leavesSources = isSourcesPath(pathname) && destination.pathname !== pathname;
    if (leavesSources) {
      // Source editor state is route-owned, so a document navigation tears it down after guard acceptance.
      guard?.acceptLeave();
      window.location.assign(destination.href);
      return true;
    }

    if (isSourcesPath(destination.pathname)) {
      if (mode === 'replace') window.location.replace(destination.href);
      else window.location.assign(destination.href);
      return true;
    }

    const appHref = `${destination.pathname}${destination.search}${destination.hash}`;
    if (mode === 'replace') router.replace(appHref);
    else router.push(appHref);
    return true;
  }, [pathname, router]);

  /** Navigates through the normal leave confirmation flow. */
  const navigate = useCallback((href: string, mode: NavigationMode = 'push') =>
    performNavigation(href, mode, true), [performNavigation]);

  /** Continues an exact intent after an owner has resolved its custom asynchronous leave prompt. */
  const continueNavigation = useCallback((intent: GuardedNavigationIntent) => {
    performNavigation(intent.href, intent.mode, false);
  }, [performNavigation]);

  useEffect(() => {
    /** Intercepts eligible links, including external destinations, when guarded source routes or unsaved drafts require controlled navigation. */
    const onClick = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const target = event.target;
      if (!(target instanceof Element)) return;
      const anchor = target.closest<HTMLAnchorElement>('a[href]');
      if (!anchor || anchor.download || (anchor.target && anchor.target !== '_self')) return;
      const destination = new URL(anchor.href, window.location.href);
      const sameOrigin = destination.origin === window.location.origin;
      const leavesSources = isSourcesPath(window.location.pathname)
        && (!sameOrigin || destination.pathname !== window.location.pathname);
      const entersSources = sameOrigin && isSourcesPath(destination.pathname) && !isSourcesPath(window.location.pathname);
      const guard = guardRef.current;
      const leavesGuardedPage = guard?.hasUnsavedChanges() && (!sameOrigin || destination.pathname !== window.location.pathname);
      if (!leavesSources && !entersSources && !leavesGuardedPage) return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      navigate(destination.href);
    };
    /** Prompts the browser before closing while the active editor reports unsaved changes. */
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      const guard = guardRef.current;
      if (!guard?.hasUnsavedChanges()) return;
      event.preventDefault();
      event.returnValue = '';
    };
    /** Accepts leaving the editor when the browser hides the current page. */
    const onPageHide = () => guardRef.current?.acceptLeave();
    document.addEventListener('click', onClick, true);
    window.addEventListener('beforeunload', onBeforeUnload);
    window.addEventListener('pagehide', onPageHide);
    return () => {
      document.removeEventListener('click', onClick, true);
      window.removeEventListener('beforeunload', onBeforeUnload);
      window.removeEventListener('pagehide', onPageHide);
    };
  }, [navigate]);

  return <GuardedNavigationContext.Provider value={{ registerLeaveGuard, navigate, continueNavigation, ensureSourcesDocument, ensureEntityDocument }}>
    {children}
  </GuardedNavigationContext.Provider>;
}

/** Returns the guarded-navigation context or throws when called outside its provider. */
export function useGuardedNavigation() {
  const value = useContext(GuardedNavigationContext);
  if (!value) throw new Error('Guarded navigation is unavailable');
  return value;
}
