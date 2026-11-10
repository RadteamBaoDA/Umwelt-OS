import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { DensityToggle } from './dashboard-header';

describe('DensityToggle', () => {
  beforeEach(() => { localStorage.clear(); document.documentElement.removeAttribute('data-density'); });

  it('sets data-density and persists it, then clears both on switching back', async () => {
    render(<NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'] }}><DensityToggle /></NextIntlClientProvider>);
    await userEvent.click(screen.getByRole('button', { name: /Density: Comfortable/ }));
    expect(document.documentElement.getAttribute('data-density')).toBe('compact');
    expect(localStorage.getItem('umwelt-density')).toBe('compact');
    await userEvent.click(screen.getByRole('button', { name: /Density: Compact/ }));
    expect(document.documentElement.hasAttribute('data-density')).toBe(false);
    expect(localStorage.getItem('umwelt-density')).toBeNull();
  });
});
