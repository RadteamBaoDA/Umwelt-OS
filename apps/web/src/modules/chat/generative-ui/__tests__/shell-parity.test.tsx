import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { chatMessages } from '@/core/messages/chat';
import { useChatScroll } from '../../use-chat-scroll';
import { useRevealBlocks } from '../../reveal-blocks';
import { ToolReasoning } from '../components/tool-reasoning';
import { ToolRendering } from '../components/tool-rendering';
import { DemoGallery } from '../templates/demo-gallery';
import { GALLERY_DEMOS, STARTER_DEMO_IDS } from '../templates/gallery-demos';
import { SEED_HTML_TEMPLATES } from '../templates/seed-html-templates';
import { DesktopTipModal, StarterSuggestions } from '../templates/shell-extras';
import { SaveTemplateButton, TemplateLibrary } from '../templates/template-library';
import { listUserTemplates, saveUserTemplate } from '../templates/template-store';

const SHELL_KEYS = (l: 'en-us' | 'vi-vi') => Object.keys(chatMessages[l]).filter((k) => k.startsWith('shell')).sort();

beforeEach(() => window.localStorage.clear());
afterEach(() => vi.unstubAllGlobals());

describe('shell message catalog', () => {
  it('has identical shell* keys and placeholders in en-us and vi-vi', () => {
    expect(SHELL_KEYS('vi-vi')).toEqual(SHELL_KEYS('en-us'));
    expect(SHELL_KEYS('en-us').length).toBeGreaterThan(40);
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
    render(<><SaveTemplateButton sandbox={{ html: '<b>x</b>' }} title="Mine" locale="vi-vi" /><TemplateLibrary onSendPrompt={onSendPrompt} locale="vi-vi" /></>);
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
    window.localStorage.setItem('umwelt.genui.templates.v1', '{not json');
    expect(listUserTemplates()).toEqual([]);
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota'); });
    expect(saveUserTemplate('t', '{}')).toBe(false);
    vi.restoreAllMocks();
  });
});

describe('DemoGallery', () => {
  it('has seven demos, filters by category, Try sends the prompt, Escape closes', async () => {
    const user = userEvent.setup();
    const onSendPrompt = vi.fn();
    expect(GALLERY_DEMOS).toHaveLength(7);
    render(<DemoGallery onSendPrompt={onSendPrompt} locale="en-us" />);
    await user.click(screen.getByTestId('demo-gallery-open'));
    expect(screen.getAllByTestId(/^gallery-demo-/)).toHaveLength(7);
    await user.click(screen.getByTestId('gallery-filter-make'));
    expect(screen.getAllByTestId(/^gallery-demo-/)).toHaveLength(2);
    await user.click(screen.getByTestId('gallery-try-splitter'));
    expect(onSendPrompt).toHaveBeenCalledWith(chatMessages['en-us'].shellDemoSplitterPrompt);
    expect(screen.queryByTestId('demo-gallery')).not.toBeInTheDocument();

    await user.click(screen.getByTestId('demo-gallery-open'));
    expect(screen.getByTestId('demo-gallery')).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(screen.queryByTestId('demo-gallery')).not.toBeInTheDocument();
  });

  it('renders Vietnamese strings', async () => {
    const user = userEvent.setup();
    render(<DemoGallery onSendPrompt={vi.fn()} locale="vi-vi" />);
    await user.click(screen.getByTestId('demo-gallery-open'));
    expect(screen.getByText('Bài toán Monty Hall')).toBeInTheDocument();
    expect(screen.getByTestId('gallery-filter-make')).toHaveTextContent('Tạo công cụ');
  });
});

describe('StarterSuggestions', () => {
  it('shows four demo chips and sends the prompt', async () => {
    const user = userEvent.setup();
    const onSendPrompt = vi.fn();
    render(<StarterSuggestions onSendPrompt={onSendPrompt} locale="vi-vi" />);
    expect(STARTER_DEMO_IDS).toHaveLength(4);
    expect(screen.getAllByTestId('template-chip')).toHaveLength(4);
    await user.click(screen.getByText(chatMessages['vi-vi'].shellDemoTripTitle));
    expect(onSendPrompt).toHaveBeenCalledWith(chatMessages['vi-vi'].shellDemoTripPrompt);
  });
});

describe('DesktopTipModal', () => {
  /** Stubs matchMedia so the viewport reads as phone-sized or desktop-sized. */
  const stubViewport = (narrow: boolean) =>
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: narrow && q.includes('max-width'), addEventListener() {}, removeEventListener() {} }));

  it('shows below sm, dismisses into sessionStorage and stays hidden after', async () => {
    window.sessionStorage.clear();
    stubViewport(true);
    const user = userEvent.setup();
    const { unmount } = render(<DesktopTipModal locale="en-us" />);
    expect(await screen.findByTestId('desktop-tip-modal')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Got it' }));
    expect(screen.queryByTestId('desktop-tip-modal')).not.toBeInTheDocument();
    unmount();
    render(<DesktopTipModal locale="en-us" />);
    expect(screen.queryByTestId('desktop-tip-modal')).not.toBeInTheDocument();
  });

  it('stays hidden on desktop widths', () => {
    window.sessionStorage.clear();
    stubViewport(false);
    render(<DesktopTipModal locale="en-us" />);
    expect(screen.queryByTestId('desktop-tip-modal')).not.toBeInTheDocument();
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
    constructor(public cb: (e: Array<{ isIntersecting: boolean; target: Element }>) => void) { FakeIO.last = this; }
    observe(el: Element) { this.targets.push(el); }
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

describe('useChatScroll reader mode', () => {
  /** Renders the hook against a container with one user message and returns its scroll spy. */
  const run = (reader: boolean, isStreaming: boolean) => {
    vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
    // jsdom has no element.scrollTo; the hook calls it for the initial bottom scroll.
    HTMLElement.prototype.scrollTo = vi.fn() as never;
    const scrollIntoView = vi.fn();
    const Host = ({ id }: { id: string }) => {
      const { containerRef } = useChatScroll({ conversationId: 'c', messageCount: id === 'u1' ? 1 : 2, lastMessageId: id, lastMessageRole: 'user', isStreaming, reader });
      return <div ref={containerRef}><div data-message-id="u1" ref={(el) => { if (el) el.scrollIntoView = scrollIntoView; }} /><div data-message-id="u2" ref={(el) => { if (el) el.scrollIntoView = scrollIntoView; }} /></div>;
    };
    const view = render(<Host id="u1" />);
    view.rerender(<Host id="u2" />);
    return scrollIntoView;
  };

  it('scrolls the submitted question to the top in reader mode', () => {
    expect(run(true, true)).toHaveBeenCalledWith(expect.objectContaining({ block: 'start' }));
  });

  it('does not use scrollIntoView in default follow mode', () => {
    expect(run(false, false)).not.toHaveBeenCalled();
  });
});

