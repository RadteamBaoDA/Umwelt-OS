import * as React from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  CANONICAL_STREAM_KEYS,
  clampReportedHeight,
  getStreamingStage,
  OpenGenerativeUIRenderer,
} from '../sandbox';
import { PromptBridgeProvider } from '../bridge';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('OpenGenerativeUIRenderer', () => {
  it('exports the canonical stream key ordering and streaming stage helper', () => {
    expect(CANONICAL_STREAM_KEYS).toEqual([
      'initialHeight',
      'placeholderMessages',
      'css',
      'html',
      'jsFunctions',
      'jsExpressions',
    ]);
    expect(getStreamingStage({}, 'streaming')).toBe('placeholder');
    expect(getStreamingStage({ css: '.card {}' }, 'streaming')).toBe('css');
    expect(getStreamingStage({ css: '.card {}', html: '<div>Hi</div>' }, 'streaming')).toBe(
      'html',
    );
    expect(
      getStreamingStage(
        { css: '.card {}', html: '<div>Hi</div>', jsExpressions: 'init();' },
        'streaming',
      ),
    ).toBe('js');
    expect(getStreamingStage({ html: '<div>Hi</div>' }, 'completed')).toBe('completed');
  });

  it('enforces sandbox="allow-scripts" and never includes allow-same-origin', () => {
    render(
      <OpenGenerativeUIRenderer
        status="completed"
        args={{
          title: 'Compound Interest Simulator',
          initialHeight: 380,
          css: '.box { padding: 12px; }',
          html: '<div class="box">Result: $12,400</div>',
        }}
      />,
    );

    const iframe = screen.getByTestId('sandboxed-ui-iframe') as HTMLIFrameElement;
    expect(iframe.getAttribute('sandbox')).toBe('allow-scripts');
    expect(iframe.getAttribute('sandbox')).not.toContain('allow-same-origin');
    expect(iframe.getAttribute('srcdoc')).toContain('Content-Security-Policy');
    // Content is delivered by postMessage morph (stable srcdoc), tracked on the dataset.
    expect(iframe.dataset.latestHtml).toContain('Result: $12,400');
  });

  it('rotates progressive placeholderMessages while status === "streaming" and defers JS until "completed"', () => {
    vi.useFakeTimers();

    const { rerender } = render(
      <OpenGenerativeUIRenderer
        status="streaming"
        placeholderIntervalMs={1000}
        args={{
          initialHeight: 300,
          placeholderMessages: ['Analyzing portfolio...', 'Rendering compound curves...'],
          css: '.chart { color: var(--accent); }',
          html: '<div class="chart">Streaming chart',
          jsFunctions: 'function computeCurve() { return 42; }',
          jsExpressions: 'computeCurve();',
        }}
      />,
    );

    expect(screen.getByTestId('sandboxed-ui-placeholder').textContent).toContain(
      'Analyzing portfolio...',
    );

    act(() => {
      vi.advanceTimersByTime(1100);
    });

    expect(screen.getByTestId('sandboxed-ui-placeholder').textContent).toContain(
      'Rendering compound curves...',
    );

    const streamingIframe = screen.getByTestId('sandboxed-ui-iframe') as HTMLIFrameElement;
    fireEvent.load(streamingIframe);
    // JS must NOT run (or be embedded) while streaming
    expect(streamingIframe.getAttribute('srcdoc')).not.toContain('computeCurve()');
    expect(streamingIframe.dataset.scriptsExecuted).toBeUndefined();
    // Unclosed <div class="chart"> is balanced by processPartialHtml
    expect(streamingIframe.dataset.latestHtml).toContain(
      '<div class="chart">Streaming chart</div>',
    );

    // Transition to completed -> executes JS and hides streaming placeholder
    rerender(
      <OpenGenerativeUIRenderer
        status="completed"
        args={{
          initialHeight: 300,
          placeholderMessages: ['Analyzing portfolio...', 'Rendering compound curves...'],
          css: '.chart { color: var(--accent); }',
          html: '<div class="chart">Completed chart</div>',
          jsFunctions: 'function computeCurve() { return 42; }',
          jsExpressions: 'computeCurve();',
        }}
      />,
    );

    expect(screen.queryByTestId('sandboxed-ui-placeholder')).toBeNull();
    expect(streamingIframe.dataset.scriptsExecuted).toBe('true');
  });

  it('updates container height on umwelt:resize postMessage and opens fullscreen preview via ExportOverlay', async () => {
    render(
      <OpenGenerativeUIRenderer
        status="completed"
        args={{
          title: 'Interactive Diagram',
          initialHeight: 280,
          html: '<div>Diagram Nodes</div>',
        }}
      />,
    );

    const container = screen.getByTestId('sandboxed-ui-container');
    const iframe = screen.getByTestId('sandboxed-ui-iframe') as HTMLIFrameElement;
    expect(container.style.height).toBe('280px');

    act(() => {
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'umwelt:resize', height: 540, nonce: iframe.dataset.umweltNonce },
          source: iframe.contentWindow,
          origin: 'null',
        }),
      );
    });

    expect(container.style.height).toBe('540px');
    expect(clampReportedHeight(99999)).toBe(4000);
    expect(clampReportedHeight(10)).toBe(50);
    expect(clampReportedHeight(Number.NaN)).toBeNull();

    // Open fullscreen preview modal
    const fullscreenBtn = screen.getByTestId('generative-ui-fullscreen-btn');
    await userEvent.click(fullscreenBtn);

    const modalIframe = await screen.findByTestId('generative-ui-fullscreen-iframe');
    expect(modalIframe.getAttribute('sandbox')).toBe('allow-scripts');
  });

  it('rejects resize reports with a wrong nonce or foreign origin', () => {
    render(<OpenGenerativeUIRenderer status="completed" args={{ html: '<p>x</p>', initialHeight: 280 }} />);
    const container = screen.getByTestId('sandboxed-ui-container');
    const iframe = screen.getByTestId('sandboxed-ui-iframe') as HTMLIFrameElement;
    act(() => {
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'umwelt:resize', height: 540, nonce: 'wrong' },
          source: iframe.contentWindow,
          origin: 'null',
        }),
      );
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'umwelt:resize', height: 540, nonce: iframe.dataset.umweltNonce },
          source: iframe.contentWindow,
          origin: 'https://evil.example',
        }),
      );
    });
    expect(container.style.height).toBe('280px');
  });

  it('dispatches a sandbox send-prompt to the provider exactly once', () => {
    const onSendPrompt = vi.fn();
    render(
      <PromptBridgeProvider onSendPrompt={onSendPrompt} cooldownMs={0}>
        <OpenGenerativeUIRenderer status="completed" args={{ html: '<p>x</p>' }} />
      </PromptBridgeProvider>,
    );
    const iframe = screen.getByTestId('sandboxed-ui-iframe') as HTMLIFrameElement;
    act(() => {
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'umwelt:send-prompt', prompt: 'once', nonce: iframe.dataset.umweltNonce },
          source: iframe.contentWindow,
          origin: 'null',
        }),
      );
    });
    expect(onSendPrompt).toHaveBeenCalledTimes(1);
    expect(onSendPrompt).toHaveBeenCalledWith('once');
  });

  /** Collects postMessage payloads sent to the iframe's contentWindow. */
  function spyPosts(iframe: HTMLIFrameElement) {
    const spy = vi.spyOn(iframe.contentWindow as Window, 'postMessage');
    return () =>
      spy.mock.calls
        .map((call) => call[0] as { type: string; jsFunctions?: string; jsExpressions?: string })
        .filter((m) => m.type === 'umwelt:exec-js');
  }

  it('does not reload the iframe (stable srcdoc) as html deltas stream in', () => {
    const { rerender } = render(
      <OpenGenerativeUIRenderer status="streaming" args={{ html: '<div>A' }} />,
    );
    const iframe = screen.getByTestId('sandboxed-ui-iframe') as HTMLIFrameElement;
    const initial = iframe.getAttribute('srcdoc');
    rerender(<OpenGenerativeUIRenderer status="streaming" args={{ html: '<div>AB</div>' }} />);
    expect(iframe.getAttribute('srcdoc')).toBe(initial);
    expect(iframe.dataset.latestHtml).toContain('AB');
  });

  it('never executes partial jsFunctions/jsExpressions while streaming', () => {
    render(
      <OpenGenerativeUIRenderer
        status="streaming"
        args={{ html: '<div>x</div>', jsFunctions: 'function f(){', jsExpressions: 'f(' }}
      />,
    );
    const iframe = screen.getByTestId('sandboxed-ui-iframe') as HTMLIFrameElement;
    const execs = spyPosts(iframe);
    fireEvent.load(iframe);
    expect(execs()).toHaveLength(0);
  });

  it('runs jsFunctions once then each jsExpression once, and replays all on sandbox recreate', () => {
    const args = {
      html: '<div>x</div>',
      jsFunctions: 'function f(){}',
      jsExpressions: ['f();', 'g();'] as unknown as string,
    };
    const { rerender } = render(<OpenGenerativeUIRenderer status="completed" args={args} />);
    const iframe = screen.getByTestId('sandboxed-ui-iframe') as HTMLIFrameElement;
    const execs = spyPosts(iframe);

    fireEvent.load(iframe);
    expect(execs().map((m) => m.jsFunctions ?? m.jsExpressions)).toEqual([
      'function f(){}',
      'f();',
      'g();',
    ]);

    // Unrelated re-render must not re-execute anything.
    rerender(<OpenGenerativeUIRenderer status="completed" args={{ ...args }} />);
    expect(execs()).toHaveLength(3);

    // Sandbox recreated (iframe load fires again) -> full replay.
    fireEvent.load(iframe);
    expect(execs()).toHaveLength(6);
  });

  it('clamps an out-of-range initialHeight', () => {
    const { rerender } = render(
      <OpenGenerativeUIRenderer status="completed" args={{ html: '<p>x</p>', initialHeight: 99999 }} />,
    );
    expect(screen.getByTestId('sandboxed-ui-container').style.height).toBe('4000px');
    rerender(<OpenGenerativeUIRenderer status="completed" args={{ html: '<p>x</p>', initialHeight: 1 }} />);
    expect(screen.getByTestId('sandboxed-ui-container').style.height).toBe('50px');
  });
});
