'use client';

import { useMessages, type AbstractIntlMessages } from 'next-intl';
import { useEffect, useState } from 'react';
import type { AppLocaleId } from '@/core/i18n';
import { pickNamespaces } from '@/core/message-namespaces';

/** Resolves a missing translated message from the English catalog using its namespace and key. */
export function englishMessageFallback(english: AbstractIntlMessages, namespace: string | undefined, key: string): string {
  let value: unknown = english;
  for (const part of [...(namespace?.split('.') ?? []), ...key.split('.')]) {
    value = value && typeof value === 'object' ? (value as Record<string, unknown>)[part] : undefined;
  }
  return typeof value === 'string' ? value : [namespace, key].filter(Boolean).join('.');
}

/**
 * Returns the catalog for `locale`. English arrives from the server layout (RSC payload, not client JS); Vietnamese is a
 * lazily imported chunk, so English-only sessions never download it. English is shown until the chunk resolves.
 * Returns `[active, english]`; english also feeds the missing-key fallback. `englishCatalog` overrides the provider's
 * English catalog (workspace routes receive the full one; the root layout sends only the public namespaces). The
 * Vietnamese catalog is narrowed to the same namespaces so a public page never holds more than it renders.
 */
export function useLocaleMessages(
  locale: AppLocaleId,
  englishCatalog?: AbstractIntlMessages,
): [AbstractIntlMessages, AbstractIntlMessages] {
  const fromProvider = useMessages();
  const english = englishCatalog ?? fromProvider;
  const [vietnamese, setVietnamese] = useState<AbstractIntlMessages | null>(null);
  useEffect(() => {
    if (locale !== 'vi-vi' || vietnamese) return;
    let live = true;
    void import('@/core/messages').then((m) => {
      if (live) setVietnamese(pickNamespaces(m.messages['vi-vi'], Object.keys(english)));
    });
    return () => {
      live = false;
    };
  }, [locale, vietnamese, english]);
  return [locale === 'vi-vi' && vietnamese ? vietnamese : english, english];
}
