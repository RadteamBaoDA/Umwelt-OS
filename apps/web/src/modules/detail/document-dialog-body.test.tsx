import { render } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it } from 'vitest';
import { getCatalog } from '@locales';
import { DocumentVersionPreview } from './document-dialog-body';

/** Renders the preview body with the detail catalog so the heading key resolves. */
function renderPreview(content: string, translatedExcerpt: string | null) {
  return render(
    <NextIntlClientProvider locale="en-US" messages={{ detail: getCatalog('en-us', 'detail') }}>
      <DocumentVersionPreview
        isPending={false}
        isError={false}
        isSuccess
        content={content}
        translatedExcerpt={translatedExcerpt}
        onRetry={() => undefined}
      />
    </NextIntlClientProvider>,
  );
}

describe('DocumentVersionPreview truncation marker', () => {
  it('keeps the ellipsis on a translated excerpt when the original was truncated', () => {
    const { container } = renderPreview('a'.repeat(2500), 'ban dich');
    expect(container.querySelector('blockquote')?.textContent?.endsWith('…')).toBe(true);
  });

  it('shows no ellipsis on a translated excerpt when the original fits', () => {
    const { container } = renderPreview('short original', 'ban dich ngan');
    expect(container.querySelector('blockquote')?.textContent?.endsWith('…')).toBe(false);
  });
});
