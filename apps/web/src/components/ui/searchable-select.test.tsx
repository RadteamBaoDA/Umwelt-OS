import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SearchableSelect } from './searchable-select';

afterEach(() => cleanup());

/** Renders an open-ended select and opens its popover. */
async function open(onValueChange = vi.fn(), allowCustom = true) {
  render(<SearchableSelect value="" options={['AAA']} allowCustom={allowCustom} onValueChange={onValueChange} placeholder="pick" searchLabel="Search" aria-label="Field" />);
  await userEvent.click(screen.getByRole('combobox', { name: 'Field' }));
  return onValueChange;
}

describe('SearchableSelect', () => {
  it('commits the typed value on Enter when custom entries are allowed', async () => {
    const onValueChange = await open();
    await userEvent.type(await screen.findByLabelText('Search'), 'zzz{Enter}');
    expect(onValueChange).toHaveBeenCalledWith('zzz');
  });

  it('ignores Enter without a custom value', async () => {
    const onValueChange = await open(vi.fn(), false);
    await userEvent.type(await screen.findByLabelText('Search'), 'zzz{Enter}');
    expect(onValueChange).not.toHaveBeenCalled();
  });

  it('points the combobox at the listbox with aria-controls', async () => {
    await open();
    const listbox = await screen.findByRole('listbox');
    expect(screen.getByRole('combobox', { name: 'Field' }).getAttribute('aria-controls')).toBe(listbox.id);
  });

  it.each([true, false])('selects the listed option on Enter when the text matches it case-insensitively (allowCustom=%s)', async (allowCustom) => {
    const onValueChange = await open(vi.fn(), allowCustom);
    await userEvent.type(await screen.findByLabelText('Search'), 'aaa{Enter}');
    expect(onValueChange).toHaveBeenCalledWith('AAA');
  });
});
