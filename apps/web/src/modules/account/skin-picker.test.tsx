import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { SKIN_STORAGE_KEY } from '@/core/skin';
import { messages } from '@/core/messages';
import { SkinPicker } from './skin-picker';

const view = () =>
  render(
    <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
      <SkinPicker />
    </NextIntlClientProvider>,
  );

describe('SkinPicker', () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute('data-skin');
  });
  afterEach(() => {
    cleanup();
    localStorage.clear();
    document.documentElement.removeAttribute('data-skin');
  });

  it('applies the terminal skin immediately and persists it (no draft or save step)', async () => {
    view();
    await userEvent.click(screen.getByRole('radio', { name: /^Terminal/ }));
    expect(document.documentElement.getAttribute('data-skin')).toBe('terminal');
    expect(localStorage.getItem(SKIN_STORAGE_KEY)).toBe('terminal');
    expect(screen.getByRole('radio', { name: /^Terminal/ })).toBeChecked();
  });

  it('switching back to default clears the attribute and the stored key', async () => {
    view();
    await userEvent.click(screen.getByRole('radio', { name: /^Terminal/ }));
    await userEvent.click(screen.getByRole('radio', { name: /^Default/ }));
    expect(document.documentElement.hasAttribute('data-skin')).toBe(false);
    expect(localStorage.getItem(SKIN_STORAGE_KEY)).toBeNull();
    expect(screen.getByRole('radio', { name: /^Default/ })).toBeChecked();
  });

  it('shows the terminal note only while terminal is selected', async () => {
    view();
    expect(screen.queryByText(/Terminal uses its own dark palette/)).toBeNull();
    await userEvent.click(screen.getByRole('radio', { name: /^Terminal/ }));
    expect(screen.getByText(/Terminal uses its own dark palette/)).toBeInTheDocument();
  });

  it('reflects a skin already applied on the device', () => {
    document.documentElement.setAttribute('data-skin', 'terminal');
    view();
    expect(screen.getByRole('radio', { name: /^Terminal/ })).toBeChecked();
  });
});
