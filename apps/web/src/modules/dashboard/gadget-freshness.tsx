'use client';

import { createContext, useContext, useEffect, type ReactNode } from 'react';

/** Setter a gadget frame provides so its renderer can report the newest collected-at timestamp. */
const GadgetFreshnessContext = createContext<((collectedAt: string | null) => void) | null>(null);

/**
 * Provides the frame-level freshness reporter to a gadget renderer subtree.
 *
 * @param props Reporter callback and the renderer subtree.
 * @returns The children wrapped in the freshness context.
 */
export function GadgetFreshnessProvider({ report, children }: { report: (collectedAt: string | null) => void; children: ReactNode }) {
  return <GadgetFreshnessContext.Provider value={report}>{children}</GadgetFreshnessContext.Provider>;
}

/**
 * Lets a renderer publish the newest real collected-at time of its loaded data to its frame header.
 * Renderers without a known timestamp never call it, so no freshness badge is shown for them.
 *
 * @param collectedAt ISO timestamp of the newest loaded item, or null/undefined when unknown.
 */
export function useReportGadgetFreshness(collectedAt: string | null | undefined): void {
  const report = useContext(GadgetFreshnessContext);
  const value = collectedAt ?? null;
  useEffect(() => {
    report?.(value);
    return () => report?.(null);
  }, [report, value]);
}
