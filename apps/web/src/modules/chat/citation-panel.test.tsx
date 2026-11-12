import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { chatMessages } from '@/core/messages/chat';
import type { Citation } from './api';
import { CitationPanel } from './citation-panel';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));

const doc = (url: string | null): Citation => ({
  sourceId: 's',
  documentId: 'd',
  documentVersionId: 'v',
  chunkId: 'c',
  title: 'Doc',
  url,
  observedAt: null,
  quote: 'q',
});

/** Renders the rail card variant with English chat messages. */
function renderRail(citations: Citation[]) {
  return render(
    <NextIntlClientProvider locale="en-US" messages={{ chat: chatMessages['en-us'] }}>
      <CitationPanel citations={citations} variant="card" />
    </NextIntlClientProvider>,
  );
}

describe('rail citation card provenance', () => {
  it('links a document to its safe canonical url and omits unsafe or missing ones', () => {
    renderRail([doc('https://example.com/a')]);
    expect(screen.getByRole('link', { name: /Open original/ }).getAttribute('href')).toBe('https://example.com/a');
  });
  it('shows no original link for javascript: or null urls', () => {
    renderRail([doc('javascript:alert(1)'), doc(null)]);
    expect(screen.queryByRole('link', { name: /Open original/ })).toBeNull();
  });
  it('shows the provider name before the host for web citations', () => {
    renderRail([{ sourceType: 'web', url: 'https://example.com/x', title: 'W', quote: '', provider: 'Brave' }]);
    expect(screen.getByText('Brave · example.com')).toBeTruthy();
  });
});
