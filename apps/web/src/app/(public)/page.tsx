import type { Metadata } from 'next';
import { headers } from 'next/headers';
import { NextIntlClientProvider } from 'next-intl';
import { messages } from '@/core/messages';
import { pickNamespaces } from '@/core/message-namespaces';
import type { AppLocaleId } from '@/core/i18n';
import { LandingEntry } from '@/modules/landing';

const localeIds: AppLocaleId[] = ['en-us', 'vi-vi'];

/** Render the public landing in the requested locale without checking auth or touching workspace data. */
export default async function HomePage({ searchParams }: { searchParams: Promise<{ lang?: string }> }) {
  const { lang } = await searchParams;
  const acceptLanguage = (await headers()).get('accept-language')?.toLowerCase() ?? '';
  const locale = localeIds.includes(lang as AppLocaleId) ? (lang as AppLocaleId) : acceptLanguage.startsWith('vi') ? 'vi-vi' : 'en-us';
  const nextLocale = locale === 'vi-vi' ? 'vi-VN' : 'en-US';

  return (
    <NextIntlClientProvider locale={nextLocale} messages={pickNamespaces(messages[locale], ['landing', 'shell'])}>
      <LandingEntry />
    </NextIntlClientProvider>
  );
}

/** Supply language-aware metadata for the public Google OAuth homepage. */
export async function generateMetadata({ searchParams }: { searchParams: Promise<{ lang?: string }> }): Promise<Metadata> {
  const { lang } = await searchParams;
  const acceptLanguage = (await headers()).get('accept-language')?.toLowerCase() ?? '';
  return lang === 'vi-vi' || (lang !== 'en-us' && acceptLanguage.startsWith('vi'))
    ? {
        title: 'Umwelt-OS — Kết nối nguồn dữ liệu, tương tác với AI',
        description: 'Tập hợp dữ liệu từ các nguồn của bạn và hỏi AI với câu trả lời có dẫn nguồn.',
      }
    : {
        title: 'Umwelt-OS — Connect your data and work with AI',
        description: 'Bring your sources together and ask AI questions with answers grounded in references.',
      };
}
