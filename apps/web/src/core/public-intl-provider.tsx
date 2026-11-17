'use client';

import { NextIntlClientProvider, type AbstractIntlMessages } from 'next-intl';
import type { ReactNode } from 'react';
import { missingKeyHandlers } from '@/core/i18n-missing';

/**
 * Client wrapper around `NextIntlClientProvider` for server components (the public landing page), which cannot pass
 * the e2e missing-key handlers as props across the RSC boundary. Behaves exactly like the bare provider unless the
 * build enables `NEXT_PUBLIC_E2E_FAIL_ON_MISSING_I18N` (see `i18n-missing.ts`).
 * @param locale Formatting locale (`en-US` / `vi-VN`).
 * @param messages Already-narrowed catalog to provide.
 */
export function PublicIntlProvider({
  locale,
  messages,
  children,
}: {
  locale: string;
  messages: AbstractIntlMessages;
  children: ReactNode;
}) {
  return (
    <NextIntlClientProvider locale={locale} messages={messages} {...missingKeyHandlers()}>
      {children}
    </NextIntlClientProvider>
  );
}
