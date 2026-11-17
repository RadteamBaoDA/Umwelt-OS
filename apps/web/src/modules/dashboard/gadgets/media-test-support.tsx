import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { getCatalog } from '@locales';
import type { ReactNode } from 'react';
import type { GadgetInstance } from '../api';
import type { GadgetDocumentProjection } from '@/modules/knowledge/public-api';

/** Fictional typed media fixture; no upstream requests or personal content. */
export function mediaItem(id = 'doc-1', overrides: Partial<GadgetDocumentProjection> = {}): GadgetDocumentProjection {
  return {
    document_id: id,
    document_version_id: `${id}-v1`,
    version_number: 1,
    source_id: 's1',
    title: `Title ${id}`,
    canonical_url: 'https://example.com/item',
    published_at: null,
    observed_at: '2026-11-17T00:00:00Z',
    excerpt: 'A fictional adventure',
    metadata_is_version_snapshot: true,
    image_url: '/api/v1/media/proxy?token=fiction',
    read_at: null,
    bookmarked_at: null,
    dismissed_at: null,
    provider_metadata: null,
    media: {
      media_type: 'anime',
      artwork_aspect: 'poster',
      country: 'JP',
      language: 'ja',
      genres: ['adventure'],
      release_year: 2026,
      release_season: 'fall',
      format: 'tv',
      content_rating: 'safe',
      rating: 86,
      release_status: 'upcoming',
      release_date: '2026-11-17T12:00:00Z',
      creator: 'Studio',
      season: '2',
      episode: '14',
      chapter: null,
      volume: null,
      duration_seconds: 1500,
      page_count: null,
      trailer_url: 'https://example.com/trailer',
      attribution: 'Fiction provider',
      external_ids: {},
    },
    ...overrides,
  };
}

/** Minimal persisted media definition for renderer regressions. */
export function mediaInstance(overrides: Partial<GadgetInstance['definition']> = {}): GadgetInstance {
  return {
    id: 'media-1',
    title: 'Shelf',
    definition: {
      id: 'def-1',
      name: 'Shelf',
      revision: 1,
      renderer: 'media_feed',
      source_ids: ['s1', 's2'],
      scope: {},
      filters: {},
      highlight_rules: [],
      config_version: 1,
      runtime_state: 'available',
      ...overrides,
    },
  } as GadgetInstance;
}

/** Keeps catalog/query owners stable across renderer rerenders and permits seeding the shared privacy cache. */
export function renderMedia(children: ReactNode, remote: boolean | undefined = true, locale: 'en-us' | 'vi-vi' = 'en-us') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  if (remote !== undefined) client.setQueryData(['ai-settings'], { privacy: { allow_widget_remote_media: remote } });
  return {
    ...render(children, {
      wrapper: ({ children: content }) => (
        <QueryClientProvider client={client}>
          <NextIntlClientProvider
            locale={locale === 'vi-vi' ? 'vi-VN' : 'en-US'}
            messages={{
              dashboard: getCatalog(locale, 'dashboard'),
              gadgetSettings: getCatalog(locale, 'gadgetSettings'),
              uiSweep: getCatalog(locale, 'uiSweep'),
              sources: getCatalog(locale, 'sources'),
            }}
          >
            {content}
          </NextIntlClientProvider>
        </QueryClientProvider>
      ),
    }),
    client,
  };
}
