import { cleanup, render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { getCatalog } from '@locales';
import type { BuiltinSource } from './api';
import { applicableOptionPayload } from './builtin-sources';
import { BuiltinOptions } from './builtin-options';

const tvmaze: BuiltinSource = {
  id: 'tvmaze_schedule',
  label: 'TVMaze',
  category: 'media',
  eligibility: 'open',
  terms_url: 'https://e.org',
  terms_checked_on: '2026-10-07',
  attribution: 'x',
  execution: 'supported',
  source_id: null,
  enabled: false,
  data_shapes: ['media'],
  applicable_options: ['tvmaze_mode', 'tvmaze_show_ids', 'tvmaze_global'],
  sections: null,
  default_section: null,
};

afterEach(() => cleanup());

/** Renders the built-in option panel with the sources catalog so labels resolve to their English text. */
function renderOptions(values: Record<string, unknown>) {
  const onChange = vi.fn();
  render(
    <NextIntlClientProvider locale="en-US" messages={{ sources: getCatalog('en-us', 'sources') }}>
      <BuiltinOptions builtin={tvmaze} values={values as never} onChange={onChange} />
    </NextIntlClientProvider>,
  );
  return onChange;
}

describe('TVMaze followed-shows options', () => {
  it('shows the show ID list only when the followed-shows mode is selected', () => {
    renderOptions({ tvmaze_mode: 'schedule_web' });
    expect(screen.queryByLabelText('TVMaze show IDs')).toBeNull();
    cleanup();
    renderOptions({ tvmaze_mode: 'followed_shows' });
    expect(screen.getByLabelText('TVMaze show IDs')).toBeTruthy();
    expect(screen.getByText('Followed shows')).toBeTruthy();
  });

  it('submits show IDs only in followed-shows mode', () => {
    expect(applicableOptionPayload(tvmaze, { tvmaze_mode: 'followed_shows', tvmaze_show_ids: [1, 2] } as never)).toEqual({
      tvmaze_mode: 'followed_shows',
      tvmaze_show_ids: [1, 2],
    });
    expect(applicableOptionPayload(tvmaze, { tvmaze_mode: 'schedule_web', tvmaze_show_ids: [1, 2] } as never)).toEqual({
      tvmaze_mode: 'schedule_web',
    });
  });
});
