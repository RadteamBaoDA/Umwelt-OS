import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ChatMarkdown } from './chat-markdown';
import type { Citation } from './api';

const citations: Citation[] = [
  { sourceType: 'web', url: 'https://example.com/a', title: 'Example title', quote: 'Quoted overview text' },
];

describe('ChatMarkdown citation tooltip', () => {
  it('shows an overview tooltip when hovering an inline citation', async () => {
    const { container } = render(<ChatMarkdown content="Claim [1]." citations={citations} />);
    const chip = await waitFor(() => {
      const found = container.querySelector('a.cite');
      expect(found).not.toBeNull();
      return found as Element;
    });
    fireEvent.mouseOver(chip);
    const tip = await screen.findByRole('tooltip');
    expect(tip.textContent).toContain('Example title');
    expect(tip.textContent).toContain('example.com');
    expect(tip.textContent).toContain('Quoted overview text');
    fireEvent.mouseOut(chip);
    await waitFor(() => expect(screen.queryByRole('tooltip')).toBeNull());
  });
});

describe('ChatMarkdown code blocks', () => {
  it('highlights known languages, shows the label and keeps source text intact', async () => {
    const { container } = render(<ChatMarkdown content={'```ts\nconst a: string = "<b>x</b>";\n```'} />);
    await waitFor(() => expect(container.querySelector('pre code')).not.toBeNull());
    expect(container.querySelector('.chat-code-lang')?.textContent).toBe('ts');
    expect(container.querySelector('code .hljs-keyword')?.textContent).toBe('const');
    expect(container.querySelector('code')?.textContent).toBe('const a: string = "<b>x</b>";\n');
    expect(container.querySelector('code b')).toBeNull();
  });

  it('leaves unknown languages as escaped plain text and never lets script through', async () => {
    const { container } = render(<ChatMarkdown content={'```weird\n<script>alert(1)</script>\n```'} />);
    await waitFor(() => expect(container.querySelector('pre code')).not.toBeNull());
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('code')?.textContent).toContain('<script>alert(1)</script>');
    expect(container.querySelector('code .hljs-keyword')).toBeNull();
  });
});

describe('ChatMarkdown citation click', () => {
  it('selects the citation on a plain click and keeps modified clicks as links', async () => {
    const onSelect = vi.fn();
    const { container } = render(<ChatMarkdown content="Claim [1]." citations={citations} onSelectCitation={onSelect} />);
    const chip = await waitFor(() => {
      const found = container.querySelector('a.cite');
      expect(found).not.toBeNull();
      return found as Element;
    });
    expect(fireEvent.click(chip)).toBe(false);
    expect(onSelect).toHaveBeenCalledWith(citations[0]);
    expect(fireEvent.click(chip, { ctrlKey: true })).toBe(true);
    expect(onSelect).toHaveBeenCalledTimes(1);
  });
});
