'use client';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { ThemeProvider, useTheme } from 'next-themes';
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { AppLocaleId, normalizeFormattingLocale } from '@/core/i18n';
import { ChatControllerProvider } from '@/core/app-shell/chat-controller';
import { GuardedNavigationProvider } from '@/core/guarded-navigation';
import { messages } from '@/core/messages';
import { OwnerPreferences, PreferenceValues } from '@/core/preferences';
import { RealtimeProvider } from '@/core/realtime-provider';

/** Resolves a missing translated message from the English catalog using its namespace and key. */
function englishMessageFallback(namespace: string | undefined, key: string): string {
  let value: unknown = messages['en-us'];
  for (const part of [...(namespace?.split('.') ?? []), ...key.split('.')]) {
    value = value && typeof value === 'object' ? (value as Record<string, unknown>)[part] : undefined;
  }
  return typeof value === 'string' ? value : [namespace, key].filter(Boolean).join('.');
}

type PreviewState = { value: PreferenceValues; generation: number } | null;

type DisplayPreferenceContextValue = {
  locale: AppLocaleId;
  timezone: string;
  confirmedPreferences: OwnerPreferences | null;
  preview: PreferenceValues | null;
  authGeneration: number;
  confirmPreferences: (value: OwnerPreferences, generation: number) => void;
  setPreview: (value: PreferenceValues | null, generation: number) => void;
  endAuthSession: () => void;
  isCurrentGeneration: (generation: number) => boolean;
};

const DisplayPreferenceContext = createContext<DisplayPreferenceContextValue | null>(null);

/** Returns confirmed and preview display preferences for the current provider context. */
export function useDisplayPreferences() {
  const value = useContext(DisplayPreferenceContext);
  if (!value) throw new Error('Display preferences are unavailable');
  return value;
}

/** Mounts React Query and locale-aware message providers around the application tree. */
export function QueryProvider({ children }: { children: ReactNode }) {
  const [client] = useState(
    () => new QueryClient({
      defaultOptions: {
        queries: { refetchOnWindowFocus: false, retry: false, staleTime: 10_000 },
      },
    }),
  );
  return <QueryClientProvider client={client}>
    <GuardedNavigationProvider>
      <ThemeProvider attribute="class" defaultTheme="system" enableSystem disableTransitionOnChange>
        <DisplayPreferencesProvider>
          {children}
        </DisplayPreferencesProvider>
      </ThemeProvider>
    </GuardedNavigationProvider>
  </QueryClientProvider>;
}

/** Owns application display preferences, including bootstrap values, confirmed owner values, temporary previews, and auth-generation resets. */
function DisplayPreferencesProvider({ children }: { children: ReactNode }) {
  const { setTheme } = useTheme();
  const themeSetterRef = useRef(setTheme);
  themeSetterRef.current = setTheme;
  const [confirmedPreferences, setConfirmedPreferences] = useState<OwnerPreferences | null>(null);
  const confirmedRef = useRef<OwnerPreferences | null>(null);
  const [bootstrapValues, setBootstrapValues] = useState<PreferenceValues>({ theme: 'system', locale: 'en-us', timezone: 'Asia/Ho_Chi_Minh' });
  const bootstrapRef = useRef(bootstrapValues);
  const [browserLocale, setBrowserLocale] = useState<AppLocaleId>('en-us');
  const [previewState, setPreviewState] = useState<PreviewState>(null);
  const [authGeneration, setAuthGeneration] = useState(0);
  const generationRef = useRef(0);
  useEffect(() => {
    const lang = navigator.language.toLowerCase();
    setBrowserLocale(lang.startsWith('vi') ? 'vi-vi' : 'en-us');
  }, []);

  const confirmedValues = useMemo<PreferenceValues>(() => confirmedPreferences
    ? { ...confirmedPreferences, locale: confirmedPreferences.persisted ? confirmedPreferences.locale : browserLocale }
    : { ...bootstrapValues, locale: browserLocale }, [confirmedPreferences, bootstrapValues, browserLocale]);
  const activePreview = previewState?.generation === authGeneration ? previewState.value : null;
  const effective = activePreview ?? confirmedValues;

  useEffect(() => {
    document.documentElement.lang = normalizeFormattingLocale(effective.locale);
  }, [effective.locale]);

  useEffect(() => {
    if (confirmedPreferences) themeSetterRef.current(effective.theme);
  }, [effective.theme, Boolean(confirmedPreferences)]);

  /** Checks whether asynchronous preference work still belongs to the active auth generation. */
  const isCurrentGeneration = useCallback((generation: number) => generationRef.current === generation, []);
  /** Commits confirmed preference values only for the current authentication generation. */
  const confirmPreferences = useCallback((value: OwnerPreferences, generation: number) => {
    // Async reads from a prior login must not replace the next session's confirmed preferences.
    if (generationRef.current !== generation) return;
    confirmedRef.current = value;
    setConfirmedPreferences(value);
    const next = { theme: value.theme, locale: value.locale, timezone: value.timezone };
    bootstrapRef.current = next;
    setBootstrapValues(next);
  }, []);
  /** Sets or clears preview preferences only for the current authentication generation. */
  const setPreview = useCallback((value: PreferenceValues | null, generation: number) => {
    if (generationRef.current !== generation) return;
    setPreviewState(value ? { value, generation } : (current) => current?.generation === generation ? null : current);
  }, []);
  /** Retains the current theme, locale, and time zone as bootstrap values, clears confirmed and preview state, and advances the auth generation. */
  const endAuthSession = useCallback(() => {
    const retained = confirmedRef.current
      ? { theme: confirmedRef.current.theme, locale: confirmedRef.current.locale, timezone: confirmedRef.current.timezone }
      : bootstrapRef.current;
    themeSetterRef.current(retained.theme);
    bootstrapRef.current = retained;
    setBootstrapValues(retained);
    confirmedRef.current = null;
    setConfirmedPreferences(null);
    setPreviewState(null);
    generationRef.current += 1;
    setAuthGeneration(generationRef.current);
  }, []);

  return <DisplayPreferenceContext.Provider value={{
        locale: effective.locale,
        timezone: effective.timezone,
        confirmedPreferences,
        preview: activePreview,
        authGeneration,
        confirmPreferences,
        setPreview,
        endAuthSession,
        isCurrentGeneration,
  }}>
    <NextIntlClientProvider locale={normalizeFormattingLocale(effective.locale)} messages={messages[effective.locale]} getMessageFallback={({ namespace, key }) => englishMessageFallback(namespace, key)}>
      {/* Keep chat retries across route shells, but drop their private drafts when auth ends. */}
      <ChatControllerProvider key={authGeneration}>
        <RealtimeProvider>{children}</RealtimeProvider>
      </ChatControllerProvider>
    </NextIntlClientProvider>
  </DisplayPreferenceContext.Provider>;
}
