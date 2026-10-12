'use client';

/**
 * Last-resort boundary that replaces the root layout, so no providers or translations exist here.
 * Copy is therefore bilingual and static, and uses no app styles that could have failed to load.
 */
export default function GlobalError({ reset }: Readonly<{ error: Error & { digest?: string }; reset: () => void }>) {
  return (
    <html lang="en-US">
      <body style={{ fontFamily: 'system-ui, sans-serif', padding: '2rem' }}>
        <main role="alert">
          <h1>Something went wrong / Đã xảy ra lỗi</h1>
          <button type="button" onClick={reset}>Try again / Thử lại</button>
        </main>
      </body>
    </html>
  );
}
