import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { Statusline, ShellCrumb } from './statusline';

let mockPath = '/dashboard';
vi.mock('next/navigation', () => ({ usePathname: () => mockPath }));
vi.mock('@/core/realtime-provider', () => ({ useRealtime: () => ({ status: 'connected' }) }));

/** Renders the statusline under the English catalog so visible labels are the real strings. */
function renderStatusline(apiStatus: 'connected' | 'unavailable' = 'connected') {
  return render(
    <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
      <Statusline apiStatus={apiStatus} preferences={{ locale: 'en-us', timezone: 'UTC' }} />
    </NextIntlClientProvider>,
  );
}

describe('Statusline', () => {
  beforeEach(() => {
    mockPath = '/dashboard';
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
});

describe('ShellCrumb', () => {
  it('renders the decorative prompt from the current path and is hidden from assistive technology', () => {
    mockPath = '/dashboard/abc';
    const { container } = render(<ShellCrumb />);
    const crumb = container.querySelector('.shell-crumb');
    expect(crumb?.getAttribute('aria-hidden')).toBe('true');
    expect(crumb?.textContent).toBe('umwelt:~/dashboard/abc $');
  });
});
