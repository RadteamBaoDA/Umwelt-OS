import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { getCatalog } from '@locales';
import type { ConnectorCatalogEntry } from '../api';
import type { ConnectorEditorModel } from '../connector-editor-types';
import { ProviderGroups } from './provider-step';

/** Catalog entry with the fields the provider tiles read; the server label is English, as the catalog ships it. */
function entry(over: Partial<ConnectorCatalogEntry>): ConnectorCatalogEntry {
  return {
    provider_id: 'x',
    label: 'X',
    category: 'media',
    availability: 'available',
    source_type: 'connector',
    code_available: true,
    ...over,
  } as ConnectorCatalogEntry;
}

/** Minimal editor model: only the fields `ProviderGroups` reads; the tiles are enabled and nothing is selected. */
function model(entries: ConnectorCatalogEntry[]): ConnectorEditorModel {
  return {
    provider: null,
    sourceId: null,
    addedBuiltinIds: new Set<string>(),
    selectProvider: vi.fn(),
    locked: false,
    providerGroupTitleKey: () => 'provider.groupOther',
    providerGroups: [{ key: 'media', entries }],
    catalogStatus: () => '',
  } as unknown as ConnectorEditorModel;
}

describe('ProviderGroups tile labels', () => {
  it('shows the vi-vi translation for a keyed card and for a keyless card', () => {
    const keyed = entry({ provider_id: 'tmdb_movies', label: 'TMDB movies', availability: 'requires_credentials' });
    const keyless = entry({ provider_id: 'tvmaze_schedule', label: 'TVMaze' });
    render(
      <NextIntlClientProvider locale="vi-VN" messages={{ sources: getCatalog('vi-vi', 'sources') }}>
        <ProviderGroups m={model([keyed, keyless])} />
      </NextIntlClientProvider>,
    );
    expect(screen.getByRole('button', { name: /Phim TMDB/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /Lịch TVMaze/ })).toBeTruthy();
    expect(screen.queryByText('TMDB movies')).toBeNull();
  });

  it('falls back to the catalog label for a provider with no translation', () => {
    const unknown = entry({ provider_id: 'some_new_provider', label: 'Brand new feed' });
    render(
      <NextIntlClientProvider locale="vi-VN" messages={{ sources: getCatalog('vi-vi', 'sources') }}>
        <ProviderGroups m={model([unknown])} />
      </NextIntlClientProvider>,
    );
    expect(screen.getByText('Brand new feed')).toBeTruthy();
  });
});
