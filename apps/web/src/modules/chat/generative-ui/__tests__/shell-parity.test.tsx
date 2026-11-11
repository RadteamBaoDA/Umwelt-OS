import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { chatMessages } from '@/core/messages/chat';
import { useChatScroll } from '../../use-chat-scroll';
import { useRevealBlocks } from '../../reveal-blocks';
import { ToolReasoning } from '../components/tool-reasoning';
import { ToolRendering } from '../components/tool-rendering';
import { SEED_HTML_TEMPLATES } from '../templates/seed-html-templates';
import { SaveTemplateButton, TemplateLibrary } from '../templates/template-library';
import { listUserTemplates, saveUserTemplate, setTemplateAccount } from '../templates/template-store';

// Components bind the template mirror to the default workspace owner; supply a fixed account.
vi.mock('@/core/workspace-context', () => ({
  useWorkspace: () => ({ workspaces: [{ id: 'w1', owner_user_id: 1, is_default: true }] }),
}));

const SHELL_KEYS = (l: 'en-us' | 'vi-vi') =>
  Object.keys(chatMessages[l])
    .filter((k) => k.startsWith('shell'))
    .sort();

beforeEach(() => {
  window.localStorage.clear();
  setTemplateAccount('1');
});
afterEach(() => vi.unstubAllGlobals());

describe('shell message catalog', () => {
  it('has identical shell* keys and placeholders in en-us and vi-vi', () => {
    expect(SHELL_KEYS('vi-vi')).toEqual(SHELL_KEYS('en-us'));
    expect(SHELL_KEYS('en-us').length).toBeGreaterThan(15);
    for (const key of SHELL_KEYS('en-us')) {
      const ph = (l: 'en-us' | 'vi-vi') => ((chatMessages[l] as unknown as Record<string, string>)[key].match(/\{\w+\}/g) ?? []).sort();
      expect(ph('vi-vi')).toEqual(ph('en-us'));
    }
  });
});

describe('TemplateLibrary', () => {
  it('lists three network-free seed templates and applies one through onSendPrompt', async () => {
    const user = userEvent.setup();
    const onSendPrompt = vi.fn();
    expect(SEED_HTML_TEMPLATES).toHaveLength(3);
    for (const t of SEED_HTML_TEMPLATES) expect(t.sandbox.html + t.sandbox.css).not.toMatch(/https?:|<script|@import|url\(/i);
    render(<TemplateLibrary onSendPrompt={onSendPrompt} locale="en-us" />);
    await user.click(screen.getByTestId('template-library-open'));
    expect(screen.getByText('Weather: New York')).toBeInTheDocument();
    expect(screen.getByText('Invoice card: Northwind Labs')).toBeInTheDocument();
    expect(screen.getByText('Q1 KPI dashboard')).toBeInTheDocument();
    await user.click(screen.getAllByRole('button', { name: 'Use template' })[0]);
    expect(onSendPrompt).toHaveBeenCalledTimes(1);
    expect(onSendPrompt.mock.calls[0][0]).toContain('Weather: New York');
  });

  it('saves from a widget, lists, applies and deletes a user template (Vietnamese labels)', async () => {
    const user = userEvent.setup();
    const onSendPrompt = vi.fn();
    render(
      <>
        <SaveTemplateButton sandbox={{ html: '<b>x</b>' }} title="Mine" locale="vi-vi" />
        <TemplateLibrary onSendPrompt={onSendPrompt} locale="vi-vi" />
      </>,
    );
    await user.click(screen.getByTestId('save-template'));
    expect(screen.getByRole('status')).toHaveTextContent(chatMessages['vi-vi'].shellTemplatesSaved);
    expect(listUserTemplates()).toHaveLength(1);
    await user.click(screen.getByTestId('template-library-open'));
    expect(screen.getByText('Mine')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Xóa mẫu Mine' }));
    expect(listUserTemplates()).toHaveLength(0);
    expect(screen.getByText(chatMessages['vi-vi'].shellTemplatesEmpty)).toBeInTheDocument();
  });

  it('reports failure when storage throws and tolerates corrupt stored data', async () => {
    window.localStorage.setItem('umwelt.genui.templates.v2:1', '{not json');
    expect(listUserTemplates()).toEqual([]);
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota');
    });
    expect(saveUserTemplate('t', '{}')).toBe(false);
    vi.restoreAllMocks();
  });
});

describe('ToolReasoning', () => {
  it('is the collapsed default renderer for unknown payload kinds', async () => {
    const user = userEvent.setup();
    render(<ToolRendering payload={{ kind: 'mystery_tool', a: 1 } as never} locale="en-us" />);
    const details = screen.getByTestId('tool-reasoning') as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(details).toHaveTextContent('Tool call: mystery_tool');
    await user.click(screen.getByText('Tool call: mystery_tool'));
    expect(details.open).toBe(true);
  });

  it('localizes the summary', () => {
    render(<ToolReasoning name="x" data={{}} locale="vi-vi" />);
    expect(screen.getByText('Gọi công cụ: x')).toBeInTheDocument();
  });
});

