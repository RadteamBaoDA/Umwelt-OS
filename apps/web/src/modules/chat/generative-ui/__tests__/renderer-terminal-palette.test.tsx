import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { OpenGenerativeUIRenderer } from '../sandbox';

/** Terminal semantic values set inline on <html>, standing in for the globals.css terminal block. */
function setTerminalTokens() {
  document.documentElement.style.setProperty('--surface', '#0f141a');
  document.documentElement.style.setProperty('--text', '#c9d1d9');
}

describe('OpenGenerativeUIRenderer terminal palette', () => {
  beforeEach(() => {
    document.documentElement.removeAttribute('data-skin');
    document.documentElement.removeAttribute('class');
    document.documentElement.removeAttribute('style');
  });
  afterEach(() => {
    cleanup();
    document.documentElement.removeAttribute('data-skin');
    document.documentElement.removeAttribute('style');
  });

  it('keeps the default palette (no override block) when the terminal skin is off', () => {
    render(<OpenGenerativeUIRenderer status="completed" args={{ html: '<p>x</p>' }} />);
    const frame = screen.getByTestId('sandboxed-ui-iframe') as HTMLIFrameElement;
    expect(frame.getAttribute('name')).not.toContain(':root:root');
  });

  it('bakes the terminal palette into the first frame document under the terminal skin', () => {
    document.documentElement.setAttribute('data-skin', 'terminal');
    setTerminalTokens();
    render(<OpenGenerativeUIRenderer status="completed" args={{ html: '<p>x</p>' }} />);
    const frame = screen.getByTestId('sandboxed-ui-iframe') as HTMLIFrameElement;
    const doc = frame.getAttribute('name') ?? '';
    expect(doc).toContain(':root:root {');
    expect(doc).toContain('--surface: #0f141a;');
    expect(doc).toContain("--font-mono: 'JetBrains Mono'");
    // The terminal skin is dark, so the frame is rendered with the dark theme class.
    expect(doc).toContain('class="dark"');
  });

  it('pushes the terminal palette to an open frame when the skin switches on', async () => {
    render(<OpenGenerativeUIRenderer status="completed" args={{ html: '<p>x</p>' }} />);
    const frame = screen.getByTestId('sandboxed-ui-iframe') as HTMLIFrameElement;
    const post = vi.spyOn(frame.contentWindow as Window, 'postMessage');
    setTerminalTokens();
    act(() => {
      document.documentElement.setAttribute('data-skin', 'terminal');
    });
    await waitFor(() => {
      const themeMessages = post.mock.calls
        .map((call) => call[0] as { type?: string; stylesheet?: string })
        .filter((m) => m.type === 'umwelt:theme');
      expect(themeMessages.some((m) => m.stylesheet?.includes('--surface: #0f141a;'))).toBe(true);
    });
  });
});
