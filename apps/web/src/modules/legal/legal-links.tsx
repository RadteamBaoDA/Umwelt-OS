"use client";

import Link from 'next/link';
import { useLocale } from 'next-intl';
import { Button } from '@/components/ui/button';
import { legalMessages } from './messages';

/** Links to public policies using the existing display locale; does not create preference state. */
export function LegalLinks() {
  const locale = useLocale().toLowerCase().startsWith('vi') ? 'vi-vi' : 'en-us';
  const t = legalMessages[locale];
  return <nav aria-label={t.privacy} className="flex flex-wrap gap-1">
    {(['privacy', 'terms', 'refunds'] as const).map((name) => <Button key={name} variant="link" size="sm" className="min-h-11" asChild><Link href={`/${name}?lang=${locale}`}>{t[name]}</Link></Button>)}
  </nav>;
}