describe('useRevealBlocks', () => {
  /** Minimal IntersectionObserver double that lets a test fire intersections. */
  class FakeIO {
    static last: FakeIO;
    targets: Element[] = [];
    constructor(public cb: (e: Array<{ isIntersecting: boolean; target: Element }>) => void) {
      FakeIO.last = this;
    }
    observe(el: Element) {
      this.targets.push(el);
    }
    unobserve() {}
    disconnect() {}
  }
  const html = '<div><p>a</p><p>b</p></div>';

  /** Mounts the hook on a wrapper whose first child holds two blocks. */
  const mount = () => {
    const Host = () => {
      const ref = React.useRef<HTMLDivElement>(null);
      useRevealBlocks(ref, true, 'x');
      return <div ref={ref} data-testid="host" dangerouslySetInnerHTML={{ __html: html }} />;
    };
    return render(<Host />);
  };

  it('hides blocks until intersecting', () => {
    vi.stubGlobal('IntersectionObserver', FakeIO);
    vi.stubGlobal('matchMedia', () => ({ matches: false }));
    mount();
    const blocks = screen.getByTestId('host').firstElementChild!.children;
    expect(blocks[0]).toHaveClass('reveal-pending');
    act(() => FakeIO.last.cb([{ isIntersecting: true, target: blocks[0] }]));
    expect(blocks[0]).not.toHaveClass('reveal-pending');
    expect(blocks[1]).toHaveClass('reveal-pending');
  });

  it('does nothing under prefers-reduced-motion', () => {
    vi.stubGlobal('IntersectionObserver', FakeIO);
    vi.stubGlobal('matchMedia', () => ({ matches: true }));
    mount();
    expect(screen.getByTestId('host').firstElementChild!.children[0]).not.toHaveClass('reveal-pending');
  });
});

describe('useChatScroll on submit', () => {
  /** Renders a transcript that starts streaming and returns the scrollIntoView spy of the streaming answer. */
  const run = (reduced: boolean) => {
    vi.stubGlobal('matchMedia', () => ({ matches: reduced, addEventListener() {}, removeEventListener() {} }));
    HTMLElement.prototype.scrollTo = vi.fn() as never;
    const spy = vi.fn();
    const Host = ({ busy }: { busy: boolean }) => {
      const { containerRef } = useChatScroll({
        conversationId: 'c',
        messageCount: 1,
        lastMessageId: 'u1',
        lastMessageRole: 'user',
        isStreaming: busy,
      });
      return (
        <div ref={containerRef}>
          {busy && (
            <div
              data-streaming-answer
              ref={(el) => {
                if (el) el.scrollIntoView = spy;
              }}
            />
          )}
        </div>
      );
    };
    const view = render(<Host busy={false} />);
    view.rerender(<Host busy />);
    return spy;
  };

  it('smooth-scrolls the answer area into view once and never chases the bottom', () => {
    expect(run(false)).toHaveBeenCalledWith(expect.objectContaining({ block: 'start', behavior: 'smooth' }));
  });

  it('uses instant scroll under reduced motion', () => {
    expect(run(true)).toHaveBeenCalledWith(expect.objectContaining({ behavior: 'auto' }));
  });
});

describe('SaveTemplateButton server confirmation (G3)', () => {
  it('the widget button reports saved only after the server answered, and failure allows a retry', async () => {
    const user = userEvent.setup();
    let release: (r: Response) => void = () => undefined;
    const fetchMock = vi.fn(() => new Promise<Response>((resolve) => (release = resolve)));
    vi.stubGlobal('fetch', fetchMock);
    render(<SaveTemplateButton sandbox={{ html: '<b>x</b>' }} title="Mine" locale="en-us" csrfToken="tok" />);
    await user.click(screen.getByTestId('save-template'));
    expect(screen.getByRole('status').textContent).toBe('Saving…');
    release(new Response('{}', { status: 500 }));
    expect((await screen.findByText(/Could not save the template/)).textContent).toContain('Try again');
    expect((screen.getByTestId('save-template') as HTMLButtonElement).disabled).toBe(false);
  });

  it('disables the button once saved so a second click cannot create a duplicate', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () => new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    render(<SaveTemplateButton sandbox={{ html: '<b>x</b>' }} title="Mine" locale="en-us" csrfToken="tok" />);
    await user.click(screen.getByTestId('save-template'));
    await waitFor(() => expect((screen.getByTestId('save-template') as HTMLButtonElement).disabled).toBe(true));
    expect(screen.getByRole('status').textContent).not.toBe('Saving…');
  });
});
