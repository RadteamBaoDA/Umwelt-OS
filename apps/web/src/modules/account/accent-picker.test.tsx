import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { useState } from 'react';
import { afterEach, describe, expect, it } from 'vitest';
import { ACCENT_STORAGE_KEY, applyAccent, previewAccent, readAccent, type AccentId } from '@/core/accent';
import { messages } from '@/core/messages';
import { AccentPicker } from './accent-picker';

/** Hosts the controlled picker the way the preferences dialog does. */
function Host({ initial = 'ink' }: { initial?: AccentId }) {
  const [value, setValue] = useState<AccentId>(initial);
  return <AccentPicker value={value} onChange={setValue} />;
}
const view = (initial?: AccentId) => render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}><Host initial={initial} /></NextIntlClientProvider>);

describe('AccentPicker', () => {
  afterEach(() => { cleanup(); localStorage.clear(); document.documentElement.removeAttribute('data-accent'); });

  it('previews a chosen swatch on the page without persisting it', async () => {
    view();
    await userEvent.click(screen.getByRole('radio', { name: 'Plum' }));
    expect(document.documentElement.getAttribute('data-accent')).toBe('plum');
    expect(screen.getByRole('radio', { name: 'Plum' })).toBeChecked();
    expect(localStorage.getItem(ACCENT_STORAGE_KEY)).toBeNull();
  });

  it('shows the controlled value as checked', () => {
    view('sea');
    expect(screen.getByRole('radio', { name: 'Sea' })).toBeChecked();
  });
});

describe('accent storage helpers', () => {
  afterEach(() => { localStorage.clear(); document.documentElement.removeAttribute('data-accent'); });

  it('applyAccent persists, previewAccent only sets the attribute, ink clears both', () => {
    previewAccent('sea');
    expect(document.documentElement.getAttribute('data-accent')).toBe('sea');
    expect(readAccent()).toBe('ink');
    applyAccent('plum');
    expect(localStorage.getItem(ACCENT_STORAGE_KEY)).toBe('plum');
    expect(readAccent()).toBe('plum');
    applyAccent('ink');
    expect(document.documentElement.hasAttribute('data-accent')).toBe(false);
    expect(localStorage.getItem(ACCENT_STORAGE_KEY)).toBeNull();
  });
});
