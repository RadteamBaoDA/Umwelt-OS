'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useChatController } from '@/core/app-shell/chat-controller';
import { useDisplayPreferences } from '@/core/query-provider';

/** Selected local calendar day shared by day-aware gadgets, in the owner's display timezone. */
export type SelectedDay = {
  /** ISO date (YYYY-MM-DD) in `timezone`. */
  date: string;
  timezone: string;
  /** Today's ISO date in `timezone`, used to label past/future days. */
  today: string;
  setDate: (date: string) => void;
};

const SelectedDayContext = createContext<SelectedDay | null>(null);
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/** Formats an instant as an ISO calendar date in the given IANA timezone. */
export function isoDateInZone(value: Date, timezone: string): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(value);
}

/** Adds whole calendar days to an ISO date without touching timezone or DST (pure date arithmetic). */
export function addDays(date: string, days: number): string {
  const next = new Date(`${date}T00:00:00Z`);
  next.setUTCDate(next.getUTCDate() + days);
  return next.toISOString().slice(0, 10);
}

/**
 * Provides the selected day. The date mirrors the `?date=` query parameter (read once on mount, written
 * with `history.replaceState`) so notification links and reloads keep the day without a Suspense boundary.
 */
export function SelectedDayProvider({ children }: { children: ReactNode }) {
  const { timezone } = useDisplayPreferences();
  const today = isoDateInZone(new Date(), timezone);
  const [override, setOverride] = useState<string | null>(null);

  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get('date');
    if (requested && ISO_DATE.test(requested)) setOverride(requested);
  }, []);

  const setDate = useCallback((date: string) => {
    if (!ISO_DATE.test(date)) return;
    setOverride(date);
    const url = new URL(window.location.href);
    url.searchParams.set('date', date);
    window.history.replaceState(null, '', url);
  }, []);

  // While a day conversation is active, moving the selected day moves the drawer to that day's own
  // conversation. The previous conversation (and any running response) is left untouched.
  const chat = useChatController();
  const { context: chatContext, selectDay } = chat;
  const current = override ?? today;
  useEffect(() => {
    if (chatContext?.kind === 'day' && (chatContext.date !== current || chatContext.timezone !== timezone)) {
      selectDay({ date: current, timezone });
    }
  }, [current, timezone, chatContext, selectDay]);

  const value = useMemo<SelectedDay>(
    () => ({ date: override ?? today, timezone, today, setDate }),
    [override, today, timezone, setDate],
  );
  return <SelectedDayContext.Provider value={value}>{children}</SelectedDayContext.Provider>;
}

/** Returns the shared selected day; throws when used outside `SelectedDayProvider`. */
export function useSelectedDay(): SelectedDay {
  const value = useContext(SelectedDayContext);
  if (!value) throw new Error('Selected day is unavailable');
  return value;
}
