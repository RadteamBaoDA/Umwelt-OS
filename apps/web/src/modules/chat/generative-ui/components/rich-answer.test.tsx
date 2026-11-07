import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { Citation } from '../../api';
import { RichAnswer } from './rich-answer';

const citations = [{ sourceType: 'web', url: 'https://example.com/a', title: 'A', quote: '' }] as Citation[];
const img = (n: number, src = `/api/media/${n}`) => ({ src, alt: `alt${n}`, source_host: `h${n}.test` });

describe('RichAnswer', () => {
  it('renders title, status, key value, stats, table and chips', () => {
    render(<RichAnswer locale="en-us" citations={citations} answer={{ title: 'GTA 6', blocks: [
      { type: 'status', label: 'Delayed', tone: 'warning', updated_at: '2026-11-07' },
      { type: 'key_value', value: '19/11/2026', caption: 'Release date', citations: [1] },
      { type: 'stats', items: [{ label: 'Price', value: '$70', citations: [] }, { label: 'Platforms', value: '2', citations: [] }] },
      { type: 'table', columns: ['A', 'B'], rows: [['1', '2']], citations: [] },
    ] }} />);
    expect(screen.getByRole('heading', { name: 'GTA 6' })).toBeTruthy();
    expect(screen.getByText('Updated 07/11/2026')).toBeTruthy();
    expect(screen.getByText('19/11/2026')).toBeTruthy();
    expect(screen.getByText('$70')).toBeTruthy();
    expect(screen.getByRole('columnheader', { name: 'B' })).toBeTruthy();
    expect(document.querySelector('a[data-cite="1"]')?.getAttribute('href')).toBe('https://example.com/a');
  });

  it('shows 3 tiles with +N, skips non same-origin images and hides on error', () => {
    const { container } = render(<RichAnswer locale="en-us" answer={{ title: 't', blocks: [
      { type: 'gallery', images: [img(1), img(2), img(3), img(4), img(5, 'https://evil.test/x.png')] },
    ] }} />);
    const imgs = container.querySelectorAll('img');
    expect(imgs).toHaveLength(3);
    expect(screen.getByText('+1')).toBeTruthy();
    fireEvent.error(imgs[0]);
    expect(container.querySelectorAll('img')).toHaveLength(2);
  });

  it('tolerates partial streaming payloads', () => {
    const { container } = render(<RichAnswer answer={{ blocks: [{ type: 'stats' }, { type: 'table' }, { type: 'section' }, null, { type: 'gallery' }] }} />);
    expect(container.querySelector('[data-testid="rich-answer"]')).toBeTruthy();
    expect(render(<RichAnswer answer={undefined} />).container.innerHTML).toBe('');
  });
});
