import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Spinner } from './spinner';

describe('Spinner', () => {
  it('renders the animated icon and an aria-hidden braille glyph', () => {
    const { container } = render(<Spinner className="size-4 text-primary" />);
    const icon = container.querySelector('svg');
    expect(icon?.getAttribute('class')).toContain('animate-spin');
    expect(icon?.getAttribute('class')).toContain('size-4');
    expect(icon?.getAttribute('aria-hidden')).toBe('true');
    const glyph = container.querySelector('.spinner-glyph');
    expect(glyph?.getAttribute('aria-hidden')).toBe('true');
    expect(glyph?.textContent).toBe('');
  });
});
