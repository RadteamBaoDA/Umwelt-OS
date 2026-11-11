import { render, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('./code-highlight', async (orig) => ({
  ...(await orig<typeof import('./code-highlight')>()),
  loadHighlighter: vi.fn().mockRejectedValue(new Error('chunk failed')),
}));

import { ChatMarkdown } from './chat-markdown';

describe('ChatMarkdown highlighter load failure (R10)', () => {
  it('keeps rendering the fence as plain text without an unhandled rejection', async () => {
    const unhandled = vi.fn();
    process.on('unhandledRejection', unhandled);
    const { container } = render(<ChatMarkdown content={'```ts\nconst a = 1;\n```'} />);
    await waitFor(() => expect(container.querySelector('pre code')).not.toBeNull());
    await new Promise((r) => setTimeout(r, 20));
    process.off('unhandledRejection', unhandled);
    expect(unhandled).not.toHaveBeenCalled();
  });
});
