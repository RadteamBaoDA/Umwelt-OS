import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { Statusline, ShellCrumb, sourceHealth } from './statusline';

let mockPath = '/dashboard';
let mockListAll: () => Promise<{ items: unknown[]; next_cursor: null }> = async () => ({ items: [], next_cursor: null });
let mockAiSettings: unknown = undefined;

vi.mock('next/navigation', () => ({ usePathname: () => mockPath }));
vi.mock('@/core/realtime-provider', () => ({ useRealtime: () => ({ status: 'connected' }) }));
vi.mock('@/modules/sources', () => ({
  listAllSources: () => mockListAll(),
  sourceKeys: { listAll: ['sources', 'list', 'all'] },
}));
// Mirrors react-query: a disabled read returns no data.
vi.mock('@/modules/chat', () => ({
  useAiSettings: (options?: { enabled?: boolean }) => ({ data: options?.enabled === false ? undefined : mockAiSettings }),
}));

/** Renders the statusline under the English catalog with a fresh query client, so visible labels are the real strings. */
function renderStatusline(apiStatus: 'connected' | 'unavailable' = 'connected') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <Statusline apiStatus={apiStatus} preferences={{ locale: 'en-us', timezone: 'UTC' }} />
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
}

/** Builds a source row with the fields the health rule reads; defaults to an active source with a fresh success. */
function source(overrides: Record<string, unknown> = {}) {
  return {
    id: crypto.randomUUID(),
    status: 'active',
    last_success_at: new Date(Date.now() - 60_000).toISOString(),
    last_error_at: null,
    next_due_at: new Date(Date.now() + 3_600_000).toISOString(),
    ...overrides,
  };
}

describe('Statusline', () => {
  beforeEach(() => {
    mockPath = '/dashboard';
    mockListAll = async () => ({ items: [], next_cursor: null });
    mockAiSettings = undefined;
    // Source counts and the model are terminal-only; most cases render in the terminal skin.
    document.documentElement.setAttribute('data-skin', 'terminal');
  });
  afterEach(() => document.documentElement.removeAttribute('data-skin'));

  it('fetches neither sources nor the model in the default skin, where those segments are hidden', async () => {
    document.documentElement.removeAttribute('data-skin');
    const listAll = vi.fn(async () => ({ items: [], next_cursor: null }));
    mockListAll = listAll;
    mockAiSettings = { defaults: { chat: { model: 'claude-sonnet-5-5' } } };
    const { container } = renderStatusline();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(listAll).not.toHaveBeenCalled();
    expect(container.querySelector('.statusline-sources')).toBeNull();
    expect(screen.queryByText(/^model:/)).toBeNull();
  });

  it('is a labelled footer with no live region or role=status of its own', () => {
    const { container } = renderStatusline();
    const footer = screen.getByRole('contentinfo', { name: 'Status line' });
    expect(footer.tagName).toBe('FOOTER');
    expect(footer.className).toContain('statusline');
    expect(footer.querySelector('[role="status"], [aria-live]')).toBeNull();
    expect(container.querySelectorAll('[role="status"]')).toHaveLength(0);
    expect(container.querySelector('.connection-indicator')).toBeNull();
  });

  it('exposes the portal slot used by the dashboard summary', () => {
    const { container } = renderStatusline();
    const slot = container.querySelector('#statusline-info');
    expect(slot).not.toBeNull();
    expect(slot?.className).toContain('statusline-info');
    expect(slot?.textContent).toBe('');
  });

  it('keeps the transport segment out of the accessibility tree', () => {
    const { container } = renderStatusline('unavailable');
    const transport = container.querySelector('.statusline-transport');
    expect(transport?.getAttribute('aria-hidden')).toBe('true');
    expect(transport?.textContent).toContain('API');
  });

  it('shows INSERT on chat routes and NORMAL elsewhere', () => {
    mockPath = '/chat/abc';
    const { unmount } = renderStatusline();
    expect(screen.getByText('INSERT')).toBeTruthy();
    unmount();

    mockPath = '/settings/sources';
    renderStatusline();
    expect(screen.getByText('NORMAL')).toBeTruthy();
    expect(screen.queryByText('INSERT')).toBeNull();
  });

  it('hides the mode chip from assistive technology', () => {
    const { container } = renderStatusline();
    expect(container.querySelector('.statusline-mode')?.getAttribute('aria-hidden')).toBe('true');
  });

  it('hides the source and model segments while the reads are pending', () => {
    mockListAll = () => new Promise(() => {});
    mockAiSettings = { defaults: { chat: { model: 'claude-sonnet-5-5' } } };
    const { container } = renderStatusline();
    expect(container.querySelector('.statusline-sources')).toBeNull();
    expect(screen.queryByText(/sources ok/)).toBeNull();
    // The model read has resolved, so its segment shows even while the sources read is still pending.
    expect(screen.getByText('model: claude-sonnet-5-5')).toBeTruthy();
  });

  it('hides every source segment when the source read fails', async () => {
    mockListAll = () => Promise.reject(new Error('down'));
    const { container } = renderStatusline();
    await waitFor(() => expect(container.querySelector('.statusline-transport')).not.toBeNull());
    // Give the failed query a tick to settle; the segments must still be absent.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(container.querySelector('.statusline-sources')).toBeNull();
  });

  it('counts active sources by health and omits warn and err when they are zero', async () => {
    mockListAll = async () => ({
      items: [
        source(),
        source({ last_success_at: new Date(Date.now() - 5 * 3_600_000).toISOString(), next_due_at: null }),
        source({ last_error_at: new Date().toISOString(), last_success_at: null }),
        source({ status: 'paused', last_error_at: new Date().toISOString() }),
      ],
      next_cursor: null,
    });
    renderStatusline();
    expect(await screen.findByText('1 sources ok')).toBeTruthy();
    expect(screen.getByText('1 warn')).toBeTruthy();
    expect(screen.getByText('1 err')).toBeTruthy();
    // The paused source is not counted, so the totals are 1 / 1 / 1.
    expect(screen.getAllByText(/^1 /).length).toBe(3);
  });

  it('shows only the ok count when every active source is healthy', async () => {
    mockListAll = async () => ({ items: [source(), source()], next_cursor: null });
    renderStatusline();
    expect(await screen.findByText('2 sources ok')).toBeTruthy();
    expect(screen.queryByText(/ warn$/)).toBeNull();
    expect(screen.queryByText(/ err$/)).toBeNull();
  });

  it('renders the default chat model segment, hidden from assistive technology', async () => {
    mockAiSettings = { defaults: { chat: { model: 'claude-sonnet-5-5' } } };
    renderStatusline();
    const segment = await screen.findByText('model: claude-sonnet-5-5');
    expect(segment.getAttribute('aria-hidden')).toBe('true');
    expect(segment.className).toContain('hide-sm');
  });

  it('omits the model segment when no default chat model is set', () => {
    mockAiSettings = { defaults: { chat: null } };
    renderStatusline();
    expect(screen.queryByText(/^model:/)).toBeNull();
  });

  it('groups the model, locale and clock into one right-aligned end group after the transport segment', () => {
    mockAiSettings = { defaults: { chat: { model: 'claude-sonnet-5-5' } } };
    const { container } = renderStatusline();
    const footer = container.querySelector('.statusline');
    const end = container.querySelector('.statusline-end');
    expect(end).not.toBeNull();
    expect(footer?.lastElementChild).toBe(end);
    expect(end?.querySelector('.statusline-model')).not.toBeNull();
    expect(end?.querySelector('.statusline-locale')?.textContent).toBe('en-us · UTC');
    expect(end?.querySelector('.statusline-clock')).not.toBeNull();
    expect(end?.querySelector('.statusline-transport')).toBeNull();
    expect(footer?.querySelector('.statusline-transport')).not.toBeNull();
  });
});

