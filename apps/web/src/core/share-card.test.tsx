import { describe, expect, it } from 'vitest';
import { alt, contentType, size } from '@/app/opengraph-image';
import { metadata } from '@/app/layout';
import { SHARE_TITLE, ShareCard } from '@/core/share-card';

describe('share image', () => {
  it('exports image metadata', () => {
    expect(size).toEqual({ width: 1200, height: 630 });
    expect(contentType).toBe('image/png');
    expect(alt).toContain('Umwelt-OS');
  });
  it('renders the app name', () => {
    expect(JSON.stringify(ShareCard())).toContain(SHARE_TITLE);
  });
  it('declares openGraph and twitter metadata', () => {
    expect(metadata.metadataBase).toBeInstanceOf(URL);
    expect(metadata.openGraph).toMatchObject({ title: 'Umwelt-OS' });
    expect(metadata.twitter).toMatchObject({ card: 'summary_large_image' });
  });
});
