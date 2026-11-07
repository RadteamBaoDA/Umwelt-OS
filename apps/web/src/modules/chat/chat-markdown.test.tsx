import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
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
