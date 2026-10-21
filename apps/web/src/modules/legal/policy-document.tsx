"use client";

import Link from 'next/link';
import { useLocale } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { legalMessages, type LegalLocale, type PolicyName } from './messages';

/** Displays trusted repository Markdown rendered with HTML disabled; query language overrides the shared locale. */
export function PolicyDocument({ name, documents, language, inventory }: {
  name: PolicyName; documents: Record<LegalLocale, string>; language?: LegalLocale; inventory: string;
}) {
  const current = useLocale();
  const locale = language ?? (current.toLowerCase().startsWith('vi') ? 'vi-vi' : 'en-us');
  const t = legalMessages[locale];
  return <main id="main-content" lang={locale === 'vi-vi' ? 'vi-VN' : 'en-US'} className="min-h-dvh bg-background px-4 py-8 text-foreground sm:py-12">
    <div className="mx-auto flex max-w-3xl flex-col gap-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <Link href="/login" className="font-semibold">Umwelt-OS</Link>
        <nav aria-label={t.language} className="flex flex-wrap gap-1">
          <Button asChild variant="outline" className="min-h-11"><Link href={`/${name}?lang=en-us`} lang="en-US" aria-current={locale === 'en-us' ? 'page' : undefined}>English</Link></Button>
          <Button asChild variant="outline" className="min-h-11"><Link href={`/${name}?lang=vi-vi`} lang="vi-VN" aria-current={locale === 'vi-vi' ? 'page' : undefined}>Tiếng Việt</Link></Button>
        </nav>
      </header>
      <aside className="rounded-lg border border-border bg-secondary p-4 text-sm"><strong>{t.draft}</strong><p className="mt-1">{t.notice}</p></aside>
      <nav aria-label={t.policies} className="flex flex-wrap gap-1">
        {(['privacy', 'terms', 'refunds'] as const).map((policy) => <Button asChild variant="link" key={policy} className="min-h-11"><Link href={`/${policy}?lang=${locale}`} aria-current={policy === name ? 'page' : undefined}>{t[policy]}</Link></Button>)}
        <Button asChild variant="link" className="min-h-11"><Link href="/login">{t.login}</Link></Button>
      </nav>
      <Card><CardContent className="overflow-x-auto pt-0 [&_h1]:mb-6 [&_h1]:text-3xl [&_h1]:font-semibold [&_h2]:mb-3 [&_h2]:mt-8 [&_h2]:text-xl [&_h2]:font-semibold [&_p]:my-4 [&_p]:leading-7 [&_ul]:list-disc [&_ul]:pl-6 [&_li]:my-2 [&_a]:text-primary [&_a]:underline" dangerouslySetInnerHTML={{ __html: documents[locale] }} /></Card>
      <details id="services" className="rounded-lg border border-border p-4"><summary className="min-h-11 cursor-pointer font-medium">{t.inventory}</summary><div lang="en-US" className="overflow-x-auto text-sm [&_p]:my-4 [&_h2]:my-4 [&_h2]:font-semibold [&_table]:min-w-[64rem] [&_td]:border [&_td]:border-border [&_td]:p-2 [&_th]:border [&_th]:border-border [&_th]:p-2 [&_a]:text-primary [&_a]:underline" dangerouslySetInnerHTML={{ __html: inventory }} /></details>
    </div>
  </main>;
}
