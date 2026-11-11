'use client';

import { NextIntlClientProvider, type AbstractIntlMessages } from 'next-intl';
import type { ReactNode } from 'react';
import { ChatControllerProvider } from '@/core/app-shell/chat-controller';
import { normalizeFormattingLocale } from '@/core/i18n';
import { englishMessageFallback, useLocaleMessages } from '@/core/locale-messages';
import { useDisplayPreferences } from '@/core/query-provider';
import { RealtimeProvider } from '@/core/realtime-provider';
import { WorkspaceProvider } from '@/core/workspace-context';

/**
 * Chat controller, workspace selection and realtime stream for authenticated workspace routes only. Mounting them
 * here instead of the root layout keeps their code and their `/workspaces` and event-stream requests off public pages.
 * Keyed by the auth generation so private chat drafts and retries drop when auth ends. Also re-provides messages with
 * the full catalog, because the root layout sends public namespaces only.
 * @param english Full English catalog from the server layout.
 * @param children The workspace shell and page.
 */
export function WorkspaceProviders({ english, children }: { english: AbstractIntlMessages; children: ReactNode }) {
  const { authGeneration, locale } = useDisplayPreferences();
  const [localeMessages] = useLocaleMessages(locale, english);
  return (
    <NextIntlClientProvider
      locale={normalizeFormattingLocale(locale)}
      messages={localeMessages}
      getMessageFallback={({ namespace, key }) => englishMessageFallback(english, namespace, key)}
    >
      <ChatControllerProvider key={authGeneration}>
        <WorkspaceProvider>
          <RealtimeProvider>{children}</RealtimeProvider>
        </WorkspaceProvider>
      </ChatControllerProvider>
    </NextIntlClientProvider>
  );
}
