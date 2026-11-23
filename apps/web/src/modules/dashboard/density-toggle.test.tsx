import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { getCatalog } from '@locales';
import { HeaderMoreMenu } from './dashboard-header';

/** Renders the header ⋯ menu for the given owner flag inside the dashboard message catalog. */
function renderMenu(isOwner: boolean, onOpenManagement = vi.fn()) {
  return render(
    <NextIntlClientProvider locale="en-US" messages={{ dashboard: getCatalog('en-us', 'dashboard') }}>
      <HeaderMoreMenu isOwner={isOwner} onOpenManagement={onOpenManagement} />
    </NextIntlClientProvider>,
  );
}

describe('DensityToggle (header menu item)', () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute('data-density');
  });

  it('sets data-density and persists it, then clears both on switching back', async () => {
    renderMenu(false);
    await userEvent.click(screen.getByRole('button', { name: 'More dashboard actions' }));
    await userEvent.click(screen.getByRole('menuitem', { name: /Density: Comfortable/ }));
    expect(document.documentElement.getAttribute('data-density')).toBe('compact');
    expect(localStorage.getItem('umwelt-density')).toBe('compact');
    await userEvent.click(screen.getByRole('button', { name: 'More dashboard actions' }));
    await userEvent.click(screen.getByRole('menuitem', { name: /Density: Compact/ }));
    expect(document.documentElement.hasAttribute('data-density')).toBe(false);
    expect(localStorage.getItem('umwelt-density')).toBeNull();
  });
});

describe('HeaderMoreMenu', () => {
  it('omits Manage topics for non-owners', async () => {
    renderMenu(false);
    await userEvent.click(screen.getByRole('button', { name: 'More dashboard actions' }));
    expect(screen.queryByRole('menuitem', { name: 'Topics' })).toBeNull();
  });

  it('opens topic management from the menu for owners', async () => {
    const onOpenManagement = vi.fn();
    renderMenu(true, onOpenManagement);
    await userEvent.click(screen.getByRole('button', { name: 'More dashboard actions' }));
    await userEvent.click(screen.getByRole('menuitem', { name: 'Topics' }));
    expect(onOpenManagement).toHaveBeenCalledTimes(1);
  });
});