describe('sourceHealth', () => {
  const now = Date.parse('2026-11-18T12:00:00Z');
  const iso = (offsetMs: number) => new Date(now + offsetMs).toISOString();

  it('is err when the latest error is newer than the latest success', () => {
    expect(sourceHealth({ last_success_at: iso(-60_000), last_error_at: iso(-1_000), next_due_at: iso(60_000) }, now)).toBe('err');
  });

  it('is err when there is an error and no success', () => {
    expect(sourceHealth({ last_success_at: null, last_error_at: iso(-1_000), next_due_at: null }, now)).toBe('err');
  });

  it('is ok while the next due time has not passed, and warn after it', () => {
    expect(sourceHealth({ last_success_at: iso(-60_000), last_error_at: null, next_due_at: iso(60_000) }, now)).toBe('ok');
    expect(sourceHealth({ last_success_at: iso(-60_000), last_error_at: null, next_due_at: iso(-1_000) }, now)).toBe('warn');
  });

  it('falls back to a two-hour window when no cadence is known', () => {
    expect(sourceHealth({ last_success_at: iso(-3_600_000), last_error_at: null, next_due_at: null }, now)).toBe('ok');
    expect(sourceHealth({ last_success_at: iso(-3 * 3_600_000), last_error_at: null, next_due_at: null }, now)).toBe('warn');
  });

  it('is warn when the source never succeeded and has no error', () => {
    expect(sourceHealth({ last_success_at: null, last_error_at: null, next_due_at: null }, now)).toBe('warn');
  });
});

describe('ShellCrumb', () => {
  it('renders the decorative prompt from the current path and is hidden from assistive technology', () => {
    mockPath = '/dashboard/abc';
    const { container } = render(<ShellCrumb />);
    const crumb = container.querySelector('.shell-crumb');
    expect(crumb?.getAttribute('aria-hidden')).toBe('true');
    expect(crumb?.textContent).toBe('owner@umwelt:~/dashboard/abc $');
    expect(crumb?.querySelector('b')?.textContent).toBe('owner@umwelt');
  });
});
