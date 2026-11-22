import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { NextIntlClientProvider } from 'next-intl';
import { messages } from '@/core/messages';
import { QueryProvider } from '@/core/query-provider';
import { PreferencesDialog, type OwnerPreferences } from './preferences-dialog';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => '/dashboard',
  useSearchParams: () => new URLSearchParams(),
}));

const preferences: OwnerPreferences = {
  configuration_revision: 1,
  persisted: true,
  theme: 'light',
  locale: 'en-us',
  timezone: 'UTC',
};

const view = () =>
  render(
    <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
      <QueryProvider>
        <PreferencesDialog open preferences={preferences} csrfToken="t" onOpenChange={() => {}} />
      </QueryProvider>
    </NextIntlClientProvider>,
  );

describe('PreferencesDialog skin', () => {
  beforeEach(() => {
    document.documentElement.removeAttribute('data-skin');
    // next-themes reads the system scheme; jsdom has no matchMedia.
    window.matchMedia ??= ((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })) as unknown as typeof window.matchMedia;
  });
  afterEach(() => {
    cleanup();
    localStorage.clear();
    document.documentElement.removeAttribute('data-skin');
  });

  it('hides light/dark/system and accent while the terminal skin is selected', async () => {
    view();
    expect(await screen.findByRole('radiogroup', { name: 'Theme' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('radio', { name: /^Terminal/ }));
    expect(await screen.findByRole('radio', { name: /^Terminal/ })).toBeChecked();
    expect(screen.queryByRole('radiogroup', { name: 'Theme' })).not.toBeInTheDocument();
    expect(screen.queryByRole('radio', { name: /^Dark/ })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('radio', { name: /^Default/ }));
    expect(await screen.findByRole('radiogroup', { name: 'Theme' })).toBeInTheDocument();
  });
});
