import type { Metadata } from 'next';
import { NextIntlClientProvider } from 'next-intl';
import type { ReactNode } from 'react';
import { messages } from '@/core/messages';
import { QueryProvider } from '@/core/query-provider';
import './globals.css';

export const metadata: Metadata = {
  title: 'Umwelt-OS',
  description: 'Private personal intelligence workspace',
};

/** Renders the root document and mounts shared providers around route content. */
export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en-US" suppressHydrationWarning>
      <body>
        <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
          <QueryProvider>{children}</QueryProvider>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
