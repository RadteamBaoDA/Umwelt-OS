import { describe, expect, it, vi } from 'vitest';
import type { ConnectorCatalogEntry } from './api';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 't' }) }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({}) }));
vi.mock('@/core/guarded-navigation', () => ({ useGuardedNavigation: () => ({}) }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({}) }));

import { providerCredentialRequired } from './connector-editor';

const entry = (provider_id: string, auth_methods: string[], key_fields: string[] = []) =>
  ({ provider_id, auth_methods, key_fields } as unknown as ConnectorCatalogEntry);

describe('providerCredentialRequired', () => {
  it('does not require a key for generic REST (none or optional header)', () => {
    expect(providerCredentialRequired(entry('rest', ['none', 'http_header']))).toBe(false);
  });
  it('keeps the Mastodon token optional', () => {
    expect(providerCredentialRequired(entry('mastodon', ['none', 'http_header']))).toBe(false);
  });
  it('requires API-key presets and header-only providers', () => {
    expect(providerCredentialRequired(entry('coingecko', ['none'], ['api_key']))).toBe(true);
    expect(providerCredentialRequired(entry('github_releases', ['http_header']))).toBe(true);
  });
});
