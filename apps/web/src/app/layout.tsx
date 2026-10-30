import type { Metadata } from 'next';
import { Inter } from 'next/font/google';
import { NextIntlClientProvider } from 'next-intl';
import type { ReactNode } from 'react';
import { messages } from '@/core/messages';
import { QueryProvider } from '@/core/query-provider';
import { SHARE_TAGLINE, SHARE_TITLE } from '@/core/share-card';
import './globals.css';

/** Inter is downloaded at build time and served from this origin (no runtime Google request); globals.css reads --font-inter. */
const inter = Inter({ subsets: ['latin', 'vietnamese'], variable: '--font-inter', display: 'swap' });

export const metadata: Metadata = {
  // PUBLIC_ORIGIN is the existing public-origin setting (.env.example); the fallback matches its local default.
  metadataBase: new URL(process.env.PUBLIC_ORIGIN ?? 'http://localhost:3000'),
  title: SHARE_TITLE,
  description: SHARE_TAGLINE,
  openGraph: { title: SHARE_TITLE, description: SHARE_TAGLINE, siteName: SHARE_TITLE, type: 'website' },
  twitter: { card: 'summary_large_image', title: SHARE_TITLE, description: SHARE_TAGLINE },
};

/** Renders the root document and mounts shared providers around route content. */
export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en-US" className={inter.variable} suppressHydrationWarning>
      <body>
        <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
          <QueryProvider>{children}</QueryProvider>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
